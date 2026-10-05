"""形象渲染。

* 默认（assets/pet 里没有图片时）用内置的矢量占位形象「黄豆」：
  呼吸、眨眼、说话、思考都有动画。
* 你把形象图丢进 assets/pet/ 后会自动切换：
      idle_0.png idle_1.png ...   待机帧
      talk_0.png talk_1.png ...   说话帧（没有就用待机帧代替）
  帧按文件名排序播放，PNG 透明底最佳，建议正方形、256px 以上。
* 如果 assets/pet 里还有 eye_layer.png + eye_layer.json（tools/make_pet.py
  找到眼睛时会自动生成），就会把眼珠单独叠上去，并按 draw(..., gaze=...)
  传进来的鼠标方向挪一点——也就是「眼睛跟着鼠标转」。
* 如果还有 hand_layer.png + hand_layer.json（找到搭在下巴上的那只手时会生成），
  就会把食指单独叠上去，隔一会儿轻轻抬一下再落回去——摸下巴想事情的样子。
* 两只串门一起玩的时候会做动作（见 pet/play.py）：动作名就是帧名前缀，形如
  highfive_00.png / hug_01.png……有帧就按动作进度播那套帧，没有就退回"蹦一下"
  （见 act_lift）——**缺帧不会崩，只是不做那个动作**。
* 动作帧和日常状态帧（见 pet/states.py）**不参与**"统一内容框"的计算（见 _fit_to_window）：
  它们跟基础帧共用同一个缩放，冒出去的那点**允许被圆切掉**。让它们进来会把框撑大，
  于是"站着不动"的时候人也跟着变小——得不偿失。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QRegion,
    QTransform,
)

from . import mood as mood_mod
from . import play as play_mod
from . import states as states_mod
from . import style as style_mod

# 长相（颜色 / 身形 / 五官）**不在这儿写死**：一份定义在 pet/style.py，用户能在
# config.json 的 `ui.pet_style` 里改，也能用 tools/design_pet.py 现调现看。
# 早先这里是 BODY_TOP / BODY_BOTTOM / OUTLINE / EYE / BLUSH / ACCENT 六个常量，
# 现在它们由 PetStyle 接管（`classic` 预设就是原来那六个值）——别再往这儿加回来。

# 每种情绪对应的动作与表情：弹跳幅度/频率、身体倾斜、眼睛与嘴的画法、主题色混入比例
# tempo = 一个"呼吸周期"相对待机的倍率（<1 更快、>1 更慢），决定整套帧转一圈要多久
MOOD_STYLE: dict = {
    "happy": {"bounce": 0.022, "speed": 2.6, "tilt": 0.0, "eyes": "arc", "mouth": "grin", "tint": 0.20, "tempo": 0.45},
    "speechless": {"bounce": 0.005, "speed": 0.9, "tilt": 5.0, "eyes": "line", "mouth": "flat", "tint": -0.30, "tempo": 1.30},
    "excited": {"bounce": 0.042, "speed": 5.0, "tilt": 0.0, "eyes": "wide", "mouth": "open", "tint": 0.32, "tempo": 0.26},
    "curious": {"bounce": 0.010, "speed": 1.6, "tilt": -7.0, "eyes": "look", "mouth": "small", "tint": 0.16, "tempo": 0.72},
    "smirk": {"bounce": 0.008, "speed": 1.3, "tilt": -2.5, "eyes": "smirk", "mouth": "smirk", "tint": 0.0, "tempo": 0.86},
    "surprised": {"bounce": 0.040, "speed": 4.6, "tilt": -3.0, "eyes": "wide", "mouth": "open", "tint": 0.28, "tempo": 0.10},
    "angry": {"bounce": 0.030, "speed": 4.0, "tilt": 0.0, "eyes": "wide", "mouth": "flat", "tint": 0.34, "tempo": 0.06},
    "sad": {"bounce": 0.004, "speed": 0.8, "tilt": 6.0, "eyes": "line", "mouth": "flat", "tint": -0.24, "tempo": 0.90},
}
NEUTRAL_STYLE = {"bounce": 0.006, "speed": 1.0, "tilt": 0.0, "eyes": "round", "mouth": "smile", "tint": 0.0, "tempo": 1.0}

# 说话的时候整体快一点（帧循环周期乘这个数）
TALK_TEMPO = 0.55

# 图片帧最终是画进一个**圆形**窗口里的，圆的四角是空的。形象要是画满整张画布，
# 底部的脚和身体两侧就会掉到圆外面——被遮罩切成一条直边（现场就是这样：鞋只剩一半）。
# 所以装帧的时候统一做一次「裁掉透明边 → 缩到圆里放得下 → 居中」：
# 缩放按**内容外接矩形**算，让矩形的四个角刚好落在圆上（留 2% 余量）——
# 瘦长的人物就能画得尽量大，矮胖的自动收一点，反正都不会被圆切到。
FRAME_SAFE = 0.98        # 内容外接矩形的角，最多放到窗口内接圆半径的多少
FRAME_MAX_ZOOM = 4.0     # 内容特别小的图最多放大这么多倍（再放就糊了）
FRAME_CANVAS = 256       # 归一化后的画布边长；一套动效十几帧，画布再大就很吃内存了
FRAME_CANVAS_MAX = 384


# ---------- 部件层（眼珠跟鼠标 / 食指敲下巴） ----------
#
# make_pet.py 生成形象时会顺手把某些小部件抠成单独一层，并把「部件位置 + 每帧的
# 身体变换矩阵」写进同名 json。这里的任务就一件：把那一层按同一个变换叠回帧上，
# 再额外挪一点点（眼珠按鼠标方向，食指按时间轻轻敲）。
EYE_STEM = "eye_layer"      # eye_layer.png + eye_layer.json
HAND_STEM = "hand_layer"    # hand_layer.png + hand_layer.json

# 仿射矩阵统一写成 (a, b, c, d, e, f)，含义和 PIL 一致：
#     x' = a·x + b·y + c        y' = d·x + e·y + f
# Qt 的 QTransform 参数顺序不一样，所以下面自己算，不依赖任何库的乘法约定。
Affine = Tuple[float, float, float, float, float, float]

# 单位矩阵：部件"一点都不动"就用它（眼珠不动 / 手指贴着下巴没在敲的时候）
IDENTITY: Affine = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def _affine_mul(outer: Affine, inner: Affine) -> Affine:
    """先套 inner、再套 outer（写成数学就是 outer ∘ inner）。"""
    a, b, c, d, e, f = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a * a2 + b * d2,
        a * b2 + b * e2,
        a * c2 + b * f2 + c,
        d * a2 + e * d2,
        d * b2 + e * e2,
        d * c2 + e * f2 + f,
    )


def _affine_inv(matrix: Affine) -> Affine:
    """逆矩阵（找不到就退回单位矩阵，宁可不动也别画飞）。"""
    a, b, c, d, e, f = matrix
    det = a * e - b * d
    if abs(det) < 1e-9:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    ia, ib = e / det, -b / det
    idd, ie = -d / det, a / det
    return (ia, ib, -(ia * c + ib * f), idd, ie, -(idd * c + ie * f))


def _affine_qt(matrix: Affine) -> QTransform:
    """(a,b,c,d,e,f) → QTransform；Qt 的顺序是 (m11, m12, m21, m22, dx, dy)。"""
    a, b, c, d, e, f = matrix
    return QTransform(a, d, b, e, c, f)


def _gaze_offset(gaze: Tuple[float, float], travel: float) -> Tuple[float, float]:
    """把鼠标方向（每轴 -1~1）换算成画布上眼珠的位移；超出单位圆的按圆裁一下。"""
    if not travel:
        return (0.0, 0.0)
    dx, dy = float(gaze[0]), float(gaze[1])
    length = math.hypot(dx, dy)
    if length > 1.0:
        dx, dy = dx / length, dy / length
    return (dx * travel, dy * travel)


def _tap_offset(t: float, period: float, lift: float) -> Tuple[float, float]:
    """（旧）食指轻轻敲一下的位移：整块贴片往上挪。

    现在已经不用它了，换成下面的 _tap_matrix：整块挪会让贴片下沿和手之间裂开一条缝，
    看着像手在自己伸缩。留着是为了老的 hand_layer.json（没有 box，没法算锚点）还能动。
    """
    if lift <= 0.0 or period <= 0.0:
        return (0.0, 0.0)
    phase = (t / period) % 1.0
    bump = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
    return (0.0, -lift * bump)


def _translate(dx: float, dy: float) -> Affine:
    """平移写成仿射矩阵，和 _tap_matrix 一起喂给 _draw_part。"""
    return (1.0, 0.0, dx, 0.0, 1.0, dy)


# 食指敲下巴的节奏：一个周期里**轻轻敲两下**，其余时间贴着下巴不动。
# 以前是一整圈只做一个缓慢的起伏（半余弦），看起来像手在自己伸缩——敲击是"快起快落 + 停"，
# 不是"慢慢鼓起来再瘪回去"，所以这里把抬起量压进两小段窄窗口里。
TAP_BEATS = (0.25, 0.45)  # 两下敲在周期的什么位置（0~1）；周期头尾都是"贴着下巴不动"
TAP_DUTY = 0.18           # 每一下占周期的多少（0.18 × 2.4s ≈ 0.43s，起落都是软的）


def _tap_curve(phase: float) -> float:
    """转过 phase（0~1）这一刻的抬起量：0 = 贴着下巴，1 = 抬到最高。"""
    best = 0.0
    for center in TAP_BEATS:
        span = (phase - center) % 1.0
        if span > 0.5:
            span -= 1.0                      # 折到 -0.5~0.5，越界的敲击也能接上
        step = span / TAP_DUTY
        if abs(step) < 0.5:
            best = max(best, 0.5 + 0.5 * math.cos(2.0 * math.pi * step))
    return best


def _tap_matrix(t: float, period: float, lift: float, box: Sequence[int]) -> Affine:
    """食指轻轻抬一下：**以指根为锚点，把这一层往上纵向拉伸**一点点。

    为什么不是整块往上挪：贴片的下沿压在指根/拳头上，整块挪会在下沿和手之间露出
    一条缝（补色的那块），看起来就是手在自己伸缩。改成绕指根拉伸之后——
    指根那一行原地不动，越靠近指尖抬得越多，底下**永远连着**，也不会出现缝。

    box 是贴片在画布上的位置（hand_layer.json 里的 box）；没有它（老资产）就退回整块平移。
    """
    if lift <= 0.0 or period <= 0.0:
        return IDENTITY
    bump = _tap_curve((t / period) % 1.0)
    if bump <= 0.0:
        return IDENTITY
    if not box or len(box) < 4:
        return _translate(*_tap_offset(t, period, lift))
    height = max(1.0, float(box[3]) - float(box[1]))
    scale = 1.0 + (lift * bump) / height      # 贴片顶边正好抬起 lift 像素
    anchor = float(box[3])                    # 指根那一行（贴片下沿）
    return (1.0, 0.0, 0.0, 0.0, scale, anchor * (1.0 - scale))


def _read_part(folder: Path, stem: str) -> Tuple[Optional[QPixmap], dict]:
    """读一个部件层（<stem>.png + <stem>.json）；缺文件 / 没有变换表就当没这个部件。"""
    layer_path = folder / f"{stem}.png"
    meta_path = folder / f"{stem}.json"
    if not layer_path.exists() or not meta_path.exists():
        return None, {}
    pixmap = QPixmap(str(layer_path))
    if pixmap.isNull():
        return None, {}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:                       # 元数据坏了就当没有，别把挂件拖崩
        print(f"[sprite] 读不了 {meta_path.name}：{exc}")
        return None, {}
    if not (meta.get("warp") or {}):
        return None, {}
    return pixmap, meta


def _warp_table(meta: dict) -> Dict[str, List[Tuple[float, ...]]]:
    """把 json 里的「每帧身体变换矩阵」整理成 {动作名: [(a,b,c,d,e,f), ...]}。"""
    return {
        str(name): [tuple(float(value) for value in matrix) for matrix in matrices]
        for name, matrices in (meta.get("warp") or {}).items()
    }


def style_for(mood: str) -> dict:
    return MOOD_STYLE.get(mood_mod.normalize(mood), NEUTRAL_STYLE)


def _tint(color: QColor, mood: str, amount: float) -> QColor:
    """把身体颜色朝情绪主题色靠一点；amount 为负表示去饱和（情绪低落/无语）。"""
    if not amount:
        return color
    target = QColor(mood_mod.color(mood))
    if amount < 0:
        gray = (color.red() + color.green() + color.blue()) / 3.0
        base = QColor(int(gray), int(gray), int(gray))
        return QColor(
            int(base.red() + (color.red() - base.red()) * (1 + amount)),
            int(base.green() + (color.green() - base.green()) * (1 + amount)),
            int(base.blue() + (color.blue() - base.blue()) * (1 + amount)),
        )
    ratio = max(0.0, min(1.0, amount))
    return QColor(
        int(color.red() * (1 - ratio) + target.red() * ratio),
        int(color.green() * (1 - ratio) + target.green() * ratio),
        int(color.blue() * (1 - ratio) + target.blue() * ratio),
    )



# 做动作时最多跳多高（占身高的比例）——见 act_lift()
ACT_LIFT = 0.12


def act_lift(act: str, act_t: float, rect: QRectF) -> float:
    """做动作时上下蹦的那点位移（0 = 不挪）。

    这是**没有动作帧时的兜底**（见 pet/play.py）：`act` 是动作名（highfive / hug …），
    `act_t` 是 0→1 的进度，由窗口在 paintEvent 里按"动作播了多久"算好传进来。
    `assets/pet/` 里有 `<动作名>_*.png` 那套帧时，`_draw_frames` 会优先播那套帧，
    这里返回 0（见 PetRenderer.draw）——两个一起上会像在蹦床。
    """
    if not act:
        return 0.0
    t = min(1.0, max(0.0, float(act_t)))
    return -math.sin(math.pi * t) * float(rect.height()) * ACT_LIFT


class PetRenderer:
    def __init__(
        self,
        assets_dir: Optional[Path] = None,
        fps: float = 8.0,
        period: float = 30.0,
        crossfade: bool = True,
        tap: bool = True,
        style: Optional[style_mod.PetStyle] = None,
        prefer_vector: bool = False,
    ):
        # fps 是老的帧率参数，只留个兼容位：现在动快慢由"呼吸周期"period 决定，
        # 一整套帧在 period 秒里转完一圈（越慢越像呼吸，越快越像兴奋）。
        self.fps = fps
        self.period = max(0.4, float(period))
        self.crossfade = bool(crossfade)
        # 食指敲下巴：形象带 hand_layer.* 时才有效（见 ui.hand_tap）
        self.tap = bool(tap)
        # 「长什么样」：颜色 / 身形 / 五官。没传就是原来那套黄豆（见 pet/style.py）。
        # 装了图片帧时它也用得上：帧缺了哪种情绪、或者用户把 prefer_vector 打开了，
        # 画的都是这一套——所以帧和矢量共用一个长相，不会一边一个样。
        self.pet_style = style or style_mod.PetStyle()
        # 就算 assets/pet 里有帧也先用矢量形象（设计器"先看看"用；见 ui.prefer_vector）
        self.prefer_vector = bool(prefer_vector)
        self.frames_idle: List[QPixmap] = []
        self.frames_talk: List[QPixmap] = []
        self.frames_moods: Dict[str, List[QPixmap]] = {}
        # 动作帧（串门时两只一起玩，见 pet/play.py）：动作名 → 一套帧。
        # 没配帧的动作不在里面，那时退回 act_lift 那个"蹦一下"。
        self.frames_acts: Dict[str, List[QPixmap]] = {}
        # 缩放结果按 (哪套帧, 第几张, 目标尺寸) 缓存：每帧都重缩放的话画面会一直抖
        self._scaled_cache: Dict[tuple, QPixmap] = {}
        # 部件层：没有对应的 eye_layer.* / hand_layer.* 就是 None，一切照旧
        self.eye_layer: Optional[QPixmap] = None
        self.eye_canvas = float(FRAME_CANVAS)
        self.eye_travel = 0.0
        self._eye_warp: Dict[str, List[Tuple[float, ...]]] = {}
        self.hand_layer: Optional[QPixmap] = None
        self.hand_canvas = float(FRAME_CANVAS)
        self.hand_lift = 0.0
        self.hand_period = 0.0
        self.hand_box: List[float] = []       # 食指贴片的位置（画布坐标），敲的时候绕它的下沿转
        self._hand_warp: Dict[str, List[Tuple[float, ...]]] = {}
        self._eye_box = QRect()
        self._eye_fit: Tuple[float, float, float, float] = (1.0, 1.0, 0.0, 0.0)
        if assets_dir and not self.prefer_vector:
            folder = Path(assets_dir)
            self.frames_idle = _load_frames(folder, "idle")
            self.frames_talk = _load_frames(folder, "talk") or self.frames_idle
            # 可选：给每种情绪准备一套帧，命名如 happy_0.png / excited_1.png。
            # 按 mood.MOODS 全量认（不光 MOOD_STYLE 里那几个）：新加一种情绪、
            # 又给它画了帧时，不用回来改这里就能切过去。
            for name in dict.fromkeys([*MOOD_STYLE, *mood_mod.MOODS]):
                frames = _load_frames(folder, name)
                if frames:
                    self.frames_moods[name] = frames
            # 动作帧：名字就是动作表里的 key（见 pet/play.py 的 MOVES）
            for name in (move.key for move in play_mod.MOVES):
                frames = _load_frames(folder, name)
                if frames:
                    self.frames_acts[name] = frames
            # 日常状态 / 界面互动的帧（见 pet/states.py）：跟动作帧**共用这一张表**——
            # 都是 `<名字>_*.png`，区别只在窗口那边"播一遍"还是"循环"。
            # idle / talk 跳过：它们本来就是基础帧，别在这里盖一层。
            for name in (pose.key for pose in states_mod.POSES if pose.key not in ("idle", "talk")):
                if name in self.frames_acts:
                    continue
                frames = _load_frames(folder, name)
                if frames:
                    self.frames_acts[name] = frames
            self._fit_to_window()
            self._load_parts(folder)

    def _load_parts(self, folder: Path) -> None:
        """读各个部件层和它们的元数据；缺哪个就少哪个部件（没有也能照常跑）。"""
        pixmap, meta = _read_part(folder, EYE_STEM)
        if pixmap is not None:
            self.eye_layer = pixmap
            self.eye_canvas = float(meta.get("canvas") or pixmap.width())
            self.eye_travel = float(meta.get("travel") or 0.0)
            self._eye_warp = _warp_table(meta)
        pixmap, meta = _read_part(folder, HAND_STEM)
        if pixmap is not None:
            self.hand_layer = pixmap
            self.hand_canvas = float(meta.get("canvas") or pixmap.width())
            self.hand_lift = float(meta.get("lift") or 0.0)
            self.hand_period = float(meta.get("period") or 0.0)
            self.hand_box = [float(value) for value in (meta.get("box") or [])]
            self._hand_warp = _warp_table(meta)

    @property
    def uses_eyes(self) -> bool:
        """当前形象有没有"会看人的眼珠"（窗口靠它决定要不要盯着鼠标）。"""
        return self.eye_layer is not None and bool(self._eye_warp)

    @property
    def uses_hand(self) -> bool:
        """当前形象有没有"会轻敲下巴的食指"。"""
        return self.hand_layer is not None and bool(self._hand_warp)

    def set_style(self, style: style_mod.PetStyle) -> None:
        """换一套长相（设计器边调边看就靠它，改完立刻生效，不用重启）。

        图片帧一概不动：帧是画死的一张张图，矢量形象才是照着 `pet_style` 现画的。
        两者共用这一份定义（帧缺情绪时也回落到它），所以这里只换这份定义就够。
        """
        self.pet_style = style or style_mod.PetStyle()

    def _fit_to_window(self) -> None:
        """把图片帧统一裁边 + 缩到圆内安全区（见 FRAME_SAFE 的说明）。

        基础帧（待机 / 说话 / 各情绪）一起算出**同一个**内容框、同一个缩放比例，
        这样换情绪的时候人不会突然变大变小。没装图片帧就什么都不做。

        动作帧 / 日常状态帧（`frames_acts`）**不参与**这个框：蹦到高处、伸个懒腰、
        睡觉蜷成一团，本来就该往外冒一点。让它们进来会把框撑大，于是"站着不动"
        的时候人也跟着变小——所以它们只跟着基础帧的框走，冒出去的那点**允许被
        窗口圆边切掉**（见 _fit_frames 的 overflow）。
        """
        base: List[List[QPixmap]] = []
        acts: List[List[QPixmap]] = []
        seen = set()
        for frames in [self.frames_idle, self.frames_talk, *self.frames_moods.values()]:
            if frames and id(frames) not in seen:
                seen.add(id(frames))
                base.append(frames)
        for frames in self.frames_acts.values():
            if frames and id(frames) not in seen:
                seen.add(id(frames))
                acts.append(frames)
        flat = [pm for frames in base for pm in frames]
        if not flat:
            return
        # 画布大小还是看所有帧：帧本来就是同一个尺寸生成出来的，这里只是不想漏
        longest = max(
            max(pm.width(), pm.height())
            for pm in flat + [pm for frames in acts for pm in frames]
        )
        box = _content_box(base)
        canvas = max(FRAME_CANVAS, min(FRAME_CANVAS_MAX, longest))
        # 眼珠层要套同一份「裁框 + 缩放 + 居中」，所以把参数记下来
        content_w = max(1, box.width() if not box.isNull() else flat[0].width())
        content_h = max(1, box.height() if not box.isNull() else flat[0].height())
        self._eye_box = QRect(box)
        self._eye_fit = _fit_mapping(content_w, content_h, canvas)
        for frames in base:
            frames[:] = _fit_frames(frames, box, canvas, self._eye_fit)
        for frames in acts:
            frames[:] = _fit_frames(frames, box, canvas, self._eye_fit, overflow=True)

    @property
    def uses_frames(self) -> bool:
        return bool(self.frames_idle)

    def act_has_frames(self, act: str) -> bool:
        """这个动作有没有自己的帧（有就按进度播它，别再叠 act_lift 那一蹦）。"""
        return str(act or "").strip() in self.frames_acts

    def icon(self, size: int = 32) -> QPixmap:
        """给托盘用的图标。"""
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        self.draw(painter, QRectF(0, 0, size, size), t=0.0, talking=False)
        painter.end()
        return pm

    # ---------- 主入口 ----------

    def draw(
        self,
        painter: QPainter,
        rect: QRectF,
        t: float,
        talking: bool = False,
        thinking: bool = False,
        blink: bool = False,
        mood: str = "",
        gaze: Tuple[float, float] = (0.0, 0.0),
        act: str = "",
        act_t: float = 0.0,
        act_loop: bool = False,
        act_period: float = 0.0,
    ) -> None:
        """画一帧。

        act / act_t 是"正在做的动作"（见 pet/play.py）：动作名 + 0→1 的进度。
        装了这一套动作帧（`assets/pet/<动作名>_*.png`）就按进度播它，
        没有就退回"整体蹦一下"（见 act_lift）——先让"在玩"看得见。

        act_loop=True 是**日常状态**（走路 / 坐下 / 睡觉…，见 pet/states.py）：
        这时候不看 act_t，而是按时间一直循环；转完一轮的秒数由 act_period 定。
        """
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        name = str(act or "").strip()
        if act_loop and not self.act_has_frames(name):
            # 日常状态没配帧：不能一直拿"蹦一下"当走路 / 睡觉（那是"在做动作"的兜底），
            # 老实退回待机 / 说话那套基础帧。
            act, name, act_loop, act_period = "", "", False, 0.0
        # 有动作帧就别再叠那一下蹦：帧里已经是这个动作了，两个一起上会像在蹦床。
        lift = 0.0 if self.act_has_frames(name) else act_lift(name, act_t, rect)
        if lift:
            moved = QRectF(rect)
            moved.translate(0.0, lift)
            rect = moved
        if self.uses_frames:
            self._draw_frames(
                painter, rect, t, talking, mood, gaze, act, act_t,
                act_loop=act_loop, act_period=act_period,
            )
        else:
            self._draw_vector(painter, rect, t, talking, thinking, blink, mood)

    # ---------- 图片帧 ----------

    def _draw_frames(
        self,
        painter: QPainter,
        rect: QRectF,
        t: float,
        talking: bool,
        mood: str = "",
        gaze: Tuple[float, float] = (0.0, 0.0),
        act: str = "",
        act_t: float = 0.0,
        act_loop: bool = False,
        act_period: float = 0.0,
    ) -> None:
        """播图片帧。

        两个关键点，都是为了「不闪」：
        * **交叉淡化**：不是到点硬切下一张，而是在相邻两张之间渐变过去，
          所以 12 张帧也能走出连续的呼吸感，不会有"一秒抖一下"的顿挫。
        * **缩放只做一次 + 位置对齐整像素**：每帧重新缩放、再按亚像素位置画，
          在 125% 缩放的屏幕上会一直重采样，看起来就是边上在闪。

        act / act_t 是"正在做的动作"（见 pet/play.py 和 sprite.act_lift）：
        `assets/pet` 里放了 `<动作名>_*.png` 那套帧，就在这儿按 act 挑帧、
        用 act_t 在这套帧里走一遍（**不循环**：动作是一段，不是呼吸）；
        挑不到就照下面挑 mood/talk/idle——**没有帧也不会崩，只是不做动作**。

        act_loop=True 是**日常状态**（见 pet/states.py：走路 / 坐下 / 睡觉…）：
        这时候不看 act_t，改成按时间一轮一轮转，转完的秒数由 act_period 定
        （走路那种"一直在走"的样子全靠它，否则走完一遍就定住了）。
        """
        key = mood_mod.normalize(mood) if mood else ""
        name = key if key in self.frames_moods else ""
        frames = self.frames_moods.get(name) or []
        act_name = str(act or "").strip()
        act_frames = self.frames_acts.get(act_name) or []
        if act_frames:
            frames, name = act_frames, act_name
        if not frames:
            if talking and self.frames_talk and self.frames_talk is not self.frames_idle:
                frames, name = self.frames_talk, "talk"
            else:
                frames, name = self.frames_idle, "idle"
        if not frames:
            return

        if act_frames:
            if act_loop:
                # 日常状态：按时间一轮一轮转（走路 / 坐下 / 睡觉…），转完接着来
                turn = float(act_period) if float(act_period) > 0 else float(self.period)
                position = (t / max(0.35, turn) % 1.0) * len(frames)
            else:
                # 动作帧：进度说了算（0→1 走一遍就停，不来回呼吸）
                span = float(max(1, len(frames) - 1))
                position = min(span, max(0.0, float(act_t)) * span)
        else:
            style = style_for(mood) if mood else NEUTRAL_STYLE
            # 一个呼吸周期 = period × 这套动作的节奏；说话时整体快一点
            period = max(0.35, self.period * float(style.get("tempo", 1.0)))
            if talking:
                period *= TALK_TEMPO

            position = (t / period % 1.0) * len(frames)
        first = int(position) % len(frames)
        second = (first + 1) % len(frames)
        blend = position - int(position)

        dpr = _device_ratio(painter)
        width = max(8.0, rect.width())
        height = max(8.0, rect.height())
        try:
            base_opacity = float(painter.opacity())   # 调用者可能已经设过透明度
        except Exception:
            base_opacity = 1.0

        if self.crossfade and blend > 0.002:
            painter.setOpacity(base_opacity * (1.0 - blend))
            self._draw_frame(painter, frames, first, rect, width, height, dpr, name, gaze, t)
            painter.setOpacity(base_opacity * blend)
            self._draw_frame(painter, frames, second, rect, width, height, dpr, name, gaze, t)
            painter.setOpacity(base_opacity)
        else:
            self._draw_frame(painter, frames, first, rect, width, height, dpr, name, gaze, t)

    def _draw_frame(
        self,
        painter: QPainter,
        frames: List[QPixmap],
        index: int,
        rect: QRectF,
        width: float,
        height: float,
        dpr: float,
        motion: str = "",
        gaze: Tuple[float, float] = (0.0, 0.0),
        t: float = 0.0,
    ) -> None:
        pixmap = self._scaled(frames, index, width, height, dpr)
        if pixmap is None:
            return
        logical_w = pixmap.width() / dpr
        logical_h = pixmap.height() / dpr
        x = rect.center().x() - logical_w / 2.0
        y = rect.center().y() - logical_h / 2.0
        painter.drawPixmap(
            QPointF(round(x * dpr) / dpr, round(y * dpr) / dpr),
            pixmap,
        )
        # 部件层（眼珠、食指）叠在刚画好的这一帧上，用同一套身体变换
        self._draw_eyes(painter, index, rect, logical_w, logical_h, motion, gaze)
        self._draw_hand(painter, index, rect, logical_w, logical_h, motion, t)

    def _draw_part(
        self,
        painter: QPainter,
        pixmap: QPixmap,
        warp: Affine,
        canvas: float,
        extra: Affine,
        rect: QRectF,
        drawn_w: float,
        drawn_h: float,
    ) -> None:
        """把某一层按「这一帧的身体变换 + 一点额外变换」叠到刚画好的帧上。

        整条链路：先把预揉的形变倒回去（帧是形变后的样子，部件层是原图坐标），
        再补回这一帧的裁框原点、按帧的缩放居中，最后按画面上的实际尺寸落到 painter 里。
        extra 是部件自己的小动作（眼珠按鼠标方向平移、食指绕指根拉伸），写在原图坐标里。
        整套变换都在 _affine_* 里自己算，不碰任何库的乘法约定。
        """
        fit_sx, fit_sy, fit_x, fit_y = self._eye_fit
        box = self._eye_box
        left = float(box.x()) if not box.isNull() else 0.0
        top = float(box.y()) if not box.isNull() else 0.0
        span = max(1.0, canvas)
        to_frame = _affine_mul(
            (fit_sx, 0.0, fit_x - left * fit_sx, 0.0, fit_sy, fit_y - top * fit_sy),
            _affine_inv(warp),
        )
        to_painter = (
            drawn_w / span, 0.0, rect.center().x() - drawn_w / 2.0,
            0.0, drawn_h / span, rect.center().y() - drawn_h / 2.0,
        )
        total = _affine_mul(to_painter, _affine_mul(to_frame, extra))
        painter.save()
        painter.setWorldTransform(_affine_qt(total))
        painter.drawPixmap(0, 0, pixmap)
        painter.restore()

    def _draw_eyes(
        self,
        painter: QPainter,
        index: int,
        rect: QRectF,
        drawn_w: float,
        drawn_h: float,
        motion: str,
        gaze: Tuple[float, float],
    ) -> None:
        """眼珠按鼠标方向挪一点（鼠标不动时和原图一模一样）。"""
        if self.eye_layer is None or not motion:
            return
        matrices = self._eye_warp.get(motion)
        if not matrices:
            return
        dx, dy = _gaze_offset(gaze, self.eye_travel)
        self._draw_part(
            painter,
            self.eye_layer,
            matrices[index % len(matrices)],
            self.eye_canvas,
            _translate(dx, dy),
            rect,
            drawn_w,
            drawn_h,
        )

    def _draw_hand(
        self,
        painter: QPainter,
        index: int,
        rect: QRectF,
        drawn_w: float,
        drawn_h: float,
        motion: str,
        t: float,
    ) -> None:
        """食指轻轻抬一下再落回去——摸着下巴想事情的样子（见 _tap_matrix）。"""
        if self.hand_layer is None or not motion or not self.tap:
            return
        matrices = self._hand_warp.get(motion)
        if not matrices:
            return
        self._draw_part(
            painter,
            self.hand_layer,
            matrices[index % len(matrices)],
            self.hand_canvas,
            _tap_matrix(t, self.hand_period, self.hand_lift, self.hand_box),
            rect,
            drawn_w,
            drawn_h,
        )

    def _scaled(
        self,
        frames: List[QPixmap],
        index: int,
        width: float,
        height: float,
        dpr: float,
    ) -> Optional[QPixmap]:
        """按目标尺寸缩好并存起来；同一尺寸第二次直接拿缓存（画面才不会一直重采样）。"""
        key = (id(frames), index, int(round(width)), int(round(height)))
        cached = self._scaled_cache.get(key)
        if cached is not None:
            return cached
        source = frames[index]
        if source.isNull():
            return None
        scaled = source.scaled(
            max(1, int(round(width * dpr))),
            max(1, int(round(height * dpr))),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        if len(self._scaled_cache) > 320:      # 尺寸变了（换屏幕/改大小）就整体重来
            self._scaled_cache.clear()
        self._scaled_cache[key] = scaled
        return scaled

    # ---------- 内置矢量形象 ----------

    def _face_style(self, mood: str) -> dict:
        """这一帧的**五官画法**（眼睛 / 嘴）。

        MOOD_STYLE 里那几套是"表情"，不算长相；设计器挑的眼睛和嘴只替换**待机**那一帧。
        想让喜怒哀乐也照它画（比如整套都是弯月眼），把 `face_locked` 打开。
        """
        base = dict(style_for(mood)) if mood else dict(NEUTRAL_STYLE)
        design = self.pet_style
        if design.face_locked or not mood:
            if design.eyes:
                base["eyes"] = design.eyes
            if design.mouth:
                base["mouth"] = design.mouth
        return base

    def _draw_vector(
        self,
        painter: QPainter,
        rect: QRectF,
        t: float,
        talking: bool,
        thinking: bool,
        blink: bool,
        mood: str = "",
    ) -> None:
        pet = self.pet_style
        # 动作 / 节奏照样跟着情绪走（弹跳幅度、歪头、快慢、明暗），
        # 长相（颜色 / 身形 / 五官）从 pet_style 拿——两件事分开了。
        style = style_for(mood) if mood else NEUTRAL_STYLE
        face = self._face_style(mood)
        speed = float(style["speed"]) * (1.5 if talking else 1.0)
        amplitude = float(style["bounce"]) + (0.014 if talking else 0.0)

        outline = pet.qcolor("outline")
        accent = pet.qcolor("accent")
        ink = pet.qcolor("eye")

        painter.save()
        # 情绪驱动的整体动作：上下弹跳 + 歪头
        painter.translate(0.0, -abs(math.sin(t * speed)) * amplitude * rect.height() * 2.0)
        tilt = float(style["tilt"]) * (1.0 + 0.35 * math.sin(t * 1.1))
        if tilt:
            painter.translate(rect.center().x(), rect.bottom())
            painter.rotate(tilt)
            painter.translate(-rect.center().x(), -rect.bottom())

        s = min(rect.width(), rect.height())
        cx = rect.center().x()
        # 呼吸只留一点点起伏（和 make_pet.py 的 MOTIONS 一个口径：看得出在动，但不"喘"）
        breathe = math.sin(t * 1.8)
        body_w = s * pet.body_width * (1.0 + 0.008 * breathe)
        body_h = s * pet.body_height * (1.0 - 0.012 * breathe)
        top = rect.top() + s * 0.22 + breathe * s * 0.0015
        bottom = top + body_h

        # 地面阴影（能关掉：渲染成图片帧时它会被抠图当成背景吃掉，见 tools/design_pet.py）
        if pet.shadow:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 46))
            painter.drawEllipse(
                QRectF(cx - body_w * 0.38, bottom - s * 0.02, body_w * 0.76, s * 0.075)
            )

        # 触角（也能关掉：想画个光溜溜的团子时）
        # 长度和位置照算不误：思考泡泡挂在它上面，关了触角泡泡还在原来的高度。
        antenna_len = s * 0.13 + math.sin(t * 2.2) * s * 0.012
        tip = QPointF(cx + math.sin(t * 1.3) * s * 0.03, top - antenna_len)
        if pet.antenna:
            painter.setPen(QPen(outline, max(1.6, s * 0.017)))
            painter.drawLine(QPointF(cx, top + s * 0.01), tip)
            glow = QRadialGradient(tip, s * 0.06)
            glow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 230))
            glow.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(tip, s * 0.06, s * 0.06)

        # 身体：形状是设计器挑的（史莱姆的波浪裙边 / 圆团子 / 方块 / 水滴）
        path = _body_path(pet.body_shape, cx, top, bottom, body_w, body_h, s)

        gradient = QLinearGradient(QPointF(0, top), QPointF(0, bottom))
        gradient.setColorAt(0.0, _tint(pet.qcolor("body_top"), mood, float(style["tint"])))
        gradient.setColorAt(1.0, _tint(pet.qcolor("body_bottom"), mood, float(style["tint"])))
        painter.setBrush(gradient)
        painter.setPen(QPen(outline, max(1.6, s * 0.018)))
        painter.drawPath(path)

        # 高光
        if pet.highlight:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 110))
            painter.drawEllipse(
                QRectF(cx - body_w * 0.30, top + body_h * 0.12, body_w * 0.24, body_h * 0.20)
            )

        # 眼睛
        eye_dx = body_w * 0.20
        eye_y = top + body_h * 0.42
        eye_r = s * 0.052
        look = math.sin(t * 0.7) * eye_r * 0.35
        _draw_eyes(painter, face, cx, eye_dx, eye_y, eye_r, look, blink, s, t, ink)

        # 腮红
        if pet.blush_on:
            blush = pet.qcolor("blush")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(blush.red(), blush.green(), blush.blue(), 105))
            for side in (-1, 1):
                painter.drawEllipse(
                    QPointF(cx + side * body_w * 0.30, eye_y + body_h * 0.20), s * 0.045, s * 0.028
                )

        # 嘴
        mouth_y = eye_y + body_h * 0.30
        _draw_mouth(painter, face, cx, mouth_y, s, talking, t, ink)

        # 思考泡泡
        if thinking:
            painter.setPen(Qt.PenStyle.NoPen)
            for i in range(3):
                alpha = int(90 + 130 * abs(math.sin(t * 3.4 - i * 0.6)))
                painter.setBrush(QColor(255, 255, 255, alpha))
                painter.drawEllipse(
                    QPointF(cx + (i - 1) * s * 0.11, top - antenna_len - s * 0.06), s * 0.032, s * 0.032
                )
        painter.restore()


def _body_path(
    shape: str,
    cx: float,
    top: float,
    bottom: float,
    body_w: float,
    body_h: float,
    s: float,
) -> QPainterPath:
    """身体的轮廓：形状是设计器挑的（见 pet/style.py 的 BODY_SHAPES）。

    四个形状共用同一套锚点：`top` 是头顶（触角从这儿长出去）、`bottom` 是脚底（地面阴影
    贴这儿）、体宽体高就是 body_w / body_h。换形状只换"腰线怎么走"，五官位置、呼吸幅度、
    阴影一概不用动——所以设计器里换个身形，脸上那套还是原地不动。
    """
    path = QPainterPath()
    if shape == "round":        # 圆团子
        path.addEllipse(QRectF(cx - body_w / 2, top, body_w, body_h))
        return path
    if shape == "square":       # 方块（圆角跟着尺寸走，缩到多小都不走形）
        radius = min(body_w, body_h) * 0.22
        path.addRoundedRect(QRectF(cx - body_w / 2, top, body_w, body_h), radius, radius)
        return path
    if shape == "drop":         # 水滴：上头收成一个尖，下头圆回来
        left, right = cx - body_w / 2, cx + body_w / 2
        path.moveTo(cx, top)
        path.cubicTo(
            cx - body_w * 0.16, top + body_h * 0.26,
            left, top + body_h * 0.56,
            left, bottom - body_h * 0.30,
        )
        path.cubicTo(
            left, bottom - body_h * 0.02,
            cx - body_w * 0.26, bottom,
            cx, bottom,
        )
        path.cubicTo(
            cx + body_w * 0.26, bottom,
            right, bottom - body_h * 0.02,
            right, bottom - body_h * 0.30,
        )
        path.cubicTo(
            right, top + body_h * 0.56,
            cx + body_w * 0.16, top + body_h * 0.26,
            cx, top,
        )
        path.closeSubpath()
        return path

    # slime（默认，也就是原来那套）：圆润的身子 + 底下那圈波浪裙边
    path.moveTo(cx - body_w / 2, bottom)
    path.cubicTo(
        cx - body_w * 0.54, top + body_h * 0.55,
        cx - body_w * 0.30, top,
        cx, top,
    )
    path.cubicTo(
        cx + body_w * 0.30, top,
        cx + body_w * 0.54, top + body_h * 0.55,
        cx + body_w / 2, bottom,
    )
    hump = s * 0.035
    path.quadTo(cx + body_w * 0.375, bottom + hump, cx + body_w * 0.25, bottom)
    path.quadTo(cx + body_w * 0.125, bottom + hump, cx, bottom)
    path.quadTo(cx - body_w * 0.125, bottom + hump, cx - body_w * 0.25, bottom)
    path.quadTo(cx - body_w * 0.375, bottom + hump, cx - body_w / 2, bottom)
    path.closeSubpath()
    return path


def _draw_eyes(
    painter: QPainter,
    style: dict,
    cx: float,
    eye_dx: float,
    eye_y: float,
    eye_r: float,
    look: float,
    blink: bool,
    s: float,
    t: float,
    ink: QColor,
) -> None:
    """按情绪画眼睛：圆眼 / 弯月眼 / 横线眼 / 大眼 / 斜眼。

    `style` 是**这一帧**的表情画法（情绪 + 设计器覆盖，见 PetRenderer._face_style），
    `ink` 是设计器挑的眼珠颜色（线条也用它）。
    """
    kind = str(style.get("eyes", "round"))
    if blink:
        kind = "line"

    for side in (-1, 1):
        ex = cx + side * eye_dx
        painter.setPen(Qt.PenStyle.NoPen)

        if kind == "arc":  # 开心：弯月眼
            pen = QPen(ink, max(2.0, s * 0.020))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(
                QRectF(ex - eye_r, eye_y - eye_r * 0.55, eye_r * 2, eye_r * 1.5), 20 * 16, 140 * 16
            )
            continue
        if kind == "line":  # 无语/眨眼：一条横线
            pen = QPen(ink, max(2.0, s * 0.018))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(ex - eye_r * 0.9, eye_y), QPointF(ex + eye_r * 0.9, eye_y))
            continue

        painter.setBrush(QColor(255, 255, 255, 245))
        if kind == "wide":  # 激动：眼睛放大 + 高光更大
            painter.drawEllipse(QPointF(ex, eye_y), eye_r * 1.18, eye_r * 1.26)
            painter.setBrush(ink)
            painter.drawEllipse(QPointF(ex + look, eye_y), eye_r * 0.70, eye_r * 0.78)
            painter.setBrush(QColor(255, 255, 255, 240))
            painter.drawEllipse(
                QPointF(ex + look - eye_r * 0.24, eye_y - eye_r * 0.34), eye_r * 0.26, eye_r * 0.26
            )
            continue

        # round / look / smirk 都是普通圆眼，只是瞳孔位置不同
        painter.drawEllipse(QPointF(ex, eye_y), eye_r, eye_r * 1.08)
        painter.setBrush(ink)
        if kind == "look":  # 好奇：瞳孔一致偏向一侧，配合歪头
            offset = eye_r * (0.45 + 0.15 * math.sin(t * 1.7))
        elif kind == "smirk":
            offset = -eye_r * 0.25
        else:
            offset = look
        painter.drawEllipse(QPointF(ex + offset, eye_y + eye_r * 0.1), eye_r * 0.56, eye_r * 0.62)
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawEllipse(
            QPointF(ex + offset - eye_r * 0.18, eye_y - eye_r * 0.26), eye_r * 0.18, eye_r * 0.18
        )

    # 吐槽：一只眼半闭，做出"挑眉"的味道
    if kind == "smirk":
        pen = QPen(ink, max(2.0, s * 0.018))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(cx + eye_dx - eye_r * 1.1, eye_y - eye_r * 0.55),
            QPointF(cx + eye_dx + eye_r * 1.1, eye_y - eye_r * 0.85),
        )


def _draw_mouth(
    painter: QPainter,
    style: dict,
    cx: float,
    mouth_y: float,
    s: float,
    talking: bool,
    t: float,
    ink: QColor,
) -> None:
    """说话时以张嘴动画为主，其它时候按情绪换嘴型（线条用设计器挑的眼珠色）。"""
    kind = str(style.get("mouth", "smile"))
    painter.setPen(Qt.PenStyle.NoPen)

    if talking:
        open_ratio = 0.45 + 0.55 * abs(math.sin(t * 11.0))
        painter.setBrush(QColor(70, 30, 40, 230))
        painter.drawEllipse(QPointF(cx, mouth_y), s * 0.045, s * 0.052 * open_ratio)
        return

    pen = QPen(ink, max(2.0, s * 0.017))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if kind == "grin":  # 开心：张大嘴笑
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(70, 30, 40, 235))
        painter.drawEllipse(QPointF(cx, mouth_y), s * 0.052, s * 0.040)
        painter.setPen(QPen(QColor(255, 255, 255, 220), max(1.4, s * 0.012)))
        painter.drawLine(QPointF(cx - s * 0.03, mouth_y - s * 0.018), QPointF(cx + s * 0.03, mouth_y - s * 0.018))
        return
    if kind == "flat":  # 无语：一条直线嘴
        painter.drawLine(QPointF(cx - s * 0.042, mouth_y), QPointF(cx + s * 0.042, mouth_y))
        return
    if kind == "open":  # 激动：圆嘴
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(70, 30, 40, 235))
        painter.drawEllipse(QPointF(cx, mouth_y), s * 0.036, s * 0.046)
        return
    if kind == "small":  # 好奇：小小的 o 嘴
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(70, 30, 40, 215))
        painter.drawEllipse(QPointF(cx, mouth_y), s * 0.022, s * 0.026)
        return
    if kind == "smirk":  # 吐槽：歪着的坏笑
        painter.drawArc(
            QRectF(cx - s * 0.05, mouth_y - s * 0.04, s * 0.10, s * 0.07), 210 * 16, 150 * 16
        )
        return
    painter.drawArc(  # 默认微笑
        QRectF(cx - s * 0.045, mouth_y - s * 0.03, s * 0.09, s * 0.06), 200 * 16, 140 * 16
    )


def _device_ratio(painter: QPainter) -> float:
    """画笔所在设备的缩放比（125% 的屏上就是 1.25）：位置对齐整像素要用它。"""
    try:
        device = painter.device()
        ratio = float(device.devicePixelRatioF()) if device is not None else 1.0
    except Exception:
        ratio = 1.0
    return ratio if ratio > 0 else 1.0


def _frame_order(path: Path) -> tuple:
    """帧文件排序：按名字末尾的数字排（idle_10 排在 idle_2 后面，不被当成 1）。"""
    match = re.search(r"(\d+)$", path.stem)
    if not match:
        return (path.stem, 0, path.suffix)
    return (path.stem[: match.start()], int(match.group(1)), path.suffix)


def _load_frames(folder: Path, prefix: str) -> List[QPixmap]:
    if not folder.is_dir():
        return []
    files: Sequence[Path] = sorted(
        list(folder.glob(f"{prefix}_*.png")) + list(folder.glob(f"{prefix}_*.webp")),
        key=_frame_order,
    )
    frames: List[QPixmap] = []
    for path in files:
        pm = QPixmap(str(path))
        if not pm.isNull():
            frames.append(pm)
    return frames


def _content_box(groups: Sequence[Sequence[QPixmap]]) -> QRect:
    """所有帧的**共同**内容边界（取并集）。

    必须用同一个框裁每一帧：各帧按自己的边界裁的话，动作幅度会被归一化掉，
    "在上下轻浮"就变成"原地不动"了。
    """
    box = QRect()
    for frames in groups:
        for pm in frames:
            mask = pm.mask()
            if mask.isNull():                       # 没有透明通道：整张都算内容
                piece = QRect(0, 0, pm.width(), pm.height())
            else:
                piece = QRegion(mask).boundingRect()   # 透明的地方不算
            if piece.isEmpty():
                continue
            box = piece if box.isNull() else box.united(piece)
    return box


def _fit_mapping(width: int, height: int, canvas: int) -> Tuple[float, float, float, float]:
    """「裁框 → 缩放 → 居中」这套参数（帧和眼珠层共用一份，保证永远对得齐）。

    返回 (x 缩放, y 缩放, x 偏移, y 偏移)，作用在裁好的内容上。缩放按**内容外接
    矩形**算，让矩形的四个角刚好落在圆上（留 FRAME_SAFE 的余量），瘦长的人物就能
    画得尽量大，矮胖的自动收一点，反正都不会被圆切到。
    """
    radius = 0.5 * canvas * FRAME_SAFE
    scale = min(FRAME_MAX_ZOOM, radius / math.hypot(width / 2.0, height / 2.0))
    target_w = max(1, int(round(width * scale)))
    target_h = max(1, int(round(height * scale)))
    return (
        target_w / float(width),
        target_h / float(height),
        float(int(round((canvas - target_w) / 2.0))),
        float(int(round((canvas - target_h) / 2.0))),
    )


def _canvas_region(box: QRect, canvas: int, mapping: Tuple[float, float, float, float]) -> QRect:
    """画布上看得见的那一整块，反算回**原图坐标**是哪儿。

    动作帧「允许出框」就是靠它：不按内容框裁，而是裁到"再往外就掉出画布了"为止，
    于是蹦到高处 / 伸懒腰伸出去的那点都保住了，超出画布（也就是圆外）的部分才被切掉。
    """
    sx, sy, off_x, off_y = mapping
    left = math.floor(box.x() - off_x / sx)
    top = math.floor(box.y() - off_y / sy)
    right = math.ceil(box.x() + (canvas - off_x) / sx)
    bottom = math.ceil(box.y() + (canvas - off_y) / sy)
    return QRect(int(left), int(top), max(1, int(right - left)), max(1, int(bottom - top)))


def _fit_frames(
    frames: Sequence[QPixmap],
    box: QRect,
    canvas: int,
    mapping: Optional[Tuple[float, float, float, float]] = None,
    overflow: bool = False,
) -> List[QPixmap]:
    """裁到 box → 缩到"外接矩形的角刚好落在圆上" → 居中放进 canvas 见方的透明画布。

    overflow=True 是给动作帧 / 日常状态帧用的（见 _fit_to_window）：不按 box 裁，
    只裁到画布边上为止。**缩放和居中还是用基础帧那一份**（mapping），所以做动作的时候
    人不会突然变大变小，跟眼珠 / 食指层也照样对得齐；代价就是冒出去的那点会被圆切掉。
    """
    width = max(1, box.width() if not box.isNull() else frames[0].width())
    height = max(1, box.height() if not box.isNull() else frames[0].height())
    sx, sy, off_x, off_y = mapping or _fit_mapping(width, height, canvas)
    origin_x = float(box.x()) if not box.isNull() else 0.0
    origin_y = float(box.y()) if not box.isNull() else 0.0
    # 允许出框时，提前算好"画布盖得住的那一大块"在原图里的位置
    loose = _canvas_region(box, canvas, (sx, sy, off_x, off_y)) if (overflow and not box.isNull()) else None
    out: List[QPixmap] = []
    for pm in frames:
        if loose is not None:
            area = loose.intersected(QRect(0, 0, pm.width(), pm.height()))
        elif box.isNull():
            area = QRect(0, 0, pm.width(), pm.height())
        else:
            area = box
        shell = QPixmap(canvas, canvas)
        shell.fill(Qt.GlobalColor.transparent)
        if area.isEmpty():
            out.append(shell)                   # 这一帧正好整块都在画布外
            continue
        cropped = pm if area == QRect(0, 0, pm.width(), pm.height()) else pm.copy(area)
        target = cropped.scaled(
            max(1, int(round(area.width() * sx))),
            max(1, int(round(area.height() * sy))),
            Qt.AspectRatioMode.IgnoreAspectRatio,   # 比例上面已经算准，这里只负责缩放
            Qt.TransformationMode.SmoothTransformation,
        )
        painter = QPainter(shell)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(
            int(round(off_x + (area.x() - origin_x) * sx)),
            int(round(off_y + (area.y() - origin_y) * sy)),
            target,
        )
        painter.end()
        out.append(shell)
    return out
