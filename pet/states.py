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
* `REACTIONS` 管「你做了什么」（单击 / 双击 / 戳它 / 应一声）——播一遍就停；
* `MOOD_ACTIONS` 管「抓到的画面是什么情绪」（开心 → 欢呼、好奇 → 疑惑…）——也是播一遍。

三张表都是「名字 → 状态名」，窗口那边只看名字
（见 `window.PetWindow._sync_pose` / `react` / `play_mood`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

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


def _k(**params: float) -> Dict[str, float]:
    """写一组关键帧参数（没写的当 0；scale 单独给，不写就是 1.0）。"""
    return {name: float(value) for name, value in params.items()}


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
    Pose("sit", "坐下", "happy", 3.0, True,
         {"breathe_x": 0.004, "breathe_y": 0.010, "bob": 0.004, "drift": 0.006,
          "sway": 1.5, "jitter": 0.0}),
    # 睡觉：一泡一泡的慢呼吸（4 拍转一圈，配合 4.2 秒的周期，"最慢"那一档）
    Pose("sleep", "睡觉", "speechless", 4.2, True, keys=(
        _k(dy=0.004),
        _k(dy=-0.004, scale=1.015, squash=-0.012),
        _k(dy=0.008, scale=0.988, squash=0.020),
        _k(dy=0.006, scale=0.995, squash=0.008),
    )),
    Pose("stretch", "伸个懒腰", "happy", 1.4, False,
         {"breathe_x": 0.012, "breathe_y": 0.018, "bob": 0.045, "drift": 0.003,
          "sway": 2.0, "jitter": 0.0}),
    Pose("think", "想事情", "curious", 2.6, True,
         {"breathe_x": 0.003, "breathe_y": 0.005, "bob": 0.006, "drift": 0.003,
          "sway": 3.5, "jitter": 0.0}),
    Pose("look", "张望", "curious", 2.2, True,
         {"breathe_x": 0.004, "breathe_y": 0.005, "bob": 0.008, "drift": 0.014,
          "sway": 5.0, "jitter": 0.0}),
    Pose("nod", "点点头", "happy", 0.9, False,
         {"breathe_x": 0.004, "breathe_y": 0.006, "bob": 0.020, "drift": 0.002,
          "sway": 1.0, "jitter": 0.020}),
    Pose("shake", "摇摇头", "speechless", 0.9, False,
         {"breathe_x": 0.003, "breathe_y": 0.006, "bob": 0.008, "drift": 0.004,
          "sway": 7.0, "jitter": 0.0}),
    Pose("wave", "挥挥手", "happy", 1.1, False,
         {"breathe_x": 0.008, "breathe_y": 0.006, "bob": 0.026, "drift": 0.004,
          "sway": 5.0, "jitter": 0.010}),
    Pose("jump", "蹦一下", "excited", 0.8, False,
         {"breathe_x": 0.006, "breathe_y": 0.010, "bob": 0.060, "drift": 0.002,
          "sway": 0.0, "jitter": 0.0}),
    # 加油 / 欢呼：握拳蓄力 → 举起来 → 双拳鼓劲 → 收（播一遍就停）
    Pose("cheer", "欢呼", "excited", 1.2, False, keys=(
        _k(dy=0.012, scale=0.965, squash=0.050),           # 蓄力，先缩一下
        _k(dy=-0.030, scale=1.040, squash=-0.040),         # 举起来
        _k(dy=-0.048, scale=1.060, squash=-0.060),         # 到顶
        _k(dy=-0.024, scale=1.030, squash=-0.030, tilt=-3),
        _k(dy=-0.010, scale=1.010, squash=-0.010, tilt=3),
        _k(dy=0.000),
    ), deco="spark"),
    # ---- 界面互动 / 场景情绪那几条（见 REACTIONS / MOOD_ACTIONS）----
    # 打招呼：抬手 → 左摆 → 右摆 → 再左 → 收（单击它的时候放）
    Pose("greet", "打招呼", "happy", 0.9, False, keys=(
        _k(dy=-0.006, scale=1.010, tilt=-2),    # 抬手
        _k(dy=-0.016, scale=1.020, tilt=-6),    # 往左摆
        _k(dy=-0.018, scale=1.020, tilt=6),     # 往右摆
        _k(dy=-0.014, scale=1.015, tilt=-5),    # 再往左
        _k(dy=0.000),                            # 收
    ), deco="spark"),
    # 被戳：缩团闭眼 → 惊跳弹起 → 到顶 → 落地压扁 → 挠头傻笑（双击它的时候放）
    Pose("poke", "被戳", "surprised", 0.8, False, keys=(
        _k(scale=0.880, dy=0.022, squash=0.100),                 # 缩成一团
        _k(scale=1.070, dy=-0.050, squash=-0.060, tilt=-2),      # 惊跳
        _k(scale=1.100, dy=-0.068, squash=-0.080, tilt=2),       # 到顶
        _k(scale=0.940, dy=0.020, squash=0.120),                 # 落地，压扁
        _k(scale=1.020, dy=-0.008, squash=-0.020, tilt=-3),      # 挠头
        _k(scale=1.000),                                          # 收
    ), deco="!"),
    # 疑惑：托腮 → 歪头 → 回正（拖着长音的"嗯？"）
    Pose("confused", "疑惑", "curious", 1.0, False, keys=(
        _k(),
        _k(tilt=-9, dy=0.004, scale=0.990),
        _k(tilt=5, dy=-0.004),
        _k(),
    ), deco="?"),
    # 吃饭：捧碗张嘴 → 鼓腮咀嚼 → 再嚼 → 拍肚子满足（循环）
    Pose("eat", "吃饭", "happy", 1.6, True, keys=(
        _k(),
        _k(dy=-0.010, squash=-0.020, tilt=-2),   # 张嘴，往上一够
        _k(dy=0.006, squash=0.045, tilt=2),      # 鼓腮，嚼
        _k(dy=-0.010, squash=-0.020, tilt=-2),
        _k(dy=0.006, squash=0.045, tilt=2),
        _k(),                                      # 满足，拍拍肚子
    )),
    # 摸鱼：往左偷看 → 往右偷看 → 偷笑 → 慌忙缩起来藏 → 装无辜（循环）
    Pose("slack", "摸鱼", "smirk", 1.4, True, keys=(
        _k(dx=-0.012, dy=0.004, tilt=-7),                 # 往左瞟一眼
        _k(dx=0.012, dy=0.004, tilt=7),                   # 往右瞟一眼
        _k(dx=0.004, dy=-0.006, squash=-0.020, tilt=-3),  # 偷笑，撑一下
        _k(scale=0.940, dy=0.016, squash=0.060),          # 慌忙缩起来藏
        _k(),                                              # 装无辜
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
REACTIONS: Dict[str, str] = {
    "click": "greet",         # 单击它一下：抬手跟你打个招呼（原来是 wave，见下面那行）
    "double": "poke",         # 双击它一下：像被戳了似的惊跳（暂停 / 继续看也走这一下）
    "greet": "greet",
    "wave": "wave",           # 老名字留着：以前"点一下"走的就是它
    "poke": "poke",           # 戳它
    "agree": "nod",           # 应一声 / 答应
    "deny": "shake",          # 不赞同、没听明白
    "cheer": "cheer",         # 高兴一下
    "rest": "stretch",        # 坐久了伸个懒腰
    "eat": "eat",             # 喂它一口
    "slack": "slack",         # 摸会儿鱼
    "confused": "confused",   # 没看懂
}

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

    * 手写了 `keys` 就带上（`keys_loop` 说明是"转一圈"还是"走一遍"）；
    * 有 `deco` 就带上（画在帧上的小提示符号）；
    * `motion` 里的抖法照旧（没写 keys 的那几条靠它）。
    """
    spec: Dict[str, object] = dict(pose.motion)
    if pose.keys:
        spec["keys"] = [dict(frame) for frame in pose.keys]
        spec["keys_loop"] = bool(pose.loop)
    if pose.deco:
        spec["deco"] = pose.deco
    return spec


def motions() -> Dict[str, Dict[str, object]]:
    """状态 → 生成这套帧用的参数（`tools/make_pet.py` 靠它写 `<状态名>_NN.png`）。

    只返回**真给了参数**的那些：idle / talk 的帧早就有了，不用重做。
    """
    return {pose.key: spec_of(pose) for pose in POSES if pose.motion or pose.keys}


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
