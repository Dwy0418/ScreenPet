"""好友系统面板：加好友、看谁在门口、去谁家串门、把客人请回家。

和聊天面板同一套皮（无边框 + 深色玻璃），但它是**控制台**不是对话：
每个动作都直接交给 `friends.VisitHub`（那些方法本身就跑在 GUI 线程，
网络/模型部分在里面自己丢后台线程，见 friends.py 的 _spawn）。

"出门"只有一个入口：**加了好友之后，好友那一行上的「去串门」**
（以前顶上还有一行"让它出门：去别的屏幕逛逛（本机）"，那是在自己的屏幕上换个位置，
跟串门是两回事，已经删掉——出门就该是去谁家）。

它不改配置、不落盘——好友簿的读写全在 `FriendBook` 那边。
"""
from __future__ import annotations

import time
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication, QPainter, QPainterPath, QPen, QColor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import style as style_mod
from .friends import FriendBook

#: 面板描边那点强调色：同 chatpanel，取出厂长相的 accent（定义都在 style.py）
ACCENT = QColor(style_mod.PetStyle().accent)

PANEL_W = 420
PAD = 12
RADIUS = 14
NOTE_H = 84
MAX_FRIEND_ROWS = 8
# 「好友那边主人这会儿在忙什么」这句显示多久（过了就当过期，别再拿出来说）
DOING_TTL_SEC = 1800.0

BUTTON_QSS = (
    "QPushButton { background: rgba(124,224,255,45); border: none; border-radius: 8px;"
    " padding: 4px 10px; color: #DFF6FF; }"
    "QPushButton:hover { background: rgba(124,224,255,80); }"
    "QPushButton:disabled { color: #7D8A99; background: rgba(255,255,255,14); }"
)
LINE_QSS = (
    "QLineEdit { background: rgba(255,255,255,22); border: 1px solid rgba(124,224,255,90);"
    " border-radius: 8px; padding: 4px 8px; color: #EAF6FF; }"
    "QLineEdit:focus { border: 1px solid rgba(124,224,255,170); }"
)


class FriendPanel(QWidget):
    """好友系统的控制台（一个窗口，内容按状态重建）。"""

    closed = Signal()

    def __init__(self, cfg, hub, book, parent: Optional[QWidget] = None):
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool,
        )
        self.cfg = cfg
        self.hub = hub
        self.book = book
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(f"{cfg.persona.name}的好友系统")
        self._build()

    # ---------- 界面骨架 ----------

    def _build(self) -> None:
        font = QFont()
        font.setPointSizeF(max(9.0, float(self.cfg.ui.font_size)))
        self.setFont(font)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(PAD, PAD, PAD, PAD)
        layout.setSpacing(7)

        self.title = QLabel(f"{self.cfg.persona.name}的好友系统")
        title_font = QFont(font)
        title_font.setPointSizeF(max(8.5, font.pointSizeF() - 1.5))
        title_font.setBold(True)
        self.title.setFont(title_font)
        self.title.setStyleSheet("color: #9FB6CC;")
        layout.addWidget(self.title)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #7CE0FF; font-size: 11px;")
        layout.addWidget(self.status)

        # 我的名片：复制一行给对方（微信 / QQ 发过去就行）
        card_row = QHBoxLayout()
        card_row.setSpacing(6)
        self.card = QLineEdit(self)
        self.card.setReadOnly(True)
        self.card.setStyleSheet(LINE_QSS + "QLineEdit { color: #9FB6CC; }")
        card_row.addWidget(self.card, 1)
        self.copy_btn = QPushButton("复制名片", self)
        self.copy_btn.setStyleSheet(BUTTON_QSS)
        self.copy_btn.clicked.connect(self._copy_card)
        card_row.addWidget(self.copy_btn)
        layout.addLayout(card_row)

        # 收名片：把对方发来的一行粘进来
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self.card_in = QLineEdit(self)
        self.card_in.setPlaceholderText("把对方发来的名片粘在这儿（SPET1|…）")
        self.card_in.setStyleSheet(LINE_QSS)
        self.card_in.returnPressed.connect(self._remember_card)
        add_row.addWidget(self.card_in, 1)
        self.add_btn = QPushButton("加好友", self)
        self.add_btn.setStyleSheet(BUTTON_QSS)
        self.add_btn.clicked.connect(self._remember_card)
        add_row.addWidget(self.add_btn)
        layout.addLayout(add_row)

        # 动态区：门口的客人 / 好友列表 / 屏幕上的客人
        self.dynamic = QVBoxLayout()
        self.dynamic.setSpacing(5)
        layout.addLayout(self.dynamic)

        self.note = QTextEdit(self)
        self.note.setReadOnly(True)
        self.note.setFrameStyle(0)
        self.note.setFixedHeight(NOTE_H)
        self.note.setStyleSheet("QTextEdit { background: transparent; border: none; color: #EAF6FF; }")
        layout.addWidget(self.note)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self.close_btn = QPushButton("收起（Esc）", self)
        self.close_btn.setStyleSheet(BUTTON_QSS)
        self.close_btn.clicked.connect(self.close_panel)
        close_row.addWidget(self.close_btn)
        layout.addLayout(close_row)

    # ---------- 内容 ----------

    def _clear_dynamic(self) -> None:
        while self.dynamic.count():
            item = self.dynamic.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("color: #7CE0FF; font-size: 11px; padding-top: 4px;")
        return label

    def _row_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: #EAF6FF; font-size: 12px;")
        return label

    def _button(self, text: str, tooltip: str = "") -> QPushButton:
        button = QPushButton(text, self)
        button.setStyleSheet(BUTTON_QSS)
        if tooltip:
            button.setToolTip(tooltip)
        return button

    def refresh(self) -> None:
        """按现在的状态把内容重建一遍（门开没开、谁在门外、来没来客人）。"""
        try:
            self.card.setText(self.book.card())
        except Exception as exc:                       # 名片本身不该把面板搞崩
            self.card.setText(f"（名片生成失败：{exc}）")
        self._clear_dynamic()

        state = self.hub.state()
        if state.get("running"):
            self.status.setText(f"门口开着：{state.get('listen')}（口令 {self.book.token}）")
        else:
            self.status.setText(
                "门没开——见 config.json 的 friends.enabled / friends.port（端口被占也会起不来）"
            )

        away = str(state.get("away") or "")
        if away:
            row = self.book.get(away) or {}
            self.dynamic.addWidget(self._section(f"我家这只现在在「{FriendBook.label(row)}」家里"))
            self.dynamic.addLayout(self._away_row())

        knocks = self.hub.pending_knocks()
        if knocks:
            self.dynamic.addWidget(self._section("门口有人（要你点一下才算数）"))
            for knock in knocks:
                self.dynamic.addLayout(self._knock_row(knock))

        guests = self.hub.guests
        if guests:
            self.dynamic.addWidget(self._section(f"现在在我屏幕上（{len(guests)} 位）"))
            for guest in guests.values():
                self.dynamic.addLayout(self._guest_row(guest))

        if self.book.friends:
            self.dynamic.addWidget(self._section(f"好友（{len(self.book.friends)}）"))
            for row in self.book.friends[:MAX_FRIEND_ROWS]:
                self.dynamic.addLayout(self._friend_row(row))
        else:
            self.dynamic.addWidget(
                self._row_label("还没有好友：让对方复制名片发给你，粘在上面那一栏。"
                                "加上了就能在它那一行点「去串门」。")
            )

        self.adjustSize()

    def _away_row(self) -> QHBoxLayout:
        row_layout = QHBoxLayout()
        row_layout.setSpacing(6)
        doing = self._doing_note(self.hub.away_home or {})
        text = "它在别人家做客，到点自己会回来。"
        row_layout.addWidget(self._row_label(f"{text}{doing}"))
        home = self._button("喊它回家", "立刻把它从人家屏幕上撤回来")
        home.clicked.connect(lambda *_: self.hub.come_home("主人喊我回家"))
        row_layout.addWidget(home)
        poke = self._button("戳对方那只", "让它戳一下主人家的宠物")
        poke.clicked.connect(lambda *_: self.hub.poke_home())
        row_layout.addWidget(poke)
        play = self._button("一起玩一下", "两只同时做个动作（见 pet/play.py）")
        play.clicked.connect(lambda *_: self.hub.play_home())
        row_layout.addWidget(play)
        ask = self._button("问它在忙啥", "问一句「你家主人这会儿在忙什么」")
        ask.clicked.connect(lambda *_: self.hub.ask_home_doing())
        row_layout.addWidget(ask)
        return row_layout

    def _knock_row(self, knock) -> QHBoxLayout:
        profile = knock.get("profile") or {}
        row_layout = QHBoxLayout()
        row_layout.setSpacing(6)
        row_layout.addWidget(self._row_label(f"{FriendBook.label(profile)} 在门口"))
        pet_id = str(profile.get("pet_id") or "")
        yes = self._button("让它进来")
        yes.clicked.connect(lambda *_: self._let_in(pet_id))
        row_layout.addWidget(yes)
        no = self._button("今天先不")
        no.clicked.connect(lambda *_: self._refuse(pet_id))
        row_layout.addWidget(no)
        return row_layout

    def _guest_row(self, guest) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(4)
        row_layout = QHBoxLayout()
        row_layout.setSpacing(6)
        row_layout.addWidget(self._row_label(f"{guest.label}{self._doing_note({'doing': guest.doing})}"))
        pet_id = str(guest.pet_id)

        poke = self._button("戳一下")
        poke.clicked.connect(lambda *_: self.hub.poke_guest(pet_id))
        row_layout.addWidget(poke)

        talk = self._button("跟它说句话…")
        talk.clicked.connect(lambda *_: self._talk_to(pet_id, guest.label))
        row_layout.addWidget(talk)

        nudge = self._button("让它挪挪", "它俩都不说话时，让自家这只再搭一句")
        nudge.clicked.connect(lambda *_: self.hub.nudge(pet_id))
        row_layout.addWidget(nudge)

        home = self._button("请它回家")
        home.clicked.connect(lambda *_: self.hub.drop_guest(pet_id))
        row_layout.addWidget(home)
        box.addLayout(row_layout)

        # 第二行：一起玩 / 打听它那边主人在忙啥（见 pet/play.py、pet/doing.py）
        extra = QHBoxLayout()
        extra.setSpacing(6)
        play = self._button("一起玩一下", "两只同时做个动作")
        play.clicked.connect(lambda *_: self.hub.play_guest(pet_id))
        extra.addWidget(play)
        ask = self._button("问它那边在忙啥", "问一句「你家主人这会儿在忙什么」")
        ask.clicked.connect(lambda *_: self.hub.ask_guest_doing(pet_id))
        extra.addWidget(ask)
        extra.addStretch(1)
        box.addLayout(extra)
        return box

    def _friend_row(self, row) -> QHBoxLayout:
        row_layout = QHBoxLayout()
        row_layout.setSpacing(6)
        where = str(row.get("host") or "").strip()
        port = int(row.get("port") or 0)
        addr = f"{where}:{port}" if where and port else "还没见过面（等它敲门）"
        row_layout.addWidget(self._row_label(f"{FriendBook.label(row)} · {addr}{self._doing_note(row)}"))

        pet_id = str(row.get("pet_id") or "")
        go = self._button("去串门", "我的宠物去它那边待一会儿——这就是「出门」")
        go.clicked.connect(lambda *_: self._visit(pet_id))
        row_layout.addWidget(go)

        drop = self._button("忘掉")
        drop.clicked.connect(lambda *_: self._forget(pet_id))
        row_layout.addWidget(drop)
        return row_layout

    def _doing_note(self, row) -> str:
        """好友那一行后面缀一句「他这会儿在忙什么」（有、且不算太旧才显示）。

        这句话是**对方那边拼好发过来的那一句**（见 pet/doing.py），本地存一份而已；
        过了 `DOING_TTL_SEC` 就不再显示——"这会儿"过期了再拿出来说就不对了。
        """
        text = str((row or {}).get("doing") or "").strip()
        if not text:
            return ""
        at = float((row or {}).get("doing_at") or 0.0)
        if at and time.time() - at > DOING_TTL_SEC:
            return ""
        return f"（{text}）"

    # ---------- 动作（都只是转发给 hub，回来的样子靠 refresh） ----------

    def _say(self, text: str) -> None:
        self.note.append(str(text))

    def add_note(self, text: str) -> None:
        """外面（app）往面板下面那栏写一行——比如「正在敲门…」这种状态。"""
        self._say(text)

    def _copy_card(self) -> None:
        card = self.card.text().strip()
        if card:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(card)
        self._say("名片复制好了，发给好友就行（对方粘进他那边那一栏）。")

    def _remember_card(self) -> None:
        text = self.card_in.text().strip()
        if not text:
            return
        self._say("好友：" + self.hub.remember_card(text))
        self.card_in.clear()
        self.refresh()

    def _let_in(self, pet_id: str) -> None:
        self.hub.accept(pet_id)
        self._say("好，让它进来坐坐。")
        self.refresh()

    def _refuse(self, pet_id: str) -> None:
        self.hub.refuse(pet_id)
        self._say("跟对方说了今天不方便。")
        self.refresh()

    def _visit(self, pet_id: str) -> None:
        row = self.book.get(pet_id) or {}
        self.hub.visit(pet_id)
        self._say(f"去「{FriendBook.label(row)}」家串门：敲了门，等对方点头（等一会儿没应就算了）。")
        self.refresh()

    def _forget(self, pet_id: str) -> None:
        row = self.book.get(pet_id) or {}
        self.hub.forget(pet_id)
        self._say(f"忘了「{FriendBook.label(row)}」——它下次敲门还得你放行。")
        self.refresh()

    def _talk_to(self, pet_id: str, label: str) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(self, "跟客人说句话", f"对「{label}」说：")
        text = (text or "").strip()
        if not ok or not text:
            return
        if self.hub.talk_to_guest(pet_id, text):
            self._say(f"我说：{text}（话送到它那边，由它回你）")
        else:
            self._say("这句没送出去——可能它已经回家了，或者你把 allow_owner_chat 关了。")

    # ---------- 摆放 / 收起 ----------

    def show_near(self, anchor: QWidget) -> None:
        """弹在挂件旁边（跟聊天面板一个思路）。"""
        self.refresh()
        size = self.sizeHint()
        self.resize(max(PANEL_W, size.width()), size.height())
        screen = QGuiApplication.screenAt(anchor.geometry().center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = anchor.geometry().right() + 10
            if x + self.width() > area.right():
                x = max(area.left() + 4, anchor.geometry().left() - self.width() - 10)
            y = min(max(area.top() + 4, anchor.geometry().top()), area.bottom() - self.height() - 4)
            self.move(int(x), int(y))
        self.show()
        self.raise_()

    def close_panel(self) -> None:
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close_panel()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.closed.emit()

    # ---------- 皮 ----------

    def paintEvent(self, event) -> None:
        """深色玻璃 + 一圈青色微光：跟气泡、聊天面板一套。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(body, RADIUS, RADIUS)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(19, 24, 35, 242))
        painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        glow = QColor(ACCENT)
        glow.setAlpha(70)
        painter.setPen(QPen(glow, 1.4))
        painter.drawPath(path)
        painter.end()
