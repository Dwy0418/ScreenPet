"""主动搭话：不等画面出槽点，自己找话说。

它原本是被动的——画面变了才吐槽一句；画面没变化、或者模型一直选择沉默，
它就能安静半小时。这里补上"自己开口"这一半。

四种基本由头，两种代价：
* 不花钱（本地台词）：人疑似走开了 → 打声招呼先去眯一会儿；人回来了 → 招呼一声。
* 花一次模型调用：画面久未变化 / 它自己太久没说话 → 让它就着当前画面自己找话题。

进阶版再加两条，让它不只是"随机找话说"：
* 翻长期记忆找话头（memory_topic）：沉默太久时，优先拿"你最近老在看的东西"开口。
  有记忆的陪伴才像熟人——"你那个王者荣耀还打呢？"比"你怎么不说话"自然得多。
  记忆**只读**：搭话永远不写进长期记忆（免得它拿自己的碎碎念当你的偏好）。
* 按时间段变化（late_night）：深夜还盯着屏幕，就唠叨一句该睡了（本地台词，不花钱），
  且夜里这句优先于其他由头——这个点说"你该睡了"比说"你怎么不说话"合适。

再进阶两条，把「夸夸我 / 抱抱我」那两个热键并进来（praise / care_topic）：
他看视频 / 打游戏 / 干活**做久了**，就主动夸一句具体的（片子里的场景人物、他那一下
操作、他手头这摊活），再久一点就主动关心一句（身体 + 针对他正在做的事给点建议）。
"他现在在干嘛"由调用方通过 observe(..., activity=...) 传进来（见 keyinfo.activity_of），
认不出来就什么都不说——夸空话比不夸还尴尬。

一个关键判断：**"走开"必须是键鼠空闲 + 画面也没变**。
不然窝在沙发上看两小时电影（手不碰键鼠、但画面一直在动）会被误判成离开。

这里只管"什么时候该开口"，不碰 Qt、不碰网络、不读时钟（now 和 hour 都由调用方传进来），
所以可以用假时钟把几分钟的时序在毫秒内跑完，见 tools/smoke_test.py。
"""
from __future__ import annotations

import ctypes
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

from .mood import Comment

# 由头
DOZING = "dozing"              # 人走了，打个招呼去眯一会儿
WELCOME_BACK = "welcome_back"  # 人回来了
STILL_SCREEN = "still_screen"  # 画面久未变化
LONG_QUIET = "long_quiet"      # 它自己太久没说话
MEMORY_TOPIC = "memory_topic"  # 拿"你最近老在看的东西"开口
LATE_NIGHT = "late_night"      # 深夜还盯着屏幕，劝一句睡觉
HUG = "hug"                    # 用户送来一个抱抱（本地台词，不花钱，立刻回）
PRAISE = "praise"              # 他看视频/打游戏/干活有一会儿了，主动夸一句具体的
CARE_TOPIC = "care_topic"      # 同一件事做太久了，主动关心一句（身体 + 针对内容给建议）

STATE_WATCHING = "watching"
STATE_AWAY = "away"

PHASE_LATE_NIGHT = "late_night"
PHASE_DAY = "day"

TOPIC_MEMORY = 4          # 最近聊过的这几个话题不再重复


# 本地台词（不花钱，所以这类只在状态切换时说一次）
LOCAL_LINES: Dict[str, Tuple[Comment, ...]] = {
    DOZING: (
        Comment("这么久没动静，我先眯一会儿", "speechless"),
        Comment("人不在的话……我打个盹可以吧", "speechless"),
        Comment("走了？那我也歇会儿", "speechless"),
    ),
    WELCOME_BACK: (
        Comment("哟，回来了？", "curious"),
        Comment("人呢，刚还以为你把我忘了", "curious"),
        Comment("回来啦，继续看", "happy"),
    ),
    LATE_NIGHT: (
        Comment("都这个点了，你不睡我可要睡了", "speechless"),
        Comment("凌晨了还盯着屏幕，眼睛不要了？", "speechless"),
        Comment("这个点还在看，明天可不许怪我", "smirk"),
        Comment("夜猫子，我就陪你到这儿了", "speechless"),
    ),
    HUG: (
        Comment("抱抱收到啦 ❤", "happy"),
        Comment("嗯——被我抱住就别想跑了 ❤", "happy"),
        Comment("（一把抱住）今天辛苦了 ❤", "happy"),
        Comment("这抱抱我收下了，回你一个 ❤", "happy"),
    ),
    # 点名要夸：模型那条路没接上时，worker 会从这里挑一句（见 _emit_nudge）
    # 按"他正在干嘛"主动夸 / 主动关心：没配 Key（或者模型没接上）时也从这里挑，
    # 免得"看他打了俩小时游戏，一句关心都没有"。
    PRAISE: (
        Comment("你这一手真可以，我早想说这句了", "happy"),
        Comment("看你这么弄我是真服气", "happy"),
        Comment("别说，你挑东西的眼光是真行", "smirk"),
        Comment("你这状态我喜欢，稳得住", "happy"),
    ),
    CARE_TOPIC: (
        Comment("坐挺久了吧，先直起腰歇会儿", "speechless"),
        Comment("这个点了还没挪窝，腰不酸吗", "speechless"),
        Comment("别一口气干到底，起来晃两圈再回来", "curious"),
        Comment("眼睛也歇一下，抬头看看远处", "speechless"),
    ),
}

# 问模型那两种：万一模型还是不肯开口，用这句兜底，免得白等一场
FALLBACK_LINES: Dict[str, Comment] = {
    STILL_SCREEN: Comment("这画面定住半天了，是在发呆吗", "curious"),
    LONG_QUIET: Comment("怎么一声不吭的，我陪你看呢", "curious"),
    MEMORY_TOPIC: Comment("你最近老在看这个，我记着呢", "curious"),
    "manual": Comment("嗯……我就随便说一句，你别不理我", "happy"),
    PRAISE: Comment("说真的，你这一手我看着挺服", "happy"),
    CARE_TOPIC: Comment("坐这么久了，起来活动活动吧", "speechless"),
}

# 给模型的由头说明
REASONS: Dict[str, str] = {
    STILL_SCREEN: "画面已经很久没有变化了",
    LONG_QUIET: "你已经很久没说话了",
    MEMORY_TOPIC: "你想起了他最近老在看的东西",
    LATE_NIGHT: "现在是深夜，他还盯着屏幕",
    "manual": "用户点名要你现在说一句",
    HUG: "用户给了你一个抱抱",
    PRAISE: "他在这件事上已经待了一阵，你想主动夸夸他",
    CARE_TOPIC: "他在这件事上坐太久了，你想主动关心关心他",
}

# 按"他正在干嘛"来夸 / 来关心时，动词用哪个（拼进给模型的由头里）
_ACTIVITY_VERB: Dict[str, str] = {
    "video": "看",
    "game": "打",
    "work": "忙",
}


# ---------- 时间段 ----------


def in_night(hour: int, start: int, end: int) -> bool:
    """hour 是否落在 [start, end) 这个时段里（跨零点也认，比如 23 → 6）。"""
    h, s, e = int(hour) % 24, int(start) % 24, int(end) % 24
    if s == e:
        return False
    if s < e:
        return s <= h < e
    return h >= s or h < e          # 跨零点


def time_phase(hour: int, pro) -> str:
    """当前处在哪个时间段。只有"深夜"会改变它说话的内容，其余一律算白天。"""
    if not getattr(pro, "time_aware", False) or hour is None:
        return PHASE_DAY
    if in_night(int(hour), int(pro.night_start_hour), int(pro.night_end_hour)):
        return PHASE_LATE_NIGHT
    return PHASE_DAY


def clock_text(ts: Optional[float] = None) -> str:
    """把时间点说成人话："凌晨 1:20" / "上午 9:05" / "下午 3:20" / "晚上 11:40"。

    塞进提示词里，模型才知道"这个点还看"是能拿来搭话的。
    """
    clock = time.localtime(time.time() if ts is None else ts)
    hour, minute = clock.tm_hour, clock.tm_min
    if hour < 5:
        label = "凌晨"
    elif hour < 9:
        label = "早上"
    elif hour < 12:
        label = "上午"
    elif hour < 13:
        label = "中午"
    elif hour < 18:
        label = "下午"
    else:
        label = "晚上"
    display = hour % 12 or 12
    return f"{label} {display}:{minute:02d}"



# ---------- 键鼠空闲 ----------


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


_user32 = ctypes.windll.user32 if os.name == "nt" else None
# 注意：GetTickCount/GetTickCount64 在 kernel32，GetLastInputInfo 才在 user32
_kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None

if _user32 is not None:
    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
    _user32.GetLastInputInfo.restype = ctypes.c_bool

if _kernel32 is not None and hasattr(_kernel32, "GetTickCount64"):
    _kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    _tick_counter = _kernel32.GetTickCount64
elif _kernel32 is not None and hasattr(_kernel32, "GetTickCount"):
    _tick_counter = _kernel32.GetTickCount
else:  # pragma: no cover - 真机上不会走到
    _tick_counter = None


def _tick_ms() -> Optional[int]:
    """系统启动以来的毫秒数（拿不到返回 None，那就干脆不判断走开/回来）。"""
    if _tick_counter is None:
        return None
    try:
        return int(_tick_counter())
    except Exception:  # pragma: no cover
        return None


def user_idle_seconds() -> Optional[float]:
    """用户多久没碰键鼠了（秒）；拿不到就返回 None，那就不判断走开/回来。

    Windows 的 GetLastInputInfo 是**全系统**的最后一次输入，正合我们需要的语义。
    dwTime 是 32 位 tick，机器开够 49.7 天会回绕，所以差值按 2^32 取模。
    """
    if _user32 is None:
        return None
    tick = _tick_ms()
    if tick is None:
        return None
    try:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        delta = ((tick & 0xFFFFFFFF) - int(info.dwTime)) & 0xFFFFFFFF
        return delta / 1000.0
    except Exception:  # pragma: no cover - 拿不到就当没有这个能力
        return None


# ---------- 一次"被搭话" ----------


@dataclass(frozen=True)
class Nudge:
    """一次主动开口。line 非空 = 用本地台词；line 为空 = 需要问一次模型。"""

    kind: str
    line: str = ""
    mood: str = ""
    note: str = ""
    topic: str = ""     # 话头（"他最近老在看「王者荣耀」"），交给模型当素材

    @property
    def local(self) -> bool:
        return bool(self.line)


class ProactivePolicy:
    """什么时候该主动开口。时间都由调用方通过 observe(now, ..., hour) 传进来，方便用假时钟测。"""

    def __init__(self, cfg, rng: Optional[random.Random] = None, topic_source=None):
        self.cfg = cfg
        self.pro = cfg.proactive
        self.rng = rng or random.Random()
        # 口味画像给的"劲头系数"：>1 更少主动开口（他最近没什么兴致），<1 更愿意陪两句
        self.scale = 1.0
        # topic_source() -> 候选话头列表（worker 传 memory.topics；测试里传假数据）
        self.topic_source = topic_source
        self.state = STATE_WATCHING
        self.count = 0               # 一共主动搭过几次话
        self._started = False
        self._still_since = 0.0      # 画面最近一次"变了"的时刻
        self._last_spoke = 0.0       # 最近一次开口（吐槽或主动搭话）
        self._last_nudge = 0.0       # 最近一次主动搭话（用来算冷却）
        self._nudges: Deque[float] = deque()
        self._last_local = ""        # 上一次的本地台词，别连着说同一句
        self._last_line: Dict[str, str] = {}   # 按由头记：local_line 挑过的那句（每个由头各记各的）
        self._last_idle: Optional[float] = None
        self._recent_topics: Deque[str] = deque(maxlen=TOPIC_MEMORY)
        self._last_topic = ""        # 最近一次拿来搭话的话头
        self._phase = PHASE_DAY
        # 按"他正在干嘛"夸 / 关心：记着上次夸的是哪件事，同一件事别连着重来
        self._content_key: Tuple[str, str] = ("", "")
        self._content_at = 0.0

    # ---------- 外部 ----------

    def reset(self, now: Optional[float] = None) -> None:
        """把计时器拨到"现在"（刚打开这个功能时别立刻炸出一句）。"""
        stamp = time.monotonic() if now is None else now
        self._started = True
        self._still_since = stamp
        self._last_spoke = stamp
        self._last_nudge = 0.0       # 0 = "还没主动搭过话"，别拿它跟时钟比
        self._nudges.clear()
        self.state = STATE_WATCHING

    def note_spoke(self, now: float) -> None:
        """它开口了（吐槽或搭话都算），计时器重来。"""
        self._last_spoke = now

    def observe(
        self,
        now: float,
        changed: Optional[bool],
        idle: Optional[float],
        hour: Optional[int] = None,
        activity=None,
    ) -> Optional[Nudge]:
        """看一眼该不该开口。changed=None 表示这一轮没拿到画面（按"没变化"算）。

        hour 是当前钟点（0~23），只在"按时间段说话"时用得上，不传就当白天。

        activity 是"他正在干嘛"，worker 传进来的 (类型, 具体是什么, 已经做了几分钟)；
        类型见 keyinfo.ACTIVITY_*。不传就跳过"主动夸 / 主动关心"这一块
        （所以老调用方和测试一句都不用改）。
        """
        if not self.pro.enabled:
            return None
        if not self._started:
            self.reset(now)

        if changed:
            self._still_since = now
        self._last_idle = idle

        away = self._is_away(now, idle)

        if away and self.state == STATE_WATCHING:
            self.state = STATE_AWAY
            return self._local(DOZING, now)

        if not away and self.state == STATE_AWAY:
            self.state = STATE_WATCHING
            self._still_since = now
            self._last_spoke = now          # 刚回来先别急着吐槽
            if idle is not None and idle <= float(self.pro.back_idle_sec):
                return self._local(WELCOME_BACK, now, cooldown=float(self.pro.back_cooldown_sec))
            return None

        if self.state == STATE_AWAY:
            return None                      # 人不在，就别自言自语了

        # 深夜还盯着屏幕，先说这句（比"你怎么不说话"合适）
        night = self._night_nudge(now, hour)
        if night is not None:
            return night

        # 他看视频 / 打游戏 / 干活已经有一阵了：主动夸一句、或者关心一句
        # （这就是把"夸夸我 / 抱抱我"并进主动搭话的那一半）
        content = self._content_nudge(now, activity)
        if content is not None:
            return content

        quiet_for = now - self._last_spoke
        if quiet_for >= float(self.pro.quiet_after_sec):
            # 憋不住要开口时先翻翻长期记忆：有"你最近老在看的东西"就拿它当话头
            topic = self._pick_topic()
            if topic is not None:
                return self._model_nudge(MEMORY_TOPIC, now, quiet_for, "没说话", topic=topic)
            return self._model_nudge(LONG_QUIET, now, quiet_for, "没说话")
        still_for = now - self._still_since
        if still_for >= float(self.pro.still_after_sec):
            return self._model_nudge(STILL_SCREEN, now, still_for, "没变化")
        return None

    def _content_nudge(self, now: float, activity) -> Optional[Nudge]:
        """就着他**正在做的事**主动夸一句、或者关心一句。

        activity = (类型, 具体是什么, 已经做了几分钟)，由 worker 算好递进来
        （见 keyinfo.activity_of）。三种类型三种夸法：
            video → 夸片子里的具体内容（场景 / 人物 / 这段情节）
            game  → 夸他刚刚那一下操作
            work  → 夸他这个人，再顺手关心身体
        做久了（care_after_min）优先"关心"，还没那么久（praise_after_min）就"夸"。

        认不出来（other）、或者同一件事刚夸过，就什么都不说——
        夸空话不如不夸，这是"情绪价值"和"尬聊"的分界线。
        """
        if not bool(getattr(self.pro, "content_nudge", True)) or not activity:
            return None
        parts = list(activity) + ["", 0.0]
        kind = str(parts[0] or "")
        label = str(parts[1] or "").strip()
        minutes = max(0.0, float(parts[2] or 0.0))
        if kind not in _ACTIVITY_VERB:
            return None

        care_after = float(getattr(self.pro, "care_after_min", 50.0) or 0.0)
        praise_after = float(getattr(self.pro, "praise_after_min", 20.0) or 0.0)
        if care_after > 0 and minutes >= care_after:
            target = CARE_TOPIC
        elif praise_after > 0 and minutes >= praise_after:
            target = PRAISE
        else:
            return None

        key = (target, label)
        gap = float(getattr(self.pro, "content_cooldown_min", 30.0) or 0.0) * 60.0
        if key == self._content_key and gap > 0 and (now - self._content_at) < gap:
            return None                      # 同一件事刚说过，别念第二遍

        verb = _ACTIVITY_VERB[kind]
        detail = f"{verb}「{label}」" if label else f"{verb}手头这个"
        note = f"他已经{detail} {int(minutes)} 分钟了"

        if bool(self.pro.model_nudge) and bool(getattr(self.cfg, "ready", False)):
            nudge = self._model_nudge(target, now, minutes, "", topic=label, note=note)
        else:
            # 没配 Key / 关了"问模型"这条路：也得有句关心，用本地台词顶上
            nudge = self._local(target, now)
        if nudge is None:
            return None                      # 撞上冷却或每小时配额了，这次就算了
        self._content_key, self._content_at = key, now
        return nudge

    def force(self, kind: str = "manual") -> Nudge:
        """忽略冷却，硬要一句（菜单/托盘里的测试项）。"""
        self._started = True
        return Nudge(kind=kind, note=REASONS.get(kind, ""))

    def local_line(self, kind: str, avoid=()) -> Optional[Nudge]:
        """立刻给一句本地台词（不看冷却、不排队、不花接口钱）。

        由头得在 LOCAL_LINES 里有池子（比如他诉苦时兜底的那个抱抱）：
        不记账也不占配额——这一句是"现在就得给"，不该被冷落。
        avoid 是"刚说过的几句"：同一句话刚说过就换一句，不然会被防重复那道闸整个挡掉。
        """
        pool = LOCAL_LINES.get(kind)
        if not pool:
            return None
        blocked = {str(text) for text in (avoid or ()) if str(text).strip()}
        choices = [c for c in pool if c.text not in blocked and c.text != self._last_line.get(kind)]
        if not choices:
            choices = [c for c in pool if c.text not in blocked] or list(pool)
        picked = self.rng.choice(choices)
        self._last_line[kind] = picked.text
        return Nudge(kind=kind, line=picked.text, mood=picked.mood)

    def stats(self) -> Dict[str, object]:
        now = time.monotonic()
        return {
            "enabled": bool(self.pro.enabled),
            "state": self.state,
            "count": self.count,
            "idle": self._last_idle,
            "still_for": (now - self._still_since) if self._started else 0.0,
            "quiet_for": (now - self._last_spoke) if self._started else 0.0,
            "recent": len(self._prune(now)),
            "phase": self._phase,               # 上一次 observe 时判断出来的时间段
            "last_topic": self._last_topic,     # 最近一次拿来搭话的话头
            "topics_used": list(self._recent_topics),
        }

    # ---------- 内部 ----------

    def _night_nudge(self, now: float, hour: Optional[int]) -> Optional[Nudge]:
        """深夜劝睡：本地台词（不花钱）。

        要等它自己先安静一会儿才说——刚打开挂件就一句"该睡了"太突兀；
        夜里这句最少隔 night_cooldown_sec 才唠叨一次，免得整晚听同一个数落。
        """
        self._phase = time_phase(hour, self.pro)
        if self._phase != PHASE_LATE_NIGHT:
            return None
        if (now - self._last_spoke) < float(self.pro.quiet_after_sec):
            return None
        wait = max(float(self.pro.cooldown_sec), float(self.pro.night_cooldown_sec))
        return self._local(LATE_NIGHT, now, cooldown=wait)

    def _pick_topic(self) -> Optional[str]:
        """挑一个"他最近老在看"的话头；最近聊过的那个不再重复。挑不到返回 None。"""
        if not self.pro.topic_from_memory or self.topic_source is None:
            return None
        try:
            candidates = list(self.topic_source() or [])
        except Exception as exc:      # 记忆出问题不能把搭话这条路堵死
            print(f"[proactive] 取话题失败：{exc}")
            return None
        for topic in candidates:
            label = str(getattr(topic, "label", "") or "")
            if not label or label in self._recent_topics:
                continue
            self._recent_topics.append(label)
            self._last_topic = label
            return str(getattr(topic, "hint", "") or label)
        return None

    def _is_away(self, now: float, idle: Optional[float]) -> bool:
        """走开 = 键鼠空闲够久 **且** 画面也没在动（否则是在躺着看电影）。"""
        if idle is None:
            return False
        limit = float(self.pro.away_after_sec)
        if idle < limit:
            return False
        return (now - self._still_since) >= limit

    def _prune(self, now: float) -> Deque[float]:
        while self._nudges and now - self._nudges[0] > 3600.0:
            self._nudges.popleft()
        return self._nudges

    def _allowed(self, now: float, cooldown: float, local: bool) -> bool:
        # _last_nudge 为 0 表示"还没主动搭过话"，不能拿它跟时钟比
        # （否则机器刚开机、monotonic 还很小时，第一次深夜劝睡会被自己的冷却挡住一小时）
        if cooldown > 0 and self._last_nudge > 0.0 and (now - self._last_nudge) < cooldown:
            return False
        if not local and not self.pro.model_nudge:
            return False
        if not local and not getattr(self.cfg, "ready", False):
            return False                     # 没配 Key 就别费劲问模型了
        return len(self._prune(now)) < max(0, int(self.pro.max_per_hour))

    def _local(self, kind: str, now: float, cooldown: Optional[float] = None) -> Optional[Nudge]:
        wait = float(self.pro.cooldown_sec) if cooldown is None else cooldown
        wait *= max(0.2, float(getattr(self, "scale", 1.0)))
        if not self._allowed(now, wait, local=True):
            return None
        comment = self._pick(kind)
        self._mark(now)
        return Nudge(kind=kind, line=comment.text, mood=comment.mood)

    def _model_nudge(
        self,
        kind: str,
        now: float,
        elapsed: float,
        verb: str,
        topic: str = "",
        note: str = "",
    ) -> Optional[Nudge]:
        wait = float(self.pro.cooldown_sec) * max(0.2, float(getattr(self, "scale", 1.0)))
        if not self._allowed(now, wait, local=False):
            return None
        self._mark(now)
        # note 是给模型的由头（"他已经打「王者荣耀」40 分钟了"）；没给就退回秒数那种说法
        return Nudge(kind=kind, note=note or f"已经 {int(elapsed)} 秒{verb}", topic=topic)

    def _mark(self, now: float) -> None:
        self._nudges.append(now)
        self._last_nudge = now
        self._last_spoke = now
        self.count += 1

    def _pick(self, kind: str) -> Comment:
        pool = LOCAL_LINES.get(kind) or (Comment("……", "speechless"),)
        choices = [c for c in pool if c.text != self._last_local] or list(pool)
        picked = self.rng.choice(choices)
        self._last_local = picked.text
        return picked

