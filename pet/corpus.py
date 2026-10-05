"""边看边学：把它在屏幕上读到的字幕/台词，攒成"怎么接话"的语料。

为什么要有它
    watchlog.py 记的是「这支视频讲了什么」，taste.py 记的是「他爱看什么」——
    都没管「里面的人是怎么说话的、怎么互相接的」。而 humanstyle 那套语料本来
    就是给人往里贴聊天记录用的（`data/chat_style.json`），只是没人替它贴。

    这个模块就是替它贴：挂件每一眼 OCR 出来的弹幕/字幕先攒进 `data/learn.json`，
    过了闸门的相邻两句再升格进 `data/chat_style.json`（那 6 个键一个都不动），
    然后 humanstyle.reload()——**不用重启，下一次说话就用上了**。

三道闸（OCR 读出来的东西大半是垃圾，没闸门等于往语料里倒垃圾）
    ① 不像人话的丢：太短太长、时间码、「第 25 集」、UI 按钮词、台标平台名，以及
       **中英数字掺半的 OCR 碎片**（按"中文占比"卡，见 MIN_CJK_RATIO）；
    ② 每帧都在的丢（见 _is_furniture）：标题、「已关注」这种**一直挂在那儿**的字
       不是台词，台词是说一次就过去的；同一句在一半以上的观察里都在，就判成"家具"；
    ③ 解说腔 / 书面腔丢：借 humanstyle 现成的尺子（looks_like_narration / banned）。

认哪句是"同一句"，只认字、不认标点和空格（见 _key）——OCR 每一帧读出来的标点都会变，
按原文算的话，招牌闸和够格线都会因为"同一句被拆成好几种写法"而永远够不着。

学什么
    字幕是一句接一句过去的，所以把"新出现的台词"按时间排成一条线，相邻两句就是
    一个接话样本。其中「上一句在问、下一句在答」最值得学（真人接话大多就是这么接的），
    标成 qa；不是问答的只当"腔调节奏"素材，得反复出现才够格。

一句话：**一边看一边把别人的话拆成样本，攒够了自动进语料，下次说话就不一样了。**
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import humanstyle

VERSION = 2

# ---------- ① 不像人话的：直接丢 ----------

# 画面上的按钮 / 功能区：OCR 天天读得到，但没人会在聊天里说这些。
# 只收「真人几乎不会说」的：像「打开 / 更多 / 保存 / 查看」这种口语里也会出现的词**不能收**，
# 否则「把门打开」这种正常台词会被误杀（位置信息 OCR 层没留，只能靠词面判断）。
_UI_WORDS = (
    "已关注", "取消关注", "+关注", "已收藏", "取消收藏", "点赞", "收藏", "分享", "投币",
    "订阅", "登录", "注册", "弹幕", "倍速", "清晰度", "全屏", "暂停", "不感兴趣", "客户端",
    "举报", "首页", "热榜", "排行榜", "完整版", "正片", "预告", "花絮", "下集", "上集",
    "看更多", "抖音", "快手", "哔哩哔哩", "小红书", "微博", "腾讯视频", "爱奇艺", "优酷",
)

#: 「第 25 集」「03:10 / 12:26」「10万播放」这种：长得就像元数据，不像人在说话
_META_RE = re.compile(
    r"^\s*第\s*[\d一二三四五六七八九十百]+\s*[集期季话部篇]"
    r"|\d{1,2}\s*[:：]\s*\d{2}"
    r"|\d+\s*(?:万|亿)?\s*(?:播放|点赞|评论|粉丝|观看)"
)

#: 评论区 / 私信区的界面文字。**比例式招牌闸抓不到这一类**：每支视频的评论、
#: 用户名都不一样，出现比例低得很，可它们又一直往台词池里灌。只能按长相认——
#: 下面这三条都是「真人不会在台词里这么说」的形状，照现场日志加的：
#:   `@一只白色 QvQ。4 天前`（用户名 + 评论时间）、`# 王者荣耀 # 对抗路 # 娱乐`（话题标签）。
_UI_PATTERNS = (
    re.compile(r"^\s*@"),                                        # 行首的 @ 就是用户名
    re.compile(r"\d\s*(?:天|小时|分钟|秒|周|个月)前\s*$"),          # 评论时间戳「4 天前」
    re.compile(r"#.*#"),                                         # 话题标签（成对的两个 #）
)

#: **OCR 把界面读歪了**的形状：成对的半角括号 / 大括号 / 竖线，或者混着错字「囗」。
#: 真人打台词不用半角方括号；游戏设置面板、背包格子、装备栏被读成一串字才长这样。
#: 现场抓到过一条混进语料的：`杰可在 [ 设首 ] 里关闭或打开局内肯包中 3 囗色展示`。
_DEBRIS_RE = re.compile(r"[\[\]{}<>]|囗|\|\s*\|")

#: 「奔跑吧第六季第十一期」这种**集数标记在句子中间**的标题——锚在行首的 _META_RE 抓不到它。
#: 一句真话里不会同时出现两个「第 X 季 / 第 X 期」，所以数到两个以上就判成标题。
#: （只数一个的话，「这第六期太难看了」这种真台词会被误杀。）
_EPISODE_RE = re.compile(r"第\s*[\d一二三四五六七八九十百]+\s*[集期季话部篇]")

_CJK = re.compile(r"[\u3400-\u9fff]")

#: 一行里中文得占到这么多（去空格算）。台词是中文的；UI 碎片是
#: 「@ 暗区刂 TenZ 一 Valorant Agnes」「1 57 封耒读 〕 网易 “ 0 D 0418」这种中英数字掺半的样子。
#: 这条是照现场日志加的——原来那几条闸都拦不住它们。
MIN_CJK_RATIO = 0.5


def cjk_ratio(text: str) -> float:
    """一行字里中文占多少（空格不算进去）。"""
    packed = re.sub(r"\s+", "", text or "")
    if not packed:
        return 0.0
    return len(_CJK.findall(packed)) / float(len(packed))


# ---------- ② 问句：认「问 → 答」这种最值得学的接话对 ----------

_ASK_RE = re.compile(
    r"[？?]|(?:为什么|为啥|咋|怎么|什么|啥|多少|几点|哪[儿里个]|谁|多久|多远|多大"
    r"|是不是|对不对|有没有|能不能|行不行|好不好)"
)
_ASK_TAIL = ("吗", "呢")


def _clip(text: str, limit: int = 60) -> str:
    """压空白、剥掉两头包着的引号和尾巴上的分隔符；**句末的 ？。！不动**（判断问句要用）。

    两件事是照现场日志加的：
    * OCR 会在中文标点两边塞空格（`男人从没学过刑侦 ， 竟波破格提拔为刑警队`）——
      语料是给模型当示范的，带着这种空格看着就不像人打的字，得收到「刑侦，竟波」这样；
    * 碎片尾巴常拖着一个孤零零的 `·`、`-`、`、`（`叁我的 ·`），一并剥掉。
    `．` / `.` 也算在标点里：现场日志里字幕被读成 `幸福者退让 ．` 这种（点被空格顶开了）。
    """
    value = re.sub(r"\s*([，。？！、；：．.])\s*", r"\1", (text or "").strip())
    value = re.sub(r"\s+", " ", value)
    value = value.strip("「」『』\"'“”《》[]【】()（）")
    value = value.strip(" ·-—、,，．.")
    if limit and len(value) > limit:
        value = value[: max(1, limit - 1)].rstrip() + "…"
    return value


def _key(text: str) -> str:
    """比对的"耳朵"：只认字，不认标点和空格。

    同一句台词，OCR 每一帧读出来的标点都可能不一样（现场日志里同一条水印，
    一会儿是「作者声明：虚构演绎，仅供娱乐」，一会儿是「作者声明 ． 虚构演绎，仅供娱乐」）。
    拿原文当身份的话，这两个写法就是"两句不同的话"，于是：

    * 招牌闸的数永远攒不到 3 次、比例也压不过一半 → **认不出那块招牌**；
    * 同一行被当成"上一句"和"下一句"，攒出「作者声明 → 作者声明」这种自己接自己的对；
    * 见到的次数永远停在 1 → **够格线（问→答 ×2 / 前后 ×3）在实际使用中根本到不了**，
      升格那条路等于白摆着。

    所以计数和配对一律用这个键，写进语料 / 打印给人看的还是 `_clip` 收拾过的那句原话。
    """
    return re.sub(r"[\W_]+", "", text or "")      # \W 里不含中文，所以中文都留着


def looks_like_speech(
    text: str,
    min_chars: int = 4,
    max_chars: int = 40,
    banned: Sequence[str] = (),
) -> bool:
    """这一行"像不像有人在说话"——不像的话一个字节都不该进语料。

    OCR 读到的多半不是台词：台标、标题、时间码、第几集、按钮上的三个字。
    台词是完整的口语句子，所以这里卡四条：长度、**中文占比**（现场日志里
    `@ 暗区刂 TenZ 一 Valorant Agnes`、`1 57 封耒读 〕 网易 “ 0 D 0418` 这种中英数字掺半的
    OCR 碎片就是这么漏过去的）、像不像界面文字（含句中的「第 X 季第 X 期」这种标题，
    以及评论区的「@用户名 4 天前」「# 话题 #」）、有没有解说腔/书面腔。
    短句（「好家伙」）一律照收——真人张嘴就是这么短。
    """
    body = _clip(text)
    if len(body) < max(2, int(min_chars)) or len(body) > int(max_chars):
        return False
    if cjk_ratio(body) < MIN_CJK_RATIO:   # 中英数字掺半：多半是 UI 或 OCR 拼出来的碎片
        return False
    if not _CJK.search(body):             # 一个中文都没有：界面或水印
        return False
    if _META_RE.search(body):
        return False
    if len(_EPISODE_RE.findall(body)) >= 2:   # 「奔跑吧第六季第十一期」：集数标记在句子中间
        return False
    if any(word and word in body for word in _UI_WORDS):
        return False
    if any(pat.search(body) for pat in _UI_PATTERNS):    # 评论区 UI：用户名 / 时间戳 / 话题标签
        return False
    if _DEBRIS_RE.search(body):
        # 半角括号 / 大括号 / 竖线成对出现，或混着那个 OCR 错字「囗」：
        # 真人打台词不用半角方括号，网上视频的 UI（设置面板、背包格子）被读歪了才长这样。
        # 现场抓到过一条混进语料的：`杰可在 [ 设首 ] 里关闭或打开局内肯包中 3 囗色展示`
        return False
    if len(set(body)) <= 1:            # 「哈哈哈哈哈」这种没信息量的（短反应有 ACKS 那池子管）
        return False
    if humanstyle.looks_like_narration(body):   # 「谁和谁站在…前，似乎是在参与某个环节」
        return False
    if any(word and word in body for word in banned):     # 书面腔 / 客服腔
        return False
    return True


def is_question(text: str) -> bool:
    """这句是不是在问——用来判断"上一句在问"，问→答是最值得学的接话对。"""
    body = _clip(text)
    if not body:
        return False
    if _ASK_RE.search(body):
        return True
    return body.endswith(_ASK_TAIL)


# ---------- 家具：一直挂在屏幕上的东西，不是台词 ----------

FURNITURE_MIN_OBSERVATIONS = 3   # 观察还不够多的时候不下结论（免得把第一眼的台词误杀）
FURNITURE_MIN_HITS = 3           # 至少见过这么多次
FURNITURE_RATIO = 0.5            # 而且一半以上的观察里都在 → 那就是块招牌，不是话


@dataclass
class Pair:
    """一个接话样本：上一句 ask，下一句 reply。"""

    ask: str
    reply: str
    hits: int = 1
    qa: bool = False           # 是不是「问 → 答」
    at: float = 0.0

    @property
    def row(self) -> List[str]:
        """写进语料 dialogue 的样子（`chat_style.json` 里一段就是一行数组）。"""
        return [self.ask, self.reply]

    @property
    def key(self) -> Tuple[str, str]:
        """这一对的身份：只认字不认标点（见 _key），同一句话的两种 OCR 写法算同一对。"""
        return (_key(self.ask), _key(self.reply))


class CorpusLearner:
    """边看边学的那台机器：吃 OCR 出来的行，把"别人怎么接话"攒成语料。

    挂在 worker 上，每看一眼画面喂一次 `observe()`；攒够了的接话对由 `apply()`
    写进 `data/chat_style.json` 并让 humanstyle 重读（不用重启就生效）。

    记录永远只写 `data/learn.json`——语料是"再往前一步"的事，默认关着
    （config 里 `learn.promote`），得你点头它才敢自己往嘴里塞东西。
    """

    VERSION = VERSION
    SAVE_INTERVAL = 30.0      # 这么久了才落一次盘（它是挂着跑几天的进程，不能每眼都写）

    def __init__(self, cfg):
        self.cfg = cfg
        self.learn_cfg = getattr(cfg, "learn", None)
        self.path = self._resolve(getattr(self.learn_cfg, "path", "data/learn.json"), cfg)
        self.corpus_path = self._resolve(
            getattr(self.learn_cfg, "corpus", "data/chat_style.json"), cfg
        )
        self.pool: Dict[str, int] = {}            # 台词池：**键**（见 _key）-> 见过几次
        self.texts: Dict[str, str] = {}           # 键 -> 那句原话（最近一次读到的写法，给人看的）
        self.appearances: Dict[str, int] = {}     # 键 -> 出现在多少次观察里（判家具用）
        self.pairs: List[Pair] = []               # 攒着的接话对
        self.promoted: List[List[str]] = []       # 已经写进语料的（别再写一遍）
        self.observations = 0
        self._last = ""                            # 上一句台词
        self._last_at = 0.0
        self._last_speech: Tuple[str, ...] = ()    # 上一次观察到的台词（用来认"新出现的"）
        self._since_promote = 0
        self._last_digest = 0.0                    # 上一次消化（升格）是什么时候
        self._last_save = 0.0
        self._dirty = False
        self.load()

    @staticmethod
    def _resolve(raw: str, cfg) -> Path:
        """相对路径按**配置文件所在目录**算（跟 memory.path / taste.path 一个规矩）。"""
        path = Path(str(raw or ""))
        if not path.is_absolute():
            path = Path(cfg.config_path()).parent / path
        return path

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.learn_cfg, "enabled", True))

    def _opt(self, name: str, default):
        return getattr(self.learn_cfg, name, default) if self.learn_cfg is not None else default

    def digest_on(self) -> bool:
        """允不允许自己消化（往语料里升格）。

        `promote`（老写法，按观察次数触发）和 `auto_promote`（新写法，默认开）
        有一个开着就算开——留老的那个是为了不打断已经写好的配置。
        """
        return bool(self._opt("promote", False)) or bool(self._opt("auto_promote", True))

    # ---------- 读写 ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[learn] 读取 {self.path} 失败：{exc}")
            return
        if not isinstance(data, dict):
            return
        self.observations = int(data.get("observations") or 0)
        for word, count in (data.get("pool") or {}).items():
            self.pool[str(word)] = int(count or 0)
        for word, text in (data.get("texts") or {}).items():
            self.texts[str(word)] = str(text)
        for word in list(self.pool) + list(data.get("appearances") or {}):
            # 老档案（v1）里的键是原话，没存过 texts：拿键自己当那句话，别让报告里空着
            self.texts.setdefault(str(word), str(word))
        for word, count in (data.get("appearances") or {}).items():
            self.appearances[str(word)] = int(count or 0)
        for item in data.get("pairs") or []:
            if not isinstance(item, dict):
                continue
            ask, reply = str(item.get("ask") or ""), str(item.get("reply") or "")
            if not ask or not reply:
                continue
            self.pairs.append(
                Pair(
                    ask=ask,
                    reply=reply,
                    hits=max(1, int(item.get("hits") or 1)),
                    qa=bool(item.get("qa")),
                    at=float(item.get("at") or 0.0),
                )
            )
        for row in data.get("promoted") or []:
            if isinstance(row, list) and len(row) >= 2:
                self.promoted.append([str(row[0]), str(row[1])])

    def save(self, force: bool = False) -> bool:
        if not self.enabled:
            return False
        now = time.monotonic()
        if not force and (not self._dirty or now - self._last_save < self.SAVE_INTERVAL):
            return False
        payload = {
            "version": self.VERSION,
            "observations": self.observations,
            "pool": self.pool,
            "texts": self.texts,
            "appearances": self.appearances,
            "pairs": [asdict(pair) for pair in self.pairs],
            "promoted": self.promoted,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            print(f"[learn] 写 {self.path} 失败：{exc}")
            return False
        self._dirty = False
        self._last_save = now
        return True

    def reset(self) -> None:
        """把学到的东西全清掉（**语料文件不动**，只是不再记得自己学过什么）。"""
        self.pool.clear()
        self.texts.clear()
        self.appearances.clear()
        self.pairs.clear()
        self.promoted.clear()
        self.observations = 0
        self._last = ""
        self._last_at = 0.0
        self._last_speech = ()
        self._dirty = True
        self.save(force=True)

    # ---------- 看一眼 ----------

    def observe(self, lines: Sequence[str], at: Optional[float] = None) -> None:
        """把这一眼画面上读到的行喂进来：筛台词 → 剔家具 → 配相邻对 → 攒够了升格。

        频繁调用没关系：内容跟上次一模一样（OCR 命中了缓存 / 画面没动）就直接返回，
        不算一次观察，也不会把同一句台词重复计数——不然"每帧都在的标题"会更快被
        误当成台词，而真正的台词反而被算成高频。

        注意"一模一样"是**按 _key 算的**（只认字）：同一句字幕被读成两种标点写法，
        仍然算同一句，不然一条水印就能把观察数刷爆、还自己跟自己配成对。
        """
        if not self.enabled:
            return
        speech = self._speech_lines(lines)
        keys = tuple(_key(line) for line in speech)
        if keys == self._last_speech:
            return
        stamp = float(at if at is not None else time.time())
        self.observations += 1

        for line in speech:
            key = _key(line)
            self.appearances[key] = self.appearances.get(key, 0) + 1
            self.texts.setdefault(key, line)   # 显示用**第一次**读到的写法（同一句别一会儿一个标点）
        self._drop_furniture()

        fresh = [
            line
            for line in speech
            if _key(line) not in self._last_speech and not self._is_furniture(_key(line))
        ]
        self._last_speech = keys

        if fresh:
            # 一次观察里冒出好几句（两行字幕、或者上一句的残影）：字幕是一句一句过去的，
            # 拿最长的那句当"这一句"，碎片丢掉——猜错顺序比少学一句更糟。
            utter = max(fresh, key=len)
            self._remember_line(utter)
            gap = float(self._opt("pair_gap_sec", 20.0))
            # 比的是键：同一句的另一种标点写法不算"接话"，别凑出自己接自己的对
            if self._last and _key(self._last) != _key(utter) and (stamp - self._last_at) <= gap:
                self._add_pair(self._last, utter, at=stamp)
            self._last, self._last_at = utter, stamp

        self._dirty = True
        self._since_promote += 1
        if self._since_promote >= max(1, int(self._opt("promote_every", 40))):
            self._since_promote = 0
            if self.digest_on():
                self.apply()
        else:
            # 观察次数没攒够也可以消化：按时间看一眼（见 maybe_digest）
            self.maybe_digest()
        self.save()

    def _speech_lines(self, lines: Sequence[str]) -> List[str]:
        """这一眼读到的行里，哪几行像人在说话（同一句的几种标点写法只算一句）。"""
        banned = humanstyle.banned()
        out: List[str] = []
        seen: Set[str] = set()
        for raw in lines or ():
            body = _clip(raw)
            if not body:
                continue
            key = _key(body)
            if not key or key in seen:      # 这帧里两种写法都读到了：还是同一句
                continue
            if looks_like_speech(
                body,
                int(self._opt("min_chars", 4)),
                int(self._opt("max_chars", 40)),
                banned,
            ):
                seen.add(key)
                out.append(body)
        return out

    # ---------- 家具：一直挂在那儿的不是台词 ----------

    def _is_furniture(self, key: str) -> bool:
        """这个键是不是"招牌"（标题 / 台标 / 按钮）——一直挂在屏幕上，不是有人在说它。

        判据只有一条：出现的**比例**。台词说一次就过去了（四五秒换一句），
        标题和 UI 是每一帧都在的，看几眼之后比例就压过台词一大截。
        """
        if self.observations < FURNITURE_MIN_OBSERVATIONS:
            return False
        seen = self.appearances.get(key, 0)
        if seen < FURNITURE_MIN_HITS:
            return False
        return seen / float(self.observations) >= FURNITURE_RATIO

    def _drop_furniture(self) -> None:
        """家具一旦认出来，就把之前误收的台词和接话对一起撤掉。

        为什么要撤：认出来之前它在池子里待过几眼，可能已经跟真台词配成对写进语料了。
        """
        gone = [key for key in list(self.appearances) if self._is_furniture(key)]
        if not gone:
            return
        for key in gone:
            self.pool.pop(key, None)
            self.texts.pop(key, None)
        drop = set(gone)
        kept = [
            pair
            for pair in self.pairs
            if _key(pair.ask) not in drop and _key(pair.reply) not in drop
        ]
        if len(kept) != len(self.pairs):
            self.pairs = kept
            self._dirty = True

    # ---------- 台词池 / 接话对 ----------

    def _remember_line(self, line: str) -> None:
        key = _key(line)
        first = key not in self.pool
        if first:
            self.texts[key] = line
        self.pool[key] = self.pool.get(key, 0) + 1
        limit = max(1, int(self._opt("max_lines", 400)))
        if len(self.pool) > limit:      # 池子满了：丢见得最少的那批
            for word, _ in sorted(self.pool.items(), key=lambda kv: kv[1])[: len(self.pool) - limit]:
                self.pool.pop(word, None)
                self.texts.pop(word, None)
        if first:
            print(f"[learn] 记下第 {len(self.pool)} 句台词：{_clip(line, 28)}")

    def _add_pair(self, ask: str, reply: str, at: float = 0.0) -> Optional[Pair]:
        ask_key, reply_key = _key(ask), _key(reply)
        for pair in self.pairs:            # 见过就计一次数（同一对反复出现 = 这才是常说的接法）
            if _key(pair.ask) == ask_key and _key(pair.reply) == reply_key:
                pair.hits += 1             # 身份只看字，所以"换个标点的同一对"也数得进来
                pair.at = at or pair.at    # 写法沿用第一次记下的，写进语料不会忽变
                return pair
        pair = Pair(ask=ask, reply=reply, qa=is_question(ask), at=at or time.time())
        self.pairs.append(pair)
        limit = max(1, int(self._opt("max_candidates", 200)))
        if len(self.pairs) > limit:
            del self.pairs[:-limit]
        kind = "问→答" if pair.qa else "前后"
        print(f"[learn] 攒到一对接话（{kind}）：{_clip(ask, 18)} → {_clip(reply, 18)}")
        return pair

    def ready_pairs(self) -> List[Pair]:
        """够格进语料的那几对：问→答 见过 promote_min_hits 次就算；不是问答的更严一点。

        为什么问答宽、其它严：字幕里"上一句在问、下一句在答"基本可以确定是两个人在
        对话（解说的视频里没人提问），这种样本可信；普通相邻两句可能都是同一个人在说，
        只学到节奏，所以得反复出现才敢收。
        """
        need = max(1, int(self._opt("promote_min_hits", 2)))
        return [pair for pair in self.pairs if pair.hits >= (need if pair.qa else need + 1)]

    def auto_pairs(self) -> List[Pair]:
        """**自己动手消化**时只收这一批：问→答，而且过闸门。

        为什么要跟"够格"分开：`ready_pairs` 是"可以进语料了"，`auto_pairs` 是"放心
        让它**自己**进语料"。区别就在非问答的相邻两句——它们可能都是同一个人在念屏幕，
        甚至是一段被读歪的界面文字（现场真混进来过 `杰可在 [ 设首 ] 里关闭或打开局内
        肯包中 3 囗色展示`）。问答对可信得多：解说的视频里没人提问，能配上"问→答"
        的，基本就是真有人在聊。剩下那些非问答的照旧攒着，想收就 `python -m pet.corpus apply`。
        """
        return [pair for pair in self.ready_pairs() if pair.qa]

    def maybe_digest(self, now: Optional[float] = None) -> int:
        """到点了就消化一次（跟观察次数无关）：画面不动的时候，旧的照样在被吸收。

        为什么要按时间而不能只看 observe：observe 只在"读到的字变了"那一刻才涨——
        画面停在直播间、字幕半天不动的时候，攒下来的那批接话对永远等不到
        promote_every 那次触发，档案就一直躺在硬盘上（现场：2117 次观察、0 段进语料）。
        """
        if not self.enabled or not self.digest_on():
            return 0
        if not self.auto_pairs():
            return 0
        now = float(now if now is not None else time.time())
        gap = max(30.0, float(self._opt("digest_interval_sec", 300.0)))
        if self._last_digest and (now - self._last_digest) < gap:
            return 0
        return self.apply(pairs=self.auto_pairs())

    def retire_promoted(self) -> int:
        """已经进语料的接话对，从"还在攒的"名单里请出去。

        为什么必须请：`max_candidates` 管的是**还在攒的**，进了语料还占着名额的话，
        池子会一直满着、新读到的接话对进不来（满了丢最旧的）——只攒不消化是"攒满就停"，
        消化了却不退休一样是"攒满就停"。
        """
        keys = self._promoted_keys()
        keep = [pair for pair in self.pairs if pair.key not in keys]
        gone = len(self.pairs) - len(keep)
        if gone:
            self.pairs = keep
            self._dirty = True
        return gone

    @staticmethod
    def _row_key(row: Sequence[str]) -> Tuple[str, str]:
        """一段对话的身份：按 _key（只认字）算，跟 _promoted_keys 一个规矩。"""
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            return ("", "")
        return (_key(str(row[0])), _key(str(row[1])))

    def _promoted_keys(self) -> Set[Tuple[str, str]]:
        """已经写进语料的那几段的身份（档案里存的是原话，比的时候按 _key 算）。"""
        return {self._row_key(row) for row in self.promoted}

    # ---------- 升格：把够格的接话对写进语料 ----------

    def apply(self, force: bool = False, pairs: Optional[Sequence[Pair]] = None) -> int:
        """够格的接话对写进 `data/chat_style.json`，然后让 humanstyle 重读。返回写了几段。

        `pairs` 是**这一轮要收哪些**：留空 = 所有够格的（手动 `python -m pet.corpus apply`
        就是这个口径，你点过头了）；自动消化会传 `auto_pairs()`（只收问→答那一批，见那儿）。

        只动 `dialogue` 这一个键：
        * `dialogue` 就是"一段真人对话"，我们攒的相邻两句正好是那个形状；
        * `patterns`（聊天的做法：只给反应、改口、抬杠……）得靠人总结，机器从两句
          字幕里总结不出来，硬写进去只会是车轱辘话；
        * `openers/fillers/enders/banned` 同理，不是这两句能定的。
        写之前先备份成 `.bak`，写完 humanstyle.reload()——**不重启就生效**。
        """
        if not self.enabled:
            return 0
        allowed = self.ready_pairs() if pairs is None else list(pairs)
        todo = [pair for pair in allowed if pair.key not in self._promoted_keys()]
        if not todo:
            if force:
                print(
                    f"[learn] 还没有够格的接话对"
                    f"（攒了 {len(self.pairs)} 对，够格 {len(self.ready_pairs())} 对，"
                    f"其中放心自己收的问→答 {len(self.auto_pairs())} 对）"
                )
            return 0

        data = self._read_corpus()
        if data is None:      # 语料读不出来（写坏了 / 权限）：宁可不动，也别覆盖人家的东西
            return 0
        dialogue = data.get("dialogue")
        if not isinstance(dialogue, list):
            dialogue = []
        rows = [row for row in dialogue if isinstance(row, list) and row]
        limit = max(1, int(self._opt("max_dialogue", 60)))

        added = 0
        keys = self._promoted_keys()
        for pair in todo:
            if self._already_in(rows, pair.row):
                self.promoted.append(pair.row)   # 语料里本来就有：记一笔，别再排队
                continue
            if len(rows) >= limit and not self._make_room(rows, limit, keys):
                break
            self.promoted.append(pair.row)
            rows.append(pair.row)
            added += 1
            print(f"[learn] 升格进语料：{_clip(pair.ask, 20)} → {_clip(pair.reply, 20)}")
        self._last_digest = time.time()     # 这一次消化过了（见 maybe_digest）
        if not added:
            self._dirty = True
            self.save(force=True)
            return 0

        data["dialogue"] = rows
        try:
            self.corpus_path.parent.mkdir(parents=True, exist_ok=True)
            if self.corpus_path.exists():
                backup = self.corpus_path.with_name(self.corpus_path.name + ".bak")
                shutil.copy2(self.corpus_path, backup)
            self.corpus_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:
            print(f"[learn] 写 {self.corpus_path.name} 失败：{exc}")
            return 0

        humanstyle.reload()   # 语料是启动时读一次的：改完必须让它重读，不然要重启才生效
        self._dirty = True
        self.retire_promoted()    # 进语料的对从"还在攒的"里请出去，池子才接得进新的
        self.save(force=True)
        print(f"[learn] 语料多了 {added} 段（现在一共 {len(rows)} 段），不用重启就生效")
        return added

    def _make_room(self, rows: List[List[str]], limit: int, ours: Set[Tuple[str, str]]) -> bool:
        """语料塞满了：把自己升格进去的**最旧一段**请出去，给新的一段腾位置。

        只动我们自己写进去的那些（身份在 `promoted` 里）：用户手写的对话永远不碰——
        `max_dialogue` 这个上限是给"自己学来的东西"定的，不能因为它把人家的语料挤掉。
        返回 False 表示满了并且没有自己人可退（那这一轮就到此为止）。
        """
        mine = [row for row in rows if self._row_key(row) in ours]
        if not mine:
            return False
        oldest = mine[0]
        key = self._row_key(oldest)
        rows.remove(oldest)
        for index, row in enumerate(self.promoted):
            if self._row_key(row) == key:
                del self.promoted[index]
                break
        print(f"[learn] 语料满了（{limit} 段）：把最旧的一段自己学的请出去，腾个位置")
        return True

    @staticmethod
    def _already_in(rows: Sequence[Sequence[str]], row: Sequence[str]) -> bool:
        """语料里是不是已经有这么一段了（换了几个字也算）——别再塞一遍。

        用 humanstyle 那把尺子：跟已有对话的**第一句**像到 0.7 以上就当成同一段。
        语料是喂给模型当示范的，重样会把它往"一句话反复说"上带。
        """
        ask, reply = row[0], row[1]
        for exist in rows:
            other = str(exist[0]).strip()
            if other == ask and len(exist) > 1 and str(exist[1]).strip() == reply:
                return True
            if humanstyle.similarity(other, ask) >= 0.7:
                return True
        return False

    def _read_corpus(self) -> Optional[Dict[str, object]]:
        """读现在的语料文件；读不出来返回 None（调用方就什么都别写）。"""
        if not self.corpus_path.exists():
            return {}
        try:
            loaded = json.loads(self.corpus_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[learn] 读 {self.corpus_path.name} 失败，这次不动它：{exc}")
            return None
        return loaded if isinstance(loaded, dict) else None

    # ---------- 给人看的 ----------

    def digest_line(self) -> str:
        """一行"消化进度"：给托盘 / 日志看的后台口径（见 app.study_detail_text）。

        跟 `report()` 的区别：这个是**一句话**，塞得进日志行；report 是给人翻着看的报告。
        """
        ready = self.ready_pairs()
        auto = self.auto_pairs()
        pending = [pair for pair in auto if pair.key not in self._promoted_keys()]
        return (
            f"边看边学：接话对 {len(self.pairs)} 个（够格 {len(ready)}，其中放心自己收的问→答"
            f" {len(auto)}，还没消化 {len(pending)}） / 已进语料 {len(self.promoted)} 段"
        )

    def report(self) -> str:
        """一份报告：攒了什么、够格了什么（`python -m pet.corpus` 打的就是它）。"""
        need = max(1, int(self._opt("promote_min_hits", 2)))
        ready = self.ready_pairs()
        auto = self.auto_pairs()
        pending = [pair for pair in auto if pair.key not in self._promoted_keys()]
        lines = [
            f"观察 {self.observations} 次｜台词 {len(self.pool)} 句｜接话对 {len(self.pairs)} 个"
            f"（够格 {len(ready)} 个，已进语料 {len(self.promoted)} 段）",
            f"档案：{self.path}",
            f"语料：{self.corpus_path}"
            f"（自己消化：{'开' if self.digest_on() else '关'}，"
            f"够格线：问→答 ×{need} / 前后 ×{need + 1}）",
            f"还没消化的：自己会收的问→答 {len(pending)} 个 · 等你点头的其它 {len(ready) - len(auto)} 个"
            f"（`python -m pet.corpus apply` 收它们；"
            f"最多 {int(self._opt('max_lines', 400))} 句 / {int(self._opt('max_candidates', 200))} 对，"
            f"到顶了先消化再腾位置）",
        ]
        ready = self.ready_pairs()
        if ready:
            lines.append("")
            lines.append("够格进语料的：")
            for pair in ready[:20]:
                flag = "问→答" if pair.qa else "前后"
                lines.append(f"  [{flag} ×{pair.hits}] {pair.ask}  →  {pair.reply}")
        if self.pairs:
            lines.append("")
            lines.append(f"还在攒的（前 20 / 共 {len(self.pairs)} 个）：")
            for pair in self.pairs[:20]:
                flag = "问→答" if pair.qa else "前后"
                lines.append(f"  [{flag} ×{pair.hits}] {pair.ask}  →  {pair.reply}")
        if self.pool:
            lines.append("")
            lines.append(f"台词池（前 20 / 共 {len(self.pool)} 句）：")
            for key, count in sorted(self.pool.items(), key=lambda kv: -kv[1])[:20]:
                lines.append(f"  [×{count}] {self.texts.get(key, key)}")
        return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """不启动挂件，单独看 / 管这份学习档案：

        python -m pet.corpus             看报告（默认）
        python -m pet.corpus apply       把够格的接话对写进 data/chat_style.json
        python -m pet.corpus reset       清空学习档案（**语料不动**）

    挂件在跑的时候也能用：档案是同一份文件，apply 完它下一句话就用上了
    （拼提示词每次都要过 humanstyle，reload 一下缓存就通了，见 apply）。
    """
    from .config import Config

    args = [str(item) for item in (sys.argv[1:] if argv is None else argv) if str(item).strip()]
    action = (args[0] if args else "report").lower()
    learner = CorpusLearner(Config.load())

    if action in ("report", "r", "show"):
        print(learner.report())
        return 0
    if action == "apply":
        print(f"写进语料 {learner.apply(force=True)} 段。")
        return 0
    if action in ("reset", "clear"):
        learner.reset()
        print(f"学习档案已清空：{learner.path}")
        return 0
    print("用法：python -m pet.corpus [report|apply|reset]")
    print("  report  看攒到了什么（默认）")
    print("  apply   把够格的接话对写进 data/chat_style.json（写前自动备份 .bak）")
    print("  reset   清空学习档案（语料文件不动）")
    return 2


if __name__ == "__main__":        # pragma: no cover - 手动跑的小工具
    raise SystemExit(main())
