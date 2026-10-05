"""全局热键：Windows 的 RegisterHotKey，零依赖、不用管理员权限。

在独立线程里注册并跑消息循环（GetMessage），按下时通过 Qt 信号通知主线程
（跨线程 emit 会自动排队到主线程，安全）。
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIER_ALIASES: Dict[str, int] = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "ctl": MOD_CONTROL,
    "alt": MOD_ALT, "option": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "cmd": MOD_WIN, "meta": MOD_WIN,
}

_SPECIAL_KEYS: Dict[str, int] = {
    "space": 0x20, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "enter": 0x0D, "return": 0x0D, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}

_MODIFIER_KEYS = {"ctrl", "control", "alt", "shift", "win", "meta", "super", "cmd", "option"}


def parse_hotkey(spec: str) -> Optional[Tuple[int, int]]:
    """把 "ctrl+alt+m" 解析成 (修饰键位掩码, 虚拟键码)；非法返回 None。"""
    text = (spec or "").strip().lower()
    if not text:
        return None
    modifiers = 0
    key_code = 0
    for part in [p.strip() for p in text.replace(" ", "+").split("+") if p.strip()]:
        if part in _MODIFIER_ALIASES:
            modifiers |= _MODIFIER_ALIASES[part]
            continue
        if len(part) == 1 and part.isalnum():
            key_code = ord(part.upper())
        elif part.isdigit() is False and part.startswith("f") and part[1:].isdigit():
            index = int(part[1:])
            if not 1 <= index <= 24:
                return None
            key_code = 0x6F + index  # F1 = 0x70
        elif part in _SPECIAL_KEYS:
            key_code = _SPECIAL_KEYS[part]
        else:
            return None
    if not key_code or not modifiers:
        return None
    return modifiers | MOD_NOREPEAT, key_code


def format_hotkey(spec: str) -> str:
    """规范化成 "Ctrl+Alt+M" 这种好看的样子。"""
    parsed = parse_hotkey(spec)
    if not parsed:
        return spec or ""
    modifiers, key_code = parsed
    parts: List[str] = []
    if modifiers & MOD_CONTROL:
        parts.append("Ctrl")
    if modifiers & MOD_ALT:
        parts.append("Alt")
    if modifiers & MOD_SHIFT:
        parts.append("Shift")
    if modifiers & MOD_WIN:
        parts.append("Win")
    if 0x41 <= key_code <= 0x5A or 0x30 <= key_code <= 0x39:
        parts.append(chr(key_code))
    elif 0x70 <= key_code <= 0x87:
        parts.append(f"F{key_code - 0x6F}")
    else:
        for name, code in _SPECIAL_KEYS.items():
            if code == key_code:
                parts.append(name.capitalize())
                break
        else:
            parts.append(f"0x{key_code:02X}")
    return "+".join(parts)


class HotkeyListener(QThread):
    """注册一组全局热键，按下时 emit triggered(action)。"""

    triggered = Signal(str)
    failed = Signal(str, str)  # (action, 原因)

    HOTKEY_BASE_ID = 0xB100

    def __init__(self, bindings: Dict[str, str], parent=None):
        super().__init__(parent)
        self.bindings = dict(bindings or {})
        self._stop_flag = threading.Event()
        self._ready = threading.Event()
        self._thread_id = 0
        self.registered: List[str] = []

    # ---------- 生命周期 ----------

    def stop(self) -> None:
        self._stop_flag.set()
        self._ready.wait(1.0)
        if self._thread_id and os.name == "nt":
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass

    # ---------- 线程体 ----------

    def run(self) -> None:  # noqa: C901 - 消息循环本身就不适合拆
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = int(kernel32.GetCurrentThreadId())

        mapping: Dict[int, str] = {}
        for index, (action, spec) in enumerate(self.bindings.items()):
            parsed = parse_hotkey(spec)
            if not parsed:
                if spec:
                    self.failed.emit(action, f"热键写法看不懂：{spec}")
                continue
            modifiers, key_code = parsed
            hotkey_id = self.HOTKEY_BASE_ID + index
            if user32.RegisterHotKey(None, hotkey_id, modifiers, key_code):
                mapping[hotkey_id] = action
                self.registered.append(f"{action}={format_hotkey(spec)}")
            else:
                self.failed.emit(action, f"{format_hotkey(spec)} 已被其它程序占用")

        self._ready.set()
        if not mapping:
            return

        message = wintypes.MSG()
        try:
            while not self._stop_flag.is_set():
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result in (0, -1):  # WM_QUIT 或出错
                    break
                if message.message == WM_HOTKEY:
                    action = mapping.get(int(message.wParam))
                    if action:
                        self.triggered.emit(action)
        finally:
            for hotkey_id in list(mapping.keys()):
                try:
                    user32.UnregisterHotKey(None, hotkey_id)
                except Exception:
                    pass
