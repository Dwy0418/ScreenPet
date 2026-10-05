"""聊天面板：打字跟它说话，也可以点麦克风说一句。

和挂件/气泡一样是无边框小窗，但它**必须能拿到键盘焦点**（不然没法打字），
所以这里没有用 WindowDoesNotAcceptFocus；打开时会抢焦点，Esc 收起。
面板自己在挂件旁边弹出来，内容就是"最近几轮对话 + 一个输入框"。
"""
from __future__ import annotations

import html
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import mood as mood_mod
from . import style as style_mod

#: 面板描边那点强调色，取出厂长相的 accent——用户在设置里改的是**形象本身**，
#: 这层玻璃只是装饰。六色常量早搬进了 style.py，sprite 里不再有颜色可拿
ACCENT = QColor(style_mod.PetStyle().accent)

PANEL_W = 380
PAD = 12
RADIUS = 14
MAX_ROWS = 14          # 面板里最多留多少行对话
LOG_H = 132


class ChatPanel(QWidget):
    """输入框 + 对话记录。它只负责"收集用户想说的话"，其余交给 app/worker。"""

    submitted = Signal(str)     # 用户按了回车 / 点了发送
    voiceRequested = Signal()   # 用户点了麦克风
    closed = Signal()           # 面板收起来了

    def __init__(self, cfg, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool,
        )
        self.cfg = cfg
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(f"跟{cfg.persona.name}说话")
        self._rows: List[str] = []
        self._listening = False
        self._build()

    # ---------- 界面 ----------

    def _build(self) -> None:
        font = QFont()
        font.setPointSizeF(max(9.0, float(self.cfg.ui.font_size)))
        self.setFont(font)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(PAD, PAD, PAD, PAD)
        layout.setSpacing(7)

        self.title = QLabel(f"跟 {self.cfg.persona.name} 说点什么")
        title_font = QFont(font)
        title_font.setPointSizeF(max(8.5, font.pointSizeF() - 1.5))
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.title.setStyleSheet("color: #9FB6CC;")
        layout.addWidget(self.title)

        self.log = QTextEdit(self)
        self.log.setReadOnly(True)
        self.log.setFrameStyle(0)
        self.log.setFixedHeight(LOG_H)
        self.log.setStyleSheet("QTextEdit { background: transparent; border: none; color: #EAF6FF; }")
        self.log.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.log.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.log)

        row = QHBoxLayout()
        row.setSpacing(6)

        self.input = QLineEdit(self)
        self.input.setPlaceholderText("打字，回车发我（Esc 收起）")
        self.input.setStyleSheet(
            "QLineEdit { background: rgba(255,255,255,22); border: 1px solid rgba(124,224,255,90);"
            " border-radius: 9px; padding: 5px 8px; color: #EAF6FF; }"
            "QLineEdit:focus { border: 1px solid rgba(124,224,255,170); }"
        )
        self.input.returnPressed.connect(self._submit)
        row.addWidget(self.input, 1)

        self.mic = QPushButton("麦克风", self)
        self.mic.setToolTip("点一下，我听着")
        self.mic.setFixedWidth(64)
        self.mic.clicked.connect(self._voice)
        row.addWidget(self.mic)

        self.send = QPushButton("发送", self)
        self.send.setFixedWidth(56)
        self.send.clicked.connect(self._submit)
        row.addWidget(self.send)

        for button in (self.mic, self.send):
            button.setStyleSheet(
                "QPushButton { background: rgba(124,224,255,45); border: none; border-radius: 9px;"
                " padding: 5px 6px; color: #DFF6FF; }"
                "QPushButton:hover { background: rgba(124,224,255,80); }"
                "QPushButton:disabled { color: #7D8A99; background: rgba(255,255,255,14); }"
            )

        layout.addLayout(row)
        self.resize(PANEL_W, LOG_H + 92)

    # ---------- 对话记录 ----------

    def _push(self, row: str) -> None:
        self._rows.append(row)
        if len(self._rows) > MAX_ROWS:
            del self._rows[:-MAX_ROWS]
        self.log.setHtml("<br>".join(self._rows))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def add_user(self, text: str) -> None:
        body = html.escape((text or "").strip())
        if not body:
            return
        self._push(f'<span style="color:#8FB6FF">你：{body}</span>')

    def add_reply(self, text: str, mood: str = "") -> None:
        body = html.escape((text or "").strip())
        if not body:
            return
        # 情绪不上界面：不再写成「[好奇] 台词」那样——只拿它的主题色给这句话上色
        color = mood_mod.color(mood) if mood else "#EAF6FF"
        self._push(
            f'<span style="color:{color}">{html.escape(self.cfg.persona.name)}：{body}</span>'
        )

    def add_notice(self, text: str) -> None:
        body = html.escape((text or "").strip())
        if not body:
            return
        self._push(f'<span style="color:#9AA7B4">{body}</span>')

    def clear_log(self) -> None:
        self._rows.clear()
        self.log.clear()

    # ---------- 输入 ----------

    def set_input(self, text: str, focus: bool = True) -> None:
        self.input.setText(text or "")
        if focus:
            self.input.setFocus()
            self.input.setCursorPosition(len(self.input.text()))

    def input_text(self) -> str:
        return self.input.text().strip()

    def set_listening(self, listening: bool) -> None:
        self._listening = bool(listening)
        self.mic.setText("听着…" if listening else "麦克风")
        self.mic.setEnabled(not listening)
        self.title.setText(
            "我在听，你说完停一下就好…" if listening else f"跟 {self.cfg.persona.name} 说点什么"
        )

    @property
    def listening(self) -> bool:
        return self._listening

    def _submit(self) -> None:
        text = self.input_text()
        if not text:
            return
        self.input.clear()
        self.submitted.emit(text)

    def _voice(self) -> None:
        if not self._listening:
            self.voiceRequested.emit()

    # ---------- 显示 / 收起 ----------

    def show_near(self, anchor: QWidget) -> None:
        """在挂件旁边弹出来（挂件靠右就弹在它左边）。"""
        rect = anchor.geometry()
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else rect
        x = rect.left() - self.width() - 10
        if x < area.left() + 4:
            x = rect.right() + 10
        x = max(area.left() + 4, min(x, area.right() - self.width() - 4))
        y = rect.center().y() - self.height() // 2
        y = max(area.top() + 4, min(y, area.bottom() - self.height() - 4))
        self.move(int(x), int(y))
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def close_panel(self) -> None:
        if self.isVisible():
            self.hide()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.set_listening(False)
        self.closed.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_panel()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------- 外观 ----------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(1, 1, self.width() - 2, self.height() - 2, RADIUS, RADIUS)
        painter.setBrush(QColor(18, 22, 30, 244))
        painter.setPen(QPen(QColor(ACCENT.red(), ACCENT.green(), ACCENT.blue(), 190), 1.6))
        painter.drawPath(path)
        painter.end()
