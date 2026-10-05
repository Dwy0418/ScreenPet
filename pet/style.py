# -*- coding: utf-8 -*-
"""形象长相：让用户自己设计桌宠长什么样。

内置的矢量形象以前是写死的——颜色是 pet/sprite.py 顶上那六个常量，机身固定是那坨
史莱姆（波浪裙边 + 触角）。想换个样子只有两条路：手画 396 张帧（33 套 × 12 帧），
或者拿张照片贴上去（tools/paste_face.py，而那只做得成"你的脸 + 黄豆的身体"）。
所以这里把「长相」单独抽出来：

    预设（PRESETS） + 用户想改的那几项  ->  PetStyle  ->  pet/sprite.py 照着画

* **存在哪儿**：`config.json` 的 `ui.pet_style`。可以只写想改的那几个键，没写的从
  `preset`（默认 classic，就是原来那套「黄豆」）里取：

      "pet_style": {"preset": "peach", "eyes": "arc", "body_shape": "round"}

  图省事也可以整节只写一个预设名：`"pet_style": "mint"`。
* **认哪些键**：见 FIELDS 和各张 ENUM 表；表外的键一律忽略、不报错（手写配置打错一个
  字不该把挂件拖崩）。
* **写错了怎么办**：颜色认不出 / 枚举不在表里 / 数字超范围，一律**退回预设里的值**，
  并往 `style.problems` 里记一条（设计器会把它们摆在界面上）。
  为什么不静默兜底：用户改的那行没生效、界面上又看不出任何异样——这是最难查的一类问题
  （`--chroma` 那次就是这么查了一下午的）。
* **谁来画**：pet/sprite.py 的矢量分支。这个模块**不 import sprite**（渲染器反过来用它），
  所以这里只有数据和几个小工具，没有一行画图代码。
* 情绪那几套表情（开心 / 无语 / 激动…）不算长相，仍然走 pet/sprite.py 的 MOOD_STYLE；
  这里选的五官是**待机**时候的样子。想让喜怒哀乐都用你挑的这套：`face_locked: true`。
* assets/pet 里装了图片帧时默认还是播帧（那是另一条路）。想让这套设计上场，要么把它
  渲染成源图再交给 make_pet（tools/design_pet.py 里有这个按钮），要么把 `ui.prefer_vector`
  打开——"就算装了图片帧也用矢量形象"。

用法：
    from pet import style as style_mod
    pet_style = style_mod.PetStyle.from_config(cfg.ui)
    if pet_style.problems:
        print("\\n".join(pet_style.problems))
"""
from __future__ import annotations

import colorsys
import random
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtGui import QColor

#: 能写进 `ui.pet_style` 的键。表外的键忽略（见文件头）。
FIELDS = (
    "preset",
    "body_shape",
    "body_top",
    "body_bottom",
    "outline",
    "eye",
    "blush",
    "accent",
    "eyes",
    "mouth",
    "face_locked",
    "blush_on",
    "antenna",
    "shadow",
    "highlight",
    "body_width",
    "body_height",
)
COLOR_FIELDS = ("body_top", "body_bottom", "outline", "eye", "blush", "accent")
FLAG_FIELDS = ("face_locked", "blush_on", "antenna", "shadow", "highlight")

BODY_SHAPES = ("slime", "round", "square", "drop")
BODY_SHAPE_LABELS = {
    "slime": "史莱姆（波浪裙边）",
    "round": "圆团子",
    "square": "方块",
    "drop": "水滴",
}

# 眼睛 / 嘴：空字符串 = **跟着情绪走**（也就是原来那套：开心弯月眼、无语一条线…，
# 画法见 pet/sprite.py 的 MOOD_STYLE）。选了值就等于把待机时的五官钉成这一种。
EYE_KINDS = ("", "round", "wide", "look", "smirk", "arc", "line")
EYE_LABELS = {
    "": "跟着情绪走",
    "round": "圆眼",
    "wide": "大眼",
    "look": "瞟一眼",
    "smirk": "挑眉",
    "arc": "弯月眼",
    "line": "一条线",
}
MOUTH_KINDS = ("", "smile", "grin", "flat", "open", "small", "smirk")
MOUTH_LABELS = {
    "": "跟着情绪走",
    "smile": "微笑",
    "grin": "咧嘴笑",
    "flat": "一条直线",
    "open": "圆嘴",
    "small": "小小的 o 嘴",
    "smirk": "坏笑",
}

# 身形：本体的宽 / 高占窗口短边的比例。默认值和写死的那一版一模一样，所以没配过
# `pet_style` 的人看到的长相**一点没变**。
BODY_W_DEFAULT = 0.68
BODY_H_DEFAULT = 0.60
BODY_W_MIN, BODY_W_MAX = 0.35, 1.0
BODY_H_MIN, BODY_H_MAX = 0.30, 1.0

DEFAULT_PRESET = "classic"

#: 预设：每个只写"和默认不一样"的键（`_resolve` 拿默认打底再叠它）。
#: `label` 只是给人看的名字，不上任何界面标签以外的地方。
PRESETS: Dict[str, Dict[str, Any]] = {
    "classic": {
        "label": "黄豆（默认）",
        "body_top": "#CFF3FF", "body_bottom": "#5FB8EE", "outline": "#2A7FB8",
        "eye": "#123043", "blush": "#FF9BB8", "accent": "#7CE0FF",
    },
    "peach": {
        "label": "蜜桃",
        "body_top": "#FFE3EC", "body_bottom": "#FF96B8", "outline": "#D2597E",
        "eye": "#4A2230", "blush": "#FF7F9C", "accent": "#FFD1E0",
    },
    "mint": {
        "label": "薄荷",
        "body_top": "#DFF7EA", "body_bottom": "#68CFA3", "outline": "#2E8A66",
        "eye": "#17362B", "blush": "#FFA6B8", "accent": "#B8F0D2",
    },
    "grape": {
        "label": "葡萄",
        "body_top": "#EBE0FF", "body_bottom": "#9A7BE8", "outline": "#5A3FA8",
        "eye": "#2A1F45", "blush": "#FFA7CE", "accent": "#D6C4FF",
    },
    "caramel": {
        "label": "焦糖",
        "body_top": "#FFE9CC", "body_bottom": "#E39B4C", "outline": "#9A5E21",
        "eye": "#3A2410", "blush": "#FF9E7A", "accent": "#FFD9A8",
    },
    "snow": {
        "label": "雪团", "body_shape": "round",
        "body_top": "#FFFFFF", "body_bottom": "#DCE8F2", "outline": "#7E9BB5",
        "eye": "#274052", "blush": "#FFB3C7", "accent": "#BFE6FF",
    },
    "night": {
        "label": "夜色", "body_shape": "drop",
        "body_top": "#3B4A6B", "body_bottom": "#1B2233", "outline": "#8FA6D8",
        "eye": "#E8F0FF", "blush": "#FF6F91", "accent": "#9AD8FF",
    },
    "ink": {
        "label": "水墨", "body_shape": "square",
        "body_top": "#D8DCE0", "body_bottom": "#59616B", "outline": "#22282F",
        "eye": "#0E1216", "blush": "#C97F92", "accent": "#AFC0CC",
    },
}
PRESET_ORDER = tuple(PRESETS)


# ---------- 取值 / 校验：坏值一律退回预设，并留一句话 ----------

def _defaults() -> Dict[str, Any]:
    """出厂那份长相：就是 dataclass 上的默认值（不含 preset / problems）。

    为什么非得有这一层：预设里**只写它要改的键**（classic 就六个颜色），所以
    "默认打底"不能拿预设当底——那样 `body_shape` / `body_width` / 那几个开关
    在字典里根本不存在，用户一改身形就会 KeyError（设计器上就是这么炸出来的）。
    """
    return {
        item.name: item.default
        for item in fields(PetStyle)
        if item.name not in ("preset", "problems")
    }


def _resolve(name: str) -> Dict[str, Any]:
    """某个预设最终生效的那一套（默认打底 + 它的那几项）。"""
    merged: Dict[str, Any] = _defaults()
    merged.update({key: value for key, value in PRESETS[DEFAULT_PRESET].items() if key != "label"})
    merged.update({key: value for key, value in PRESETS[name].items() if key != "label"})
    return merged


def _as_color(value: Any, fallback: str) -> Tuple[str, Optional[str]]:
    """认 `#rrggbb` / `#rgb` / Qt 认得的颜色名（`skyblue`、`tomato`…）。"""
    text = str(value if value is not None else "").strip()
    if not text:
        return fallback, "颜色不能留空，已用回预设的值"
    color = QColor(text)
    if not color.isValid() or not color.alpha():
        return fallback, f"认不出「{text}」这个颜色（写成 #rrggbb 这样就行），已用回预设的值"
    return color.name(QColor.NameFormat.HexRgb).upper(), None


def _as_choice(value: Any, options: Tuple[str, ...], fallback: str, what: str) -> Tuple[str, Optional[str]]:
    text = str(value if value is not None else "").strip().lower()
    if text in options:
        return text, None
    return fallback, f"{what}「{value}」不在可选范围里（{'/'.join(o or '空' for o in options)}），已用回预设的值"


def _as_flag(value: Any, fallback: bool) -> Tuple[bool, Optional[str]]:
    """开关：布尔、0/1，以及 true/false、on/off、yes/no 都认（手写 json 时省事）。"""
    if isinstance(value, bool):
        return value, None
    if isinstance(value, (int, float)):
        return bool(value), None
    text = str(value if value is not None else "").strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True, None
    if text in ("false", "no", "off", "0"):
        return False, None
    return fallback, f"「{value}」不是个开关（写 true / false），已用回预设的值"


def _as_number(value: Any, fallback: float, low: float, high: float, what: str) -> Tuple[float, Optional[str]]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback, f"{what}「{value}」不是个数字，已用回预设的值"
    if not (low <= number <= high):
        clamped = min(high, max(low, number))
        return clamped, f"{what} {number:g} 超出了 {low:g}~{high:g}，已收到 {clamped:g}"
    return number, None


def _hex_from_hsl(hue: float, saturation: float, lightness: float) -> str:
    """色相 / 饱和度 / 亮度 → `#RRGGBB`（随机配色用，保证颜色是一家人）。"""
    red, green, blue = colorsys.hls_to_rgb(hue % 1.0, min(1.0, max(0.0, lightness)), saturation)
    return "#%02X%02X%02X" % (round(red * 255), round(green * 255), round(blue * 255))


def palette_from_hue(hue: float, saturation: float = 0.55) -> Dict[str, str]:
    """从一个色相推出一整张配色：底浅、顶亮、描边深、眼珠近黑、腮红偏粉。

    为什么不是一个通道一个通道地乱掷：三个独立的随机数几乎必定撞成"荧光绿身子 + 紫描边"
    那种配色，掷十次也挑不出一张能看的。同一个色相派生出来的深浅版本**天然是一家人**，
    随机按钮才真的有用。
    """
    return {
        "body_top": _hex_from_hsl(hue, min(0.85, saturation * 0.8), 0.90),
        "body_bottom": _hex_from_hsl(hue, saturation, 0.62),
        "outline": _hex_from_hsl(hue, min(0.9, saturation * 1.15), 0.30),
        "eye": _hex_from_hsl(hue, min(0.9, saturation * 1.2), 0.13),
        "blush": _hex_from_hsl(0.96, 0.62, 0.74),
        "accent": _hex_from_hsl(hue + 0.08, min(0.9, saturation * 0.9), 0.85),
    }


def random_style(rng: Optional[random.Random] = None) -> "PetStyle":
    """随机但**好看**的一套（见 palette_from_hue）。形状和五官也一起掷。"""
    picker = rng or random.Random()
    data: Dict[str, Any] = {"preset": DEFAULT_PRESET}
    data.update(palette_from_hue(picker.random(), 0.42 + picker.random() * 0.3))
    data["body_shape"] = picker.choice(BODY_SHAPES)
    data["eyes"] = picker.choice(EYE_KINDS)
    data["mouth"] = picker.choice(MOUTH_KINDS)
    data["blush_on"] = picker.random() > 0.15
    data["antenna"] = picker.random() > 0.25
    data["highlight"] = picker.random() > 0.2
    data["body_width"] = round(0.55 + picker.random() * 0.4, 2)
    data["body_height"] = round(0.48 + picker.random() * 0.35, 2)
    return PetStyle.from_dict(data)


# ---------- 一套长相 ----------

@dataclass
class PetStyle:
    """一套长相（字段含义见 FIELDS 上面的说明；颜色都是 `#RRGGBB`）。"""

    preset: str = DEFAULT_PRESET
    body_shape: str = "slime"
    body_top: str = "#CFF3FF"
    body_bottom: str = "#5FB8EE"
    outline: str = "#2A7FB8"
    eye: str = "#123043"
    blush: str = "#FF9BB8"
    accent: str = "#7CE0FF"
    eyes: str = ""
    mouth: str = ""
    face_locked: bool = False
    blush_on: bool = True
    antenna: bool = True
    shadow: bool = True
    highlight: bool = True
    body_width: float = BODY_W_DEFAULT
    body_height: float = BODY_H_DEFAULT
    #: 读配置时遇到的毛病（"这个颜色认不出，用回预设的"）。设计器会把它们摆在界面上。
    problems: List[str] = field(default_factory=list)

    # ---------- 读 ----------

    @classmethod
    def from_preset(cls, name: str = DEFAULT_PRESET) -> "PetStyle":
        """只要某个预设，不要用户改的那几项。"""
        if name not in PRESETS:
            name = DEFAULT_PRESET
        return cls(preset=name, **{k: v for k, v in _resolve(name).items()})

    @classmethod
    def from_config(cls, ui: Any) -> "PetStyle":
        """从配置的 `ui` 那一节读（`ui.pet_style` 可以是个字典，也可以直接写预设名）。"""
        return cls.from_dict(getattr(ui, "pet_style", None))

    @classmethod
    def from_dict(cls, data: Any) -> "PetStyle":
        """把配置里那一节解析成一套长相。**永远不抛异常**（坏值退回预设并记进 problems）。"""
        problems: List[str] = []
        if isinstance(data, str):
            raw: Dict[str, Any] = {"preset": data}
        elif isinstance(data, dict):
            raw = dict(data)
        elif data in (None, ""):
            raw = {}
        else:
            problems.append("ui.pet_style 该是个字典（或直接写一个预设名），已按默认长相处理")
            raw = {}

        name = str(raw.get("preset") or "").strip()
        if not name:
            name = DEFAULT_PRESET
        elif name not in PRESETS:
            problems.append(f"没有「{name}」这个预设，改用「{PRESETS[DEFAULT_PRESET]['label']}」")
            name = DEFAULT_PRESET

        values = _resolve(name)
        for key, value in raw.items():
            if key in ("preset", "label"):
                continue
            if key not in FIELDS:
                problems.append(f"不认识的项「{key}」，已忽略（可选的有：{'、'.join(FIELDS)}）")
                continue
            if key in COLOR_FIELDS:
                got, problem = _as_color(value, values[key])
            elif key == "body_shape":
                got, problem = _as_choice(value, BODY_SHAPES, values[key], "身形")
            elif key == "eyes":
                got, problem = _as_choice(value, EYE_KINDS, values[key], "眼睛")
            elif key == "mouth":
                got, problem = _as_choice(value, MOUTH_KINDS, values[key], "嘴巴")
            elif key in FLAG_FIELDS:
                got, problem = _as_flag(value, values[key])
            elif key == "body_width":
                got, problem = _as_number(value, values[key], BODY_W_MIN, BODY_W_MAX, "身宽")
            else:
                got, problem = _as_number(value, values[key], BODY_H_MIN, BODY_H_MAX, "身高")
            values[key] = got
            if problem:
                problems.append(problem)
        return cls(preset=name, problems=problems, **values)

    # ---------- 写 ----------

    def values(self) -> Dict[str, Any]:
        """最终生效的那一套（不含 preset / problems）。"""
        return {name: getattr(self, name) for name in FIELDS if name != "preset"}

    def to_dict(self) -> Dict[str, Any]:
        """完整的这一套（含 `preset`）：导出 / 打印用，一看就知道长什么样。"""
        return {"preset": self.preset, **self.values()}

    def to_config_dict(self) -> Dict[str, Any]:
        """写进 `ui.pet_style` 的那份：**只留和预设不一样的那几项**。

        和预设一模一样时长这样：`{"preset": "peach"}`——配置文件里干干净净一行，
        以后预设本身微调了也能跟着变（全量写进去就锁死了）。
        """
        base = PetStyle.from_preset(self.preset)
        out: Dict[str, Any] = {"preset": self.preset}
        for name, value in self.values().items():
            if value != getattr(base, name):
                out[name] = value
        return out

    def with_changes(self, **changes: Any) -> "PetStyle":
        """改几项，拿回新的一套（表外的键忽略）。"""
        data = self.to_dict()
        data.update({name: value for name, value in changes.items() if name in FIELDS})
        return PetStyle.from_dict(data)

    # ---------- 用 ----------

    @property
    def label(self) -> str:
        """预设的中文名（界面上显示用）。"""
        return str(PRESETS.get(self.preset, {}).get("label") or self.preset)

    def qcolor(self, name: str) -> QColor:
        """某个颜色字段的 QColor（渲染器每一帧都来拿，见 pet/sprite.py）。"""
        return QColor(str(getattr(self, name)))

    def describe(self) -> str:
        """一行中文说明，给 CLI / 日志用。"""
        parts = [
            f"{self.label}｜{BODY_SHAPE_LABELS.get(self.body_shape, self.body_shape)}",
            f"身色 {self.body_top}→{self.body_bottom}",
            f"眼 {EYE_LABELS.get(self.eyes, self.eyes)}",
            f"嘴 {MOUTH_LABELS.get(self.mouth, self.mouth)}",
        ]
        if self.face_locked:
            parts.append("情绪也用这套表情")
        if not self.blush_on:
            parts.append("没腮红")
        if not self.antenna:
            parts.append("没触角")
        return "，".join(parts)
