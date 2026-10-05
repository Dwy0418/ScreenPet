"""客人窗口：别人家的桌宠来串门时，坐在我家屏幕上的那个小东西。

跟 `window.PetWindow` 是一套皮（圆窗、气泡、右键菜单、眼珠追鼠标），
但**故意另起一份**：客人不是"我"——它不能改配置、不能抓屏、不能替我说话，
它说的话全由 `friends.VisitHub` 递过来（见那里的 guest_said 信号）。

形象走 `PetRenderer`：访客的待机帧由对方在串门时寄过来，落在
`friends.guest_dir_for(pet_id)`，没有帧时退化成内置的矢量形象。
"""
from __future__ import annotations

import math
import random
import time
from typing import Optional

from PySide6.QtCore import QPoint, QRect, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QPainter, QRegion
from PySide6.QtWidgets import QMenu, QWidget

from . import mood as mood_mod
from .sprite import PetRenderer
from .window import MENU_QSS, BubbleWindow

_TICK_MS = 33
# 眼珠追鼠标的顺滑度：客人比主人略慢一点，看着更"拘谨"
GAZE_BLEND = 0.16
# 鼠标离它多远算"看到最边上"
GAZE_REACH_PX = 300.0
# 自己挪窝：隔多久挪一次、一次挪多远（别太勤，免得在人家屏幕上乱窜）
WANDER_EVERY = (7.0, 16.0)
WANDER_STEP = (6, 26)


class GuestWindow(QWidget):
    """坐在我家的一位客人（一位客人一个窗口）。"""

    poked = Signal(str)              # pet_id：主人戳了它一下
    talkRequested = Signal(str)      # pet_id：主人想跟它说句话
    playRequested = Signal(str)      # pet_id：主人让它俩一起玩一下（见 pet/play.py）
    nudgeRequested = Signal(str)     # pet_id：让它在自己家挪个地方
    sendHomeRequested = Signal(str)  # pet_id：请它回家
    moved = Signal(str)              # pet_id：被拖到了新位置（记着，下次来还坐这儿）

    def __init__(self, cfg, guest, renderer: Optional[PetRenderer] = None, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.cfg = cfg
        self.guest = guest
        self.pet_id = str(getattr(guest, "pet_id", "") or "")
        self.renderer = renderer if renderer is not None else PetRenderer(getattr(guest, "frames_dir", None))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle(str(getattr(guest, "label", "") or "客人"))
        self.setToolTip("左键拖着挪个地方 · 双击戳它一下 · 右键还有菜单")

        size = max(64, int(cfg.ui.pet_size))
        self.setFixedSize(size, size)
        # 圆外点击穿透到视频上：跟主人那个挂件一个道理
        self.setMask(QRegion(QRect(1, 1, size - 2, size - 2), QRegion.RegionType.Ellipse))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        self._bubble = BubbleWindow(cfg)
        self._t0 = time.monotonic()
        self._talking_until = 0.0
        self._mood = ""
        self._mood_until = 0.0
        self._blinking_until = 0.0
        self._next_blink = time.monotonic() + random.uniform(2.0, 5.0)
        self._gaze = (0.0, 0.0)
        self._drag_from: Optional[QPoint] = None
        self._next_wander = time.monotonic() + random.uniform(*WANDER_EVERY)
        self._bubble_was_visible = False
        # 正在做的动作（跟主人一起玩，见 pet/play.py）：动作名 + 从几点播到几点
        self._act = ""
        self._act_from = 0.0
        self._act_until = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---------- 位置 ----------

    def _screen_area(self) -> QRect:
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 720)

    def _move_clamped(self, x: int, y: int, area: Optional[QRect] = None) -> None:
        area = area if area is not None else self._screen_area()
        x = max(area.left(), min(int(x), area.right() - self.width()))
        y = max(area.top(), min(int(y), area.bottom() - self.height()))
        self.move(int(x), int(y))

    def place_near(self, rect: QRect, slot: int = 0) -> None:
        """坐在主人挂件旁边；来好几个客人时依次岔开一点，别叠成一堆。"""
        area = self._screen_area()
        size = self.width()
        gap = max(6, size // 8)
        spread = slot * max(10, size // 3)
        left_x = rect.left() - size - gap - spread
        x = left_x if left_x >= area.left() else rect.right() + gap + spread
        y = rect.top() + rect.height() // 2 - size // 2 - spread // 2
        self._move_clamped(x, y, area)

    def restore_pos(self) -> bool:
        """上次它坐哪儿就还坐哪儿（拖过就记住，见 moved）。"""
        pos = getattr(self.guest, "pos", None)
        if pos is None:
            return False
        self._move_clamped(int(pos.x()), int(pos.y()))
        return True

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self.guest.pos = QPoint(self.pos())
        self._reposition_bubble()
        self.moved.emit(self.pet_id)

    def _reposition_bubble(self) -> None:
        bubble = self._bubble
        if not bubble.isVisible():
            return
        x = self.geometry().center().x() - bubble.width() // 2
        y = self.geometry().top() - bubble.height() + 2
        area = self._screen_area()
        x = max(area.left() + 4, min(x, area.right() - bubble.width() - 4))
        if y < area.top() + 4:
            y = self.geometry().bottom() - 2
        bubble.move(int(x), int(y))

    # ---------- 状态 ----------

    @property
    def mood(self) -> str:
        return self._mood if time.monotonic() < self._mood_until else ""

    def say(self, text: str, mood: str = "") -> None:
        """客人说一句（气泡 + 表情；文字是对方那边生成好递过来的）。"""
        text = (text or "").strip()
        if not text:
            return
        seconds = min(9.0, 0.9 + len(text) * 0.22)
        self._talking_until = time.monotonic() + seconds
        if mood:
            self._mood = mood_mod.normalize(mood)
            self._mood_until = time.monotonic() + seconds + self.cfg.ui.bubble_ms / 1000.0 * 0.5
        self._bubble.show_text(text, mood)
        self._reposition_bubble()
        self.update()

    def act(self, info) -> None:
        """跟着做一个动作（见 pet/play.py）：它家那只先动，它跟着一起动。

        跟主人那个挂件是同一套接口（`PetWindow.act`）：动画还没做，现在蹦一下
        （`sprite.act_lift`）+ 换上这个动作的表情；以后放 `<动作名>_*.png` 就能播真动作。
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

    def shutdown(self) -> None:
        self._timer.stop()
        self._bubble.hide_bubble()
        self._bubble.close()
        self.close()

    # ---------- 抓屏时一起让开（跟主人挂件同一套握手） ----------

    def hide(self) -> None:
        """藏起来的时候把气泡也带上——气泡是另一个顶层窗口，不然还是会被拍进去。"""
        self._bubble_was_visible = self._bubble.isVisible()
        self._bubble.hide()
        super().hide()

    def show(self) -> None:
        super().show()
        if self._bubble_was_visible and time.monotonic() < self._talking_until:
            self._bubble.show()
        self._bubble_was_visible = False

    def hideForCapture(self) -> None:
        self.hide()

    def showAfterCapture(self) -> None:
        self.show()

    # ---------- 动画 ----------

    def _tick(self) -> None:
        now = time.monotonic()
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
        if now >= self._next_wander:
            self._wander(now)
        self.update()

    def _wander(self, now: float) -> None:
        """自己挪一小步——坐在人家屏幕上也得有点活气（拖着的时候不挪）。"""
        self._next_wander = now + random.uniform(*WANDER_EVERY)
        if self._drag_from is not None:
            return
        step = random.randint(*WANDER_STEP)
        self._move_clamped(
            self.x() + random.choice((-step, step)),
            self.y() + random.randint(-step // 2, step // 3),
        )

    def _track_cursor(self) -> None:
        """眼珠跟着鼠标转（形象带眼珠层才有用，见 sprite.uses_eyes）。"""
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
        self.renderer.draw(
            painter,
            QRectF(2, 2, self.width() - 4, self.height() - 4),
            t=now - self._t0,
            talking=now < self._talking_until,
            thinking=False,
            blink=now < self._blinking_until,
            mood=self.mood,
            gaze=self._gaze,
            act=act,
            act_t=act_t,
        )
        painter.end()

    # ---------- 鼠标 ----------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_from)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = None
            self._reposition_bubble()
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.poked.emit(self.pet_id)
            event.accept()

    # ---------- 右键菜单 ----------

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        menu.setStyleSheet(MENU_QSS)
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        menu.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        title = menu.addAction(f"—— {str(getattr(self.guest, 'label', '') or '客人')} ——")
        title.setEnabled(False)
        menu.addAction("戳它一下").triggered.connect(lambda *_: self.poked.emit(self.pet_id))
        menu.addAction("跟它说句话…").triggered.connect(
            lambda *_: self.talkRequested.emit(self.pet_id)
        )
        menu.addAction("跟它玩一下").triggered.connect(
            lambda *_: self.playRequested.emit(self.pet_id)
        )
        menu.addAction("让它挪个地方").triggered.connect(
            lambda *_: self.nudgeRequested.emit(self.pet_id)
        )
        menu.addSeparator()
        menu.addAction("让它回家").triggered.connect(
            lambda *_: self.sendHomeRequested.emit(self.pet_id)
        )
        return menu

    def _show_menu(self, pos: QPoint) -> None:
        self._build_menu().exec(self.mapToGlobal(pos))
