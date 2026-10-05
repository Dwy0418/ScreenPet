"""取当前前台窗口的标题。

很便宜的一个线索：标题里往往写着"王者荣耀""XX直播间""B站"，比模型看半天画面还准。
只读标题，不读文件、不读进程内存。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

MAX_CHARS = 260


def foreground_title(limit: int = 120) -> str:
    """返回前台窗口标题；拿不到就返回空串。"""
    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        handle = user32.GetForegroundWindow()
        if not handle:
            return ""
        length = int(user32.GetWindowTextLengthW(handle))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(min(length, MAX_CHARS) + 1)
        user32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value.strip()[:limit]
    except Exception:
        return ""


def process_name() -> str:
    """前台窗口属于哪个进程（用于区分浏览器 / 独立客户端）。"""
    if os.name != "nt":
        return ""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        handle = user32.GetForegroundWindow()
        if not handle:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            return ""
        try:
            size = wintypes.DWORD(MAX_CHARS)
            buffer = ctypes.create_unicode_buffer(MAX_CHARS)
            if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return os.path.basename(buffer.value)
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        return ""
    return ""


def describe(limit: int = 120) -> str:
    """拼成一行给模型看的上下文，比如 "抖音精选 - Google Chrome（chrome.exe）"。"""
    title = foreground_title(limit)
    if not title:
        return ""
    name = process_name()
    return f"{title}（{name}）" if name else title
