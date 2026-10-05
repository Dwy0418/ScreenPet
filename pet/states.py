"""日常状态：桌宠**自己**身上那些动画（待机 / 说话 / 走路 / 坐下 / 睡觉…）+ 界面互动的小反应。

跟 `pet/play.py` 那 7 套分清楚，别混：

    play.py    两只凑一起才做的（击掌 / 抱抱 / 躲猫猫…），动作名**要过网**，两边各播各的；
    states.py  它**一只自己**就有的日常样子，以及**你在本机跟它互动**（点它一下、它暂停睡觉、
               它正等你一句话）时切过去的动画——**不过网**，也不需要谁配合。

每条状态对应 `assets/pet/<名字>_*.png` 一套帧，规则和 `idle_*` / `talk_*` 一模一样：

    loop=True   界面按「它现在什么状态」切过去，一直循环，直到下次切换；
                转完一轮要多久由 `seconds` 定（待机那种就交给 ui.anim_period）。
    loop=False  播一遍就停（挥手 / 点头 / 蹦一下…），适合「被点了一下」这种一次性反应。

帧还是从同一张源图生成（`tools/make_pet.py` 取 `motions()` 里的抖法，口径跟 play.py 一样）。
哪套没配帧也不冷场：渲染器会退回 idle / talk（见 `sprite._draw_frames`），绝不崩。

界面怎么用，三个触发口子互不打扰：

* `STATES` 管「它现在什么情况」（暂停 / 在听 / 在等 / 被拖）——一直循环的日常样子；
* `REACTIONS` 管「你做了什么」（鼠标悬停 / 双击 / 拖完放下）——播一遍就停；
* `MOOD_ACTIONS` 管「抓到的画面是什么情绪」（开心 → 欢呼、好奇 → 疑惑…）——也是播一遍。

前两张表都是「名字 → 状态名」，窗口那边只看名字
（见 `window.PetWindow._sync_pose` / `react` / `play_mood`）；**左键点一下演哪个**
单独写在 `CLICK_ACTIONS` 里（那一串就是随机池，见 `window.PetWindow._on_click`）——
省得再做出"动作有了、没人触发"那种事（有帧没人触发，等于白做）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

from . import mood as mood_mod


@dataclass(frozen=True)
class Pose:
    """一条日常状态。

    key 既是**帧名前缀**（`assets/pet/<key>_00.png …`），也是界面上切状态用的名字；
    它跟串门动作名共用一套加载规则（见 `sprite.PetRenderer.frames_acts`），
    区别只在"播一遍"还是"循环"。
    """

    key: str
    label: str               # 面板 / 日志里显示的名字
    mood: str                # 切到这条状态时的表情（见 mood.py；空串 = 不改）
    seconds: float           # loop=True 时是"转完一轮"的秒数；loop=False 时是播一遍的秒数
    loop: bool = False
    # 生成 `<key>_NN.png` 用的抖动参数，跟 pet/play.py 的 motion 同一个口径
    # （breathe_x/breathe_y 横向鼓 · 纵向压，bob 往上蹦，drift 左右漂，sway 摇摆多少度，
    #   jitter 高频小抖）。idle / talk 这两条不在这儿给——它们的帧早就有了。
    motion: Dict[str, float] = field(default_factory=dict)
    #: 手写的关键帧（可选）：给了就按它摆姿势，不再用 motion 那套正弦抖。
    #: 每组参数：dx/dy 整体位移（占画布的比例，正 = 右 / 下）、scale 整体缩放（1.0 = 原样）、
    #: squash 纵向压扁（正 = 压扁且横向鼓，负 = 拉长）、tilt 旋转角度（度，正 = 顺时针）。
    #: 采成多少帧由 make_pet 的 --frames 定：关键帧摆姿势，帧数是采样密度。
    keys: Tuple[Dict[str, float], ...] = ()
    #: 画在帧上的小提示符号（可选）：spark 星星 / ? / ! / anger 怒气 / sad 汗滴。
    #: 只是让动作更好读（一个字都不上界面），见 tools/make_pet.py 的 paint_deco。
    deco: str = ""
    #: **逐部件**关键帧（可选）：头绕脖子转、手臂绕肩转，各摆各的姿势——
    #: 这才是"真的在点头 / 在挥手"，而不是整张图一起上下抖。
    #: 每一组：整体参数（dx/dy/scale/squash/tilt，同 `keys`）+ 两个部件：
    #:
    #:     "head"  头绕**脖子**：dy/dx 位移（占画布比例，正 = 下 / 右）、angle 旋转（度，正 = 顺时针）
    #:     "arm"   手臂绕**肩关节**：angle 正 = 往外侧抬起来（画面左边那只手）
    #:
    #: 骨架见 tools/make_pet.py 的 RIG_*（换形象要改那几行，或者生成时加 --no-rig）。
    #: 没有骨架时会自动退回"只套整体参数"：幅度小一点，但一帧都不会崩。
    rig_keys: Tuple[Dict[str, object], ...] = ()


def _k(**params: float) -> Dict[str, float]:
    """写一组关键帧参数（没写的当 0；scale 单独给，不写就是 1.0）。"""
    return {name: float(value) for name, value in params.items()}


def _rig(*frames: Mapping[str, object]) -> Tuple[Dict[str, object], ...]:
    """写一串**逐部件**关键帧（头 / 手臂各摆各的，见 Pose.rig_keys）。"""
    out = []
    for frame in frames:
        item: Dict[str, object] = {}
        for name, value in frame.items():
            item[name] = dict(value) if isinstance(value, Mapping) else float(value)  # type: ignore[arg-type]
        out.append(item)
    return tuple(out)


# 18 条日常状态。前两条（待机 / 说话）是本来就有的帧，列在这儿是为了"一共多少条"说得清，
# 生成帧时会自动跳过它们（`motions()` 只返回真给了抖法的那些）。
# 末尾那 5 条（打招呼 / 被戳 / 疑惑 / 吃饭 / 摸鱼）是给「单击 / 双击 / 场景情绪」准备的，
# 见下面的 REACTIONS 和 MOOD_ACTIONS。
POSES: Tuple[Pose, ...] = (
    Pose("idle", "待机", "", 30.0, True),
    Pose("talk", "说话", "", 16.0, True),
    # 走路：抬脚 → 落地压一下 → 换另一只脚，左右交替（8 拍那版的姿势，采成 12 帧）
    Pose("walk", "走路", "happy", 1.6, True, keys=(
        _k(dy=0.018, dx=-0.008, tilt=-4, squash=0.050),    # 左脚着地，身子压一下
        _k(dy=-0.012, dx=-0.002, tilt=-1, squash=-0.020),  # 抬起来，往上
        _k(dy=0.018, dx=0.008, tilt=4, squash=0.050),      # 右脚着地
        _k(dy=-0.012, dx=0.002, tilt=1, squash=-0.020),    # 再抬起来
    )),
    # 坐下：身子松下来慢慢起伏，头也跟着轻轻往下垂一点（坐久了犯困那种）
    Pose("sit", "坐下", "happy", 3.0, True, rig_keys=_rig(
        {"dy": 0.002, "scale": 0.995, "head": {"dy": 0.000}},
        {"dy": 0.004, "scale": 0.992, "head": {"dy": 0.006}},
        {"dy": 0.002, "scale": 0.995, "head": {"dy": 0.002}},
        {"dy": 0.000, "scale": 0.998, "head": {"dy": 0.000}},
    )),
    # 睡觉：一泡一泡的慢呼吸（4 拍转一圈，配合 4.2 秒的周期，"最慢"那一档）+ 头慢慢垂下去
    Pose("sleep", "睡觉", "speechless", 4.2, True, rig_keys=_rig(
        {"dy": 0.004, "head": {"dy": 0.000}},
        {"dy": -0.004, "scale": 1.015, "squash": -0.012, "head": {"dy": 0.008, "angle": 3.0}},
        {"dy": 0.008, "scale": 0.988, "squash": 0.020, "head": {"dy": 0.018, "angle": 5.0}},
        {"dy": 0.006, "scale": 0.995, "squash": 0.008, "head": {"dy": 0.010, "angle": 3.0}},
    )),
    # 伸个懒腰：身子往上长，那只垂着的手**绕肩举起来**（举过头顶那种），再收回来
    Pose("stretch", "伸个懒腰", "happy", 1.5, False, rig_keys=_rig(
        {"head": {"dy": 0.000}, "arm": {"angle": 0.0}},
        {"dy": 0.010, "scale": 0.985, "squash": 0.030, "head": {"dy": 0.006}, "arm": {"angle": 26.0}},
        {"dy": -0.028, "scale": 1.030, "squash": -0.040, "head": {"dy": -0.010, "angle": -4.0}, "arm": {"angle": 104.0}},
        {"dy": -0.034, "scale": 1.038, "squash": -0.048, "head": {"dy": -0.012, "angle": -5.0}, "arm": {"angle": 116.0}},
        {"dy": -0.012, "scale": 1.010, "squash": -0.014, "head": {"dy": -0.004}, "arm": {"angle": 72.0}},
        {"head": {"dy": 0.000}, "arm": {"angle": 0.0}},
    )),
    # 想事情：头一点一点地轻晃（配上那根敲下巴的食指，就是"在想"）
    Pose("think", "想事情", "curious", 2.6, True, rig_keys=_rig(
        {"head": {"dy": 0.000, "angle": 0.0}},
        {"head": {"dy": 0.008, "angle": -4.0}},
        {"head": {"dy": 0.004, "angle": -1.0}},
        {"head": {"dy": 0.010, "angle": 4.0}},
    )),
    Pose("look", "张望", "curious", 2.2, True,
         {"breathe_x": 0.004, "breathe_y": 0.005, "bob": 0.008, "drift": 0.014,
          "sway": 5.0, "jitter": 0.0}),
    # ---- 点头 / 摇头 / 挥手：这三个是**逐部件**的，头绕脖子、手绕肩 ----
    # 点头：绕脖子往下点两下（整体几乎不动）。以前是"整只上下抖"，看不出是头在点。
    Pose("nod", "点点头", "happy", 0.9, False, rig_keys=_rig(
        {"head": {"dy": 0.000, "angle": 0.0}},                    # 正
        {"dy": 0.004, "head": {"dy": 0.030, "angle": 3.0}},       # 往下一沉
        {"dy": 0.006, "head": {"dy": 0.048, "angle": 5.0}},       # 低到底
        {"dy": 0.002, "head": {"dy": 0.014, "angle": 1.5}},       # 抬起来
        {"dy": 0.005, "head": {"dy": 0.040, "angle": 4.0}},       # 再点一下
        {"dy": 0.001, "head": {"dy": 0.010, "angle": 1.0}},
        {"dy": 0.003, "head": {"dy": 0.022, "angle": 2.0}},       # 轻轻收住
        {"head": {"dy": 0.000, "angle": 0.0}},                    # 回正
    )),
    # 摇头：绕脖子左右转（角度为主、横移一点点）。横移给多了就成"平移"，不像转头。
    Pose("shake", "摇摇头", "speechless", 0.9, False, rig_keys=_rig(
        {"head": {"angle": 0.0}},
        {"head": {"dx": -0.010, "angle": -9.0}},
        {"head": {"dx": 0.012, "angle": 10.0}},
        {"head": {"dx": -0.012, "angle": -10.0}},
        {"head": {"dx": 0.009, "angle": 7.0}},
        {"head": {"dx": -0.005, "angle": -4.0}},
        {"head": {"dx": 0.002, "angle": 2.0}},
        {"head": {"angle": 0.0}},
    )),
    # 挥手：垂在身侧那只手**绕肩抬起来**，再左右摆两下（角度大 = 抬得高）
    Pose("wave", "挥挥手", "happy", 1.2, False, rig_keys=_rig(
        {"arm": {"angle": 0.0}},                                              # 手垂着
        {"dy": -0.004, "head": {"angle": -2.0}, "arm": {"angle": 32.0}},      # 抬起来
        {"dy": -0.006, "head": {"angle": -3.0}, "arm": {"angle": 76.0}},
        {"dy": -0.006, "head": {"angle": -3.0}, "arm": {"angle": 104.0}},     # 举到头边
        {"dy": -0.004, "head": {"angle": -2.0}, "arm": {"angle": 86.0}},      # 摆回来
        {"dy": -0.006, "head": {"angle": -3.0}, "arm": {"angle": 108.0}},     # 再摆出去
        {"dy": -0.003, "head": {"angle": -1.0}, "arm": {"angle": 68.0}},
        {"arm": {"angle": 0.0}},                                              # 放下
    )),
    # 蹦一下：身子整个弹起来，那只手也跟着往上一扬
    Pose("jump", "蹦一下", "excited", 0.8, False, rig_keys=_rig(
        {"scale": 0.985, "squash": 0.030, "head": {"dy": 0.008}, "arm": {"angle": 8.0}},
        {"dy": -0.046, "scale": 1.040, "squash": -0.050, "head": {"dy": -0.014}, "arm": {"angle": 78.0}},
        {"dy": -0.062, "scale": 1.055, "squash": -0.070, "head": {"dy": -0.020, "angle": -4.0}, "arm": {"angle": 100.0}},
        {"dy": -0.018, "scale": 1.012, "squash": -0.012, "head": {"dy": -0.004}, "arm": {"angle": 54.0}},
        {"dy": 0.006, "scale": 0.972, "squash": 0.060, "head": {"dy": 0.014}, "arm": {"angle": 12.0}},
        {"head": {"dy": 0.000}, "arm": {"angle": 0.0}},
    )),
    # 加油 / 欢呼：握拳蓄力 → 举起来 → 再举一下 → 收（播一遍就停）
    Pose("cheer", "欢呼", "excited", 1.3, False, rig_keys=_rig(
        {"dy": 0.012, "scale": 0.965, "squash": 0.050, "head": {"dy": 0.010}, "arm": {"angle": 6.0}},      # 蓄力，先缩一下
        {"dy": -0.030, "scale": 1.040, "squash": -0.040, "head": {"dy": -0.012, "angle": -3.0}, "arm": {"angle": 82.0}},   # 举起来
        {"dy": -0.048, "scale": 1.060, "squash": -0.060, "head": {"dy": -0.018, "angle": -5.0}, "arm": {"angle": 116.0}},  # 到顶
        {"dy": -0.026, "scale": 1.030, "squash": -0.030, "head": {"dy": -0.008, "angle": -2.0}, "arm": {"angle": 96.0}},
        {"dy": -0.044, "scale": 1.050, "squash": -0.052, "head": {"dy": -0.016, "angle": -4.0}, "arm": {"angle": 120.0}},  # 再举一下
        {"dy": -0.010, "scale": 1.008, "squash": -0.010, "head": {"dy": -0.002}, "arm": {"angle": 58.0}},
        {"dy": 0.004, "scale": 0.992, "squash": 0.012, "head": {"dy": 0.004}, "arm": {"angle": 16.0}},
        {"head": {"dy": 0.000}, "arm": {"angle": 0.0}},
    ), deco="spark"),
    # ---- 界面互动 / 场景情绪那几条（见 REACTIONS / MOOD_ACTIONS）----
    # 打招呼：抬手 → 左摆 → 右摆 → 再左 → 收（单击它的时候放）
    Pose("greet", "打招呼", "happy", 1.0, False, rig_keys=_rig(
        {"head": {"dy": 0.000}, "arm": {"angle": 0.0}},
        {"dy": -0.006, "scale": 1.010, "head": {"angle": -3.0}, "arm": {"angle": 40.0}},       # 抬手
        {"dy": -0.010, "scale": 1.016, "head": {"angle": -5.0}, "arm": {"angle": 96.0}},       # 举到头边
        {"dy": -0.010, "scale": 1.016, "head": {"angle": -5.0}, "arm": {"angle": 78.0}},       # 往左摆
        {"dy": -0.012, "scale": 1.018, "head": {"angle": -6.0}, "arm": {"angle": 102.0}},      # 往右摆
        {"dy": -0.008, "scale": 1.012, "head": {"angle": -4.0}, "arm": {"angle": 66.0}},       # 收
        {"head": {"dy": 0.000}, "arm": {"angle": 0.0}},
    ), deco="spark"),
    # 被戳：缩团闭眼 → 惊跳弹起 → 到顶 → 落地压扁 → 挠头傻笑（双击它的时候放）
    Pose("poke", "被戳", "surprised", 0.9, False, rig_keys=_rig(
        {"scale": 0.880, "dy": 0.022, "squash": 0.100, "head": {"dy": 0.020, "angle": 4.0}, "arm": {"angle": 4.0}},       # 缩成一团
        {"scale": 1.070, "dy": -0.050, "squash": -0.060, "head": {"dy": -0.022, "angle": -6.0}, "arm": {"angle": 86.0}},   # 惊跳
        {"scale": 1.100, "dy": -0.068, "squash": -0.080, "head": {"dy": -0.028, "angle": -8.0}, "arm": {"angle": 112.0}},  # 到顶
        {"scale": 0.940, "dy": 0.020, "squash": 0.120, "head": {"dy": 0.024, "angle": 6.0}, "arm": {"angle": 30.0}},       # 落地，压扁
        {"scale": 1.020, "dy": -0.008, "squash": -0.020, "head": {"dy": -0.004, "angle": -3.0}, "arm": {"angle": 20.0}},   # 挠头
        {"scale": 1.000, "head": {"dy": 0.000}, "arm": {"angle": 0.0}},                                                     # 收
    ), deco="!"),
    # 疑惑：**歪头**（绕脖子转，不是整只斜过去）→ 回正，拽着长音的"嗯？"
    Pose("confused", "疑惑", "curious", 1.1, False, rig_keys=_rig(
        {"head": {"angle": 0.0}},
        {"head": {"angle": -12.0, "dy": 0.004, "dx": -0.004}},    # 往一边歪
        {"head": {"angle": -10.0, "dy": 0.002}},
        {"head": {"angle": 7.0, "dx": 0.003}},                    # 往另一边歪
        {"head": {"angle": 0.0}},
    ), deco="?"),
    # 吃饭：**低下头去够一口** → 鼓腮咀嚼 → 再嚼 → 满足地拍拍肚子（循环）
    Pose("eat", "吃饭", "happy", 1.6, True, rig_keys=_rig(
        {"head": {"dy": 0.000}, "arm": {"angle": 0.0}},
        {"dy": -0.008, "squash": -0.020, "head": {"dy": -0.006, "angle": -4.0}, "arm": {"angle": 14.0}},  # 抬起头张嘴
        {"dy": 0.008, "squash": 0.045, "head": {"dy": 0.024, "angle": 7.0}, "arm": {"angle": 4.0}},       # 低头咬一口
        {"dy": -0.004, "squash": -0.010, "head": {"dy": 0.004, "angle": 1.0}, "arm": {"angle": 10.0}},    # 鼓腮，嚼
        {"dy": 0.006, "squash": 0.030, "head": {"dy": 0.018, "angle": 5.0}, "arm": {"angle": 2.0}},       # 再嚼
        {"dy": 0.000, "head": {"dy": 0.000}, "arm": {"angle": 6.0}},                                      # 满足，拍拍肚子
    )),
    # 摸鱼：**头绕脖子左右瞟**（身子几乎不动，看着才像"偷看"）→ 偷笑 → 慌忙缩起来藏 → 装无辜（循环）
    Pose("slack", "摸鱼", "smirk", 1.5, True, rig_keys=_rig(
        {"head": {"dx": -0.010, "angle": -10.0}, "arm": {"angle": 0.0}},                    # 往左瞟一眼
        {"head": {"dx": 0.012, "angle": 11.0}},                                             # 往右瞟一眼
        {"dy": -0.004, "head": {"dx": 0.002, "angle": -3.0}},                               # 偷笑，撑一下
        {"dy": 0.018, "scale": 0.940, "squash": 0.060, "head": {"dy": 0.016, "angle": 2.0}},  # 慌忙缩起来藏
        {"head": {"dy": 0.000, "angle": 0.0}},                                              # 装无辜
    )),
)

BY_KEY: Dict[str, Pose] = {pose.key: pose for pose in POSES}

#: 它现在什么情况 → 用哪条日常状态（`window.PetWindow._sync_pose` 按这个切）。
#: 这几条都是 loop=True 的，切过去就一直在那儿。
STATES: Dict[str, str] = {
    "paused": "sleep",        # 暂停 / 眯着了
    "listening": "look",      # 麦克风开着、正在听你说话
    "thinking": "think",      # 在等你那句话从模型那边回来
    "dragging": "walk",       # 你正把它拖到别的地方去
}

#: 你在界面上做了什么 → 播哪条一次性反应（`window.PetWindow.react` 按这个放）。
#: 认不出来的键就什么都不做（不认识的别瞎放）。
#: 谁在用：鼠标那几下都在窗口里——悬停 → `hover`、双击 → `double`、
#: 拖起来再放下 → `drop`（见 `window.PetWindow._on_hover_still` / `_on_drop`）；
#: **单击不固定演哪个**，走 `CLICK_ACTIONS` 那张随机池（见 `window.PetWindow._on_click`）。
REACTIONS: Dict[str, str] = {
    "double": "poke",         # 双击它一下：像被戳了似的惊跳（暂停 / 继续看也走这一下）
    "hover": "wave",          # 鼠标在它身上停一会儿：它注意到你了，冲你挥挥手
    "drop": "jump",           # 你把它拖起来又放下：落地蹦一下（拖完那一下就是"放我下来"）
    "greet": "greet",
    "wave": "wave",           # 挥挥手
    "poke": "poke",           # 戳它
    "agree": "nod",           # 应一声 / 答应
    "deny": "shake",          # 不赞同、没听明白
    "cheer": "cheer",         # 高兴一下
    "rest": "stretch",        # 坐久了伸个懒腰
    "eat": "eat",             # 喂它一口
    "slack": "slack",         # 摸会儿鱼
    "confused": "confused",   # 没看懂
    "jump": "jump",           # 蹦一下
}

#: **左键点一下挂件** → 从这一串里随机演一个（见 `window.PetWindow._on_click`）。
#: 以前这些动作要靠右键菜单「逗它一下」那一节一项项点；现在点它一下就有，
#: 菜单里不再单列（那张 `MENU_REACTIONS` 表连同菜单一起撤了）。
#: 每一项都必须是 `REACTIONS` 认得的键——不然点了没反应（等于白点），
#: 冒烟测试会挨个走一遍，认不出来的当场报出来。
#: 顺序就是随机池的顺序，别往里放"只有特定场合才合适"的动作（比如坐下 / 睡觉）。
CLICK_ACTIONS: Tuple[str, ...] = (
    "wave",       # 挥挥手
    "greet",      # 抬手打个招呼
    "cheer",      # 高兴一下
    "agree",      # 点点头
    "deny",       # 摇摇头
    "confused",   # 歪头疑惑
    "poke",       # 被戳般惊跳
    "jump",       # 蹦一个
    "eat",        # 喂它一口
    "slack",      # 摸会儿鱼
    "rest",       # 伸个懒腰
)


#: 抓到的画面表达什么情绪 → 顺手做一个对应的动作（`window.PetWindow.play_mood` 按这个放）。
#: 情绪是模型给的那一档（或本地关键词兜底，见 pet/mood.py）；这里只把"情绪"翻译成"动作"。
#: 也是播一遍就停；认不出来的情绪什么都不做。
MOOD_ACTIONS: Dict[str, str] = {
    "happy": "cheer",         # 开心 → 欢呼
    "excited": "cheer",       # 激动 → 欢呼
    "surprised": "poke",      # 惊讶 → 被戳般惊跳
    "curious": "confused",    # 好奇 → 歪头冒问号
    "speechless": "shake",    # 无语 → 摇摇头
    "angry": "shake",         # 生气 → 摇摇头
    "sad": "sit",             # 难过 → 坐下歇会儿
    "smirk": "slack",         # 吐槽 → 摸鱼偷笑
}


def spec_of(pose: Pose) -> Dict[str, object]:
    """一条状态 → 生成帧用的那包参数（`tools/make_pet.py` 按它写 `<状态名>_NN.png`）。

    * 手写了 `rig_keys`（逐部件）就带上——**这条优先**，头绕脖子、手臂绕肩，各摆各的；
    * 手写了 `keys` 就带上（整张图一起摆那种，`keys_loop` 说明是"转一圈"还是"走一遍"）；
    * 有 `deco` 就带上（画在帧上的小提示符号）；
    * `motion` 里的抖法照旧（没写 keys 的那几条靠它）。
    """
    spec: Dict[str, object] = dict(pose.motion)
    if pose.keys:
        spec["keys"] = [dict(frame) for frame in pose.keys]
        spec["keys_loop"] = bool(pose.loop)
    if pose.rig_keys:
        spec["rig_keys"] = [
            {
                name: dict(value) if isinstance(value, Mapping) else float(value)  # type: ignore[arg-type]
                for name, value in frame.items()
            }
            for frame in pose.rig_keys
        ]
        spec["keys_loop"] = bool(pose.loop)
    if pose.deco:
        spec["deco"] = pose.deco
    return spec


def motions() -> Dict[str, Dict[str, object]]:
    """状态 → 生成这套帧用的参数（`tools/make_pet.py` 靠它写 `<状态名>_NN.png`）。

    只返回**真给了参数**的那些：idle / talk 的帧早就有了，不用重做。
    """
    return {
        pose.key: spec_of(pose)
        for pose in POSES
        if pose.motion or pose.keys or pose.rig_keys
    }


def get(key: str) -> Optional[Pose]:
    """按名字找一条状态；认不出来就返回 None（不认识的就不切，别瞎猜）。"""
    return BY_KEY.get(str(key or "").strip())


def for_state(name: str) -> Optional[Pose]:
    """界面状态名（paused / listening / thinking / dragging）→ 该切哪条状态。"""
    return BY_KEY.get(STATES.get(str(name or "").strip(), ""))


def for_reaction(event: str) -> Optional[Pose]:
    """界面互动名（click / double / poke…）→ 该播哪条一次性反应。"""
    return BY_KEY.get(REACTIONS.get(str(event or "").strip(), ""))


def for_mood(mood: str) -> Optional[Pose]:
    """画面情绪（happy / curious…，见 pet/mood.py）→ 该顺手做哪个动作。

    跟 `for_reaction` 一样是"播一遍就停"；**没给情绪、或没有对应的动作就返回 None**
    （不做，别拿兜底情绪瞎动）。
    """
    key = str(mood or "").strip()
    if not key:
        return None
    return BY_KEY.get(MOOD_ACTIONS.get(mood_mod.normalize(key), ""))


def info(pose: Pose, *, loop: Optional[bool] = None) -> Dict[str, object]:
    """发给窗口的那一包（见 `window.PetWindow.set_pose` / `react`）。"""
    return {
        "pose": pose.key,
        "anim": pose.key,
        "label": pose.label,
        "mood": pose.mood,
        "seconds": float(pose.seconds),
        "loop": bool(pose.loop if loop is None else loop),
    }
