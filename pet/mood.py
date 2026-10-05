"""情绪标签：把模型输出拆成「[情绪] 台词」，并驱动表情、气泡配色、语音语气。

模型只需要在句子开头带一个 [开心]/[无语]/[激动]/[吐槽]/[好奇]，
万一它忘了带，这里会用关键词猜一个，保证表情状态机永远有输入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .scene import Scene

DEFAULT_MOOD = "smirk"

# mood -> (中文标签, emoji, 主题色, 语音语速增量, 关键词)
MOODS: Dict[str, Dict[str, object]] = {
    "happy": {
        "label": "开心",
        "emoji": "😄",
        "color": "#FFD166",
        "rate": 1,
        "keywords": ("哈哈", "笑死", "可爱", "喜欢", "好棒", "厉害", "舒服", "爽", "真香"),
    },
    "speechless": {
        "label": "无语",
        "emoji": "😑",
        "color": "#9AA7B4",
        "rate": -1,
        "keywords": ("无语", "离谱", "服了", "算了", "何必", "随便", "反正", "何必呢", "看不下去了"),
    },
    "excited": {
        "label": "激动",
        "emoji": "🤩",
        "color": "#FF7AD9",
        "rate": 2,
        "keywords": ("卧槽", "牛", "猛", "秒", "绝了", "精彩", "刺激", "快看", "冲", "顶"),
    },
    "curious": {
        "label": "好奇",
        "emoji": "🤔",
        "color": "#A9E34B",
        "rate": 0,
        "keywords": ("为什么", "怎么", "啥", "什么", "难道", "是不是", "该不会", "?"),
    },
    "surprised": {
        "label": "惊讶",
        "emoji": "😲",
        "color": "#FFB454",
        "rate": 1,
        "keywords": ("惊讶", "震惊", "不是吧", "真的假的", "没想到", "天呐", "吓我一跳", "啊？"),
    },
    "angry": {
        "label": "生气",
        "emoji": "😠",
        "color": "#FF6B6B",
        "rate": 1,
        "keywords": ("生气", "气死", "愤怒", "可恶", "太过分", "烦死", "讨厌"),
    },
    "sad": {
        "label": "难过",
        "emoji": "😢",
        "color": "#7FA8FF",
        "rate": -1,
        "keywords": ("难过", "伤心", "想哭", "委屈", "遗憾", "可惜", "舍不得"),
    },
    DEFAULT_MOOD: {
        "label": "吐槽",
        "emoji": "😏",
        "color": "#7CE0FF",
        "rate": 2,
        "keywords": ("这操作", "居然", "哦", "行吧", "熟练", "又", "真是", "不愧"),
    },
}

# 别名 -> 标准 mood
_ALIASES = {
    "开心": "happy", "高兴": "happy", "笑": "happy", "乐": "happy", "赞": "happy",
    "无语": "speechless", "无奈": "speechless", "冷淡": "speechless", "冷漠": "speechless",
    "激动": "excited", "兴奋": "excited",
    # 惊讶单拎出来（"震惊/吃惊"也是它）：以前并到 excited 里，跟"激动"混成一股，
    # 表情和动作都没法区分（见 pet/states.py 的 MOOD_ACTIONS：惊讶 → 被戳般惊跳）
    "惊讶": "surprised", "震惊": "surprised", "吃惊": "surprised", "意外": "surprised",
    "生气": "angry", "愤怒": "angry", "火大": "angry", "可恶": "angry",
    "难过": "sad", "伤心": "sad", "委屈": "sad", "失落": "sad",
    "吐槽": DEFAULT_MOOD, "坏笑": DEFAULT_MOOD, "得意": DEFAULT_MOOD, "嫌弃": DEFAULT_MOOD,
    "好奇": "curious", "疑惑": "curious", "疑问": "curious", "困惑": "curious",
}
for _name, _spec in MOODS.items():
    _ALIASES[_name] = _name
    _ALIASES[str(_spec["label"])] = _name

_TAG_RE = re.compile(r"^\s*[\[【(（]\s*([^\]】)）]{1,8})\s*[\]】)）]\s*")


@dataclass(frozen=True)
class Comment:
    """一句吐槽 + 它的情绪（+ 这一眼看明白的 5W1H 场景）。"""

    text: str
    mood: str = DEFAULT_MOOD
    kind: str = ""  # 主动搭话时标记由头（still_screen / long_quiet…），用户对话是 chat
    scene: Optional[Scene] = None  # 模型给的「场景」行；没有就是 None，不影响别的


def normalize(mood: str) -> str:
    key = (mood or "").strip().lower()
    return _ALIASES.get(key, DEFAULT_MOOD)


def label(mood: str) -> str:
    return str(MOODS[normalize(mood)]["label"])


def emoji(mood: str) -> str:
    return str(MOODS[normalize(mood)]["emoji"])


def color(mood: str) -> str:
    return str(MOODS[normalize(mood)]["color"])


def rate_delta(mood: str) -> int:
    return int(MOODS[normalize(mood)]["rate"])


def mood_names() -> Tuple[str, ...]:
    return tuple(MOODS.keys())


def split_mood(text: str) -> Comment:
    """把 "[无语] 这操作……" 拆成 Comment(text="这操作……", mood="speechless")。"""
    raw = (text or "").strip()
    if not raw:
        return Comment("", DEFAULT_MOOD)

    mood = ""
    match = _TAG_RE.match(raw)
    if match:
        candidate = match.group(1).strip().lower()
        if candidate in _ALIASES:
            mood = _ALIASES[candidate]
            raw = raw[match.end():].strip()
    if not mood:
        mood = infer_mood(raw)
    return Comment(raw, mood)


def infer_mood(text: str) -> str:
    """没有标签时的兜底：按关键词猜。"""
    body = text or ""
    for name, spec in MOODS.items():
        for keyword in spec["keywords"]:  # type: ignore[index]
            if keyword in body:
                return name
    return DEFAULT_MOOD
