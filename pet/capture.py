"""截屏 / 缩放 / 感知哈希 / JPEG 编码。

设计要点：
* 画面没变化就不去调用大模型（dHash 汉明距离），既省钱又不会一直碎碎念。
* 只保留一帧图片，不做录像，隐私和带宽都可控。
"""
from __future__ import annotations

import base64
import io
from typing import Dict, List, Optional

from PIL import Image

try:
    import mss
except Exception:  # pragma: no cover - 依赖缺失时给友好提示
    mss = None

# mss 10 起推荐用 mss.MSS，旧版本只有 mss.mss
_MSS = getattr(mss, "MSS", None) or getattr(mss, "mss", None) if mss else None

DEFAULT_HASH_SIZE = 8  # 8x8 -> 64 bit 指纹


def grab(region: Optional[Dict[str, int]] = None) -> Image.Image:
    """抓一帧屏幕。region 为物理像素字典；传 None 抓整个虚拟桌面。"""
    if _MSS is None:
        raise RuntimeError("缺少依赖 mss，请先执行：python -m pip install -r requirements.txt")
    with _MSS() as sct:
        if region and int(region.get("width") or 0) > 0 and int(region.get("height") or 0) > 0:
            box = {
                "left": int(region["left"]),
                "top": int(region["top"]),
                "width": int(region["width"]),
                "height": int(region["height"]),
            }
        else:
            box = sct.monitors[0]
        shot = sct.grab(box)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def list_monitors() -> List[Dict[str, int]]:
    if _MSS is None:
        return []
    with _MSS() as sct:
        return [dict(m) for m in sct.monitors]


def shrink(image: Image.Image, max_width: int) -> Image.Image:
    """等比缩到指定宽度以内（够大模型看清就行）。"""
    max_width = max(120, int(max_width))
    if image.width <= max_width:
        return image
    ratio = max_width / float(image.width)
    size = (max_width, max(1, int(round(image.height * ratio))))
    return image.resize(size, Image.LANCZOS)


def is_blank(image: Optional[Image.Image], spread: int = 8) -> bool:
    """整张图几乎一个颜色（全黑/全白）= 这次其实没拍到东西。"""
    if image is None or image.width < 2 or image.height < 2:
        return True
    small = image.convert("L").resize((24, 24), Image.BILINEAR)
    data = list(small.getdata())
    return (max(data) - min(data)) <= int(spread)


def sequence_sheet(images, total_width: int = 1280, gap: int = 6) -> Optional[Image.Image]:
    """把最近几张连拍横向拼成一张"分镜图"（从左到右按时间顺序）。

    为什么要拼：单张静图只能看出"这一眼是什么样"，看不出**在发生什么**——
    综艺的环节、剧情的来龙去脉、游戏里刚打完哪一波，都得靠前后几眼对比。
    拼成一张图的好处是：接口那边仍然只花一张图的 token，提示词也不用改协议。

    每张等比缩到 total_width / n，所以总宽度和只看一张时差不多，不会更贵。
    """
    frames = [img for img in (images or ()) if img is not None]
    if not frames:
        return None
    if len(frames) == 1:
        return frames[0]
    gap = max(0, int(gap))
    count = len(frames)
    tile_width = max(120, int((int(total_width) - gap * (count - 1)) / count))
    tiles = [shrink(img, tile_width) for img in frames]
    height = max(tile.height for tile in tiles)
    sheet = Image.new(
        "RGB", (sum(tile.width for tile in tiles) + gap * (count - 1), height), (18, 18, 18)
    )
    x = 0
    for tile in tiles:
        sheet.paste(tile, (x, 0))
        x += tile.width + gap
    return sheet


def dhash(image: Image.Image, size: int = DEFAULT_HASH_SIZE) -> int:
    """差异哈希：相邻像素比较，返回 64 位整数指纹。"""
    gray = image.convert("L").resize((size + 1, size), Image.BILINEAR)
    pixels = list(gray.getdata())
    bits = 0
    for row in range(size):
        offset = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if pixels[offset + col] < pixels[offset + col + 1] else 0)
    return bits


def hash_distance(a: int, b: int, bits: int = DEFAULT_HASH_SIZE * DEFAULT_HASH_SIZE) -> float:
    """归一化汉明距离，0 表示一模一样，1 表示完全不同。"""
    return bin(a ^ b).count("1") / float(bits)


def to_jpeg_base64(image: Image.Image, quality: int = 72) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=int(quality), optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def brightness(image: Image.Image) -> float:
    """0~1 的平均亮度，mock 模式下用来凑点"见解"。"""
    small = image.convert("L").resize((16, 16), Image.BILINEAR)
    data = list(small.getdata())
    return sum(data) / (255.0 * len(data))


def dominant_hue(image: Image.Image) -> str:
    """粗略判断主色调，返回 warm / cool / green / gray / bright / dark 之一。"""
    small = image.convert("RGB").resize((24, 24), Image.BILINEAR)
    data = list(small.getdata())
    count = float(len(data))
    r = sum(p[0] for p in data) / count
    g = sum(p[1] for p in data) / count
    b = sum(p[2] for p in data) / count
    lum = (r * 0.299 + g * 0.587 + b * 0.114)
    if lum > 190:
        return "bright"
    if lum < 45:
        return "dark"
    if g > r * 1.12 and g > b * 1.12:
        return "green"
    if r > g * 1.15 and r > b * 1.05:
        return "warm"
    if b > r * 1.1:
        return "cool"
    return "gray"
