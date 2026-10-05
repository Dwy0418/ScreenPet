"""Qt 逻辑坐标 <-> 屏幕物理像素的换算。

Qt6 在 Windows 上默认是 per-monitor DPI aware，窗口坐标是"逻辑像素"；
而 mss 抓屏用的是"物理像素"。开了 125% 缩放的两者会差 1.25 倍，
不换算的话框选的区域会整体偏掉。
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import QRect
from PySide6.QtGui import QGuiApplication


def _dpr_for(rect: QRect) -> float:
    """取矩形所在屏幕的缩放比；找不到就退回主屏。"""
    screen = QGuiApplication.screenAt(rect.center()) if rect.isValid() else None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return 1.0
    dpr = float(screen.devicePixelRatio() or 1.0)
    return dpr if dpr > 0 else 1.0


def logical_rect_to_physical(rect: QRect) -> Dict[str, int]:
    """Qt 逻辑矩形 -> mss 需要的物理像素字典。"""
    dpr = _dpr_for(rect)
    return {
        "left": int(round(rect.x() * dpr)),
        "top": int(round(rect.y() * dpr)),
        "width": int(round(rect.width() * dpr)),
        "height": int(round(rect.height() * dpr)),
    }


def physical_rect_to_logical(region: Optional[Dict[str, int]]) -> Optional[QRect]:
    """配置里的物理像素矩形 -> Qt 逻辑矩形（用于判断是否互相遮挡）。"""
    if not region:
        return None
    width = int(region.get("width") or 0)
    height = int(region.get("height") or 0)
    if width <= 0 or height <= 0:
        return None
    probe = QRect(int(region.get("left", 0)), int(region.get("top", 0)), width, height)
    dpr = _dpr_for(probe)
    return QRect(
        int(round(probe.x() / dpr)),
        int(round(probe.y() / dpr)),
        int(round(width / dpr)),
        int(round(height / dpr)),
    )


def virtual_desktop_logical() -> QRect:
    """所有屏幕合起来的总区域（Qt 逻辑坐标），用于铺满全屏的框选遮罩。"""
    rect = QRect()
    for screen in QGuiApplication.screens():
        rect = rect.united(screen.geometry())
    return rect
