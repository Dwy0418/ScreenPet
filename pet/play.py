"""两只桌宠能一起干点啥：动作表 + 台词兜底 + **给动画留的那个口子**。

这一层只决定「做什么、谁发起、两边各播多久」，**自己不画任何东西**：决定好了就发
`VisitHub.acted` 信号（谁、动作名、时长、表情），窗口拿去播
（见 `window.PetWindow.act` / `guest.GuestWindow.act`）。

动作帧不是手画的也能有：`motion` 里那几行就是这套动作的**抖法**（跟 `make_pet.py` 的
MOTIONS 同一个口径），`tools/make_pet.py` 拿它从同一张源图生成 `assets/pet/<动作名>_NN.png`。
想换成手画的：把 `assets/pet/<动作名>_00.png …` 覆盖掉就行，加载规则完全一样
（`sprite.PetRenderer` 认这个命名，`idle_*` / `talk_*` / 情绪帧是同一套机制）。
**连帧都没有的时候也看得出"在玩"**：它会跳一下（`sprite.act_lift`）+ 说那句配词。

动作分两种：

* `both`：两只**同时**做同一件事（击掌、撞拳、抱一下）——串门的招牌动作；
* `one`：一只对另一只做（拍一拍、抛个媚眼）——对方接着应一句。

台词不由这里定（那是各自机器上调模型的事，见 `persona.visit_prompt` 的 play 那一档），
这里只给**兜底那句**和写进提示词的 `hint`——模型不给力也得有话说。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Move:
    """一个动作。key 是**过网用的名字**（英文短词），别的都是给人看 / 给动画用的。"""

    key: str                 # 过网 + 当动画名（assets/pet/<key>_*.png）
    label: str               # 面板、菜单里显示的名字
    mood: str                # 做这个动作时的表情（见 mood.py）
    seconds: float           # 两边各播多久
    hint: str                # 写进提示词的一句：这会儿正在干什么
    both: bool = True         # True = 两只同时做；False = 一只对另一只做
    fallback: Tuple[str, ...] = ()    # 模型不给力时的兜底台词
    # 生成 `<key>_NN.png` 用的抖动参数（见 tools/make_pet.py 的 motion_affine）：
    #   breathe_x/breathe_y 横向鼓 · 纵向压，bob 往上蹦，drift 左右漂，
    #   sway 摇摆多少度，jitter 高频小抖（说话那种）
    # 幅度刻意压得很小（跟 idle 一个量级）：它是**这一套帧自己的动作**，
    # 渲染器不会再叠程序化弹跳，写大了整只会像在喘气。
    motion: Dict[str, float] = field(default_factory=dict)

    @property
    def anim(self) -> str:
        """动画名 = key（`assets/pet/<key>_*.png` 就是这套动作的帧）。"""
        return self.key


# 动作表。加动作只在这张表里加一行：过网、菜单、提示词、动画名、生成帧的抖法全跟着走。
MOVES: Tuple[Move, ...] = (
    Move("highfive", "击个掌", "excited", 1.2, "两只正举起手来击了个掌",
         True, ("[激动] 来，击个掌！", "[激动] 啪——这一下交情够铁了"),
         {"breathe_x": 0.006, "breathe_y": 0.006, "bob": 0.055, "drift": 0.004,
          "sway": 2.0, "jitter": 0.006}),
    Move("bump", "撞个拳", "happy", 1.1, "两只正撞了下拳",
         True, ("[开心] 撞个拳，自己人", "[开心] 碰一下就算认识了"),
         {"breathe_x": 0.014, "breathe_y": 0.009, "bob": 0.022, "drift": 0.011,
          "sway": 1.4, "jitter": 0.008}),
    Move("hug", "抱一下", "happy", 1.6, "两只正抱了一下",
         True, ("[开心] 来抱一个，别跟我客气", "[开心] 抱完我就该回家啦"),
         {"breathe_x": 0.018, "breathe_y": 0.004, "bob": 0.012, "drift": 0.003,
          "sway": 1.0, "jitter": 0.0}),
    Move("sway", "一起晃两下", "happy", 1.5, "两只正并排晃来晃去",
         True, ("[开心] 跟着我摇，一二三", "[开心] 你晃得比我还起劲"),
         {"breathe_x": 0.005, "breathe_y": 0.006, "bob": 0.018, "drift": 0.008,
          "sway": 8.0, "jitter": 0.0}),
    Move("peekaboo", "躲猫猫", "curious", 1.4, "两只正一个躲一个找",
         True, ("[好奇] 我不见了，你找我呀", "[好奇] 抓到你了，别躲了"),
         {"breathe_x": 0.010, "breathe_y": 0.020, "bob": 0.006, "drift": 0.012,
          "sway": 3.0, "jitter": 0.0}),
    Move("pat", "拍一拍", "happy", 0.9, "一只正抬手拍了对方一下",
         False, ("[开心] 拍你一下，算打个招呼", "[开心] 拍拍你，乖乖的"),
         {"breathe_x": 0.006, "breathe_y": 0.007, "bob": 0.034, "drift": 0.003,
          "sway": 1.2, "jitter": 0.014}),
    Move("wink", "抛个媚眼", "happy", 0.9, "一只正对另一只抛了个媚眼",
         False, ("[开心] 看你一眼，够意思吧", "[开心] 别害羞啊"),
         {"breathe_x": 0.005, "breathe_y": 0.006, "bob": 0.014, "drift": 0.006,
          "sway": 6.0, "jitter": 0.0}),
)

BY_KEY: Dict[str, Move] = {move.key: move for move in MOVES}


def motions() -> Dict[str, Dict[str, float]]:
    """动作 → 生成这套帧用的抖法（`tools/make_pet.py` 靠它写 `<动作名>_NN.png`）。

    没写 motion 的动作返回空字典——那就只是"还没配帧"，渲染器会退回蹦一下那种兜底。
    """
    return {move.key: dict(move.motion) for move in MOVES if move.motion}


def get(key: str) -> Optional[Move]:
    """按名字找一个动作；认不出来就返回 None（不认识的就不做，别瞎猜）。"""
    return BY_KEY.get(str(key or "").strip())


def pick(exclude: Sequence[str] = ()) -> Move:
    """随便挑一个（exclude 里的是刚做过的，别再连着来一遍）。"""
    seen = {str(key) for key in exclude or ()}
    pool = [move for move in MOVES if move.key not in seen] or list(MOVES)
    return random.choice(pool)


def brief(move: Move) -> str:
    """给人看的一句话（面板 / 状态栏）。"""
    return f"两只凑一起{move.label}"


def info(move: Move, *, by: str = "", both: Optional[bool] = None) -> Dict[str, object]:
    """发给窗口的那一包：怎么播、播多久、配什么表情（见 PetWindow.act）。"""
    return {
        "move": move.key,
        "anim": move.anim,
        "label": move.label,
        "mood": move.mood,
        "seconds": float(move.seconds),
        "both": bool(move.both if both is None else both),
        "by": str(by or ""),
    }
