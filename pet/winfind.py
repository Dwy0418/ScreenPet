"""按进程找窗口：给「只盯着某个程序」用（抖音、B站、游戏、浏览器…）。

只要 Windows 自己的信息，不装依赖、不读别人的进程内存：
  * 有哪些看得见的顶层窗口、标题是什么、属于哪个 exe
  * 这个窗口的**内容区**（客户区，不含标题栏和边框）在屏幕上的物理像素矩形，
    直接交给 mss 抓屏

最小化的窗口（`include_minimized=True` 才列出来）单独说一句：
    这时 GetWindowRect / GetClientRect 给的是**缩成一坨的假几何**——实测 1370×929 的
    浏览器收起来之后是 `160×28 @(-32000,-32000)`。所以最小化的窗口一律改用
    `restored_rect()`（GetWindowPlacement 的 rcNormalPosition），也就是**还原之后**它
    会占哪块。这个矩形只用来判断"它还在、多大"（托盘怎么显示、挂件挡没挡住），
    **别拿去抓屏**——那块地方此刻是别的窗口，拍下来的是别人的画面。

坐标说明：Qt6 会把进程设成 per-monitor DPI aware，所以我们拿到的客户区矩形本来
就是物理像素，和 mss 的坐标系一致（125% 缩放下也不用再换算）。

非 Windows 上所有函数返回空 / None，挂件自动退回「整屏 / 框选」模式。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from typing import Dict, List, Optional

MAX_CHARS = 260

# 比这个还小的窗口不列进菜单（任务栏小窗、输入法候选框之类）
MIN_SIDE = 160

# DwmGetWindowAttribute 的 DWMWA_CLOAKED：UWP 应用「挂着但不在屏幕上」的窗口
DWMWA_CLOAKED = 14

# 取 exe 名要的最小权限（QueryFullProcessImageName 够用了，不需要 PROCESS_VM_READ）
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _WINDOWPLACEMENT(ctypes.Structure):
    """GetWindowPlacement 的返回结构（我们只要 rcNormalPosition：还原之后占哪块）。

    `ctypes.wintypes` 里没有这个结构，所以自己摆一份；字段顺序必须跟 Windows 的一致，
    少一个字段后面读出来的就是垃圾。
    """

    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT),
    ]


if os.name == "nt":
    _USER32 = ctypes.WinDLL("user32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        _DWMAPI = ctypes.WinDLL("dwmapi", use_last_error=True)
    except OSError:  # pragma: no cover - 极老的系统
        _DWMAPI = None

    _USER32.GetForegroundWindow.restype = wintypes.HWND
    _USER32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _USER32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _USER32.IsWindowVisible.argtypes = [wintypes.HWND]
    _USER32.IsIconic.argtypes = [wintypes.HWND]
    _USER32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _USER32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _USER32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _USER32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
    _ENUM_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    _USER32.EnumWindows.argtypes = [_ENUM_PROC, wintypes.LPARAM]
    _USER32.EnumWindows.restype = ctypes.c_bool
    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
else:  # pragma: no cover - 只为让非 Windows 上 import 不炸
    _USER32 = None
    _KERNEL32 = None
    _DWMAPI = None
    _ENUM_PROC = None


def available() -> bool:
    """这台机器能不能按进程找窗口。"""
    return _USER32 is not None


@dataclass(frozen=True)
class WindowInfo:
    """一个候选窗口：标题 + 进程 + 客户区矩形（物理像素）。"""

    hwnd: int
    title: str
    process: str
    pid: int
    left: int
    top: int
    width: int
    height: int
    foreground: bool = False
    minimized: bool = False

    @property
    def rect(self) -> Dict[str, int]:
        """窗口（或客户区）在屏幕上的物理像素矩形。

        最小化的窗口给的是**还原之后**那块（Windows 收起来时那两个数是假的，
        见 restored_rect），别拿它去抓屏。
        """
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def label(self) -> str:
        """菜单里那一行，比如「抖音 - Google Chrome · chrome.exe」。"""
        title = (self.title or "").strip()
        if len(title) > 28:
            title = title[:28] + "…"
        name = self.process or "未知程序"
        return f"{title} · {name}" if title else name

    def matches(self, process: str = "", title: str = "") -> bool:
        wanted_process = (process or "").strip().lower()
        wanted_title = (title or "").strip().lower()
        name = (self.process or "").lower()
        if wanted_process:
            if name != wanted_process and wanted_process not in name and name not in wanted_process:
                return False
        if wanted_title and wanted_title not in (self.title or "").lower():
            return False
        return True


def process_name_of(pid: int) -> str:
    """pid -> exe 文件名（比如 Douyin.exe）。拿不到就返回空串。"""
    if _KERNEL32 is None or not pid:
        return ""
    handle = _KERNEL32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(MAX_CHARS)
        buffer = ctypes.create_unicode_buffer(MAX_CHARS)
        if _KERNEL32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value)
    except Exception:
        return ""
    finally:
        try:
            _KERNEL32.CloseHandle(handle)
        except Exception:
            pass
    return ""


def window_pid(hwnd: int) -> int:
    if _USER32 is None or not hwnd:
        return 0
    try:
        pid = wintypes.DWORD()
        _USER32.GetWindowThreadProcessId(wintypes.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def foreground_hwnd() -> int:
    if _USER32 is None:
        return 0
    try:
        return int(_USER32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def foreground_pid() -> int:
    return window_pid(foreground_hwnd())


def is_own_pid(pid: int) -> bool:
    """这个 pid 是不是我们自己（挂件、气泡、聊天面板都算自己）。"""
    return bool(pid) and int(pid) == os.getpid()


def window_title(hwnd: int) -> str:
    if _USER32 is None or not hwnd:
        return ""
    try:
        handle = wintypes.HWND(int(hwnd))
        length = int(_USER32.GetWindowTextLengthW(handle))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(min(length, MAX_CHARS) + 1)
        _USER32.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value.strip()
    except Exception:
        return ""


def window_rect(
    hwnd: int, client_area: bool = True, allow_minimized: bool = False
) -> Optional[Dict[str, int]]:
    """窗口在屏幕上的物理像素矩形；最小化的窗口返回 None。

    client_area=True 只取内容区（看视频要的就是这块），False 连标题栏和边框一起算。
    allow_minimized=True 时最小化的窗口也给个尺寸（位置不可信，只当"它还在"用）。
    """
    if _USER32 is None or not hwnd:
        return None
    handle = wintypes.HWND(int(hwnd))
    try:
        if _USER32.IsIconic(handle) and not allow_minimized:
            return None
        rect = wintypes.RECT()
        if client_area:
            if not _USER32.GetClientRect(handle, ctypes.byref(rect)):
                return None
            point = wintypes.POINT(0, 0)
            if not _USER32.ClientToScreen(handle, ctypes.byref(point)):
                return None
            left, top = int(point.x), int(point.y)
        else:
            if not _USER32.GetWindowRect(handle, ctypes.byref(rect)):
                return None
            left, top = int(rect.left), int(rect.top)
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


def restored_rect(hwnd: int) -> Optional[Dict[str, int]]:
    """最小化的窗口「还原之后」会占哪块（物理像素）；拿不到返回 None。

    为什么非它不可：窗口一最小化，Windows 就把它缩成一小坨挪到屏幕外，
    这时候 GetWindowRect / GetClientRect 给出来的**全是假数**——实测一台 1370×929 的
    浏览器收起来之后是 `160×28 @(-32000,-32000)`。想说出"它多大"就只有问
    GetWindowPlacement 的 rcNormalPosition（它记的是还原后的位置和大小）。

    位置跟着能当"它在哪"，但**别拿它去抓屏**：那块地方此刻站着的是别的窗口
    （见模块开头那段）。
    """
    if _USER32 is None or not hwnd:
        return None
    try:
        placement = _WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
        if not _USER32.GetWindowPlacement(wintypes.HWND(int(hwnd)), ctypes.byref(placement)):
            return None
        rect = placement.rcNormalPosition
    except Exception:
        return None
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        return None
    return {"left": int(rect.left), "top": int(rect.top), "width": width, "height": height}


def _is_cloaked(hwnd: int) -> bool:
    """UWP 应用「还活着但不在屏幕上」的窗口：DWM 会标成 cloaked，别列出来。"""
    if _DWMAPI is None:
        return False
    try:
        value = wintypes.DWORD(0)
        result = _DWMAPI.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
        )
        return result == 0 and bool(value.value)
    except Exception:
        return False


def _dedupe(windows: List[WindowInfo]) -> List[WindowInfo]:
    """同进程同标题的窗口只留一个（面积最大的那个），再按「前台优先、大的在前」排序。"""
    ordered = sorted(windows, key=lambda w: (not w.foreground, -w.area, w.process.lower(), w.title))
    seen = set()
    kept: List[WindowInfo] = []
    for info in ordered:
        key = (info.process.lower(), info.title)
        if key in seen:
            continue
        seen.add(key)
        kept.append(info)
    return kept


def list_windows(
    min_side: int = MIN_SIDE, client_area: bool = True, include_minimized: bool = False
) -> List[WindowInfo]:
    """所有「看得见、有标题、够大」的顶层窗口，前台排在最前面。

    include_minimized=True 时也把最小化的窗口列出来（只用来判断"它还在，只是收起来了"；
    这种窗口的矩形给的是**还原之后**那块，别拿去抓屏——见 restored_rect）。

    自动跳过：隐藏的、最小化的、cloaked 的、没标题的、太小的、我们自己开的窗口。
    """
    if _USER32 is None:
        return []
    own_pid = os.getpid()
    front = foreground_hwnd()
    found: List[WindowInfo] = []

    def _visit(hwnd, _lparam):
        try:
            if not hwnd:
                return True
            handle = int(hwnd)
            typed = wintypes.HWND(handle)
            minimized = bool(_USER32.IsIconic(typed))
            if not _USER32.IsWindowVisible(typed) and not (include_minimized and minimized):
                return True
            if minimized and not include_minimized:
                return True
            title = window_title(handle)
            if not title:
                return True
            pid = window_pid(handle)
            if not pid or pid == own_pid:
                return True
            if _is_cloaked(handle):
                return True
            if minimized:
                # 最小化时那两个几何 API 都不靠谱：GetClientRect 有的程序返回 0（窗口会在这里
                # 被直接丢掉 → 变成"没找到窗口"），有的返回缩成一坨的假尺寸（160×28 @-32000）。
                # 一律改用"还原之后"那块，拿不到才退回原来那套。
                rect = restored_rect(handle) or window_rect(
                    handle, client_area=client_area, allow_minimized=True
                )
            else:
                rect = window_rect(handle, client_area=client_area)
            if rect is None:
                return True
            if not minimized and (rect["width"] < min_side or rect["height"] < min_side):
                return True
            if minimized:
                # 最小化时 Windows 把它缩成一小坨挪到屏幕外，那两个数是假的：
                # 换成「还原之后」那块矩形——只用来判断"它还在、多大"，
                # 谁都不许拿它去抓屏（那块地方此刻是别的窗口，见 restored_rect）
                full = restored_rect(handle)
                if full is not None:
                    rect = full
            found.append(
                WindowInfo(
                    hwnd=handle,
                    title=title,
                    process=process_name_of(pid),
                    pid=pid,
                    left=rect["left"],
                    top=rect["top"],
                    width=rect["width"],
                    height=rect["height"],
                    foreground=handle == front,
                    minimized=minimized,
                )
            )
        except Exception:
            return True
        return True

    callback = _ENUM_PROC(_visit)
    try:
        _USER32.EnumWindows(callback, 0)
    except Exception:
        return []
    return _dedupe(found)


def list_apps(min_side: int = MIN_SIDE) -> List[WindowInfo]:
    """每个进程只留一个代表窗口——菜单里选「盯着哪个程序」用这个。

    同一个 exe 开了好几个窗口时（比如 Chrome 好几个标签页窗口），取前台那个，
    都没在前台的取面积最大的；选中之后只按 exe 名盯，标题变了也不影响。
    """
    by_process: Dict[str, WindowInfo] = {}
    for info in list_windows(min_side=min_side):
        name = (info.process or "").lower()
        if not name:
            continue
        best = by_process.get(name)
        if best is None or (info.foreground and not best.foreground) or (
            info.foreground == best.foreground and info.area > best.area
        ):
            by_process[name] = info
    return sorted(by_process.values(), key=lambda w: (not w.foreground, -w.area, w.process.lower()))


def find_window(
    process: str, title: str = "", client_area: bool = True, include_minimized: bool = False
) -> Optional[WindowInfo]:
    """按 exe 名（可选再按标题关键字）找那个该盯的窗口；找不到返回 None。

    exe 名匹配是「包含」关系，所以填 `douyin` 也能命中 `Douyin.exe`。
    多个窗口都命中时：没最小化的优先 → 前台的优先 → 面积大的（主窗口一般比浮层大）。
    include_minimized=True 时最小化的窗口也算命中（调用方靠 info.minimized 分辨）。
    """
    wanted_process = (process or "").strip()
    wanted_title = (title or "").strip()
    if not wanted_process and not wanted_title:
        return None
    best: Optional[WindowInfo] = None
    for info in list_windows(client_area=client_area, include_minimized=include_minimized):
        if not info.matches(wanted_process, wanted_title):
            continue
        if best is None:
            best = info
        elif best.minimized and not info.minimized:
            best = info
        elif info.minimized == best.minimized:
            if info.foreground and not best.foreground:
                best = info
            elif info.foreground == best.foreground and info.area > best.area:
                best = info
    return best


def describe(process: str, title: str = "") -> str:
    """一行文字：现在盯着谁、窗口多大、在不在前台（气泡和托盘里显示用）。"""
    info = find_window(process, title, include_minimized=True)
    if info is None:
        return f"{process}（没找到窗口）"
    if info.minimized:
        return f"{info.title[:24]}（{info.process}，最小化了，还原后 {info.width}×{info.height}）"
    state = "前台" if info.foreground else "后台"
    return f"{info.title[:24]}（{info.process}，{info.width}×{info.height}，{state}）"
