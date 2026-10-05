"""直接从一个窗口取画面——哪怕它被别的窗口盖住、或者根本不在前台。

为什么需要它
    mss 只能拍"屏幕上此刻显示的东西"。你一用别的窗口盖住它（或者切去干活），
    拍到的就全是上面那个窗口——所以老版本一失去焦点就干脆不工作。
    这里改用 Windows 自己的 PrintWindow：让窗口把**自己**画一遍到离屏位图上，
    不依赖它在不在最前面。于是"你在写代码、它在旁边看视频"也成立。

能拿到什么 / 拿不到什么
    * 能：没最小化、没被系统挂起的普通窗口（浏览器、Electron 应用、大部分播放器）
    * 拿不到：最小化的时候（Windows 根本不渲染它）；少数用独占全屏 / 硬件覆盖层的播放器
      只会给出一张黑图——这两种情况我们都返回 None，调用方退回"拍屏幕"的老路，
      不会拿黑图去糊弄模型。

最小化的窗口（`allow_minimized=True`）单独说一句
    默认直接返回 None（下面 `grab` 里那道 IsIconic 闸）。实测两种典型结果，都不是能看的东西：

        Chromium 浏览器（1370×929）  全黑 + 左上角一点灰，平均亮度 0.3
        Tk / GDI 窗口（436×299）     **只剩标题栏那三个按钮**，其余全黑（暗像素 96.6%）

    后一种最坑：那三个按钮够亮，`is_blank`（看"最亮-最暗差"）判它"不空白"，
    拿着它能去问模型，模型只会说"怎么黑屏了"。所以这条路另配一道 `looks_unpainted`。

    那为什么还留 `allow_minimized=True` 这个口子？因为"最小化就不画"是**大多数程序**的
    行为，不是 Windows 的铁律：真碰上收起来还在往 DC 里画内容的老程序，那就是白白丢帧。
    调用方（`worker._grab_minimized`）的策略是"先问一句，拿不到就按没画处理、回放上一眼"，
    代价是每 30 秒一次 PrintWindow。

    顺带一个坑：最小化时 GetWindowRect 是假的（实测 `160×28 @(-32000,-32000)`），
    按它开位图只会得到一条 160×28 的黑缝。所以这条路一律按 `winfind.restored_rect`
    的**还原尺寸**开位图，画的才是它本来的大小。

非 Windows 上 available() 返回 False，调用方自动走老路。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Optional

from PIL import Image

from . import winfind

# PrintWindow 的 flag：让用 DirectComposition / 硬件合成的窗口（浏览器、Electron）
# 也把完整内容画出来，否则经常只给一张黑图
PW_RENDERFULLCONTENT = 0x00000002

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

# 一张图里最亮和最暗差不过这个数，就当成"什么都没画出来"（黑图/白图）
BLANK_SPREAD = 8

# 「根本没画内容」的另一把尺子：整张图里大部分像素都是黑的
# （亮度 < DARK_LEVEL 的占比 >= UNPAINTED_DARK）。
#
# 为什么 BLANK_SPREAD 不够用：最小化的窗口经常交回来一张"**只剩标题栏那几个按钮**、
# 其余全黑"的图——实测一个 436×299 的窗口收起来之后：暗像素占 96.6%、平均亮度 8.2，
# 而那三个按钮足够亮，最亮-最暗差早超过 8 了，`is_blank` 判它"不空白"。
# 拿这种图去问模型，它只会说"怎么黑屏了"。所以最小化那条路再加这一道。
DARK_LEVEL = 16
UNPAINTED_DARK = 0.90


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


if os.name == "nt":
    _USER32 = ctypes.WinDLL("user32", use_last_error=True)
    _GDI32 = ctypes.WinDLL("gdi32", use_last_error=True)

    _USER32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _USER32.GetWindowRect.restype = wintypes.BOOL
    _USER32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _USER32.GetClientRect.restype = wintypes.BOOL
    _USER32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    _USER32.ClientToScreen.restype = wintypes.BOOL
    _USER32.GetWindowDC.argtypes = [wintypes.HWND]
    _USER32.GetWindowDC.restype = wintypes.HDC
    _USER32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _USER32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    _USER32.PrintWindow.restype = wintypes.BOOL
    _USER32.IsWindow.argtypes = [wintypes.HWND]
    _USER32.IsWindow.restype = wintypes.BOOL
    _USER32.IsIconic.argtypes = [wintypes.HWND]
    _USER32.IsIconic.restype = wintypes.BOOL

    _GDI32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _GDI32.CreateCompatibleDC.restype = wintypes.HDC
    _GDI32.DeleteDC.argtypes = [wintypes.HDC]
    _GDI32.DeleteDC.restype = wintypes.BOOL
    _GDI32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    _GDI32.SelectObject.restype = wintypes.HGDIOBJ
    _GDI32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    _GDI32.DeleteObject.restype = wintypes.BOOL
    _GDI32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    _GDI32.BitBlt.restype = wintypes.BOOL
    _GDI32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    _GDI32.CreateDIBSection.restype = wintypes.HBITMAP
else:  # pragma: no cover - 非 Windows 只为让 import 不炸
    _USER32 = None
    _GDI32 = None


def available() -> bool:
    """这台机器能不能直接抓窗口画面。"""
    return _USER32 is not None and _GDI32 is not None


def is_blank(image: Optional[Image.Image]) -> bool:
    """整张图几乎一个颜色（全黑/全白）= 这次没画出来，别拿去问模型。"""
    if image is None or image.width < 2 or image.height < 2:
        return True
    small = image.convert("L").resize((24, 24), Image.BILINEAR)
    data = list(small.getdata())
    return (max(data) - min(data)) <= BLANK_SPREAD



def grab(hwnd: int, client_only: bool = True, allow_minimized: bool = False) -> Optional[Image.Image]:
    """抓这个窗口现在的画面；抓不到（最小化 / 黑图 / 已关闭）返回 None。

    client_only=True 时按内容区裁掉标题栏和边框，和 mss 那边的矩形口径一致。

    allow_minimized=True 时最小化的窗口也试一把（默认直接放弃，理由见模块开头）：
    这种窗口一般画不出东西，画得出来（老式 GDI 程序）那就是真画面，照收。
    注意它一定**不裁标题栏**——最小化时那个窗口矩形是假的，裁出来的只有错位的一角。
    """
    if not available() or not hwnd:
        return None
    handle = wintypes.HWND(int(hwnd))
    if not _USER32.IsWindow(handle):
        return None
    minimized = bool(_USER32.IsIconic(handle))
    if minimized and not allow_minimized:
        return None

    win_rect = wintypes.RECT()
    if minimized:
        # 最小化时 GetWindowRect 给的是"缩成一坨"的假数（-32000,-32000 / 160×28），
        # 按它开位图只能得到一小条黑缝：改用「还原之后」的尺寸
        restored = winfind.restored_rect(int(hwnd))
        if restored is None:
            return None
        win_rect.left, win_rect.top = int(restored["left"]), int(restored["top"])
        win_rect.right = win_rect.left + int(restored["width"])
        win_rect.bottom = win_rect.top + int(restored["height"])
    elif not _USER32.GetWindowRect(handle, ctypes.byref(win_rect)):
        return None
    width = int(win_rect.right - win_rect.left)
    height = int(win_rect.bottom - win_rect.top)
    if width < 16 or height < 16:
        return None

    hdc_win = _USER32.GetWindowDC(handle)
    if not hdc_win:
        return None
    hdc_mem = None
    bitmap = None
    old_bitmap = None
    try:
        hdc_mem = _GDI32.CreateCompatibleDC(hdc_win)
        if not hdc_mem:
            return None
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height        # 负数 = 从上到下存，省一次翻转
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0         # BI_RGB
        bits = ctypes.c_void_p()
        bitmap = _GDI32.CreateDIBSection(
            hdc_mem, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        if not bitmap or not bits:
            return None
        old_bitmap = _GDI32.SelectObject(hdc_mem, bitmap)

        painted = bool(_USER32.PrintWindow(handle, hdc_mem, PW_RENDERFULLCONTENT))
        if not painted:
            painted = bool(_USER32.PrintWindow(handle, hdc_mem, 0))
        if not painted:
            # 老程序不认 PrintWindow：那就从窗口自己的 DC 上拷一份
            _GDI32.BitBlt(hdc_mem, 0, 0, width, height, hdc_win, 0, 0, SRCCOPY)

        raw = ctypes.string_at(bits, width * height * 4)
        image = Image.frombuffer("RGBA", (width, height), raw, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        if hdc_mem and old_bitmap:
            _GDI32.SelectObject(hdc_mem, old_bitmap)
        if bitmap:
            _GDI32.DeleteObject(bitmap)
        if hdc_mem:
            _GDI32.DeleteDC(hdc_mem)
        _USER32.ReleaseDC(handle, hdc_win)

    if client_only and not minimized:
        # 最小化时窗口矩形是"还原后"的，ClientToScreen 那套换算对不上，别裁（见 grab 的说明）
        image = _crop_client(handle, image, win_rect)
    if is_blank(image):
        return None
    if minimized and looks_unpainted(image):
        # 最小化时"没画内容"最常见的形态不是纯黑，而是**只剩标题栏按钮**的那种黑
        # （is_blank 拦不住，见上面的说明）：这种图不能当画面用，交回给调用方回放上一眼
        return None
    return image


def looks_unpainted(image: Image.Image) -> bool:
    """这张图是不是"根本没画内容"（全黑 / 只剩下窗口边框那几个亮块）。

    专门给"最小化的窗口"用（见 `grab` 的 allow_minimized）：那时 PrintWindow 交回来的
    常常是一张黑底 + 标题栏按钮的图，肉眼一看就知道不是画面。判据很直白——
    亮度低于 `DARK_LEVEL` 的像素占到 `UNPAINTED_DARK` 以上就算没画。

    大图抽样算，别为了这点判定在每一帧上多花几十毫秒；抽样是**跳着取**不是缩放
    （缩放会把一条细亮线糊成灰的，反而判不出来）。
    """
    try:
        gray = image.convert("L")
        pixels = list(gray.getdata())
    except Exception:
        return False
    if not pixels:
        return True
    step = max(1, len(pixels) // 200000)
    sample = pixels[::step]
    dark = sum(1 for value in sample if value < DARK_LEVEL)
    return dark / len(sample) >= UNPAINTED_DARK


def _crop_client(hwnd: wintypes.HWND, image: Image.Image, win_rect: wintypes.RECT) -> Image.Image:
    """把标题栏和边框裁掉，只留内容区（和 mss 那条路径的矩形对齐）。"""
    try:
        origin = wintypes.POINT(0, 0)
        if not _USER32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return image
        client = wintypes.RECT()
        if not _USER32.GetClientRect(hwnd, ctypes.byref(client)):
            return image
        width = int(client.right - client.left)
        height = int(client.bottom - client.top)
        left = int(origin.x - win_rect.left)
        top = int(origin.y - win_rect.top)
        if width >= 16 and height >= 16 and 0 <= left and 0 <= top:
            right = min(image.width, left + width)
            bottom = min(image.height, top + height)
            if right - left >= 16 and bottom - top >= 16:
                return image.crop((left, top, right, bottom))
    except Exception:  # 裁失败就用整窗，别把这一轮弄丢
        pass
    return image
