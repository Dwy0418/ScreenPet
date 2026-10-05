"""看片笔记：把「这一眼看到的是什么」攒成一份能查的看片记录。

为什么要有它
    5W1H（scene.py）只负责"这一眼看准"，worker._last_scene 只用来防重复，
    真到用户开口问"刚才那男的为啥不打怪"的时候，光有上一眼是不够的。
    所以这里多做两件事：

    1. 每换一支视频（画面大跳变）就把这一眼**完整读一遍**，攒成一份 WatchNote
       （主题 / 内容 / 看点 / 人物 / 地点 / 关键词）——就是"视频一刷出来它就看完"；
    2. 之后每一眼的场景、弹幕字幕、它自己说过的吐槽，按时间排进一条时间线；
       用户提问时，拿问题里的词去时间线和笔记里做本地检索（字符二元组重合度），
       把最相关的几条挑出来拼进提示词——不花接口钱，也不写进 memory.json。

一句话：**看的时候它自己记，聊的时候它翻笔记**。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import humanstyle
from . import taste as taste_mod

# ---------- 看片笔记的解析 ----------

# 模型按提示词给的键名，都认
_NOTE_KEY_FIELDS: Dict[str, str] = {
    "主题": "title",
    "标题": "title",
    "这支视频": "title",
    "内容": "what",
    "讲什么": "what",
    "讲了什么": "what",
    "梗概": "what",
    "剧情": "what",
    "看点": "point",
    "槽点": "point",
    "笑点": "point",
    "亮点": "point",
    "人物": "who",
    "主角": "who",
    "角色": "who",
    "地点": "where",
    "场景": "where",
    "关键词/标签": "keywords",
    "关键词": "keywords",
    "标签": "keywords",
    "类型": "genre",
    "类别": "genre",
    "分类": "genre",
    "玩法": "playstyle",
    "玩什么": "playstyle",
    "怎么玩": "playstyle",
    "画风": "art",
    "风格": "art",
    "画面风格": "art",
    "玩家群体": "audience",
    "受众": "audience",
    "观众群体": "audience",
    "互动": "actions",
    "互动信号": "actions",
    "反应": "actions",
}

# 「主题：xxx」/「- 主题: xxx」/「【主题】xxx」都认
_KV_RE = re.compile(r"^[\s\-*·•\d.、]*[【\[]?\s*([^：:\[\]【】\s]{1,8})\s*[】\]]?\s*[:：]\s*(.*)$")

# 每一项最多留这么多字，免得模型写小作文把提示词撑爆
MAX_FIELD_CHARS = 90
MAX_KEYWORDS = 8
MAX_KEYWORD_CHARS = 12


def _clip(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    value = value.strip("「」『』\"'“”《》[]【】()（）。.,，、 *`~")
    if limit and len(value) > limit:
        value = value[: max(1, limit - 1)].rstrip() + "…"
    return value


#: 提示词里那几句话的痕迹（"**是游戏**才写"、"看不清一律写「否」"）：
#: 模型把整行说明抄回来时，值长得像内容，其实是说明书（见 _is_content）。
_PROMPT_FRAGMENTS = ("才写", "一律写", "写「未知」", "写「其他」", "看不清", "别猜", "每一项都短")
#: 这道闸只管**新增的那三个字段**：它们提示词里带着"才写 / 写「未知」"这类说明，
#: 模型最容易连着抄；老字段（内容 / 看点…）里出现"看不清"是正常说话，别误杀。
PROMPT_FRAGMENT_FIELDS = ("playstyle", "art", "audience")


def _is_content(value: str, field: str = "") -> bool:
    """这一项**能不能当笔记的内容**。

    模型偶尔会把提示词骨架原样抄回来——现场见过的是看片笔记的标题变成：

        键名：类型 / 答案：影视综艺 / 键名：主题

    这种字一个都不是"画面里有什么"，可它偏偏长得像内容：留在笔记里会被当成
    「他刚在看的那一支」的话头送去学、也会混进时间线，回头又端到用户面前。
    判据复用 `humanstyle.looks_like_note_skeleton`（字段形状），跟气泡那道 meta 闸同源；
    另外对**玩法 / 画风 / 玩家群体**这三个字段再挡一道"提示词说明书的痕迹"
    （`_PROMPT_FRAGMENTS`）——模型把「玩法：**是游戏**才写」这种**整行说明**抄回来时，
    值本身不像骨架，但也不是内容。`field` 留空 = 只走骨架闸。
    """
    text = (value or "").strip()
    if not text:
        return False
    if field in PROMPT_FRAGMENT_FIELDS and any(mark in text for mark in _PROMPT_FRAGMENTS):
        return False
    return not humanstyle.looks_like_note_skeleton(text)


def _split_keywords(value: str) -> List[str]:
    """「我的世界、村民、不打怪」→ ["我的世界", "村民", "不打怪"]。"""
    parts = re.split(r"[、,，/|｜;；\s]+", (value or "").strip())
    out: List[str] = []
    for part in parts:
        word = _clip(part, MAX_KEYWORD_CHARS)
        if word and word not in out:
            out.append(word)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


@dataclass
class WatchNote:
    """一支视频的"看片笔记"（换视频时生成一次）。"""

    at: float = 0.0
    title: str = ""
    what: str = ""
    point: str = ""
    who: str = ""
    where: str = ""
    genre: str = ""                                          # 内容大类（taste.GENRES 之一）
    # 这三项是给"看懂他的喜好"用的（游戏尤其管用：玩法 / 画风 / 玩家群体）：
    # 它们跟类型一起写进口味档案，之后它接话就知道该往哪个方向聊（见 taste.profile_block）
    playstyle: str = ""                                      # 玩法：射击搜刮 / 抽卡养成 / 大乱斗…
    art: str = ""                                            # 画风：写实军武 / 像素 / 二次元…
    audience: str = ""                                       # 玩家群体：硬核老玩家 / 学生党 / 休闲向…
    keywords: List[str] = field(default_factory=list)
    actions: Dict[str, bool] = field(default_factory=dict)   # 点赞/收藏/关注/评论（画面按钮状态）
    raw: str = ""          # 模型原样输出（解析不出键值时兜底用）

    def is_empty(self) -> bool:
        return not (
            self.title or self.what or self.point or self.who or self.where
            or self.playstyle or self.art or self.audience or self.keywords
        )

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at or time.time()))

    def line(self, limit: int = 120) -> str:
        """一行版本：给"防重复/连贯性"这种便宜场合用。"""
        parts: List[str] = []
        if self.genre:
            parts.append(f"类型={self.genre}")
        if self.title:
            parts.append(f"主题={self.title}")
        if self.what:
            parts.append(f"内容={self.what}")
        if self.playstyle:
            parts.append(f"玩法={self.playstyle}")
        if self.point:
            parts.append(f"看点={self.point}")
        return _clip("｜".join(parts), limit)

    def block(self) -> str:
        """多行版本：塞进提示词，让它答得上话。"""
        rows: List[str] = [f"（{self.clock} 这一眼读完的）"]
        for label, value in (
            ("类型", self.genre),
            ("主题", self.title),
            ("内容", self.what),
            ("玩法", self.playstyle),
            ("画风", self.art),
            ("玩家群体", self.audience),
            ("看点", self.point),
            ("人物", self.who),
            ("地点", self.where),
        ):
            if value:
                rows.append(f"{label}：{value}")
        if self.keywords:
            rows.append("关键词：" + "、".join(self.keywords))
        marks = [taste_mod.ACTION_LABELS[key] for key, hit in (self.actions or {}).items() if hit]
        if marks:
            rows.append("他的反应：" + "、".join(marks))
        if len(rows) == 1 and self.raw:
            rows.append(_clip(self.raw, 220))
        return "\n".join(rows)


def parse_note(text: str, at: Optional[float] = None) -> WatchNote:
    """把模型的多行「看片笔记」解析成 WatchNote；解析不出来就把原文塞进 raw。"""
    note = WatchNote(at=float(at if at is not None else time.time()))
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    leftovers: List[str] = []
    fields: Dict[str, str] = {}

    for line in body.split("\n"):
        item = line.strip()
        if not item:
            continue
        match = _KV_RE.match(item)
        name = match.group(1).strip() if match else ""
        field_name: Optional[str] = _NOTE_KEY_FIELDS.get(name)
        if match and field_name:
            value = match.group(2).strip()
            if field_name == "genre":
                if value:
                    note.genre = taste_mod.normalize_genre(value)
                continue
            if field_name == "actions":
                for action, hit in taste_mod.parse_actions(value).items():
                    note.actions[action] = bool(hit)
                continue
            if field_name == "keywords":
                for word in _split_keywords(value):
                    if word not in note.keywords:
                        note.keywords.append(word)
                if len(note.keywords) > MAX_KEYWORDS:
                    del note.keywords[MAX_KEYWORDS:]
                continue
            # 同一个键写了两遍，只认第一遍；抄回来的提示词骨架不算内容（见 _is_content）
            if value and not fields.get(field_name) and _is_content(value, field_name):
                fields[field_name] = _clip(value, MAX_FIELD_CHARS)
            continue
        if not _is_content(item):
            continue        # 模型把提示词骨架抄回来了：不记进笔记，也不留进 raw
        leftovers.append(item)

    note.title = fields.get("title", "")
    note.what = fields.get("what", "")
    note.point = fields.get("point", "")
    note.who = fields.get("who", "")
    note.where = fields.get("where", "")
    note.playstyle = fields.get("playstyle", "")
    note.art = fields.get("art", "")
    note.audience = fields.get("audience", "")
    if not note.genre:   # 模型没给类型：本地按标题/内容/关键词猜一个，保证统计口径不断
        note.genre = taste_mod.normalize_genre(
            " ".join([note.title, note.what, note.point] + list(note.keywords))
        )
    if leftovers:
        note.raw = _clip(" / ".join(leftovers), 260)
    if note.is_empty() and note.raw:
        note.title = _clip(note.raw, MAX_FIELD_CHARS)
    return note


# ---------- 时间线 ----------


@dataclass
class WatchItem:
    """时间线里的一条：某一秒它看到了什么、说了什么。"""

    at: float
    scene: str = ""
    ocr: str = ""
    said: str = ""

    @property
    def clock(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))

    def search_text(self) -> str:
        return " ".join(part for part in (self.scene, self.ocr, self.said) if part)

    def line(self, ocr_limit: int = 60) -> str:
        parts: List[str] = [self.clock]
        if self.scene:
            parts.append(self.scene)
        if self.said:
            parts.append(f"我当时说「{_clip(self.said, 26)}」")
        if self.ocr:
            parts.append(f"画面文字：{_clip(self.ocr, ocr_limit)}")
        return " ｜ ".join(parts)


# 检索用的字符二元组（中文没空格，二元组比按词切稳，也不需要额外依赖）
_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def _bigrams(text: str) -> set:
    clean = _CLEAN_RE.sub("", (text or "").lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[index : index + 2] for index in range(len(clean) - 1)}


def _overlap(query: set, text: str) -> float:
    if not query:
        return 0.0
    hit = len(query & _bigrams(text))
    return hit / float(len(query)) if hit else 0.0


class WatchLog:
    """当前这支视频的笔记 + 一条时间线（全是本地字符串，不联网、不写记忆）。"""

    def __init__(self, timeline_size: int = 12, history_size: int = 5):
        self.timeline_size = max(1, int(timeline_size))
        self.history_size = max(0, int(history_size))
        self.current: Optional[WatchNote] = None
        self.timeline: List[WatchItem] = []
        self.history: List[WatchNote] = []      # 前面几支视频的笔记（只留几支）
        self.started_at: float = 0.0

    # ---------- 写入 ----------

    def reset(self) -> None:
        self.current = None
        self.timeline = []
        self.history = []
        self.started_at = 0.0

    def start_video(self, note: Optional[WatchNote], at: Optional[float] = None) -> Optional[WatchNote]:
        """换了一支视频：旧笔记归档，新笔记就位，时间线重新开始。"""
        if note is None or note.is_empty():
            return None
        now = float(at if at is not None else time.time())
        if note.at <= 0:
            note.at = now
        if self.current is not None and not self.current.is_empty() and self.history_size:
            self.history.append(self.current)
            if len(self.history) > self.history_size:
                del self.history[:-self.history_size]
        self.current = note
        self.timeline = []
        self.started_at = now
        return note

    def refresh(self, note: WatchNote) -> WatchNote:
        """同一支视频重读一遍：把新读到的内容合并进当前笔记，不把旧的挤进历史。"""
        now = time.time()
        if note is None:
            return note
        if note.at <= 0:
            note.at = now
        if self.current is None or self.current.is_empty():
            return self.start_video(note)
        current = self.current
        current.at = now
        for name in ("title", "what", "point", "who", "where", "playstyle", "art", "audience"):
            value = getattr(note, name, "")
            if value:
                setattr(current, name, value)
        genre = taste_mod.normalize_genre(getattr(note, "genre", ""))
        if genre != "其他" or not current.genre:
            current.genre = genre
        for action, hit in (getattr(note, "actions", None) or {}).items():
            if hit or action not in current.actions:
                current.actions[action] = bool(hit)
        if note.keywords:
            words = list(current.keywords)
            for word in note.keywords:
                if word not in words:
                    words.append(word)
            current.keywords = words[:MAX_KEYWORDS]
        if note.raw:
            current.raw = note.raw
        return current

    def remember(self, scene: str = "", ocr: str = "", said: str = "", at: Optional[float] = None) -> None:
        """记一眼：这一眼的 5W1H / 画面文字 / 它当时说的话。三条全空就不记。"""
        scene = _clip(scene, 120)
        ocr = _clip(ocr, 200)
        said = _clip(said, 80)
        if not (scene or ocr or said):
            return
        self.timeline.append(
            WatchItem(at=float(at if at is not None else time.time()), scene=scene, ocr=ocr, said=said)
        )
        if len(self.timeline) > self.timeline_size:
            del self.timeline[:-self.timeline_size]

    # ---------- 读取 ----------

    def has_content(self) -> bool:
        if self.current is not None and not self.current.is_empty():
            return True
        return bool(self.timeline)

    def current_line(self, limit: int = 120) -> str:
        if self.current is None:
            return ""
        return self.current.line(limit)

    def _pick(self, question: str, limit: int) -> List[WatchItem]:
        """按问题挑最相关的几条；问题是空的就挑最新几条。"""
        if not self.timeline:
            return []
        query = _bigrams(question)
        if not query:
            return self.timeline[-limit:]
        total = float(len(self.timeline))
        scored: List[Tuple[float, int, WatchItem]] = []
        for index, item in enumerate(self.timeline):
            score = _overlap(query, item.search_text())
            if score > 0:
                # 同样命中，越新的越相关（别老翻十秒前的事）
                score += 0.15 * (index / total)
            else:
                score = 0.0001 * (index + 1)
            scored.append((score, index, item))
        scored.sort(key=lambda row: row[0], reverse=True)
        return [item for _, _, item in scored[:limit]]

    def context_for(self, question: str = "", limit: int = 6) -> str:
        """拼给提示词的「看片笔记 + 相关记录」；什么都没攒到时返回空串。"""
        blocks: List[str] = []
        note = self.current
        if note is not None and not note.is_empty():
            blocks.append("【你刚看完的视频（你已经读过了，别再说「我还没看到」）】\n" + note.block())
        elif self.history:
            blocks.append("【你上一支看过的视频】\n" + self.history[-1].block())

        picked = self._pick(question, max(1, int(limit)))
        if picked:
            head = "【看片记录】（越靠后越新"
            head += "；已经按你问的挑过最相关的几条）" if question else "）"
            blocks.append(head + "\n" + "\n".join(item.line() for item in picked))
        return "\n\n".join(blocks)

    def stats(self) -> Dict[str, object]:
        note = self.current
        return {
            "has_note": bool(note is not None and not note.is_empty()),
            "title": note.title if note is not None else "",
            "timeline": len(self.timeline),
            "history": len(self.history),
        }


def format_log(note: Optional[WatchNote], log: Optional[WatchLog] = None) -> str:
    """给日志/自检用的一行摘要。"""
    if note is None or note.is_empty():
        return "（没读出来）"
    text = note.line()
    if log is not None:
        text += f"（时间线 {len(log.timeline)} 条）"
    return text or "（没读出来）"
