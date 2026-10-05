"""后台分析线程：抓屏 → 变化检测 → 读弹幕/字幕 → 查记忆 → 问模型 → 吐槽 + 表情。

整个循环都在子线程里跑，只通过信号跟界面通信；
唯一需要触碰界面的地方是"抓屏前把自己藏起来"，用信号 + 事件做握手。
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque
from typing import Deque, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Signal

from . import capture, care, corpus, dialog as dialog_mod, episode, foreground, humanstyle, keyinfo, proactive, progress, taste, webstudy, wincap, winfind
from .config import Config
from .memory import Memory
from .mood import Comment
from .ocr import TextReader
from .vlm import VlmError, VisionClient
from .watchlog import WatchLog, WatchNote, format_log


class AnalysisWorker(QThread):
    comment = Signal(object)       # mood.Comment（一句话 + 情绪）
    notice = Signal(str)           # 需要跟用户说的话（会显示在气泡里）
    failure = Signal(str)          # 出错信息（已做限频）
    thinking = Signal(bool)        # 正在等模型返回
    hide_requested = Signal()      # 请主线程把挂件藏起来（抓屏前）
    show_requested = Signal()      # 请主线程把挂件放回来

    ERROR_COOLDOWN = 45.0          # 同类错误最多 45 秒提示一次
    TARGET_NOTICE_COOLDOWN = 90.0  # 锁的程序找不到了，最多这么久提醒一次
    MIN_GRAB_RETRY = 30.0          # 被盯的窗口最小化时，隔这么久再问它一次"能画出来吗"
    REPEAT_THRESHOLD = 0.62        # 跟最近说过的话像到这个程度就算"重复"
    NARRATION_RETRY_GAP = 15.0     # 因为"交了解说词"而补要一句，两次之间至少隔这么久
    RECENT_SIZE = 16               # 记着最近说过多少句（吐槽+对话+搭话都算）
    VIEWING_KEEP = 8               # 「跟着进度看」最多留几条"读到哪了"（越靠后越新）
    HIDDEN_SLEEP_MAX = 20.0        # 收进托盘时最多睡这么久就醒一次（好让 stop/放回来能插进来）

    def __init__(self, cfg: Config, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.cfg = cfg
        self._client = VisionClient(cfg)
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._away = threading.Event()     # 挂件收进托盘了（那段时间不看你屏幕）
        self._study_warned = False         # "没配 Key，隐身学习搁着"这句只提示一次
        self._once = threading.Event()
        self._force_nudge = threading.Event()
        self._wake = threading.Event()
        self._hidden_ack = threading.Event()
        self._shown_ack = threading.Event()
        self._history: List[str] = []
        self._recent: Deque[str] = deque(maxlen=self.RECENT_SIZE)  # 防重复用：最近说过的所有话
        self._strict_repeat = str(getattr(cfg, "provider", "")) != "mock"   # mock 不做防重复
        self._last_narration_at = 0.0                    # 上次因为"交了解说词"重要一句是什么时候
        self._chat_history: List[Tuple[str, str]] = []  # 最近几轮对话（只在本次运行里有效）
        # 用户说过的话私下按类型记账（记录/分析/学习，见 dialog.py）：
        # 只在内存 + 控制台日志里，**不写 memory.json**——对话本来就不落盘
        self._turns: Counter = Counter()
        self._turn_hint = ""       # 这一轮按哪种接法（由对话类型推出来的提醒）
        self._user_queue: Deque[str] = deque()          # 用户说的话，排队等它回答
        self._last_scene = ""                           # 上一眼看明白的 5W1H
        self._last_hash: Optional[int] = None
        self._last_small = None          # 最近一帧（主动搭话时复用，不用重新抓屏）
        self._last_ocr = ""
        self._last_window = ""
        # 画面关键信息（台标/节目名/集数/话题人名）的缓存：
        # 键是「窗口标题 + 这一眼的识别文字」，一样就不再认第二遍（见 _key_block）
        self._key_cache_key: Tuple[str, str] = ("", "")
        self._key_cache_block = ""
        self._key_cache_line = ""
        self._key_cache_info = None      # 上一次认出来的 KeyInfo（"他在玩什么游戏"要从这儿取）
        # 他正在干嘛：类型 + 具体是什么 + 从什么时候开始的（见 _activity）
        self._activity_kind = ""
        self._activity_label = ""
        self._activity_since = 0.0
        # 「他换了件事」的时候往哪儿记一笔（doing.ActivityBoard，app 挂上来的）：
        # 串门时对方问"你家主人这会儿在忙什么"，答的就是它（见 pet/doing.py）。
        # 没挂就什么都不做——自测 / 单跑分析线程时就是这样。
        self.activity_board = None
        self._video_title = ""           # 当前这支视频的标题（看片笔记里那份）
        self._video_title_at = 0.0
        self._last_error = ""
        self._last_error_at = 0.0
        self._last_absorb_hash: Optional[int] = None    # 上一次"读完"那一帧的指纹
        self._last_absorb_at = 0.0
        # 「跟着他的进度看」用：上次"读"的时候进度到哪了 / 一路读到的内容（越靠后越新）
        self._last_absorb_ratio = 0.0
        self._seen_notes: List[str] = []
        self._absorb_calls: Deque[float] = deque()      # "读视频"也限个速，别刷屏就重读
        self.occluder_active = False     # 挂件是否挡住了观看区域
        self.handshake_ready = False     # 主线程是否已接上 hide/show 信号
        self.capture_region: Optional[dict] = None   # 这一轮实际要拍的矩形（物理像素）
        self._target_state = "off"       # off / ok / missing / minimized / background（锁进程时的状态）
        self._target_notice_at = 0.0
        self._target_hwnd: int = 0       # 锁的那个窗口句柄：不在前台时直接抓它自己的画面
        self._target_foreground = True   # 它现在在不在前台
        self._min_noted = False          # "它最小化了"这句每收起来一次只说一遍（别 90 秒念一次）
        self._min_tried_at = 0.0         # 上一次问"最小化的窗口能画出来吗"是什么时候
        self._last_shot = None           # 最近一张成功抓到的原图：最小化时拿它回放（见 _grab_minimized）
        self._frame_stale = False        # 这一眼是不是**回放**出来的（不是新拍的）
        self._seq: Deque[Tuple[float, object]] = deque(maxlen=6)  # 最近几眼，拼"分镜图"用
        self.memory = Memory(cfg)
        self.reader = TextReader(cfg)
        watch_cfg = getattr(cfg, "watch", None)
        # 看片笔记：换视频时先读一遍，之后聊到这支视频就靠它答话（全在本地，不写 memory.json）
        self.watch = WatchLog(
            timeline_size=int(getattr(watch_cfg, "timeline_size", 12)),
            history_size=int(getattr(watch_cfg, "note_history", 5)),
        )
        # 整集资料卡：认出"这是哪一集"之后，先把这一集讲的是什么弄到手（见 pet/episode.py）
        self.episode = episode.EpisodeLog(cfg)
        # 主动搭话能翻长期记忆找话头（memory.topics() 只读记忆，不写）
        self.proactive = proactive.ProactivePolicy(cfg, topic_source=self.memory.topics)
        # 上班时的健康提醒（喝水 / 站起来 / 闭眼）：只看钟点，文案全本地，不花钱
        self.care = care.CarePolicy(cfg)
        self.ocr_engine = ""             # 实际用上的 OCR 后端，界面上会显示
        # 口味档案：每刷一支视频记一条（类型/内容/看点 + 点赞收藏关注），攒出"他爱看啥"
        self.taste = taste.TasteLog(cfg)
        # 边看边学：每一眼读到的字幕/台词，攒成"别人怎么接话"的样本（见 pet/corpus.py）。
        # 只写 data/learn.json；要不要写进语料由 config 的 learn.promote 说了算（默认关）
        self.learn = corpus.CorpusLearner(cfg)
        # 隐身学习：收进托盘之后不看你屏幕，改成自己上网找料学（见 pet/webstudy.py）。
        # 客户端给的是"取客户端的函数"：reload_client() 换了 Key 也不会拿旧的。
        self.study = webstudy.WebStudy(
            cfg,
            memory=self.memory,
            client=lambda: self._client,
            topics_from=self._study_topics,
        )
        self._video_record = None         # 当前这支视频对应的档案
        self._video_started_at = 0.0      # 这支视频从什么时候开始看的（算观看时长）
        self._video_max_ratio = 0.0       # 这支视频见过的最大播放进度（判断"看完没"）
        self._progress_noted = False      # "进度到头了"这支视频只记一次日志
        self._actions: Dict[str, bool] = {}   # 这支视频上，他已确认过的互动
        self._last_spotlight_at = 0.0     # 上一次"必须开腔"（新视频/互动）是什么时候
        self._last_spoke_at = 0.0         # 上一次真的冒出气泡是什么时候（防两句挤一起）
        self._last_insist_at = 0.0        # 上一次"补说"（上一轮白说了，再要一次）是什么时候
        self._spotlight_calls: Deque[float] = deque()   # "接话"也限速，别猛刷视频时疯狂调用

    # ---------- 外部控制 ----------

    def ack_hidden(self) -> None:
        self._hidden_ack.set()

    def ack_shown(self) -> None:
        self._shown_ack.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
            # 别把暂停这段时间算成"很久没动静"，不然一解除暂停它就喊人
            self.proactive.reset()
            self.care.reset()
        self._wake.set()

    def set_hidden(self, hidden: bool) -> None:
        """主线程告诉它：挂件收进托盘了 / 放回来了。

        收进托盘期间它**不看你屏幕**（人都走了，看也没用），改成自己上网学
        （见 _study_while_hidden）；两头都把搭话 / 健康提醒的秒表拨到现在，
        免得它一露头就先来一句"你回来了"。
        """
        if hidden:
            self._away.set()
        else:
            self._away.clear()
        self.proactive.reset()
        self.care.reset()
        self._wake.set()

    def wake(self) -> None:
        """把睡着的循环叫醒一次（改设置之后用，比如刚打开"隐身学习"）。"""
        self._wake.set()

    def take_study_greeting(self) -> str:
        """回屏幕时它想说的那句（"我刚上网查了…"）；说过一次就清掉。"""
        line = self.study.greeting()
        if line:
            self.study.last = None
        return line

    def analyze_now(self) -> None:
        """忽略"画面没变化"和冷却，立刻来一句（`Ctrl+Alt+S` 那个热键走的就是这条路）。"""
        self._once.set()
        self._wake.set()

    def nudge_now(self) -> None:
        """立刻主动说一句，不等冷却（脚本 / 冒烟测试直接调；菜单里不放这个入口）。"""
        self._force_nudge.set()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def reload_client(self) -> None:
        self._client = VisionClient(self.cfg)

    # ---------- 主循环 ----------

    def run(self) -> None:
        cap = self.cfg.capture
        interval = max(0.5, float(cap.interval_sec))
        cooldown = max(0.0, float(cap.cooldown_sec))
        calls: Deque[float] = deque()
        chat_calls: Deque[float] = deque()
        next_allowed = 0.0
        warned = False

        if self.reader.enabled:
            self.ocr_engine = self.reader.engine_name
            if not self.reader.available() and not self.reader.disabled_reason:
                self.notice.emit("这台机器上的 OCR 用不了，我先只看画面。")

        while not self._stop.is_set():
            # 用户主动说话最优先：不等冷却，暂停了也照答（他正跟你说话呢）
            if self._user_queue:
                self._answer_user(self._user_queue.popleft(), chat_calls)
                continue

            if self._paused.is_set():
                self._sleep(0.25)
                continue

            if self._away.is_set():
                # 挂件收进托盘了：这一段不抓屏（见 _study_while_hidden）
                self._study_while_hidden()
                continue

            now = time.monotonic()
            # 学了口味就照它调劲儿：他最近看得起劲 → 更愿意陪你两句；没什么兴致 → 少开口
            self.proactive.scale = self.taste.nudge_scale()
            # 每轮都瞄一眼"人还在不在"（就一次 ctypes 调用，不花钱）+ 现在几点
            idle = proactive.user_idle_seconds()
            hour = time.localtime().tm_hour

            if self._force_nudge.is_set():    # 脚本 / 测试直接要一句，不等冷却
                self._force_nudge.clear()
                self._safe_emit(self.proactive.force(), now, "proactive")

            if not self._client.ready:
                if not warned:
                    warned = True
                    self.notice.emit("还没配 API Key，我先在旁边待着。右键我 → 打开配置文件。")
                self._maybe_nudge(now, None, idle, hour)   # 不花钱的那两句照说
                self._sleep(1.0)
                continue

            started = time.monotonic()
            force = self._once.is_set()

            region = self.target_region()
            if region is None and self._target_state != "off":
                # 锁着某个程序，但它现在不在（关掉了 / 切到后台了）：
                # 不抓屏、不花钱、也不说话——等你切回来它自己会接着看。
                # 注意两件不算"不在"的事：**最小化**（照样返回那块矩形，抓画面走
                # _grab_minimized）、以及**没锁进程**（region 本来就是 None = 整屏，
                # 交给 capture 决定）。
                self._note_target_missing()
                self._maybe_nudge(now, None, idle, hour)
                self._sleep(max(0.3, min(1.5, interval * 0.5)))
                continue
            self.capture_region = region

            try:
                image = self._grab(region)
            except Exception as exc:
                self._report(f"画面抓不到：{exc}")
                self._maybe_nudge(now, None, idle, hour)
                self._sleep(interval)
                continue

            if image is None:
                # 被盯的窗口收起来了，它自己又画不出、手上也没有"最后一眼"可回放：
                # 这一轮是真没得看——跟"窗口关掉了"一样处理（提醒有冷却，别刷屏）
                self._note_target_missing()
                self._maybe_nudge(now, None, idle, hour)
                self._sleep(max(0.3, min(1.5, interval * 0.5)))
                continue

            if not self._frame_stale:
                self._last_shot = image      # 留一张真画面：它最小化时拿这张接着"看"

            small = capture.shrink(image, int(cap.max_width))
            digest = capture.dhash(small)

            if self._frame_stale and not force:
                # 这一眼是**回放**的（目标最小化、抓不到新画面，见 _grab_minimized）：
                # 只维持状态——不吐槽、不吸收（把旧画面当"新看的一眼"重读纯属白花钱）、
                # 不喂学习、不进分镜。窗口标题那条线索照旧是活的（每次现问 winfind），
                # 所以"他换了支视频"这种事在托盘和聊天里它还是答得出来。
                # force（Ctrl+Alt+S 手动"说一句"）例外：他是当面要一句，那就按最后一眼说。
                self._maybe_nudge(now, False, idle, hour)
                elapsed = time.monotonic() - started
                self._sleep(max(0.2, interval - elapsed))
                continue

            self._last_small = small
            if not self._frame_stale:
                self._push_sequence(small)
                # 量一眼底部进度条：本地、免费，用来判断"这支看完没有"（见 _note_progress）
                self._note_progress(small)
                # 免费的那半边学习一直在跑：这一眼的字幕也喂给"边看边学"（本地 OCR + 本地档案，
                # 不花接口钱，见 _feed_learning）
                self._feed_learning(image, digest)
                # 消化也在同时进行：到了点就把攒够的接话对升格进语料（见 corpus.maybe_digest）
                self.learn.maybe_digest()
            changed = self._last_hash is None or capture.hash_distance(
                digest, self._last_hash
            ) >= float(cap.change_threshold)

            # 画面大跳变 = 多半换了一支视频/换了大场景：先把这一眼**读完**再聊
            # （回放的一眼不算：那份笔记早读过了，重读一遍只是花钱）
            scene_cut = self._is_scene_cut(digest, now) and not self._frame_stale

            allowed = force or (
                changed and now >= next_allowed and self._rate_ok(calls, int(cap.max_calls_per_min))
            )

            if allowed:
                self._once.clear()
                comment = self._think(image, small, digest)
                if comment is not None and self.is_repeat(comment.text):
                    # 跟最近说过的像到一定程度：让它再想一次，这次把"刚说过什么"摆给它看
                    again = self._think(image, small, digest, avoid=list(self._recent))
                    comment = again if (again is not None and not self.is_repeat(again.text)) else None
                    if comment is None:
                        print("[repeat] 这次只想到重复的话，干脆闭嘴")
                # 交的是解说词 / 念出来的内部信息 / 抓来的页面标题 / 它自己那套机器的话 /
                # 机器识图描述（humanstyle.looks_like_narration / looks_like_title /
                # looks_like_meta_talk / looks_like_screen_caption）
                elif comment is None and self._client.last_drop in (
                    "narration", "internal", "title", "meta", "screen",
                ):
                    # 它交的是**解说词**（"某某和某某站在…前，似乎是在参与某个环节"）、
                    # **把内部信息念了出来**（"画面关键信息：…"）、或者**念了一遍它收到的截图**
                    # （"电脑屏幕截图，包含…编辑器界面…"），都被本地闸拦了。
                    # 这种句子一次都不该出现在气泡里，但也不能就这么哑掉——立刻再要一句。
                    comment = self._narration_retry(image, small, digest)
                elif comment is None and self._should_insist():
                    # 这一眼它一句台词都没给（只交了场景行 / 把画面上的字抄回来了）：
                    # 它已经安静一会儿了，就再要一次——这次提示词把输出压成"只回一句话，不要场景行"。
                    comment = self._think(image, small, digest, avoid=list(self._recent), insist=True)
                    comment = self._dedup(comment, "补说")
                    if comment is None:
                        print("[scene] 补说还是没说出人话，这轮先放过")
                    else:
                        print(f"[insist] 白说一轮之后补要了一次，这回接上了：{comment.text[:20]}")
                if comment is not None:
                    self._record_comment(comment)
                    self._last_hash = digest
                    next_allowed = time.monotonic() + cooldown
                    self._remember(comment.text)
                    self._last_spoke_at = time.monotonic()
                    self.comment.emit(comment)
                    self.proactive.note_spoke(time.monotonic())
                    self._maybe_summarize()

            if scene_cut:
                self._absorb(image, digest)
            elif not self._frame_stale and self._should_refresh_absorb(now):
                # 同一支视频看久了：重读一眼、把笔记更新掉（这一步不说话，只记）
                # 回放的一眼不进来：它本来就是同一张图，重读一遍只是花钱，
                # 而且 _should_refresh_absorb 会顺手吃掉一个限速名额
                self._absorb(image, digest, refresh=True)

            # 画面没变 / 它自己憋太久 / 人走了又回来 / 深夜劝睡，都由这里决定要不要开口
            self._maybe_nudge(now, changed, idle, hour)

            elapsed = time.monotonic() - started
            self._sleep(max(0.2, interval - elapsed))

    def _feed_learning(self, image, digest: int) -> None:
        """每一眼都喂给「边看边学」：本地读字 → 本地档案，全程不花接口钱。

        为什么要单独有这么一步：`learn.observe()` 原来只挂在"该想一句话"（_think）和
        "换视频读一遍"（_absorb）那两条路上，而它们都有限速（`max_calls_per_min`）——
        画面变得快的时候，一大半字幕根本没机会进档案。这里补上：抓到的每一眼都过一遍
        （OCR 有指纹缓存，画面不动就不会重算），免费的那半边学习才算真的"一直在跑"。

        只喂观察，不碰 `_last_ocr` / 关键信息缓存：那两个是给吐槽和看片笔记用的，
        在这里顺手改会打乱它们的语义。
        """
        if not (self.reader.enabled and self.learn.enabled):
            return
        try:
            result = self.reader.read(image, digest)
        except Exception as exc:      # 读字出问题不能把主循环带走
            print(f"[learn] 这一眼读字失败：{exc}")
            return
        if result.ok:
            self.learn.observe(result.lines)

    def _study_topics(self) -> List[str]:
        """隐身学习的话头：优先"他刚在看的那一支 / 那个游戏"，其次才轮到长期记忆。

        为什么把标题摆前面：标题是最具体的搜索词（"奔跑吧第十四季第11期"），比
        "游戏""短视频"这种类别词准得多——现场试过：拿空话去搜，搜回来的是同名杂志、
        公司官网那种毫不相干的页面，材料跟话头对不上，学来的全是垃圾。

        补一层"他反复看的词"（taste.hot_topics）：屏幕上认不出标题的时候（游戏全局
        界面、直播间），这些词还是从他的屏幕内容里攒出来的，比长期记忆里的类别词准。
        """
        topics: List[str] = []
        title = (self._video_title or "").strip()
        if title:
            topics.append(title)
        info = self._key_cache_info
        extra = list(getattr(info, "shows", None) or []) + list(getattr(info, "apps", None) or [])
        for name in extra:
            text = str(name).strip()
            if text and text not in topics:
                topics.append(text)
        try:
            # 他点过赞/收藏的那些视频里反复出现的词（本地统计，不花钱）
            for word in self.taste.hot_topics(2):
                text = str(word).strip()
                if text and text not in topics:
                    topics.append(text)
        except Exception as exc:      # 口味档案读不出来不该挡住学习
            print(f"[study] 取口味关键词失败：{exc}")
        return topics[:3]

    # ---------- 收进托盘之后 ----------

    def _study_while_hidden(self) -> None:
        """挂件收进托盘时的循环：**不抓屏**，只管自己上网补课（见 pet/webstudy.py）。

        为什么整段停掉抓屏：人都不在，拍下来也没人看——白花算力、白花接口钱。
        学习默认开着（右键菜单那个开关已经撤了）；不想花这份钱就在配置里把
        `study.enabled` 改成 false，那时候纯歇着：一秒醒一次，等你把它放回屏幕上。
        """
        if not self.study.enabled:
            self._sleep(1.0)
            return
        if str(getattr(self.cfg, "provider", "")) == "mock":
            # 离线演示模式不联网：mock 的"学习"只是把内置样本抄进语料，自动这条路就不走
            # （真想试一次：python -m pet.webstudy --force）
            if not self._study_warned:
                self._study_warned = True
                print("[study] provider 是 mock（不联网），隐身学习先搁着")
            self._sleep(self.HIDDEN_SLEEP_MAX)
            return
        if not self.study.due():
            # 还没到该学的点：睡到快该学了再看一眼（最长 HIDDEN_SLEEP_MAX 秒，
            # 这样"放我回来"和"退出"都能立刻叫醒它）
            wait = self.study.next_wait()
            self._sleep(min(self.HIDDEN_SLEEP_MAX, max(1.0, wait)))
            return
        if not self._client.ready:
            if not self._study_warned:
                self._study_warned = True
                print("[study] 还没有配 API Key，隐身学习先搁着")
            self._sleep(self.HIDDEN_SLEEP_MAX)
            return
        try:
            self.study.run_round()
            # 顺手把"消化到哪一步了"记进日志：这是后台口径，只写日志、不往界面上摆
            print(f"[study] {self.learn.digest_line()}")
        except Exception as exc:      # 学习出问题不能把后台线程带走
            print(f"[study] 隐身学习异常：{exc}")
        self._sleep(1.0)

    # ---------- 一帧的完整思考过程 ----------

    def _narration_retry(self, image, small, digest: int) -> Optional[Comment]:
        """它交了一段**解说词**（"某某和某某站在…前，似乎是在参与某个环节"）之后：立刻再要一句。

        跟"补说"（_should_insist）不是一回事：那个是它一直不说话、等安静够了才补；
        这个是它**刚想说却说了句镜头说明书**，用户正等着看它说什么，所以当场就要，
        提示词里用 insist 那套（只回一句话、只说自己的想法、不许描述画面）。
        自己的冷却管着：最短 `NARRATION_RETRY_GAP` 秒一次，免得每帧都翻倍请求。
        """
        now = time.monotonic()
        if now - self._last_narration_at < self.NARRATION_RETRY_GAP:
            return None
        self._last_narration_at = now
        print("[narration] 再要一句（这次只准说自己的想法）")
        return self._dedup(
            self._think(image, small, digest, avoid=list(self._recent), insist=True), "解说词"
        )

    def _should_insist(self) -> bool:
        """这一眼"白说了"之后，要不要再要它一次（唯一一处会多发一次请求的地方）。

        只在两种条件都满足时补：① 它已经安静了 `capture.insist_after_quiet_sec` 秒
        （用户在等它说话，这时候沉默最难受）；② 距上次"补说"也过了这么久
        （免得不说话的那段时间每一眼都补一次，把请求量翻倍）。
        把 `insist_after_quiet_sec` 调成 0 就关掉这个补说。
        """
        gap = float(getattr(self.cfg.capture, "insist_after_quiet_sec", 30.0))
        if gap <= 0:
            return False
        now = time.monotonic()
        if now - self._last_spoke_at < gap or now - self._last_insist_at < gap:
            return False
        self._last_insist_at = now
        return True

    # 「他正在干嘛」多久没更新就算过期：看片笔记会一直挂在那儿，
    # 人早切去干别的了还拿它当"正在看"，就会夸错东西（见 keyinfo.activity_of）。
    ACTIVITY_VIDEO_STALE_SEC = 1800.0

    def _activity(self):
        """他现在在干嘛：返回 (类型, 具体是什么, 已经做了几分钟)；认不出来是 other。

        计时只在**这件事本身换了**的时候归零（换了游戏 / 换了活从头算），
        这样"坐这儿 50 分钟"才是真的坐了 50 分钟，而不是"这个进程开了 50 分钟"。
        刚换的那一下返回 None：别一换台就急着开口。
        """
        info = self._key_cache_info
        apps = list(getattr(info, "apps", None) or ())
        fresh = (time.monotonic() - self._video_title_at) < self.ACTIVITY_VIDEO_STALE_SEC
        video = self._video_title if (self._video_title and fresh) else ""
        kind, label = keyinfo.activity_of(self._last_window or "", apps, video=video)
        now = time.monotonic()
        if kind != self._activity_kind or label != self._activity_label:
            self._activity_kind, self._activity_label = kind, label
            self._activity_since = now
            self._note_activity(kind, label)
            return None
        if not self._activity_since:
            self._activity_since = now
            return None
        return (kind, label, (now - self._activity_since) / 60.0)

    def _note_activity(self, kind: str, label: str) -> None:
        """他换了一件事 → 往小本子里记一笔（串门时对方问的就是它，见 pet/doing.py）。

        只记**换过的事**：同一件事坐 40 分钟算一件，所以那边听到的是
        「刚在看视频，这会儿在打游戏」这种，而不是每几秒刷一次。
        """
        board = self.activity_board
        if board is not None:
            try:
                board.note(kind, label)
            except Exception as exc:            # 记个小本子而已，绝不该拖垮分析线程
                print(f"[worker] 记'他在干嘛'失败：{exc}")

    def _key_block(self, lines, window: str, ocr_text: str) -> str:
        """认出"这是什么"（台标 / 节目名 / 集数 / 话题人名），**同一眼只认一次**。

        为什么要有这个缓存：画面在播的时候每隔几秒就重新截一屏，但画面上的字
        往往一个字都没变（同一支视频、同一个标题、同一集），窗口标题更是老样子。
        那种情况下再认一遍纯属白算，控制台也会被同一行 [keyinfo] 刷屏
        （现场就是这个样子：一屏接一屏的「抖音、第九季、第一季、第25集、第三期」）。
        所以拿「窗口标题 + 识别文字」当键：没变就直接把上次的结果还回去，
        变了才重新认，而且只有**认出来的内容真的换了**才打日志。
        """
        key = (window, ocr_text)
        if key == self._key_cache_key:
            return self._key_cache_block
        info = keyinfo.extract(lines, window)
        block = info.block()
        line = info.line()
        self._key_cache_key = key
        self._key_cache_block = block
        self._key_cache_info = info
        if block and line != self._key_cache_line:
            self._key_cache_line = line
            print(f"[keyinfo] {line}")
        return block

    def _think(self, image, small, digest: int, avoid=(), insist: bool = False) -> Optional[Comment]:
        """读弹幕/字幕 + 取窗口标题 + 翻记忆，然后问模型。None 表示沉默或出错。

        这里**不写**记忆和看片笔记：写不写由调用方决定——
        万一它说的是一句重复的话，我们要让它重说一次，那就不能先记两遍。
        avoid 是刚说过的几句，重试那一轮用来堵住"换汤不换药"。
        insist 是"补说"那一轮：上一轮只交了场景行，这轮提示词里把话说死。
        """
        window = self._window_context()

        ocr_text = ""
        key_block = ""
        if self.reader.enabled:
            # OCR 用原图（更准），但图片只在本地落盘，不会上传
            result = self.reader.read(image, digest)
            self.ocr_engine = self.reader.engine_name
            if result.ok:
                ocr_text = result.as_text(self.cfg.ocr.max_lines)
                # 先从这堆字里认出"这是什么"（台标 / 节目名 / 集数 / 话题人名），
                # 再把它摆在原始识别文字前面——不然小模型盯着这坨字只会说"画面里有个人"。
                key_block = self._key_block(result.lines, window, ocr_text)
                # 顺手学一句：这堆字里像人话的那几行攒进"接话样本"（本地、免费，见 corpus.py）
                self.learn.observe(result.lines)
                # 顺手认一下"这是哪一集"：认出来了后面才好给它做功课（见 _learn_episode）
                self.episode.note(self._key_cache_info)

        # 缓存下来：主动搭话时复用这一帧的文字/标题，不重复花 OCR 和请求的钱
        self._last_ocr = (key_block + "\n\n" + ocr_text) if key_block else ocr_text
        self._last_window = window

        # 顺手看一眼他有没有点赞/收藏/关注：画面文字里就写着，免费
        self._apply_actions(taste.actions_from_text(ocr_text))

        view, frames = self._view_image(small)
        self.thinking.emit(True)
        try:
            comment = self._client.describe(
                view,
                frames=frames,
                history=self._history,
                ocr_text=ocr_text,
                window=window,
                memory=self.memory.context(),
                clock=proactive.clock_text(),
                scene=self._last_scene,
                watch=self.watch.current_line(),
                avoid=avoid,
                taste=self.taste.profile_block(),
                insist=insist,
                episode=self.episode.block(),
            )
        except VlmError as exc:
            self._report(str(exc))
            return None
        except Exception as exc:  # 解析/提示词这条链上出任何岔子，都不能把线程带走
            print(f"[think] 这一眼没想出话来：{exc!r}")
            self._report("我这轮没想出来，等会儿再说")
            return None
        finally:
            self.thinking.emit(False)

        if comment is None:
            # 沉默也记一眼：场景和弹幕照样进看片记录，等它开口或用户提问时就有料
            self.watch.remember(ocr=ocr_text)
            return None
        return comment

    def _record_comment(self, comment: Comment) -> str:
        """把真正说出口的这一句记下来：看片笔记（含这一眼的场景）+ 长期记忆。"""
        scene_line = self._scene_line(comment)
        self.watch.remember(scene=scene_line, ocr=self._last_ocr, said=comment.text)
        # 私下给它这句话分个类（记录/分析/学习）：只进统计和控制台日志，不上界面
        kind = dialog_mod.classify(comment.text, mood=comment.mood, kind=comment.kind)
        # 弹幕文字 + 这一眼的场景一起参与打标签，这样"王者荣耀""直播间"这类词能进记忆
        self.memory.add(
            comment.text,
            comment.mood,
            context=f"{self._last_ocr} {self._last_window}",
            scene=scene_line,
            dialog=kind,
        )
        self.memory.save()
        print(f"[dialog] 这句是「{dialog_mod.label(kind)}」｜累计 {dialog_mod.tally(self.memory.dialog)}")
        return scene_line

    # ---------- 口味档案（自己训练自己）----------

    def _finish_video(self) -> None:
        """一支视频看到头了：把"看了多久"结算进档案，兴趣分这时候才算得准。"""
        if self._video_record is None:
            return
        watched = max(0.0, time.monotonic() - self._video_started_at)
        end_ratio = float(getattr(self.cfg.taste, "end_ratio", 0.85))
        ended = self._video_max_ratio >= end_ratio
        self.taste.finish(self._video_record, watched, ended=ended)
        self._video_record = None
        self._video_max_ratio = 0.0
        self._progress_noted = False

    def _note_progress(self, frame) -> None:
        """量一眼底部进度条（本地、免费）：记下这支视频见过的**最大**进度。

        为什么记最大值而不是当前值：视频播到头时进度条要么消失、要么被重播画面
        盖掉，最后一眼未必量得到；但"它曾经顶到 9x%"这件事已经足够说明他看完了。
        认不出来就什么都不做——结算时 ended 还是 False，跟以前的版本一模一样。
        """
        taste_cfg = getattr(self.cfg, "taste", None)
        if not bool(getattr(taste_cfg, "progress_bar", True)) or self._video_record is None:
            return
        try:
            bar = progress.detect(frame)
        except Exception as exc:      # 量进度不能把主循环带走
            print(f"[taste] 量进度条没量成：{exc!r}")
            return
        if bar is None:
            return
        self._video_max_ratio = max(self._video_max_ratio, float(bar.ratio))
        end_ratio = float(getattr(taste_cfg, "end_ratio", 0.85))
        if not self._progress_noted and bar.ratio >= end_ratio:
            self._progress_noted = True
            print(f"[taste] 进度条快到尽头了（{bar.line()}），这支视频算看完")

    def _apply_actions(self, actions: Dict[str, bool], note: str = "") -> None:
        """把这一眼判出来的互动合进档案；**新出现**的（从没有到有）当场接一句。

        免费信号（画面文字里的「已关注」「已收藏」）和模型看按钮的结论都走这里，
        这样"他到底吃不吃这套"是攒出来的，不是猜的。
        """
        if not actions:
            return
        fresh = {name: hit for name, hit in actions.items() if hit and not self._actions.get(name)}
        self._actions.update({name: bool(hit) for name, hit in actions.items()})
        self.taste.update(self._video_record, actions=self._actions)
        if not fresh or self._video_record is None:
            return
        if not bool(getattr(self.cfg.watch, "react_to_actions", True)):
            return
        labels = "、".join(taste.ACTION_LABELS.get(name, name) for name in fresh)
        print(f"[taste] 新互动：{labels}（{self._video_record.title[:20]}）")
        self._spotlight("action", note=note, action=labels, min_gap=30.0)

    def _spotlight(self, kind: str, note: str = "", action: str = "", min_gap: float = 20.0) -> None:
        """两个"必须开腔"的场合：刷到新视频 / 他刚点赞收藏关注。

        像群聊小助手那样：有料就接一句，不再等"画面变化 + 冷却"那一套。
        万一模型没接上，就退到一句短反应顶上，别让场子冷掉。
        """
        if not self._client.ready:
            return
        now = time.monotonic()
        if (now - self._last_spotlight_at) < max(0.0, float(min_gap)):
            return
        if (now - self._last_spoke_at) < 8.0:        # 刚冒过泡，别两句挤在一起
            return
        cap = self.cfg.capture
        if not self._rate_ok(self._spotlight_calls, int(getattr(cap, "absorb_max_per_min", 6))):
            return                                        # 猛刷视频时别把它变成烧钱机器
        self._last_spotlight_at = now

        small = self._last_small
        view, frames = self._view_image(small) if small is not None else (None, 1)
        self.thinking.emit(True)
        try:
            comment = self._client.spotlight(
                kind,
                note=note,
                action=action,
                image=view,
                frames=frames,
                history=self._history,
                ocr_text=self._last_ocr,
                window=self._last_window,
                memory=self.memory.context(),
                clock=proactive.clock_text(),
                scene=self._last_scene,
                watch=self.watch_context(note or action),
                taste=self.taste.profile_block(),
                episode=self.episode.block(),
            )
        except VlmError as exc:
            self._report(f"接话失败：{exc}")
            return
        except Exception as exc:      # 一条接话不能把线程带走
            print(f"[spotlight] 异常：{exc!r}")
            return
        finally:
            self.thinking.emit(False)

        if comment is None:
            print(f"[spotlight] {kind}：这次没接上话")
            return
        if self.is_repeat(comment.text):
            ack = next((line for line in humanstyle.acks() if not self.is_repeat(line)), "")
            if not ack:
                print(f"[spotlight] {kind}：只想到重复的话，这次算了")
                return
            comment = Comment(ack, "happy", comment.kind)
        self._record_comment(comment)
        self._remember(comment.text)
        self._last_spoke_at = time.monotonic()
        self.proactive.note_spoke(self._last_spoke_at)
        self.comment.emit(comment)
        self._maybe_summarize()
        print(f"[spotlight] {kind} -> {comment.text}")

    # ---------- 主动搭话 ----------

    def _maybe_nudge(
        self,
        now: float,
        changed: Optional[bool],
        idle,
        hour: Optional[int] = None,
    ) -> None:
        """问一句策略"现在该不该主动开口"，该就说。"""
        nudge = None
        try:
            nudge = self.proactive.observe(
                now, changed, idle, hour, activity=self._activity()
            )
        except Exception as exc:  # 搭话逻辑出问题不能拖垮整个循环
            print(f"[proactive] 判定异常：{exc}")
        if nudge is None:
            # 上班时的喝水 / 站起来 / 闭眼提醒：不看画面，只看钟点（本地台词，不花钱）
            try:
                nudge = self.care.observe(now, hour, idle)
            except Exception as exc:
                print(f"[care] 判定异常：{exc}")
        if nudge is not None:
            self._safe_emit(nudge, now, "proactive")

    def _safe_emit(self, nudge, now: float, tag: str) -> None:
        """说一句之前最后兜一层网。

        `_emit_nudge` 里会碰本地台词池、防重复闸、提示词拼装……任何一处抛异常，
        整个后台线程就没了——屏幕上就是"它忽然再也不说话了"，最难查的那种毛病。
        宁可这一句没说出来，也不能让主循环死掉。
        """
        try:
            self._emit_nudge(nudge, now)
        except Exception as exc:
            print(f"[{tag}] 这一句没说出来：{exc!r}")

    def _emit_nudge(self, nudge, now: float) -> None:
        """把一次主动开口变成气泡：本地台词直接用，要问模型就问一次（问不到用兜底台词）。"""
        if nudge is None:
            return
        if nudge.local:
            # 本地台词是写死的（"哟，回来了"），跨场次说话很容易撞上刚吐槽过的那句
            comment = self._dedup(Comment(nudge.line, nudge.mood, f"proactive:{nudge.kind}"), "搭话")
        else:
            comment = self._nudge_via_model(nudge)
            if comment is not None and self.is_repeat(comment.text):
                # 搭话也别老一套：让它换个说法再来一次，还重复就用本地台词兜底
                again = self._nudge_via_model(nudge, avoid=list(self._recent))
                comment = again if (again is not None and not self.is_repeat(again.text)) else None
            if comment is None:
                fallback = proactive.FALLBACK_LINES.get(nudge.kind)
                if fallback is None:
                    return
                comment = self._dedup(Comment(fallback.text, fallback.mood, f"proactive:{nudge.kind}"), "兜底")
                if comment is None:
                    # 兜底那句也说过了：从本地台词池里换一句顶上——
                    # 这一轮已经决定要开口了，不能因为"别重复"就一点反应都没有。
                    comment = self._dedup(self._local_comment(nudge.kind), "兜底")
        if comment is None:
            return
        self.proactive.note_spoke(time.monotonic())
        self._scene_line(comment)
        self._remember(comment.text)
        self.comment.emit(comment)
        print(f"[proactive] {nudge.kind} → {comment.text}")

    def _nudge_via_model(self, nudge, avoid=()) -> Optional[Comment]:
        """就着最近那一帧问模型"自己找句话"。OCR 和窗口标题复用上一轮缓存，不重复花钱。

        它自己翻出来的话头（nudge.topic）一并带上——"你最近老在看…"比空口搭话自然得多。
        """
        if self._last_small is None or not self._client.ready:
            return None
        self.thinking.emit(True)
        try:
            return self._client.nudge(
                nudge.kind,
                image=self._last_small,
                history=self._history,
                ocr_text=self._last_ocr,
                window=self._last_window,
                memory=self.memory.context(),
                note=nudge.note,
                topic=nudge.topic,
                clock=proactive.clock_text(),
                scene=self._last_scene,
                watch=self.watch_context(nudge.topic or ""),
                avoid=avoid,
                taste=self.taste.profile_block(),
                episode=self.episode.block(),
            )
        except VlmError as exc:
            self._report(f"主动搭话失败：{exc}")
            return None
        except Exception as exc:  # 兜住一切，别让一条搭话把线程带走
            print(f"[proactive] 调用异常：{exc}")
            return None
        finally:
            self.thinking.emit(False)

    # ---------- 看片笔记 ----------

    def watch_context(self, question: str = "") -> str:
        """给提示词的看片笔记 + 「他这支看到哪了」（关掉 watch 也照样给进度）。

        两块合起来交给提示词，它才知道"他刚才看到 40%，现在跳到 70% 了"，
        也就是**跟着用户的进度**说话（见 _viewing_block）。
        """
        parts: List[str] = []
        watch_cfg = getattr(self.cfg, "watch", None)
        if bool(getattr(watch_cfg, "enabled", True)):
            parts.append(
                self.watch.context_for(question, limit=int(getattr(watch_cfg, "context_items", 6)))
            )
        parts.append(self._viewing_block())
        return "\n\n".join(part for part in parts if part)

    def _viewing_block(self) -> str:
        """「他这支片子看到哪了」：进度 + 一路看过来读到的东西。

        用户原话：「把正在看的部分给看完，然后跟随用户的进度给出互动」——
        所以这里把「进度 xx%」和每一段读到的内容攒成一块交给提示词：
        它就能按"他看到哪儿了"说话（前面看过的别当没看过，还没演到的别剧透）。
        """
        parts: List[str] = []
        if self._video_max_ratio > 0.01:
            parts.append(f"他这支已经看到 {self._video_max_ratio:.0%} 了。")
        if self._seen_notes:
            parts.append(
                "一路看过来读到的（越靠后越新）：\n"
                + "\n".join(f"· {row}" for row in self._seen_notes[-5:])
            )
        if not parts:
            return ""
        return (
            "【他这支片子看到哪了】\n"
            + "\n".join(parts)
            + "\n按他**现在的进度**说话：前面看过的事能接着提，别当没发生过；"
            "还没演到的别提前说出来（那就成剧透了）。"
        )

    # ---------- 用户说话（打字 / 语音）----------

    def submit_user(self, text: str) -> None:
        """用户说了一句：排队等它回答（主循环会优先处理）。"""
        content = (text or "").strip()
        if not content or not self.cfg.chat.enabled:
            return
        while len(self._user_queue) >= 5:   # 积压太多就丢掉最旧的，别越堆越多
            self._user_queue.popleft()
        self._user_queue.append(content)
        self._wake.set()

    def _answer_user(self, user_text: str, chat_calls: Deque[float]) -> None:
        """正面回答用户：抓一眼当前画面 + 带上记忆和刚聊过的话，然后开口。"""
        # 他这句是不是在诉苦：是的话这一轮不谈画面，只安慰 + 抱抱（见 humanstyle.needs_comfort）
        comfort = humanstyle.needs_comfort(user_text)
        self._note_turn(user_text)
        self.thinking.emit(True)
        try:
            if not self._client.ready:
                if comfort:
                    # 接口用不了，但安慰这一句是本地写好的：该给的抱抱一定要给到
                    self._safe_emit(
                        self.proactive.local_line(proactive.HUG, avoid=self._recent),
                        time.monotonic(),
                        "chat",
                    )
                    return
                self.notice.emit("我还没配 API Key，答不了话……右键我 → 打开配置文件。")
                return
            if not self._rate_ok(chat_calls, int(getattr(self.cfg.chat, "max_per_min", 10))):
                self.notice.emit("你问得有点快，让我喘口气再答")
                return

            small = None
            region = self.target_region()
            if region is None and self._target_state != "off":
                # 盯着的程序不在：不拍别的画面，就凭记忆和刚才那点上下文聊
                print("[chat] 盯着的程序现在不在，这次就凭记忆聊")
            else:
                self.capture_region = region
                try:
                    image = self._grab(region)
                except Exception as exc:
                    image = None
                    print(f"[chat] 抓画面失败，这次就凭记忆聊：{exc}")
                if image is not None:
                    small = capture.shrink(image, int(self.cfg.capture.max_width))
                    self._last_small = small
                    if self.reader.enabled:
                        self._last_ocr = self.reader.read_text(image, capture.dhash(small))
            if self.cfg.ocr.window_title:
                self._last_window = self._window_context()

            view, frames = self._view_image(small) if small is not None else (None, 1)
            comment = self._reply_via_model(
                user_text, view, frames=frames, comfort=comfort, dialog_hint=self._turn_hint
            )
            if comment is None and self._client.last_drop in ("title", "internal", "narration", "meta"):
                # 它交的是抓来的标题 / 念屏幕 / 解说词 / 自己那套机器的话——这些一律不进气泡。
                # 用户正等着回话，所以换个说法当场再要一次（跟防重复那条路同一个做法）。
                print(f"[chat] 上一句是{self._client.last_drop}，换个说法再要一次")
                comment = self._reply_via_model(
                    user_text,
                    view,
                    avoid=list(self._recent),
                    frames=frames,
                    comfort=comfort,
                    dialog_hint=self._turn_hint,
                )
            if comment is not None and self.is_repeat(comment.text):
                # 对话也不能车轱辘：换个说法重说一次（这一轮必须答话，所以两次里挑一次）
                again = self._reply_via_model(
                    user_text,
                    view,
                    avoid=list(self._recent),
                    frames=frames,
                    comfort=comfort,
                    dialog_hint=self._turn_hint,
                )
                comment = again or comment
            if (
                comment is not None
                and self._strict_repeat
                and humanstyle.looks_like_question_echo(comment.text, user_text)
            ):
                # 它把他这句话换个字原样抛了回来（现场：问「你在干嘛呢」→答「他在干嘛呢？」）：
                # 这跟没答一样，当场换个说法再要一次；再要还是这样就用原来那句兜底（别把话说空）。
                print(f"[chat] 这句把他的问句抛回来了，换个说法再要一次：{comment.text[:24]}")
                again = self._reply_via_model(
                    user_text,
                    view,
                    avoid=list(self._recent) + [comment.text],
                    frames=frames,
                    comfort=comfort,
                    dialog_hint=self._turn_hint,
                )
                if again is not None and not humanstyle.looks_like_question_echo(again.text, user_text):
                    comment = again
            if comment is not None and comfort:
                # 安慰和抱抱是一对：模型有时候只顾着讲道理，这里补上那一下（有"抱"就不动它）
                comment = self._with_hug(comment)
        except VlmError as exc:
            self.notice.emit(f"我这边接口出问题了：{exc}")
            return
        except Exception as exc:  # 一条回复出错不能把线程带走
            print(f"[chat] 异常：{exc}")
            self.notice.emit("我刚走神了，你再说一遍？")
            return
        finally:
            self.thinking.emit(False)

        if comment is None:
            self.notice.emit("这个我真答不上来，换个问法？")
            return
        self._scene_line(comment)
        self._remember_chat(user_text, comment.text)
        self._recent.append(comment.text)   # 让"别重复"这条线也管到对话
        self.proactive.note_spoke(time.monotonic())
        self.comment.emit(comment)
        print(f"[chat] 用户：{user_text} -> {comment.text}")

    def _reply_via_model(
        self,
        user_text: str,
        image,
        avoid=(),
        frames: int = 1,
        comfort: bool = False,
        dialog_hint: str = "",
    ) -> Optional[Comment]:
        """问一次模型要一句回复（用户说的话优先，不排队、不冷却）。

        dialog_hint 是他这句话的类型推出来的接话提醒（见 dialog.hint）。
        """
        return self._client.reply(
            user_text,
            image=image,
            frames=frames,
            history=self._history,
            chat_history=self._chat_history,
            ocr_text=self._last_ocr,
            window=self._last_window,
            memory=self.memory.context(),
            clock=proactive.clock_text(),
            scene=self._last_scene,
            watch=self.watch_context(user_text),
            avoid=avoid,
            taste=self.taste.profile_block(),
            comfort=comfort,
            dialog_hint=dialog_hint,
            episode=self.episode.block(),
        )

    def _with_hug(self, comment: Comment) -> Comment:
        """他说的是心事：这一句里得有"抱"。

        提示词里已经让他自己带上了，但小模型偶尔只讲道理不给抱抱——
        这里补一下（已经有「抱」字就原样不动，别硬改它的句子）。
        """
        text = (comment.text or "").strip()
        if not text or "抱" in text:
            return comment
        return Comment(f"{text} 抱抱", comment.mood, comment.kind, comment.scene)

    def _remember_chat(self, user_text: str, reply: str) -> None:
        """对话单独记一份：不写进 memory.json，也不占"刚才说过"的位置。"""
        self._chat_history.append((user_text, reply))
        limit = max(1, int(getattr(self.cfg.chat, "history_size", 6)))
        if len(self._chat_history) > limit:
            del self._chat_history[:-limit]

    def _note_turn(self, user_text: str) -> str:
        """私下给他这句话分类（记录/分析/学习）并记账，返回这一类的接话提醒。

        分类只留在这里的内存和控制台日志里——对话本来就不写进 memory.json，
        所以这份统计也不落盘。它换来的是一句更对路的提醒：
        问得多就先把答案给清楚，情绪多就先跟着他的情绪走。
        """
        kind = dialog_mod.classify(user_text)
        self._turns[kind] += 1
        self._turn_hint = dialog_mod.hint(kind)
        print(f"[dialog] 他这句是「{dialog_mod.label(kind)}」｜{dialog_mod.tally(self._turns)}")
        return kind

    # ---------- 场景 ----------

    def _scene_line(self, comment: Comment) -> str:
        """记下这一眼看明白的 5W1H（日志留一份，下一轮提示词回喂给模型）；没有就返回空串。"""
        parsed = getattr(comment, "scene", None)
        if parsed is None or parsed.is_empty():
            return ""
        line = parsed.line()
        self._last_scene = line
        print(f"[scene] {line}")
        return line

    def _is_scene_cut(self, digest: int, now: float) -> bool:
        """画面大跳变（多半是换了一支视频）= 跟"上次读完那一帧"的指纹差得远。"""
        cap = self.cfg.capture
        if not bool(getattr(cap, "absorb_enabled", True)):
            return False
        if not bool(getattr(getattr(self.cfg, "watch", None), "enabled", True)):
            return False
        if self._paused.is_set() or not self._client.ready:
            return False
        distance = (
            1.0
            if self._last_absorb_hash is None
            else capture.hash_distance(digest, self._last_absorb_hash)
        )
        if distance < float(getattr(cap, "scene_cut_threshold", 0.14)):
            return False
        if now - self._last_absorb_at < float(getattr(cap, "absorb_min_gap_sec", 15.0)):
            return False
        # 顺手限速：刷短视频的话一分钟能刷十几支，别每一支都读
        return self._rate_ok(self._absorb_calls, int(getattr(cap, "absorb_max_per_min", 6)))

    def _remember_viewing(self, note: WatchNote) -> None:
        """把"这一步读到的东西"攒进「他这支片子看到哪了」（下一条提示词用）。

        为什么不光靠 watchlog.refresh：refresh 只留**最新一份**笔记（字段是覆盖的），
        他前面 20% 看过什么就被盖掉了。而用户要的是"把正在看的部分**给看完**，
        然后跟着他的进度互动"——所以要按进度留一条条**短记录**，
        它才能说出"你前面不是还说他俩不熟吗"这种跟着进度走的话。
        顺便把"读到这里了"记下来，下次按进度判断要不要重读（见 _should_refresh_absorb）。
        """
        self._last_absorb_ratio = self._video_max_ratio
        text = format_log(note)
        if not text or text.startswith("（没读出来"):
            return
        where = f"{self._video_max_ratio:.0%} 处" if self._video_max_ratio > 0.01 else "开头"
        row = f"{where}：{text[:88]}"
        if self._seen_notes and self._seen_notes[-1] == row:
            return
        self._seen_notes.append(row)
        if len(self._seen_notes) > self.VIEWING_KEEP:
            del self._seen_notes[:-self.VIEWING_KEEP]

    def _absorb(self, image, digest: int, refresh: bool = False) -> Optional[WatchNote]:
        """把这一眼**读成一份看片笔记**：不说话，只是先看完、记下来。

        refresh=True 是"同一支视频看久了再读一遍"：只更新笔记，不把旧笔记挤进历史。
        """
        self._last_absorb_hash = digest
        self._last_absorb_at = time.monotonic()

        window = self._window_context()

        ocr_text = ""
        key_block = ""
        if self.reader.enabled:
            try:
                # 按指纹缓存过，跟这一轮吐槽用的是同一份识别结果，不重复花钱
                result = self.reader.read(image, digest)
                if result.ok:
                    ocr_text = result.as_text(self.cfg.ocr.max_lines)
                    # 看片笔记最需要"这是什么"：本地认出来的台标/节目名/集数也一并交给它
                    # （跟这一轮吐槽用的是同一份缓存，不重复认）
                    key_block = self._key_block(result.lines, window, ocr_text)
                    # 「读一遍」的时候也顺手学：这一眼的字幕同样进接话样本（跟吐槽那轮共用识别结果）
                    self.learn.observe(result.lines)
                    self.episode.note(self._key_cache_info)
            except Exception as exc:
                print(f"[watch] 读画面文字失败：{exc}")

        # 重读时用连拍（看得出这几秒发生了什么）；第一次读用单张，别把上一支视频混进来
        view, frames = self._view_image(image) if refresh else (image, 1)
        previous = self.watch.current_line() if refresh else ""

        started = time.monotonic()
        self.thinking.emit(True)
        try:
            note = self._client.absorb(
                view,
                ocr_text=(key_block + "\n\n" + ocr_text) if key_block else ocr_text,
                window=window,
                clock=proactive.clock_text(),
                frames=frames,
                previous=previous,
            )
        except VlmError as exc:
            self._report(f"读视频失败：{exc}")
            return None
        except Exception as exc:  # 读笔记失败不能把线程带走
            print(f"[watch] 读视频异常：{exc}")
            return None
        finally:
            self.thinking.emit(False)

        if note is None:
            print("[watch] 这一眼没读出东西，下次大跳变再试")
            return None
        if refresh:
            self.watch.refresh(note)
            self.taste.update(self._video_record, note=note)
            self._video_title = (note.title or self._video_title or "").strip()
            self._video_title_at = time.monotonic()
            print(f"[watch] 重读一眼（{time.monotonic() - started:.1f}s）：{format_log(note, self.watch)}")
        else:
            self._finish_video()                     # 上一支到此为止：先把看了多久结算进档案
            self.watch.start_video(note)
            self._actions = {}
            # 换了视频，上一支的"上一眼场景"就不该再挂着——不然它会把旧场景当成这一支的画面
            self._last_scene = ""
            self._video_record = self.taste.start_video(note, ocr_text=ocr_text, window=window)
            self._video_started_at = time.monotonic()
            self._video_title = (note.title or "").strip()
            self._video_title_at = self._video_started_at
            self._video_max_ratio = 0.0        # 新的一支，进度重新量
            self._progress_noted = False
            self._last_absorb_ratio = 0.0      # 跟着进度看：从 0 开始记
            self._seen_notes = []
            print(f"[watch] 读完这支视频（{time.monotonic() - started:.1f}s）：{format_log(note, self.watch)}")
        # 模型看按钮 + 画面文字里的「已关注/已收藏」，两边合起来判他有没有动手
        actions = dict(getattr(note, "actions", None) or {})
        self._remember_viewing(note)     # 记下"读到哪了 + 读到什么"（跟着进度互动靠它）
        actions.update(taste.actions_from_text(ocr_text))
        self._apply_actions(actions, note=format_log(note, self.watch))
        if not refresh and bool(getattr(self.cfg.watch, "speak_on_new_video", True)):
            # 换视频 = 有料，像群聊小助手那样当场接一句（不再等"画面有没有变化"）
            self._spotlight("new_video", note=format_log(note, self.watch), min_gap=25.0)
        if not refresh:
            # 读完了这支片子：顺手给"这一集"做一次功课（认不出是哪一集就什么都不做）
            self._learn_episode()
        return note

    def _learn_episode(self) -> None:
        """给"这一集"做功课：认出是哪一集之后，把整集的底细弄到手。

        「根据标签去扒原片、整个看完再回来」的落地版（见 pet/episode.py）：
        真去下载整集视频做不到，这里换成了"把这一集是什么内容弄到手"——
        拿「节目名+季+集」问一次模型（**纯文本**、不走画面），存成资料卡，
        之后吐槽、聊天、夸人都拿它当背景。同一集只问一次，认不出来就什么都不问。
        """
        if self.episode.note(self._key_cache_info) is None:
            return
        if not self.episode.needs_brief():
            return
        key = self.episode.current_key
        show, season, part = self.episode.current_parts
        self.episode.mark_asked()
        source = str(getattr(getattr(self.cfg, "episode", None), "source", "") or "")
        self.thinking.emit(True)
        try:
            if source:
                text = episode.fetch_source(source, show, season, part)
            else:
                text = self._client.episode_brief(
                    show, season, part, extra=(self.watch.current_line() or ""),
                )
        except VlmError as exc:
            print(f"[episode] 做功课没成：{exc}")
            return
        except Exception as exc:      # 做功课失败不能把后台线程带走
            print(f"[episode] 做功课异常：{exc}")
            return
        finally:
            self.thinking.emit(False)
        brief = self.episode.apply(key, text)
        if brief is not None and not brief.is_empty():
            print(f"[episode] 功课做完了：{brief.line()}")
        else:
            print(f"[episode] 这一集（{key}）暂时查不到，资料卡先空着")

    def _maybe_summarize(self) -> None:
        """攒够了就让模型总结一次"观众画像"，写进长期记忆。"""
        if not self.memory.needs_summary():
            return
        try:
            reply = self._client.summarize(self.memory.summary_prompt())
        except VlmError as exc:
            self._report(f"记忆总结失败：{exc}")
            return
        except Exception as exc:  # 记忆出问题不能影响吐槽
            print(f"[memory] 总结异常：{exc}")
            return
        if self.memory.apply_summary(reply):
            self.memory.save(force=True)
            print(f"[memory] 画像已更新：{self.memory.profile[:60]}")

    # ---------- 内部工具 ----------

    def _remember(self, text: str) -> None:
        """记住它刚说的这句：短的进提示词历史，长的进"别重复"名单。"""
        if not (text or "").strip():
            return
        self._history.append(text)
        limit = max(1, int(self.cfg.capture.history_size))
        if len(self._history) > limit:
            del self._history[:-limit]
        self._recent.append(text)

    def is_repeat(self, text: str, threshold: Optional[float] = None) -> bool:
        """这句话是不是在重复最近说过的（跨吐槽/对话/搭话一起比）。

        mock（离线演示）模式下不做这个判断：那套台词池本来就只有几句，
        重复是设计好的，否则演示两轮之后它就永远闭嘴了。
        """
        if not self._strict_repeat:
            return False
        return humanstyle.is_repeat(
            text, self._recent, threshold if threshold is not None else self.REPEAT_THRESHOLD
        )

    def _local_comment(self, kind: str) -> Optional[Comment]:
        """从本地台词池里挑一句，包成能过防重复闸的 Comment（挑不到就是 None）。

        `proactive.local_line()` 给的是 Nudge（因为它是"一次开口"），
        而要过闸门的是 Comment——这一步就是两者之间的那层转换。
        """
        nudge = self.proactive.local_line(kind, avoid=self._recent)
        if nudge is None:
            return None
        return Comment(nudge.line, nudge.mood, f"proactive:{kind}")

    def _dedup(self, comment: Optional[Comment], tag: str) -> Optional[Comment]:
        """说出口之前的最后一道闸：跟最近说过的话太像就别说了。

        以前这道闸只拦"模型想出来的台词"，本地台词、兜底台词、补说那一次都从旁边溜了过去——
        写死的台词池就那么几句，连着撞车的时候，屏幕上就是同一句话又说了一遍。
        现在所有出口都从这里过：宁可这一轮不说，也不要它把刚说过的话再背一遍。
        """
        if comment is None:
            return None
        if not self.is_repeat(comment.text):
            return comment
        print(f"[repeat] {tag}又想说一句刚说过的，这次不说了：{comment.text[:20]}")
        return None

    def _rate_ok(self, calls: Deque[float], limit: int) -> bool:
        now = time.monotonic()
        while calls and now - calls[0] > 60.0:
            calls.popleft()
        if limit > 0 and len(calls) >= limit:
            return False
        calls.append(now)
        return True

    @property
    def target_state(self) -> str:
        """锁程序时的状态。

        off（没锁）/ ok（正在看）/ missing（找不到窗口）/ background（切后台了）/
        **minimized**（收起来了——监视不停：窗口能画就画，画不出来就回放最后一眼，
        见 _grab_minimized）。
        """
        return self._target_state

    def target_region(self) -> Optional[dict]:
        """这一轮该拍哪块画面（物理像素）。

        * 没锁进程：框选区域，没框选就是整屏（region=None 交回给 capture 决定）
        * 锁了进程：跟着那个窗口的**内容区**走，窗口挪了、改了大小都跟得上；
          默认**不管它在不在前台**——你切去写代码，它照样盯着那个窗口
          （这时靠 wincap 直接抓窗口自己的画面，见 _grab）
        * 锁了进程、但它**最小化**了：照样返回那块矩形（还原之后的），监视不停——
          抓画面交给 _grab_minimized（先问窗口能不能画，不行就回放最后一眼）
        返回 None = 现在真的拍不到（窗口关掉了 /【勾了"只在前台看"时】切走了）。
        """
        cap = self.cfg.capture
        process = str(getattr(cap, "target_process", "") or "").strip()
        previous = self._target_state
        if not process or not winfind.available():
            self._target_state = "off"
            self._target_hwnd = 0
            self._target_foreground = True
            self._min_noted = False
            return cap.region

        info = winfind.find_window(
            process,
            str(getattr(cap, "target_title", "") or ""),
            client_area=bool(getattr(cap, "target_client_area", True)),
            include_minimized=True,
        )
        if info is None:
            self._target_state = "missing"
            self._target_hwnd = 0
            self._min_noted = False
            return None
        if info.minimized:
            # 最小化不等于"看不见了"，监视照跑（见 _grab_minimized）：
            # 句柄留着（要问它能不能画）、矩形给"还原之后"那块（托盘/挡没挡得住都看它）。
            # 提醒那句每收起来一次只说一遍，所以进这一支时才把标记放掉。
            if previous != "minimized":
                self._min_noted = False
                print(f"[min] {process} 最小化了：先按最后一眼接着看（画面等它放出来）")
            self._target_state = "minimized"
            self._target_hwnd = int(info.hwnd)
            self._target_foreground = bool(info.foreground)
            return info.rect
        if previous == "minimized":
            print(f"[min] {process} 放出来了，接着看真画面")
        self._min_noted = False
        self._target_hwnd = int(info.hwnd)
        self._target_foreground = bool(info.foreground)
        if (
            bool(getattr(cap, "target_foreground_only", False))
            and not info.foreground
            and not winfind.is_own_pid(winfind.foreground_pid())   # 聊天面板抢前台不算切走
        ):
            self._target_state = "background"
            return None
        self._target_state = "ok"
        return info.rect

    def _note_target_missing(self) -> None:
        """锁的程序现在看不到：偶尔提一句，别一直刷屏。"""
        if self._target_state == "minimized" and self._min_noted:
            return                      # 收起来的这段时间只说这一遍（下面还会回放上一眼）
        now = time.monotonic()
        if now - self._target_notice_at < self.TARGET_NOTICE_COOLDOWN:
            return
        self._target_notice_at = now
        name = str(getattr(self.cfg.capture, "target_process", "") or "")
        if self._target_state == "minimized":
            self._min_noted = True
            self.notice.emit(
                f"{name} 最小化了：系统不给它画新画面，我接着盯——"
                "先按最后一眼陪着你，放出来我马上接着看"
            )
        elif self._target_state == "background":
            self.notice.emit(f"{name} 切到后台了，我先歇着，你切回来我接着看")
        else:
            self.notice.emit(f"没找到 {name} 的窗口——打开它，或者右键我换个程序盯")

    def _grab(self, region: Optional[dict]):
        """拍一帧。锁了进程、而它又不在前台时，直接抓**窗口自己**的画面。

        为什么不能一路都走 mss：mss 拍的是"屏幕上此刻显示的像素"——被别的窗口盖住时，
        拍到的就是盖在上面的那个窗口。与其拿错画面去问模型，不如让窗口自己画一遍。

        最小化则走 _grab_minimized（先试窗口、不行回放上一眼），**绝不**落到下面的
        mss：那时候窗口已经被挪到 (-32000,-32000)，按它给的坐标拍下来的是别的窗口。
        """
        if self._target_state == "minimized":
            return self._grab_minimized()
        self._frame_stale = False
        if self._target_hwnd and self._target_state == "ok" and not self._target_foreground:
            if bool(getattr(self.cfg.capture, "window_capture", True)):
                shot = wincap.grab(
                    self._target_hwnd,
                    client_only=bool(getattr(self.cfg.capture, "target_client_area", True)),
                )
                if shot is not None:
                    return shot
                raise RuntimeError("被盯的窗口画面取不到（可能在独占全屏，或者刚被挡住）")

        if not self.occluder_active or not self.handshake_ready:
            return capture.grab(region)

        # 自己挡在画面里了：请主线程把自己藏起来，藏好了再拍
        self._hidden_ack.clear()
        self.hide_requested.emit()
        if not self._hidden_ack.wait(1.5):
            return capture.grab(region)
        try:
            time.sleep(0.06)  # 等窗口真正从合成器上消失
            return capture.grab(region)
        finally:
            self._shown_ack.clear()
            self.show_requested.emit()
            self._shown_ack.wait(1.5)

    def _grab_minimized(self):
        """被盯的窗口**最小化**了，这一眼拍什么。

        为什么不一走了之：最小化只是"你把它收起来了"，不是"你不看了"——监视得接着跑
        （看片笔记、时间线、聊到这支视频时它得答得上来），所以这一轮仍然要有一帧。

        三条路，按顺序：
          1. **先问窗口自己能不能画**：Windows 一般不渲染最小化的窗口（实测浏览器给的是
             全黑图），但老式 GDI 程序照样画得出来——拿得到就是真画面，正常往下走。
             试失败不用每轮都试（一次 PrintWindow + 一张位图，白扔），隔 MIN_GRAB_RETRY 再问。
          2. **回放最后一眼**：上一张真画面。这样它还"记得你看到哪儿了"——画面不会更新，
             所以主循环看到 `_frame_stale` 就只维持状态、不吐槽、不吸收（不拿旧画面编新词）。
          3. 连最后一眼都没有（刚启动就最小化）：返回 None，主循环当"真看不到"处理。
        """
        capture_ok = bool(getattr(self.cfg.capture, "window_capture", True))
        now = time.monotonic()
        if self._target_hwnd and capture_ok and now - self._min_tried_at >= self.MIN_GRAB_RETRY:
            self._min_tried_at = now
            try:
                shot = wincap.grab(self._target_hwnd, client_only=False, allow_minimized=True)
            except Exception as exc:      # 抓不动不该把主循环带走
                shot = None
                print(f"[min] 抓最小化的窗口失败：{exc}")
            if shot is not None:
                self._last_shot = shot
                self._frame_stale = False
                print(f"[min] 它最小化之后居然还画得出来（{shot.width}×{shot.height}），照看")
                return shot
        self._frame_stale = True
        return self._last_shot

    def _window_context(self) -> str:
        """给模型的"窗口线索"。

        锁了进程就用**被盯窗口**的标题——那才是它在看的东西（"《歌手》第 8 期 - 哔哩哔哩"
        这种标题对认内容帮助极大）；前台窗口也一并带上，方便判断你此刻在干嘛。
        """
        rows: List[str] = []
        cap = self.cfg.capture
        process = str(getattr(cap, "target_process", "") or "").strip()
        if process:
            try:
                rows.append("你在盯的窗口：" + winfind.describe(process, str(getattr(cap, "target_title", "") or "")))
            except Exception:
                pass
        if self.cfg.ocr.window_title:
            try:
                front = foreground.describe()
            except Exception:
                front = ""
            if front:
                rows.append(("当前前台窗口：" if process else "当前窗口：") + front)
        return " ｜ ".join(rows)

    def _push_sequence(self, frame) -> None:
        """攒最近几眼（至少隔 0.6 秒留一张），用来拼"分镜图"。"""
        if frame is None:
            return
        limit = max(1, int(getattr(self.cfg.capture, "seq_frames", 1) or 1))
        self._seq = deque(self._seq, maxlen=max(limit, 2))
        now = time.monotonic()
        if self._seq and now - self._seq[-1][0] < 0.6:
            return
        self._seq.append((now, frame))

    def _view_image(self, fallback):
        """这一轮真正送模型的图：凑得齐连拍就送"分镜图"，否则就送这一眼。

        返回 (图, 图里有几眼)。分镜图从左到右按时间排，模型才看得出**在发生什么**
        （综艺的环节、剧情的来龙去脉、游戏里刚打完哪一波），而不是只有一张静图。
        """
        frames = max(1, int(getattr(self.cfg.capture, "seq_frames", 1) or 1))
        if frames <= 1 or not self._seq:
            return fallback, 1
        items = [img for _, img in list(self._seq)[-frames:] if img is not None]
        if len(items) <= 1:
            return fallback, 1
        sheet = capture.sequence_sheet(items, total_width=int(self.cfg.capture.max_width))
        if sheet is None:
            return fallback, 1
        return sheet, len(items)

    def _should_refresh_absorb(self, now: float) -> bool:
        """「同一支视频」要不要重读一遍：① 看久了（按时间）或 ② **进度往前走了够多**。

        ② 才是"跟着他的进度看"：同一支片子，在 10% 和 在 60% 看到的是**两段内容**，
        只拿开头那份笔记应付，说起来就永远是开头那点事。每往前走
        absorb_progress_step（默认 10%）就重读一次，把这一段也读进去——
        这样"正在看的部分"是**一段段读完**的，笔记跟着进度长。
        """
        every = float(getattr(self.cfg.capture, "absorb_refresh_sec", 0) or 0)
        step = float(getattr(self.cfg.capture, "absorb_progress_step", 0) or 0)
        if not bool(getattr(self.cfg.capture, "absorb_enabled", True)):
            return False
        if not bool(getattr(getattr(self.cfg, "watch", None), "enabled", True)):
            return False
        if self._paused.is_set() or not self._client.ready:
            return False
        by_time = every > 0 and (now - self._last_absorb_at) >= every
        by_progress = step > 0 and (
            self._video_max_ratio - self._last_absorb_ratio >= step
            and (now - self._last_absorb_at)
            >= float(getattr(self.cfg.capture, "absorb_progress_min_gap_sec", 20.0))
        )
        if not (by_time or by_progress):
            return False
        return self._rate_ok(
            self._absorb_calls, int(getattr(self.cfg.capture, "absorb_max_per_min", 6))
        )

    def _report(self, message: str) -> None:
        now = time.monotonic()
        if message == self._last_error and now - self._last_error_at < self.ERROR_COOLDOWN:
            return
        self._last_error = message
        self._last_error_at = now
        self.failure.emit(message)

    def _sleep(self, seconds: float) -> None:
        # 等 _wake 而不是 _stop：stop()/analyze_now()/nudge_now() 都能提前把它叫醒
        self._wake.clear()
        self._wake.wait(max(0.05, seconds))
