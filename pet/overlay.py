"""「观看范围」框选遮罩：铺满所有屏幕，拖拽出一个矩形。

一处入口，两种选择（以前是"只看一小块地方"和"整块屏都看"两个菜单项，现在合并了）：

* **拖一个矩形** → 只盯这一块画面（一般是视频播放区），换算成物理像素
  存进 config.json 的 capture.region；
* **双击 / 按 F**（不拖）→ 整块屏都看（清掉 capture.region）；
* 右键 / Esc → 什么都不改。

选完就关掉，不留任何弹窗、也不打断挂件——数据只是记在配置里。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from . import dpi

ACCENT = QColor("#6FD8FF")
MIN_W = 80
MIN_H = 60


class RegionPicker(QWidget):
    picked = Signal(object)  # 成功：{"left","top","width","height"}（物理像素）；取消：None
    wholeRequested = Signal()  # 不划了：整块屏都看

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setWindowTitle("划一下黄豆要看的范围（双击 = 整块屏）")
        self.setGeometry(dpi.virtual_desktop_logical())
        self._origin: Optional[QPoint] = None
        self._current: Optional[QPoint] = None

    # ---------- 交互 ----------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self._finish(None)

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        rect = self._selection()
        self._origin = None
        self._current = None
        if rect is None or rect.width() < MIN_W or rect.height() < MIN_H:
            self.update()
            return
        top_left = self.mapToGlobal(rect.topLeft())
        bottom_right = self.mapToGlobal(rect.bottomRight())
        region = dpi.logical_rect_to_physical(QRect(top_left, bottom_right))
        self._finish(region)

    def mouseDoubleClickEvent(self, event) -> None:
        """不划、直接双击 = 整块屏都看（"看得宽一点"跟"划一块"合到这一个入口里）。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_whole()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._finish(None)
        elif key == Qt.Key.Key_F:
            self._finish_whole()
        else:
            super().keyPressEvent(event)

    def _finish(self, region) -> None:
        self.picked.emit(region)
        self.close()

    def _finish_whole(self) -> None:
        self.wholeRequested.emit()
        self.close()

    def _selection(self) -> Optional[QRect]:
        if self._origin is None or self._current is None:
            return None
        return QRect(self._origin, self._current).normalized()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(10, 14, 22, 130))

        font = QFont()
        font.setPointSize(12)
        painter.setFont(font)
        painter.setPen(QColor(235, 245, 255))
        hint = "拖一下 = 只看这一块 · 双击 / 按 F = 整块屏都看 · 右键 / Esc 算了"
        painter.drawText(self.rect().adjusted(0, 28, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, hint)

        rect = self._selection()
        if rect is None:
            painter.end()
            return

        painter.setBrush(QColor(111, 216, 255, 28))
        pen = QPen(ACCENT, 2)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRect(rect)

        label = f"{int(rect.width())} × {int(rect.height())}"
        painter.setPen(QColor(235, 245, 255))
        painter.drawText(
            rect.adjusted(8, 8, 0, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            label,
        )
        painter.end()
