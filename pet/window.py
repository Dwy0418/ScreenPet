"""挂件窗口。

拆成两个顶层窗口：
* PetWindow   —— 只包住形象本身，用 setMask 裁成圆形，所以圆形以外的点击会穿透到下面的视频上。
* BubbleWindow—— 说话气泡，整窗 WS_EX_TRANSPARENT（点击穿透），压在视频上也不挡鼠标。
"""
from __future__ import annotations

import ctypes
import math
import os
import random
import time
from typing import List, Optional

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QTimer, Qt, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRegion,
)
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QWidget, QWidgetAction

from . import mood as mood_mod
from . import states as states_mod
from . import style as style_mod
from . import winfind
from .sprite import PetRenderer

#: 气泡没情绪色时的兜底强调色：取出厂长相的 accent（颜色定义都在 style.py）
ACCENT = QColor(style_mod.PetStyle().accent)

PAD_X = 13
PAD_Y = 10
TAIL = 10
RADIUS = 18
# 四周留一点余量，给"柔光边"落脚——不然那圈光会被窗口边缘切掉，看着像糊了个方框
EDGE = 3

# "串门"用的本地台词（见 PetWindow.visit）
VISIT_GO_LINES = (
    "我去别的屏幕转一圈，马上回来",
    "出去溜达溜达，别眨眼",
    "我去那边看看有什么好玩的",
)
VISIT_BACK_LINES = (
    "我回来了，那边也就那样",
    "转了一圈，还是你这儿踏实",
    "回来啦，想我没",
)

_TICK_MS = 33
# 点一下挂件 = **随机演一个动作**（随机池见 `pet/states.py` 的 `CLICK_ACTIONS`，
# 挑哪一个是 `_on_click` 说了算）。想打字聊天走右键菜单「打字跟我唠…」，
# 语音还是开着面板点「麦克风」。
CLICK_SLOP = 4          # 松开时移动不超过这么多像素，才算"点了一下"，不是拖它
HOVER_STILL_MS = 900    # 鼠标在它身上停多久算"它注意到你了"（见 _on_hover_still）
HOVER_REACT_COOL = 25.0 # 挥手别太勤：挥过一次之后歇这么久才可能再来（秒）
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

# SetWindowPos 用的标志位
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

# 锁定（鼠标穿透）时，鼠标压在挂件身上多久才算"想跟它交互"
LOCK_HOVER_FALLBACK_MS = 200

# 眼珠追鼠标的顺滑度：每帧往目标方向走这么多（0.2 左右看起来是"慢慢转过去"）
GAZE_BLEND = 0.22
# 鼠标离挂件多远算"看到最边上"（像素）；越小眼珠越灵敏
GAZE_REACH_PX = 320.0

# 右键菜单的皮：和气泡一套语言——深色玻璃、圆角、悬停是一层青色柔光。
# 不写 QMenu::indicator，勾选项（主动搭话）继续用系统的对勾。
MENU_QSS = """
QMenu {
    background-color: rgba(19, 24, 35, 246);
    border: 1px solid rgba(124, 224, 255, 82);
    border-radius: 12px;
    padding: 8px 6px;
}
QMenu::item {
    padding: 7px 20px 7px 14px;
    margin: 1px 6px;
    border-radius: 8px;
    color: #E8F1FA;
}
QMenu::item:selected {
    background-color: rgba(124, 224, 255, 46);
    color: #FFFFFF;
}
QMenu::item:disabled {
    color: #6B7A90;
}
QMenu::separator {
    height: 1px;
    margin: 6px 14px;
    background: rgba(255, 255, 255, 28);
}
QLabel#menuGroup {
    color: #7CE0FF;
    font-size: 11px;
    padding: 9px 14px 3px 14px;
    background: transparent;
}
"""


def _set_click_through(widget: QWidget, enabled: bool) -> None:
    """Windows 专用：让窗口对鼠标完全透明（连渲染都保留）。"""
    if os.name != "nt":
        return
    try:
        hwnd = int(widget.winId())
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            style |= WS_EX_LAYERED
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        # 改 EXSTYLE 之后要重新过一次 SetWindowPos，穿透状态才会立刻生效
        swp_flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, swp_flags)
    except Exception as exc:  # pragma: no cover
        print(f"[window] 设置点击穿透失败：{exc}")


class BubbleWindow(QWidget):
    """吐槽气泡：圆角矩形 + 小尾巴，淡入淡出。"""

    def __init__(self, cfg, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.cfg = cfg
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle(f"{cfg.persona.name}在说话")

        self._text = ""
        self._mood = ""
        self._alpha = 0.0
        self._fade_in = True
        self._pop = 1.0          # 「冒出来」的小弹跳 0→1（只动画内部，不动窗口位置）
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._fade_step)
        self._stay = QTimer(self)
        self._stay.setSingleShot(True)
        self._stay.timeout.connect(self._start_fade_out)

    def prepare(self, text: str, mood: str = "") -> bool:
        """排好文字和尺寸（不显示）。返回是否有可显示的内容。"""
        text = (text or "").strip()
        if not text:
            return False
        self._text = text
        self._mood = mood_mod.normalize(mood) if mood else ""

        font = QFont()
        font.setPointSizeF(max(9.0, float(self.cfg.ui.font_size)))
        # 字距松一点点，短句在深色气泡里更透气（也能少占一行）
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)
        self.setFont(font)
        metrics = QFontMetrics(font)
        max_text_w = max(120, int(self.cfg.ui.bubble_max_width) - (PAD_X + EDGE) * 2)
        bounds = metrics.boundingRect(
            QRect(0, 0, max_text_w, 1000),
            int(Qt.TextFlag.TextWordWrap),
            text,
        )
        width = min(max_text_w, bounds.width()) + (PAD_X + EDGE) * 2
        height = bounds.height() + (PAD_Y + EDGE) * 2 + TAIL
        self.resize(width, height)
        return True

    def show_text(self, text: str, mood: str = "") -> None:
        if not self.prepare(text, mood):
            return
        self._alpha = 0.0
        self._fade_in = True
        self._pop = 0.0
        self.show()
        self.raise_()
        self._timer.start()
        self._stay.start(max(1200, int(self.cfg.ui.bubble_ms)))

    def hide_bubble(self) -> None:
        self._timer.stop()
        self._stay.stop()
        self._alpha = 0.0
        self.setWindowOpacity(0.0)
        self.hide()

    def _start_fade_out(self) -> None:
        self._fade_in = False
        self._timer.start()

    def _fade_step(self) -> None:
        step = 0.18 if self._fade_in else -0.12
        self._alpha = max(0.0, min(1.0, self._alpha + step))
        self.setWindowOpacity(self._alpha)
        if self._pop < 1.0:
            # 弹一下：200 毫秒左右归位（窗口本身不动，只在画的时候偏一点）
            self._pop = min(1.0, self._pop + 0.22)
            self.update()
        if not self._fade_in and self._alpha <= 0.0:
            self.hide_bubble()

    def paintEvent(self, event) -> None:
        if not self._text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 刚冒出来的时候从下往上轻轻弹一下（只偏绘制、不动窗口，免得跟定位逻辑打架）
        painter.translate(0.0, (1.0 - self._pop) * 7.0)

        accent = QColor(mood_mod.color(self._mood)) if self._mood else QColor(ACCENT)
        body = QRectF(EDGE, EDGE, self.width() - EDGE * 2, self.height() - TAIL - EDGE * 2)
        path = QPainterPath()
        path.addRoundedRect(body, RADIUS, RADIUS)

        # 小尾巴：两条贝塞尔收成一个圆头，比原来的硬三角顺眼，也不扎人
        tail = QPainterPath()
        cx = self.width() / 2.0
        tail.moveTo(cx - 9.0, body.bottom() - 3.0)
        tail.quadTo(cx - 4.5, body.bottom() + TAIL, cx, body.bottom() + TAIL)
        tail.quadTo(cx + 4.5, body.bottom() + TAIL, cx + 9.0, body.bottom() - 3.0)
        tail.closeSubpath()
        path = path.united(tail)

        # 底：深蓝黑 → 更深的垂直渐变，看着像一块玻璃，不是一块黑布
        glass = QLinearGradient(0.0, body.top(), 0.0, body.bottom())
        glass.setColorAt(0.0, QColor(33, 41, 58, 245))
        glass.setColorAt(0.55, QColor(21, 26, 38, 242))
        glass.setColorAt(1.0, QColor(14, 18, 27, 242))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glass)
        painter.drawPath(path)

        # 情绪色的边：外面一圈宽而淡（柔光），里面一圈细而实
        # （情绪**不上界面**那条规矩没变：不写标签、不画小胶囊，只有配色）
        glow = QColor(accent)
        glow.setAlpha(48)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(glow, 6.0))
        painter.drawPath(path)
        edge = QColor(accent)
        edge.setAlpha(210)
        painter.setPen(QPen(edge, 1.5))
        painter.drawPath(path)

        # 顶上一条淡淡的高光——玻璃感全靠它，也顺便把气泡和"一块黑方块"区分开
        painter.save()
        painter.setClipPath(path)
        sheen = QLinearGradient(body.left(), 0.0, body.right(), 0.0)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 0))
        sheen.setColorAt(0.5, QColor(255, 255, 255, 36))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(QPen(sheen, 2.0))
        top = body.top() + 2.5
        painter.drawLine(QPointF(body.left() + RADIUS, top), QPointF(body.right() - RADIUS, top))
        painter.restore()

        text_rect = body.adjusted(PAD_X, PAD_Y, -PAD_X, -PAD_Y)
        flags = int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignVCenter)
        # 先在底下描一层很淡的影：视频再花，字也读得清
        painter.setPen(QColor(6, 9, 16, 150))
        painter.drawText(text_rect.adjusted(0, 1, 0, 1), flags, self._text)
        painter.setPen(QColor(241, 248, 255))
        painter.drawText(text_rect, flags, self._text)

        # 情绪只偷偷驱动上面的配色和表情；每句话的「记录/分析/学习」分类同理，
        # 只走控制台日志和提示词（见 pet/dialog.py），一个字都不上界面。
        painter.end()


class PetWindow(QWidget):
    """圆形的挂件窗口：可拖拽、右键菜单、说话/思考动画，并驱动气泡。"""

    moved = Signal()
    pauseToggled = Signal(bool)
    pickRegionRequested = Signal()
    configRequested = Signal()
    quitRequested = Signal()
    processLockRequested = Signal(str, str)   # 只盯某个程序：(exe 名, 标题关键字)
    processUnlockRequested = Signal()         # 不盯着它了，回到整屏 / 框选
    friendsRequested = Signal()               # 打开"好友系统"面板（见 app.open_friends）
    hideRequested = Signal()                  # 收进托盘（"这会儿不用你"，见 app.hide）
    memoryRequested = Signal()                # 打开记忆文件（memory.json）
    designRequested = Signal()                 # 设计我的形象（见 app.open_designer / tools/design_pet.py）
    memoryArchiveRequested = Signal()         # 打开完整存档（只增不减的那本流水）
    clearMemoryRequested = Signal()           # 清空长期记忆（会先问一句）
    chatOpenRequested = Signal()              # 「打字跟我唠…」：弹出输入框（只开不收，见 app.open_chat）
    voiceRequested = Signal()                 # 「说一句（语音）」：弹出输入框并开始听（app.open_voice）
    lockRequested = Signal(bool)              # 「把我钉在这儿 / 松开」：鼠标穿透开关（app._on_click_through）

    def __init__(self, cfg, renderer: PetRenderer, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.cfg = cfg
        self.renderer = renderer
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle(self.cfg.persona.name)
        self.setToolTip(
            "点一下看我演一个动作 · 双击戳它一下（暂停 / 继续） · 鼠标停在我身上我会跟你挥手 · "
            "左键拖动挪位置（放下我蹦一下）· 右键打开菜单"
        )

        size = max(72, int(cfg.ui.pet_size))
        self.setFixedSize(size, size)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        # 圆形窗口遮罩：圆外的鼠标事件直接穿透到视频上
        self.setMask(QRegion(QRect(1, 1, size - 2, size - 2), QRegion.RegionType.Ellipse))

        self._bubble = BubbleWindow(cfg)
        self._t0 = time.monotonic()
        self._talking_until = 0.0
        self._mood = ""
        self._mood_until = 0.0
        self._blinking_until = 0.0
        self._next_blink = time.monotonic() + random.uniform(2.0, 5.0)
        # 眼珠跟鼠标：鼠标相对挂件的方向（每轴 -1~1），在 _tick 里慢慢追过去
        self._gaze = (0.0, 0.0)
        self._thinking = False
        self._paused = False
        self._drag_from: Optional[QPoint] = None
        self._press_pos: Optional[QPoint] = None
        self._skip_click = False        # 双击那一下的 release 别再当成"点了一下"
        # "点一下 = 想跟它说话"要等一个双击间隔才作数：见 _on_click / mouseReleaseEvent
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(max(120, QApplication.doubleClickInterval()))
        self._click_timer.timeout.connect(self._on_click)
        # 「鼠标在它身上停住」= 它注意到你了，冲你挥挥手（见 _on_hover_still）。
        # 得停够 HOVER_STILL_MS 才算数：鼠标只是从它身上扫过去的时候别乱挥手。
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(HOVER_STILL_MS)
        self._hover_timer.timeout.connect(self._on_hover_still)
        self._hover_cool = 0.0          # 下次最早什么时候还能再挥（时间戳）
        # 点一下演哪个：从 `states.CLICK_ACTIONS` 里随机挑，但别连着两次都是同一个
        # （见 _on_click / _pick_click_act）。
        self._last_click_act = ""
        self._hidden_for_capture = False
        self._bubble_was_visible = False
        # 「把我钉在这儿（鼠标点不到我）」时也要能右键：鼠标压到身上就临时把穿透关掉，走开再打开
        self._locked = False
        self._passthrough = False
        self._lock_hovering = False
        self._lock_hover_since: Optional[float] = None
        self._plain_cursor = self.cursor()
        # 聊天面板这类"也算挡住观看区域"的小窗，抓屏前一起让开（见 occlusion_rect）
        self.extra_occluders: List[QWidget] = []
        self._hidden_occluders: List[QWidget] = []
        # "串门"：出去待一会儿，到点自己走回来（见 visit / _end_visit）
        self._visiting = False
        self._home: Optional[QPoint] = None
        self._visit_timer = QTimer(self)
        self._visit_timer.setSingleShot(True)
        self._visit_timer.timeout.connect(self._end_visit)
        # 正在做的动作（串门时两只一起玩，见 pet/play.py）：动作名 + 从几点播到几点
        self._act = ""
        self._act_from = 0.0
        self._act_until = 0.0
        # 日常状态（见 pet/states.py）：跟 `_act` 那套一次性动作分开——
        # `_pose` 是"它现在是什么状态"（走路 / 坐下 / 睡觉…），一直循环，直到下次切换。
        self._pose = ""
        self._pose_period = 0.0
        self._listening = False     # 麦克风开着（在听你说话）
        self._dragging = False      # 你正把它拖到别的地方

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._place_default()
        if cfg.ui.click_through:
            self.set_click_through(True)

    # ---------- 位置 ----------

    def _place_default(self) -> None:
        screen = QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 720)
        margin = int(self.cfg.ui.edge_margin)
        if str(self.cfg.ui.side).lower().startswith("l"):
            x = area.left() + margin
        else:
            x = area.right() - self.width() - margin
        y = area.top() + int(area.height() * float(self.cfg.ui.vertical_ratio)) - self.height() // 2
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(int(x), int(y))

    def visit(self, seconds: Optional[float] = None) -> None:
        """去别的屏幕（或屏幕另一头）转一圈，到点自己走回来。

        跨电脑那种"你的桌宠来我这边转转"要一台中转服务器，这里给的是**本机**
        能做的那一半：换块屏待一会儿再回来。多屏时优先去别的屏，单屏就换到对面
        的边，走之前先记着老位置。
        """
        screens = list(QGuiApplication.screens())
        here = QGuiApplication.screenAt(self.geometry().center())
        others = [s for s in screens if s is not here] or screens
        area = random.choice(others).availableGeometry()
        margin = int(self.cfg.ui.edge_margin)
        x = area.left() + margin if random.random() < 0.5 else area.right() - self.width() - margin
        y = area.top() + int(area.height() * random.uniform(0.2, 0.75)) - self.height() // 2
        y = max(area.top(), min(y, area.bottom() - self.height()))

        if not self._visiting:
            self._home = self.pos()
        self._visiting = True
        self.move(int(x), int(y))
        wait = float(self.cfg.ui.visit_seconds if seconds is None else seconds)
        self._visit_timer.start(max(1200, int(wait * 1000)))
        self.say(random.choice(VISIT_GO_LINES), "happy")

    def _end_visit(self) -> None:
        """串门结束：走回原来的位置，说一声。"""
        if not self._visiting:
            return
        self._visiting = False
        if self._home is not None:
            self.move(self._home)
            self._home = None
        self.say(random.choice(VISIT_BACK_LINES), "happy")

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._reposition_bubble()
        self.moved.emit()

    def _reposition_bubble(self) -> None:
        bubble = self._bubble
        if not bubble.isVisible():
            return
        x = self.geometry().center().x() - bubble.width() // 2
        y = self.geometry().top() - bubble.height() + 2
        screen = QGuiApplication.screenAt(self.geometry().center())
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 4, min(x, area.right() - bubble.width() - 4))
            if y < area.top() + 4:
                y = self.geometry().bottom() - 2
        bubble.move(int(x), int(y))

    def pet_global_rect(self) -> QRect:
        return self.geometry()

    def occlusion_rect(self) -> QRect:
        """挂件当前占用的屏幕范围（含气泡和聊天面板），用来判断会不会挡住被拍的区域。"""
        rect = self.geometry()
        if self._bubble.isVisible():
            rect = rect.united(self._bubble.geometry())
        for widget in self.extra_occluders:
            if widget.isVisible():
                rect = rect.united(widget.geometry())
        return rect

    # ---------- 状态 ----------

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def mood(self) -> str:
        return self._mood if time.monotonic() < self._mood_until else ""

    def say(self, text: str, mood: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        seconds = min(9.0, 0.9 + len(text) * 0.22)
        self._talking_until = time.monotonic() + seconds
        self._thinking = False
        if mood:
            self._mood = mood_mod.normalize(mood)
            # 表情比气泡多留一会儿，说完话不至于立刻变回死鱼脸
            self._mood_until = time.monotonic() + seconds + self.cfg.ui.bubble_ms / 1000.0 * 0.5
        self._bubble.show_text(text, mood)
        self._reposition_bubble()
        self.update()

    def set_thinking(self, thinking: bool) -> None:
        if self._thinking != thinking:
            self._thinking = thinking
            self._sync_pose()       # 等模型回话那会儿切成"想事情"（见 pet/states.py）
            self.update()

    def act(self, info) -> None:
        """做一个动作（串门时两只一起玩，见 pet/play.py）。

        动画还没做：现在会蹦一下（`sprite.act_lift`）+ 换上这个动作的表情，
        但**接口就是最终的接口**——以后往 assets/pet/ 里放 `<动作名>_*.png`
        那套帧，同一份 info 直接就能播（名字对得上就行）。
        """
        data = dict(info) if isinstance(info, dict) else {}
        name = str(data.get("anim") or data.get("move") or "").strip()
        if not name:
            return
        now = time.monotonic()
        self._act = name
        self._act_from = now
        self._act_until = now + max(0.4, float(data.get("seconds") or 1.2))
        mood = str(data.get("mood") or "")
        if mood:
            self._mood = mood_mod.normalize(mood)
            self._mood_until = max(self._mood_until, self._act_until)
        self.update()

    def _act_state(self, now: float):
        """(动作名, 0~1 的进度)；没在动作就是 ("", 0.0)。"""
        if not self._act or now >= self._act_until:
            return "", 0.0
        span = max(0.001, self._act_until - self._act_from)
        return self._act, min(1.0, max(0.0, (now - self._act_from) / span))

    # ---------- 日常状态 / 界面互动（见 pet/states.py） ----------

    def set_pose(self, name: str) -> None:
        """切「它现在是什么状态」：睡着 / 在听 / 在想 / 被拖着。

        name 是 `states.STATES` 里的键，空串 = 不占着（回落到待机 / 说话那套基础帧）；
        认不出来的键也当空串——不认识的别瞎切。切过去之后会**一直循环**，
        直到下一次有人再切（转完一轮多久由 states.py 的 seconds 定）。
        """
        pose = states_mod.for_state(name)
        key = pose.key if pose is not None else ""
        if key == self._pose:
            return
        self._pose = key
        self._pose_period = float(pose.seconds) if pose is not None else 0.0
        self.update()

    def react(self, event: str) -> None:
        """你在界面上跟它互动了一下：播一遍小动作就停。

        名字走 `states.REACTIONS`——鼠标那几下（悬停 → 挥手、双击 → 被戳、拖起来放下 → 蹦一下）
        是窗口自己发的；**单击**演哪个由 `_pick_click_act` 从 `states.CLICK_ACTIONS` 里随机挑。
        认不出来就什么都不做。
        配了帧就按帧播，没配的照旧退回"蹦一下"（见 `sprite.act_lift`）——少放一套帧也不崩。
        """
        pose = states_mod.for_reaction(event)
        if pose is None:
            return
        self.act(states_mod.info(pose, loop=False))

    def play_mood(self, mood: str) -> None:
        """抓到的画面是什么情绪，就顺手做一个对应的动作（见 `states.MOOD_ACTIONS`）。

        这是"情绪带上动作"那一半：开心 → 欢呼、好奇 → 疑惑、惊讶 → 被戳般惊跳。
        跟 `react` 一样是播一遍就停；认不出来的情绪什么都不做。
        **正做着动作的时候不打断**——一串吐槽连着来，别让它一直被打断、什么都没演完；
        情绪本身（表情 + 气泡配色）走 `say`，跟这里互不影响。
        """
        if self._act and time.monotonic() < self._act_until:
            return
        pose = states_mod.for_mood(mood)
        if pose is None:
            return
        self.act(states_mod.info(pose, loop=False))

    def set_listening(self, listening: bool) -> None:
        """麦克风开着、正在听你说话：切成"张望"那条日常状态。"""
        listening = bool(listening)
        if self._listening != listening:
            self._listening = listening
            self._sync_pose()

    def _sync_pose(self) -> None:
        """按「它现在什么情况」挑一条日常状态（见 pet/states.py 的 STATES）。

        优先级：睡着 > 在听 > 在想 > 被拖着；一个都不占就回落到待机 / 说话。
        都在窗口这一侧算，不进提示词、也不联网。
        """
        if self._paused:
            name = "paused"
        elif self._listening:
            name = "listening"
        elif self._thinking:
            name = "thinking"
        elif self._dragging:
            name = "dragging"
        else:
            name = ""
        self.set_pose(name)

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        if self._paused:
            self.set_thinking(False)
        self._sync_pose()           # 暂停 = 眯着了（见 pet/states.py 的 STATES）
        self.update()

    def set_click_through(self, enabled: bool) -> None:
        """锁定位置：平时鼠标穿透，但只要鼠标压到它身上就临时解锁，右键/拖动照常用。"""
        self._locked = bool(enabled)
        self.cfg.ui.click_through = self._locked
        self._lock_hover_since = None
        if not self._locked:
            self._lock_hovering = False
            self.unsetCursor()
        self._apply_passthrough(force=True)
        self.update()

    @property
    def locked(self) -> bool:
        return self._locked

    def _hit_test(self, point: QPoint) -> bool:
        """挂件是圆的：点落在内切圆里才算碰到它（圆外本来就该穿透）。"""
        center = self.geometry().center()
        radius = min(self.width(), self.height()) / 2.0 - 1.0
        dx = point.x() - center.x()
        dy = point.y() - center.y()
        return dx * dx + dy * dy <= radius * radius

    def _apply_passthrough(self, force: bool = False) -> None:
        want = self._locked and not self._lock_hovering
        if force or want != self._passthrough:
            self._passthrough = want
            _set_click_through(self, want)

    def _poll_lock_hover(self, now: float) -> None:
        """锁定状态下每秒瞄几次鼠标在不在身上（自己花不了几个钱，也不用装钩子）。"""
        if not bool(getattr(self.cfg.ui, "lock_hover_unlock", True)):
            self._lock_hovering = False
            self._apply_passthrough()
            return
        inside = self.isVisible() and self._hit_test(QCursor.pos())
        delay_ms = max(0, int(getattr(self.cfg.ui, "lock_hover_delay_ms", LOCK_HOVER_FALLBACK_MS)))
        if inside:
            if self._lock_hovering:
                return
            if self._lock_hover_since is None:
                self._lock_hover_since = now
                return
            if (now - self._lock_hover_since) * 1000.0 < delay_ms:
                return
            self._lock_hovering = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            if not self._lock_hovering and self._lock_hover_since is None:
                return
            self._lock_hovering = False
            self._lock_hover_since = None
            self.unsetCursor()
        self._apply_passthrough()
        self.update()

    def close_bubble(self) -> None:
        self._bubble.hide_bubble()

    def shutdown(self) -> None:
        self._timer.stop()
        self._visit_timer.stop()
        self._bubble.hide_bubble()
        self._bubble.close()
        for widget in self.extra_occluders:
            widget.close()
        self.close()

    # ---------- 动画 ----------

    def _tick(self) -> None:
        now = time.monotonic()
        if self._locked:
            self._poll_lock_hover(now)
        if not self._paused:
            self._track_cursor()
        if self._blinking_until and now >= self._blinking_until:
            self._blinking_until = 0.0
        if not self._blinking_until and now >= self._next_blink:
            self._blinking_until = now + random.uniform(0.09, 0.14)
            self._next_blink = now + random.uniform(2.2, 5.5)
        if self._talking_until and now >= self._talking_until:
            self._talking_until = 0.0
        if self._mood_until and now >= self._mood_until:
            self._mood_until = 0.0
            self._mood = ""
        if self._act and now >= self._act_until:
            self._act = ""
        self.update()

    def _track_cursor(self) -> None:
        """看一眼鼠标在哪个方向，眼珠就慢慢转过去。

        只算方向（每轴 -1~1、长度不超过 1），具体挪多少像素由形象那边的元数据决定
        （见 tools/make_pet.py 的 eye_layer.json）。没有眼珠层就什么都不做。
        """
        if not getattr(self.renderer, "uses_eyes", False):
            return
        cursor = QCursor.pos()
        center = self.geometry().center()
        reach = max(60.0, float(getattr(self.cfg.ui, "eye_follow_px", GAZE_REACH_PX)))
        dx = (cursor.x() - center.x()) / reach
        dy = (cursor.y() - center.y()) / reach
        length = math.hypot(dx, dy)
        if length > 1.0:
            dx, dy = dx / length, dy / length
        # 每帧只走一小步，看起来是"慢慢转过去"而不是一闪一闪
        self._gaze = (
            self._gaze[0] + (dx - self._gaze[0]) * GAZE_BLEND,
            self._gaze[1] + (dy - self._gaze[1]) * GAZE_BLEND,
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        now = time.monotonic()
        act, act_t = self._act_state(now)
        act_loop = False
        act_period = 0.0
        if not act and self._pose:
            # 没在播一次性动作时，日常状态顶着不放（一直循环，见 pet/states.py）
            act, act_loop, act_period = self._pose, True, self._pose_period
        self.renderer.draw(
            painter,
            QRectF(2, 2, self.width() - 4, self.height() - 4),
            t=now - self._t0,
            talking=now < self._talking_until,
            thinking=self._thinking,
            blink=now < self._blinking_until,
            mood=self.mood,
            gaze=self._gaze,
            act=act,
            act_t=act_t,
            act_loop=act_loop,
            act_period=act_period,
        )
        if self._paused:
            font = QFont()
            font.setPointSizeF(8.5)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(255, 255, 255, 220))
            painter.drawText(
                QRectF(0, self.height() - 24, self.width(), 16),
                int(Qt.AlignmentFlag.AlignCenter),
                "Z z z",
            )
        if self._locked:
            self._draw_lock_badge(painter)
        painter.end()

    def _draw_lock_badge(self, painter: QPainter) -> None:
        """右下角一枚小挂锁：锁定 / 临时解锁状态一眼能看出来（不靠 emoji，字体多丑都稳）。"""
        size = max(11.0, self.width() * 0.20)
        left = self.width() - size - 5.0
        top = self.height() - size - 5.0
        body = QRectF(left, top + size * 0.42, size, size * 0.58)
        active = self._lock_hovering

        painter.setPen(QPen(QColor(255, 255, 255, 200), 1.2))
        painter.setBrush(QColor(20, 24, 33, 210))
        painter.drawEllipse(QRectF(left - size * 0.16, top - size * 0.16, size * 1.32, size * 1.32))

        # 锁梁（临时解锁时开口朝上，表示"现在能点"）
        pen = QPen(QColor(255, 214, 102) if not active else QColor(160, 231, 229), size * 0.14)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        sh = size * 0.5
        painter.drawArc(
            QRectF(left + size * 0.22, top - size * 0.05, size * 0.56, sh),
            0,
            180 * 16,
        )
        painter.setBrush(QColor(255, 214, 102) if not active else QColor(160, 231, 229))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(body, size * 0.18, size * 0.18)

    # ---------- 鼠标 ----------

    def enterEvent(self, event) -> None:
        """鼠标挪到它身上了：先掐表——停够一会儿才算"它注意到你"（见 `_on_hover_still`）。"""
        self._hover_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        """鼠标走开了：这一眼就算过去了（别等它半路挥起手来）。"""
        self._hover_timer.stop()
        super().leaveEvent(event)

    def _on_hover_still(self) -> None:
        """鼠标在它身上停住了一会儿：它抬头冲你挥挥手（`states.REACTIONS` 的 hover → wave）。

        三种场合不插队：**正演着别的动作**（一串情绪还没演完）、**刚挥过**
        （`HOVER_REACT_COOL` 秒内）、**睡着了 / 你正拖着它**——这几种时候挥手都别扭。
        """
        now = time.monotonic()
        if self._act and now < self._act_until:
            return
        if now < self._hover_cool:
            return
        if self._paused or self._dragging:
            return
        self._hover_cool = now + HOVER_REACT_COOL
        self.react("hover")

    def _on_drop(self) -> None:
        """把它拖起来又放下：落地蹦一下（`states.REACTIONS` 的 drop → jump）。

        正演着别的动作就不插队——那会儿它正忙着，蹦起来反而糊成一团。
        """
        now = time.monotonic()
        if self._act and now < self._act_until:
            return
        self.react("drop")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            if not self._dragging:
                # 真拖起来了才算"走路"，单纯点一下不算（见 states.STATES 的 dragging）
                self._dragging = True
                self._hover_timer.stop()    # 正被拖着呢，别半路挥起手来（放下那一下走 _on_drop）
                self._sync_pose()
            self.move(event.globalPosition().toPoint() - self._drag_from)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            moved = self._press_pos is not None and (
                (event.globalPosition().toPoint() - self._press_pos).manhattanLength() > CLICK_SLOP
            )
            double = self._skip_click
            self._drag_from = None
            self._press_pos = None
            self._skip_click = False
            if self._dragging:
                self._dragging = False
                self._sync_pose()
                self._on_drop()             # 放下了：落地蹦一下（见 _on_drop / states.REACTIONS 的 drop）
            self._reposition_bubble()
            if not moved and not double:
                # 点一下 = 想跟它说话。**不当场弹面板**：先等一个"双击间隔"，
                # 双击（暂停 / 继续看）来了就把这件事取消掉——不然暂停会顺手把面板也点开。
                self._click_timer.start()
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_timer.stop()
            self._skip_click = True      # 双击后面还会跟一个 release，别再当成"点了一下"
            # 先戳它一下（见 states.REACTIONS 的 double → 被戳），再暂停 / 继续看——
            # 两件事一起来：动作是先给你个反应，免得点了半天没动静
            self.react("double")
            self.pauseToggled.emit(not self._paused)
            event.accept()

    def _pick_click_act(self) -> str:
        """点一下演哪个：从 `states.CLICK_ACTIONS` 里**随机**挑一个，别跟上一个重样。

        池子可能只有一个键（或者空），那就别绕圈了，直接给（空就给空串，谁都不演）。
        """
        pool = [key for key in states_mod.CLICK_ACTIONS if states_mod.for_reaction(key)]
        if not pool:
            return ""
        if len(pool) > 1 and self._last_click_act in pool:
            pool = [key for key in pool if key != self._last_click_act]
        return random.choice(pool)

    def _on_click(self) -> None:
        """点一下挂件：**随机演一个动作**（池子见 `states.CLICK_ACTIONS`）。

        慢两下（间隔超过系统双击间隔）才算"点了一下"——第一下先等一个双击间隔，
        双击（暂停 / 继续看）来了就把这件事取消掉，走 `mouseDoubleClickEvent`，
        这里不会被叫到；拖它换位置也不算点（见 `mouseReleaseEvent`）。

        想打字聊天走右键菜单「打字跟我唠…」那一项——**点一下不再弹输入框**了：
        点它一下本来就是要逗它，弹个框出来反而把动作盖住。
        """
        key = self._pick_click_act()
        if not key:
            return
        self._last_click_act = key
        self.react(key)

    # ---------- 右键菜单 ----------

    def _region_label(self) -> str:
        """「看哪儿」那一项：一个入口同时管"划一块"和"整块屏"，标签上顺手写清现在是哪种。

        点它进框选：**拖一个矩形 = 只看这一块；双击 / 按 F = 整块屏都看；右键 / Esc = 不改**。
        （以前这里是两项——"只看一小块地方…"和"整块屏都看"，现在合成了一个。）
        """
        region = self.cfg.capture.region or {}
        if str(getattr(self.cfg.capture, "target_process", "") or "").strip():
            return "手动划观看范围（现在盯程序）…"
        width = int(region.get("width", 0) or 0)
        height = int(region.get("height", 0) or 0)
        if width > 0 and height > 0:
            return f"手动划观看范围（现在是 {width}×{height}）…"
        return "手动划观看范围（现在是整块屏）…"

    def _add_target_menu(self, menu: QMenu) -> None:
        """「只盯着一个程序」子菜单：列出眼前看得见的程序，选一个就只盯它。

        选完只按 exe 名认（不认标题），所以它换了视频标题照样跟得住；
        同一个 exe 开好几个窗口时会挑前台那个、其次挑最大的那个。
        """
        target = str(getattr(self.cfg.capture, "target_process", "") or "").strip()
        sub = menu.addMenu(
            ("只盯着一个程序：" + target) if target else "只盯着一个程序（抖音 / B站 / 游戏）…"
        )
        if target:
            sub.addAction("不盯着它了").triggered.connect(
                lambda *_: self.processUnlockRequested.emit()
            )
            sub.addSeparator()
        apps = winfind.list_apps() if winfind.available() else []
        if not apps:
            empty = sub.addAction("（这会儿没看见别的窗口）")
            empty.setEnabled(False)
            return
        for window in apps[:12]:
            action = sub.addAction(window.label)
            action.triggered.connect(
                lambda *_, name=window.process: self.processLockRequested.emit(name, "")
            )

    @staticmethod
    def _add_section(menu: QMenu, title: str) -> None:
        """菜单里的分组标题。

        没用 `menu.addSection()`：QMenu 一挂样式表，addSection 的标题就**画不出来**了
        （分隔线在、字没了，Qt 的老毛病）。所以改用一个真正的 QLabel 当标题，
        样式表里按 `QLabel#menuGroup` 上色，稳。
        """
        label = QLabel(title)
        label.setObjectName("menuGroup")
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        holder = QWidgetAction(menu)
        holder.setDefaultWidget(label)
        menu.addAction(holder)

    def _build_menu(self) -> QMenu:
        """把右键菜单搭出来（单独一个方法，冒烟测试能直接拿它截图检查样式）。"""
        menu = QMenu(self)
        menu.setStyleSheet(MENU_QSS)
        # 圆角得靠透明背景才看得出来（不然四角是方的、糊在深色方块上）。
        # 万一某个平台不吃这一套，顶多是方角，不影响用。
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        # 「陪你看」：**看什么 + 看哪儿合成一节**。以前这是「陪你看」「看哪儿」两节，
        # 但上一节撤掉一项之后只剩一个开关了，跟"看哪儿"摆在一起更像一回事。
        # 每一条写的都是点下去会发生的事。
        self._add_section(menu, "陪你看")
        menu.addAction("接着看" if self._paused else "先歇会儿（不看了）").triggered.connect(
            lambda *_: self.pauseToggled.emit(not self._paused)
        )
        # 划观看范围：一个入口解决"整块屏"和"划一块"——点开自己划，双击就是整块屏
        # （程序锁定的那层还留着，写在下面 _add_target_menu 里）
        menu.addAction(self._region_label()).triggered.connect(
            lambda *_: self.pickRegionRequested.emit()
        )
        self._add_target_menu(menu)

        # 「跟我说说话」：点一下挂件是"聊 / 收"（见 _on_click），这里再补上两个明写的入口
        # ——以前它们让位给了 `Ctrl+Alt+T` / `Ctrl+Alt+V` 两个热键，现在热键整套撤了，
        # 打字和语音都得从鼠标走得通（这两项**只开不收**，免得跟"点一下"那条路打架）。
        self._add_section(menu, "跟我说说话")
        menu.addAction("打字跟我唠…").triggered.connect(
            lambda *_: self.chatOpenRequested.emit()
        )
        menu.addAction("说一句（语音）").triggered.connect(
            lambda *_: self.voiceRequested.emit()
        )
        # 「没事也来搭话（主动搭话）」那个勾选开关撤了：主动搭话**默认就是开着的**
        # （`config.ProactiveConfig.enabled` 默认 True），不必用户特地去设一下。
        # 真不想要，配置里写 `proactive.enabled = false`（命令行 `--no-proactive` 同源）。
        # 「好友系统（串门 / 加好友）…」：出门只有一个入口——**去谁家串门**，
        # 所以面板里就是加好友 + 好友那一行的「去串门」。
        # （以前菜单里还有「去别的屏幕逛逛」，那是在自己屏幕上换个位置，两回事，已经不放了。）
        menu.addAction("好友系统（串门 / 加好友）…").triggered.connect(
            lambda *_: self.friendsRequested.emit()
        )

        # 动作入口（以前这里有一节「逗它一下」，一项项点）：现在**左键点一下挂件**就随机
        # 演一个（池子见 `states.CLICK_ACTIONS`，见 `_on_click`），菜单里不再单列一节，
        # 省得同一件事有两个入口、还得挑一个点。

        # 「我自己的事」
        # ①「把我钉在这儿」回到了菜单里：以前它只走 `Ctrl+Alt+L` 热键，热键一撤就没人点得到。
        #   钉上之后鼠标照样能操作它（鼠标压到身上会临时解锁，见 set_click_through），
        #   所以这一项点两次就能来回切——鼠标一条路走完。
        # ②「隐身时自己上网学（不看屏幕）」不再摆开关：改成**默认开着**，收进托盘就自己补课
        #   （不想花这份钱就在配置里写 `study.enabled = false`）。
        self._add_section(menu, "我自己的事")
        pinned = menu.addAction("松开（鼠标能点到我了）" if self.locked else "把我钉在这儿（鼠标点不到我）")
        pinned.triggered.connect(lambda *_: self.lockRequested.emit(not self.locked))
        menu.addAction("我先隐身（收进托盘，点托盘图标就能叫回来）").triggered.connect(
            lambda *_: self.hideRequested.emit()
        )
        menu.addAction("打开配置文件").triggered.connect(lambda *_: self.configRequested.emit())
        # 「长什么样」这件事以前只能改配置里的颜色、或者手画几百张帧；现在开了个设计器
        # （tools/design_pet.py，所见即所得，改完当场生效，见 app.open_designer）。
        menu.addAction("设计我的形象…").triggered.connect(lambda *_: self.designRequested.emit())
        # 这两项以前只在托盘菜单里有；托盘瘦成「显示挂件」之后挪到这儿，功能一样不少
        menu.addAction("打开记忆文件").triggered.connect(lambda *_: self.memoryRequested.emit())
        # 「打开记忆文件」那份会被裁剪（只留最近 200 条），这一份是**一条都不丢**的流水：
        # memory.json 怎么裁、怎么清都不动它（见 pet/memarchive.py）
        menu.addAction("打开完整存档（一条都不丢）").triggered.connect(
            lambda *_: self.memoryArchiveRequested.emit()
        )
        menu.addAction("清除长期记忆…").triggered.connect(
            lambda *_: self.clearMemoryRequested.emit()
        )
        menu.addSeparator()
        menu.addAction("我先走了（退出）").triggered.connect(lambda *_: self.quitRequested.emit())
        return menu

    def _show_menu(self, pos: QPoint) -> None:
        self._build_menu().exec(self.mapToGlobal(pos))

    # ---------- 抓屏时临时让开（避免把自己拍进画面） ----------

    @Slot()
    def hideForCapture(self) -> None:
        # 聊天面板挡在画面里就一起让开（抓完再放回来）
        self._hidden_occluders = [w for w in self.extra_occluders if w.isVisible()]
        for widget in self._hidden_occluders:
            widget.hide()
        if not self.isVisible():
            return
        self._hidden_for_capture = True
        self._bubble_was_visible = self._bubble.isVisible()
        self._bubble.hide()
        self.setVisible(False)

    @Slot()
    def showAfterCapture(self) -> None:
        for widget in self._hidden_occluders:
            widget.show()
        self._hidden_occluders = []
        if not self._hidden_for_capture:
            return
        self._hidden_for_capture = False
        self.setVisible(True)
        if self._bubble_was_visible:
            self._bubble.show()
            self._reposition_bubble()


