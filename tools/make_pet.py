"""把一张人物图做成桌宠帧：抠背景 → 裁边 → 缩放 → 生成会动的帧。

用法：
    python tools/make_pet.py assets/source/pet_src.png      # 最常用：一张图直接生成整套帧
    python tools/make_pet.py 图片.png --tol 30 --size 256   # 背景色差多少算背景
    python tools/make_pet.py --demo --install               # 没有原图也能先看动效（代码画的占位形象）
    python tools/make_pet.py --clear                        # 把 assets/pet 里的帧删掉，换回内置矢量形象

产出（写进 assets/pet/，形象渲染会自动从矢量切换成图片帧）：
    idle_00..11.png     待机：呼吸 + 上下轻浮 + 轻微摇摆（幅度很小，只在肉眼范围内起伏）
    talk_00..11.png     说话：幅度更大、更快
    happy_/excited_/speechless_/curious_/smirk_00..11.png   每种情绪一套动作
    highfive_/bump_/hug_/sway_/peekaboo_/pat_/wink_00..11.png
                        串门时两只一起玩的那几个动作（见 pet/play.py）：
                        动作名就是帧名前缀，抖法写在同一份动作表里，渲染器按动作进度播。

    文件名补零到两位，渲染器按末尾数字排序；帧之间做交叉淡化，
    所以 12 帧看起来是连续的（帧数用 --frames 调，越多越顺）。

眼睛（可选项，会自动开）：
    自动找出两只**眼珠（瞳孔）** → 抠成单独一层 → 底图里用眼珠周围的颜色把它糊上，然后
        eye_layer.png    眼珠层（透明底）
        eye_layer.json   眼珠位置 + 每帧的身体变换矩阵
    渲染器读到这两个文件就会把眼珠单独画上去，并按鼠标方向挪一点，做出
    "眼珠在眼睛里面转"的效果（见 pet/sprite.py、pet/window.py）。
    注意：抠下来的**只有眼珠本身**（含里面的高光），眼白 / 眼睑 / 周围的皮肤
    都留在底图上不动——不然动起来整只眼睛一起平移，像贴纸被推动。
    没找到眼睛就自动跳过，形象照常能用，只是不跟鼠标。想关掉：--no-eyes。
    检查找得准不准：跑完看 assets/source/eye_preview.png 那张三联图。

手（可选项，会自动开）：
    找出搭在下巴上的那只手，把竖着的那根食指抠成另一层，并把它从底图里抹掉：
        hand_layer.png   食指那一层（透明底）
        hand_layer.json  敲的幅度/周期 + 每帧的身体变换矩阵 + 贴片位置
    渲染器会让它隔一会儿**轻轻敲两下**：以指根为锚点把这一层往上拉伸一点点——指根不动、
    只有指尖抬起来，所以不会像"整块贴片往上挪"那样在手和贴片之间裂开一条缝
    （那看起来就像手在自己伸缩）。见 pet/sprite.py 的 _tap_matrix、_draw_hand。
    想关掉：--no-hand；敲得太明显/太勤：--tap-lift / --tap-period。
    检查框得准不准：跑完看 assets/source/hand_preview.png。

抠背景的做法（重点）：
    1. 从四条边取一圈像素，中位数当背景色；
    2. 找出"和背景色差不多"的像素，**只从画面边缘开始灌**（floodfill），
       所以人物内部的白色（眼白、白手套、白鞋）会被保住，不会被一起删掉；
    3. 边缘做一次很轻的模糊，得到半透明的抗锯齿边，不会留一圈白毛。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from collections import deque
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
ASSETS_PET = ROOT / "assets" / "pet"
DEFAULT_SOURCE = ROOT / "assets" / "source" / "pet_src.png"
DEMO_DIR = ROOT / "assets" / "source" / "demo"

# 串门动作帧（highfive / hug …）的抖法只有一份来源：`pet/play.py` 的动作表。
# tools/ 不在包里面，所以先把项目根挂到 sys.path 上；pet/__init__.py 是轻的（不碰 Qt），
# 拉它进来不会影响这个脚本单独跑。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from pet import play as play_mod  # noqa: E402  故意放在 ROOT 之后：要的就是这个路径
from pet import states as states_mod  # noqa: E402

FRAME_COUNT = 12         # 每套帧几只（文件名补零到两位，所以 10 以上也不会排乱）
CANVAS_MARGIN = 0.88     # 人物最多占画布多大比例，剩下的空间留给动效
MAX_FRAMES = 60          # 上限：再多也没意义，文件会很大

# 部件（眼珠 / 食指）在放大这么多倍的画布上找：3D 渲染缩到 256 之后，眼白往往只剩
# 一两个像素，判据根本站不住；放大一圈再找稳得多。几何和最终画布完全一致，只是大一圈。
PART_DETECT_SCALE = 4


# ---------- 抠背景 ----------

def estimate_background(image: Image.Image) -> Tuple[int, int, int]:
    """取四条边一圈像素的中位数当背景色（比平均值稳，边上偶尔有点别的东西也不怕）。"""
    rgb = image.convert("RGB")
    width, height = rgb.size
    picks: List[Tuple[int, ...]] = []
    for x in range(width):
        picks.append(rgb.getpixel((x, 0)))
        picks.append(rgb.getpixel((x, height - 1)))
    for y in range(height):
        picks.append(rgb.getpixel((0, y)))
        picks.append(rgb.getpixel((width - 1, y)))
    columns = list(zip(*picks))
    middle = len(picks) // 2
    return tuple(sorted(int(v) for v in column)[middle] for column in columns)  # type: ignore[return-value]


def shade_mask(
    rgb: Image.Image,
    background: Tuple[int, int, int],
    floor: float = 0.5,
    spread: float = 0.07,
    slack: int = 10,
) -> Image.Image:
    """圈出「背景色被光暗了一档」的那部分像素——也就是 3D 渲染图里那块柔和的地面投影。

    这种像素最难缠：色相和背景**一模一样**，只是整体乘了个小于 1 的系数（实测脚下那块
    是背景 × 0.73~0.86）。色差容差调到能吃掉它，人物的皮肤就先被吃掉了；容差调小，
    它就永远留在画面上——上到深色桌面上就是一块灰斑，一眼就看出"背景没抠干净"。

    好在它有个很干净的特征：**逐通道除以背景色，三个比值几乎相等**。实测：

        地面阴影  比值差 0.015~0.042   （暗到背景的 0.73~0.86 倍）
        皮肤      比值差 0.19~0.34
        白手套    比值差 0.17（而且比背景还亮）
        黑头发/黑鞋 比值差很小，但暗到只有背景的 0.10~0.30 倍

    所以「比值齐 + 别太暗 + 不比背景亮」三条一起，就只圈得住背景自己的阴影。

    返回 255 = 是背景阴影。
    """
    br, bg_g, bb = (max(1, int(v)) for v in background)
    rgba = rgb.convert("RGB")
    channels = rgba.split()
    # 每通道先除以背景色。放大 128 倍是为了不让 8 位取整把小差异抹平
    # （阴影和背景的比值差只有 0.02 上下，不放大就看不见了）。
    scaled = [
        ch.point(lambda v, base=base: min(255, int(round(128.0 * v / base))))
        for ch, base in zip(channels, (br, bg_g, bb))
    ]
    high = ImageChops.lighter(ImageChops.lighter(scaled[0], scaled[1]), scaled[2])
    low = ImageChops.darker(ImageChops.darker(scaled[0], scaled[1]), scaled[2])
    even = ImageChops.subtract(high, low).point(lambda v: 255 if v <= round(spread * 128) else 0)
    # 平均比值 ≈ 暗了多少倍（128 = 和背景一样亮）；太暗的判成人物自己的暗部
    mean = ImageChops.add(
        ImageChops.add(scaled[0], scaled[1], scale=3.0),
        scaled[2].point(lambda v: v // 3),
    )
    bright = mean.point(lambda v: 255 if v >= round(floor * 128) else 0)
    # 不能比背景还亮：白手套、白衬衫、高光都在这一关被挡掉
    ok = ImageChops.darker(
        ImageChops.subtract(channels[0], Image.new("L", rgba.size, min(255, br + slack))).point(lambda v: 255 if v == 0 else 0),
        ImageChops.darker(
            ImageChops.subtract(channels[1], Image.new("L", rgba.size, min(255, bg_g + slack))).point(lambda v: 255 if v == 0 else 0),
            ImageChops.subtract(channels[2], Image.new("L", rgba.size, min(255, bb + slack))).point(lambda v: 255 if v == 0 else 0),
        ),
    )
    return ImageChops.darker(ImageChops.darker(even, bright), ok)


def cut_background(
    image: Image.Image,
    tolerance: int = 26,
    feather: float = 0.8,
    background: Optional[Tuple[int, int, int]] = None,
    chroma: float = 0.55,
    shade: float = 0.5,
    strict: float = 12,
) -> Tuple[Image.Image, Tuple[int, int, int]]:
    """把和背景色相连的背景去掉，返回 (RGBA 图, 用到的背景色)。

    判定「看起来就是背景」要过三关：

    1. **色差**（差值图的灰度）≤ tolerance。这里刻意用灰度而不是逐通道最大值——
       灰度更宽松，背景的渐变不会在掩膜里留下小洞；一旦有洞，floodfill 要求一整条
       连通路径，就跨不过去，背景会剩一半没抠掉。
    2. **彩度**至少到「背景彩度 × chroma」。有颜色的背景（比如暖米色，彩度 34）配上
       人物身上的白手套/白衬衫（彩度 8~12）时，只比色差是分不开的：实测白手套的
       灰度差只有 3，floodfill 会从脖子缝里灌进去，把手套和下巴一起吃掉。背景本身
       是白/灰（彩度≈0）时这条自动失效，行为和以前一样。
    3. **背景的阴影**（`shade_mask`，shade = 0 关掉）。渲染图的背景上常有一块柔和的
       地面投影，色相和背景一模一样、只是整体暗一档——上面两关都认不出它
       （它"有色"，只是色差比容差大）。这条单独放它过去，见 shade_mask 的说明。
    4. **封闭在人物里的背景**（strict = 0 关掉）：两腿之间那道缝这种，四周都被人物围着，
       floodfill 从画面边缘绕不进去，只能用严格得多的色差门槛单独扫一遍。

    这几条一起，既能把米色/白色背景（连地面投影、角落的水印）抠干净，又能保住人物
    身上那些"够亮但不是背景色"的东西：白手套、白衬衫、眼白。
    """
    rgba = image.convert("RGBA")
    bg = background or estimate_background(rgba)

    # 差值 <= tolerance 的像素 = "看起来就是背景"（先把人物内部的白色一起标上了，
    # 下一步的 floodfill 会把它们救回来）。
    # 这里刻意用「差值图的灰度」而不是逐通道最大值：灰度更宽松，背景的渐变不会在掩膜里
    # 留下小洞——一旦有洞，floodfill（要求一整条连通路径）就跨不过去，背景会剩一半没抠掉。
    rgb = rgba.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgba.size, bg)).convert("L")
    similar = diff.point(lambda value: 255 if value <= tolerance else 0)

    # 再加一道「够不够有颜色」的闸。背景是暖米色（彩度 34），人物身上的白手套/白衬衫
    # 几乎没颜色（彩度 8~12）——只比色差是分不开的（实测白手套的灰度差只有 3），
    # 所以要求候选像素的彩度至少到「背景彩度 × chroma」。背景本身是白/灰（彩度接近 0）
    # 时门槛也是 0，这条自动失效，行为和以前完全一样。
    floor = (max(bg) - min(bg)) * max(0.0, chroma)
    if floor >= 2.0:
        red, green, blue = rgb.split()
        high = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        low = ImageChops.darker(ImageChops.darker(red, green), blue)
        vivid = ImageChops.subtract(high, low).point(lambda value: 255 if value >= floor else 0)
        similar = ImageChops.darker(similar, vivid)

    # 背景上那块柔和的地面投影：色相和背景一模一样，只是整体暗一档。
    # 它**不走彩度闸**——阴影本身就是"有色"的（用的是背景的色），彩度闸分不开它和
    # 人物；分开它们的是「三个通道的比值齐不齐」，见 shade_mask。
    if shade > 0:
        similar = ImageChops.lighter(similar, shade_mask(rgb, bg, floor=float(shade)))

    # 只从四条边往里面灌：和画面边缘连通的"背景色"才算背景
    filled = similar.copy()
    width, height = filled.size
    seeds: List[Tuple[int, int]] = []
    seeds += [(x, 0) for x in range(width)]
    seeds += [(x, height - 1) for x in range(width)]
    seeds += [(0, y) for y in range(height)]
    seeds += [(width - 1, y) for y in range(height)]
    for seed in seeds:
        if filled.getpixel(seed) == 255:
            ImageDraw.floodfill(filled, seed, 128, thresh=0)

    # 128 = 连通的背景
    removed = filled.point(lambda value: 255 if value == 128 else 0)

    # 还有一小块"灌不进去"的背景：**封闭在人物里面**的那种，比如两腿之间那道缝——
    # 它上头顶着裤子、两边是腿、底下两只鞋挨在一起，四周都是人物，从画布边缘怎么绕都
    # 到不了；可它颜色就是背景色，留在画面上就是腿间一条亮条。所以再用**严格得多**的
    # 色差门槛扫一遍（strict，默认 12，主容差是 26），只要它没和画面边缘连通就一并删掉。
    # 门槛这么严，人物身上的浅色（皮肤高光、牛仔布的亮面）都过不了这一关。
    if strict > 0:
        tight = diff.point(lambda value: 255 if value <= strict else 0)
        if floor >= 2.0:
            tight = ImageChops.darker(tight, vivid)
        reach = tight.copy()
        for seed in seeds:
            if reach.getpixel(seed) == 255:
                ImageDraw.floodfill(reach, seed, 128, thresh=0)
        enclosed = ImageChops.darker(tight, ImageChops.invert(reach.point(lambda value: 255 if value == 128 else 0)))
        removed = ImageChops.lighter(removed, enclosed)

    alpha = removed.point(lambda value: 0 if value else 255)
    if feather > 0:
        alpha = alpha.filter(ImageFilter.GaussianBlur(float(feather)))

    # 原图自带透明的话，取交集（两道保险）
    if rgba.mode == "RGBA":
        base_alpha = rgba.getchannel("A")
        alpha = ImageChops.darker(alpha, base_alpha)

    rgba.putalpha(alpha)
    return rgba, bg


def fit_canvas(rgba: Image.Image, size: int = 256, margin: float = CANVAS_MARGIN) -> Image.Image:
    """裁到内容边界，等比缩放后居中放进正方形透明画布。"""
    box = rgba.getbbox()
    if box:
        rgba = rgba.crop(box)
    inner = max(8, int(size * margin))
    scale = inner / float(max(rgba.width, rgba.height))
    target = (
        max(1, int(round(rgba.width * scale))),
        max(1, int(round(rgba.height * scale))),
    )
    resized = rgba.resize(target, Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(resized, ((size - target[0]) // 2, (size - target[1]) // 2), resized)
    return canvas


def despeckle(
    image: Image.Image,
    min_pixels: int = 24,
    min_ratio: float = 0.02,
) -> Tuple[Image.Image, int]:
    """去掉抠图后遗留的小黑点 / 小碎块（截图边框、压缩噪点、背景上的闪光/水印很容易留下）。

    做法：在 alpha 上找连通块，比「最大那块 × min_ratio」还小的直接抹掉。人物本体永远
    是最大的一块，所以只要门槛按它来算就不会误删；背景上那些装饰、水印则会被一起清掉
    （它们常贴在图边上，把内容框撑大，形象就被迫缩小了）。
    返回 (处理后的图, 抹掉的像素数)。
    """
    if min_pixels <= 0 and min_ratio <= 0:
        return image, 0
    rgba = image.copy()
    alpha = rgba.getchannel("A")
    width, height = rgba.size
    # 阈值取 40：太低的（羽化残影）不值得算成一块，但它们会被下面的膨胀一起带走
    solid = alpha.point(lambda value: 255 if value > 40 else 0)
    data = solid.tobytes()

    seen = bytearray(width * height)
    blobs: List[List[int]] = []
    for start in range(width * height):
        if seen[start] or not data[start]:
            continue
        stack = [start]
        seen[start] = 1
        blob: List[int] = [start]
        while stack:
            index = stack.pop()
            x, y = index % width, index // width
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    neighbour = ny * width + nx
                    if not seen[neighbour] and data[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
                        blob.append(neighbour)
        blobs.append(blob)

    if not blobs:
        return rgba, 0
    # 碎块的判定：比 min_pixels 小，或者连本体（最大那块）的 min_ratio 都不到
    # （背景上的闪光/水印就在这一档：它们贴在图边上，会把内容框撑大、形象被迫缩小）
    largest = max(len(blob) for blob in blobs)
    limit = max(int(min_pixels), int(largest * min_ratio))
    drop = bytearray(width * height)
    dropped = 0
    for blob in blobs:
        if len(blob) >= limit:
            continue
        dropped += len(blob)
        for index in blob:
            drop[index] = 255

    if not dropped:
        return rgba, 0
    # 往外扩两圈再抹：把碎块周围那圈半透明的毛边一起带走
    drop_mask = Image.frombytes("L", (width, height), bytes(drop)).filter(ImageFilter.MaxFilter(5))
    rgba.putalpha(ImageChops.darker(alpha, ImageChops.invert(drop_mask)))
    return rgba, dropped


# ---------- 细缝修补 ----------

def heal_slits(
    rgba: Image.Image,
    max_width: int = 4,
    alpha_gate: int = 128,
) -> Tuple[Image.Image, int]:
    """补掉人物身上细到画不出来的裂缝，返回 (补好的图, 补掉的像素数)。

    从耳缝、下巴缝灌进去的背景，抠完之后往往只剩 1~4 像素宽的一条透明缝（抠得其实没错，
    那个缝在源图上确实是背景）。可 256 的画布上这么细的缝根本画不出来，缩放时只会变成
    断面两侧的一圈脏边——上到深色桌面上就是耳后那两道虚线竖条，看着像戴了耳机。

    所以这里直接把「左右（或上下）都被实心像素夹住、自己又细又透明」的缝填掉：颜色按
    两侧线性过渡，补完看不出接缝。宽过 max_width 的缝不动（那可能是手指缝这种真该透出来
    的地方），顶到画布边缘的也不动（那是轮廓，不是缝）。
    """
    out = rgba.convert("RGBA")
    healed = 0
    # 跑两遍：第二遍把图转 90°，于是"左右"那一套判断就顺手把纵向的缝也管了
    for turned in (False, True):
        if turned:
            out = out.transpose(Image.Transpose.TRANSPOSE)
        pixels = out.load()
        w, h = out.size
        for y in range(h):
            x = 0
            while x < w:
                if pixels[x, y][3] >= alpha_gate:      # 实心像素，跳过
                    x += 1
                    continue
                start = x
                while x < w and pixels[x, y][3] < alpha_gate:
                    x += 1
                span = x - start
                if span > max_width or start == 0 or x >= w:
                    continue                            # 太宽 / 顶到画布边：不是细缝
                left = pixels[start - 1, y]
                right = pixels[x, y]
                for step in range(span):
                    ratio = (step + 1) / float(span + 1)
                    pixels[start + step, y] = (
                        int(round(left[0] + (right[0] - left[0]) * ratio)),
                        int(round(left[1] + (right[1] - left[1]) * ratio)),
                        int(round(left[2] + (right[2] - left[2]) * ratio)),
                        255,
                    )
                healed += span
        if turned:
            out = out.transpose(Image.Transpose.TRANSPOSE)
    return out, healed


# ---------- 眼睛：让眼珠跟着鼠标转 ----------

# 形象带眼睛的时候，assets/pet 里会多出这两个文件：
#   eye_layer.png   眼珠单独抠出来的一层（透明底，画布尺寸和帧一致）
#   eye_layer.json  两只眼珠的位置/半径 + 每帧的身体变换矩阵
# 渲染器（pet/sprite.py）读到它们就把眼珠单独画上去，并按鼠标方向挪一点；
# 没有这两个文件就和以前一模一样（静态形象，不跟鼠标）。
EYE_LAYER_FILE = "eye_layer.png"
EYE_META_FILE = "eye_layer.json"
EYE_PREVIEW = ROOT / "assets" / "source" / "eye_preview.png"

EYE_HEAD_RATIO = 0.62    # 只在形象的上半部分找眼睛（下半身不会有眼睛）
EYE_MIN_AREA = 0.00015   # 瞳孔最小面积（占画布比例）
EYE_MAX_AREA = 0.030     # 瞳孔最大面积
EYE_DARK = 95            # 比这个亮度还暗 = 瞳孔 / 头发
EYE_BRIGHT = 145         # 比这个亮度亮、又几乎没颜色 = 眼白
EYE_SATURATION = 45      # 眼白允许的最大彩度（max-min）；皮肤再亮也偏暖，这条挡得住
EYE_SHAPE_MIN = 0.30     # 深色块允许的宽高比范围：瞳孔会被眼睑带宽，所以放得很松
EYE_SHAPE_MAX = 4.00
EYE_TRAVEL = 0.55        # 眼珠最多挪多远（按**眼珠**半径的比例，不是整只眼的）
EYE_FEATHER = 0.22       # 贴片边缘羽化（按半径的比例）
# 抠「眼珠」用的阈值。find_eyes 那一套（EYE_DARK）是在放大 PART_DETECT_SCALE 倍的画布上
# 量的，而掩膜是在最终 256 的画布上抠；同一块眼珠缩到 256 之后整体被抹亮，所以这两档
# 都比 95 松一档：先按 EYE_PUPIL_CORE 找**最深的那一坨**当眼珠，再往外带一圈稍浅的虹膜。
EYE_PUPIL_CORE = 65      # 眼珠的「核」：瞳孔 + 最深的虹膜（定圆心和大小都用它）
EYE_PUPIL_DARK = 122     # 虹膜外圈还算深色的门槛
# 关键：**不许**拿「圆内所有深色」当眼珠。像这只 3D 小人，眼睛在 256 画布上只有 20 来个
# 像素，眼线 + 眼睑 + 虹膜会连成一整块深色，照着抠出来的就是**整只眼**——鼠标一动就像
# 有人把眼睛推来推去，看着不像"眼珠在转"（.probe 里量过：整只眼约 22×11 px，而最深
# 那一坨只有 64 px、等效半径 4.5）。所以半径要按**核**算、再卡上下限：
EYE_PUPIL_GROW = 1.45    # 眼珠半径 = 核的等效半径 × 这个系数（把虹膜外圈带上）
EYE_PUPIL_MIN = 0.30     # 下限（按整只眼睛的半径算）：再小就成了一颗滑动的小黑点
EYE_PUPIL_MAX = 0.62     # 上限：眼线 / 眼睑 / 眼白必须留在底图上不动
EYE_PUPIL_FEATHER = 0.35  # 眼珠掩膜的羽化系数（乘在 EYE_FEATHER 上）：实心为主，只留一圈软边


# 搭在下巴上的那只手 / 竖着的那根食指：同样抠成一层，运行时轻轻上下敲
HAND_LAYER_FILE = "hand_layer.png"
HAND_META_FILE = "hand_layer.json"
HAND_PREVIEW = ROOT / "assets" / "source" / "hand_preview.png"

HAND_HEAD_RATIO = 0.40   # 手只可能在形象的下半部分（上半部分是脸）
HAND_MIN_AREA = 0.004    # 白手套至少占画布这么大（比这小的白块是眼白/高光，不要）
HAND_EYE_CLEAR = 1.8     # 找手之前，先把眼睛周围这么大的范围当白块挖掉（眼白也是白的）
HAND_TOP_RATIO = 0.32    # 从这只手的顶部往下取这么多，算「食指那一段」
# 贴片留白（按食指那一段的高度算）。留白是**跟着一起动**的，所以越小越好：
# 留白大了，抬起来的就成了一整片手套/下巴，看着像手在自己伸缩而不是手指在敲。
HAND_PAD_RATIO = 0.25
TAP_LIFT = 6.0           # 敲一下把贴片顶边抬高多少（画布像素）；指尖实际抬起约 0.8 倍
TAP_PERIOD = 2.4         # 敲的周期（秒）：一个周期里轻敲两下，其余时间贴着下巴不动


def _luma(r: int, g: int, b: int) -> float:
    """人眼感觉到的亮度；比单纯取平均更贴近“看起来黑不黑”。"""
    return 0.299 * r + 0.587 * g + 0.114 * b


def _blob_indices(mask: bytearray, width: int, height: int) -> List[List[int]]:
    """四连通分块：把二值 mask 拆成一堆“像素下标列表”。"""
    seen = bytearray(width * height)
    out: List[List[int]] = []
    for start in range(width * height):
        if seen[start] or not mask[start]:
            continue
        seen[start] = 1
        stack = [start]
        blob: List[int] = []
        while stack:
            index = stack.pop()
            blob.append(index)
            x, y = index % width, index // width
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    neighbour = ny * width + nx
                    if mask[neighbour] and not seen[neighbour]:
                        seen[neighbour] = 1
                        stack.append(neighbour)
        out.append(blob)
    return out


def _flanked_by_sclera(
    bright: bytearray, width: int, center: int, y: int, reach: int
) -> Tuple[bool, bool]:
    """在 y 这一行上，从深色块的中心往左右各扫 reach 个像素，看有没有眼白。

    必须「从中心往外」扫，而不是只看 bbox 外面：3D 渲染里瞳孔块常把眼睑/睫毛带进来，
    bbox 本身就横跨整只眼，真正夹着瞳孔的那点眼白其实落在 bbox **里面**。
    中间隔着深色的虹膜/眼线没关系，反正它们本来就不在眼白掩膜里。
    """
    height = len(bright) // width if width else 0
    if not (0 <= y < height):
        return False, False
    row = y * width
    left = any(bright[row + x] for x in range(max(0, center - reach), center))
    right = any(bright[row + x] for x in range(center + 1, min(width, center + 1 + reach)))
    return left, right


def _color_masks(image: Image.Image, top: int, bottom: int) -> Tuple[bytearray, bytearray]:
    """把 [top, bottom) 这几行里的像素分成「深色」和「白」两张掩膜（整张画布大小）。

    白 = 又亮又几乎没颜色（眼白、白手套、白鞋）；深色 = 亮度很低（眼珠、瞳孔、头发）。
    黄皮肤虽然也亮，但彩度高，会被「白」这条挡在外面——眼睛和手都靠这个分辨。
    """
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    dark = bytearray(width * height)
    bright = bytearray(width * height)
    for y in range(max(0, top), min(height, bottom)):
        row = y * width
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a < 200:                     # 透明 / 羽化边不算数
                continue
            value = _luma(r, g, b)
            if value <= EYE_DARK:
                dark[row + x] = 1
            elif value >= EYE_BRIGHT and (max(r, g, b) - min(r, g, b)) <= EYE_SATURATION:
                bright[row + x] = 1
    return dark, bright


def find_eyes(base: Image.Image) -> List[Tuple[float, float, float]]:
    """自动找出两只眼珠，返回 [(圆心 x, 圆心 y, 半径)]，左边那只在前。

    不靠人脸识别，就一条特征：眼珠是一块**深色**、而且左右**两侧都是眼白**的家伙。
    眉毛虽然也深，但两侧是皮肤（黄皮肤彩度高，不算眼白）；头发又大又扁，
    被面积和长宽比两道筛子挡掉。找不到就返回空列表（那就安安静静不跟鼠标）。
    """
    rgba = base.convert("RGBA")
    width, height = rgba.size
    box = rgba.getbbox() or (0, 0, width, height)
    bottom = min(height, int(box[1] + (box[3] - box[1]) * EYE_HEAD_RATIO))
    dark, bright = _color_masks(rgba, box[1], bottom)

    area = float(width * height)
    found: List[Tuple[int, float, float, float]] = []
    for blob in _blob_indices(dark, width, height):
        size = len(blob)
        if size < EYE_MIN_AREA * area or size > EYE_MAX_AREA * area:
            continue
        xs = [index % width for index in blob]
        ys = [index // width for index in blob]
        x0, x1 = min(xs), max(xs)
        bw, bh = x1 - x0 + 1, max(ys) - min(ys) + 1
        if bw <= 0 or bh <= 0 or not (EYE_SHAPE_MIN <= bw / float(bh) <= EYE_SHAPE_MAX):
            continue
        cx = sum(xs) / float(size)
        cy = sum(ys) / float(size)
        # 瞳孔那一行从中心往左右各扫一段，两边都得能找到眼白——眉毛旁边只有皮肤，过不了这关
        reach = max(2, int(round(bw * 0.6)))
        left, right = _flanked_by_sclera(bright, width, int(round(cx)), int(round(cy)), reach)
        if not (left and right):
            continue
        found.append((size, cx, cy, max(bw, bh) / 2.0))

    if len(found) < 2:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    first = found[0]
    second: Optional[Tuple[int, float, float, float]] = None
    for other in found[1:]:
        if abs(other[1] - first[1]) < max(first[3], other[3]) * 1.6:
            continue                        # 挨太近：多半是同一只眼睛里的另一块
        if abs(other[2] - first[2]) > first[3] * 2.5:
            continue                        # 一高一低：不是一对
        second = other
        break
    if second is None:
        return []
    pair = sorted((first, second), key=lambda item: item[1])
    return [(pair[0][1], pair[0][2], pair[0][3]), (pair[1][1], pair[1][2], pair[1][3])]


def parse_eyes(text: str) -> List[Tuple[float, float, float]]:
    """手动指定眼睛：左x,左y,左r 右x,右y,右r（画布像素坐标）。"""
    parts = [part for part in re.split(r"[,\s]+", (text or "").strip()) if part]
    if len(parts) != 6:
        raise ValueError("--eyes 要写成 左x,左y,左r,右x,右y,右r（画布像素坐标，6 个数）")
    values = [float(part) for part in parts]
    eyes = [(values[0], values[1], values[2]), (values[3], values[4], values[5])]
    keep = [item for item in eyes if item[2] > 0.5]
    if len(keep) != 2:
        raise ValueError("--eyes 里的半径要大于 0.5 像素")
    return keep


def _eye_box(base: Image.Image, cx: float, cy: float, radius: float) -> Tuple[Tuple[int, int, int, int], float, float]:
    """给一只眼睛算一块正方形贴片（保证不出画布），返回 (框, 眼珠在贴片里的圆心)。"""
    half = max(8, int(round(radius * 2.4)))
    left = max(0, min(max(0, base.width - half * 2), int(round(cx)) - half))
    top = max(0, min(max(0, base.height - half * 2), int(round(cy)) - half))
    return (left, top, left + half * 2, top + half * 2), cx - left, cy - top


def _patch_ring_color(
    base: Image.Image, box: Tuple[int, int, int, int], mask: Image.Image, reach: float
) -> Tuple[int, int, int]:
    """取眼珠掩膜**外圈**那一圈像素的中位色——底图里就用它把眼珠糊掉。

    只能贴着掩膜取：眼珠四周通常是眼窝 / 眼睑的暗部，糊成眼白或者皮肤色都会在眼珠
    挪开的时候露出一块突兀的印子（老版整只眼被糊成一片肉色，就是这么来的）。
    """
    region = base.convert("RGBA").crop(box)
    width, height = region.size
    pixels = region.load()
    solid = mask.tobytes()                      # 每像素 1 字节（"L" 模式）
    grow = max(3, int(round(reach)) | 1)        # MaxFilter 的边长必须是奇数
    around = mask.filter(ImageFilter.MaxFilter(grow)).tobytes()
    picks: List[Tuple[int, int, int]] = []
    for index, value in enumerate(solid):
        if value or not around[index]:
            continue
        r, g, b, a = pixels[index % width, index // width]
        if a < 200:
            continue
        picks.append((r, g, b))
    if not picks:
        return (60, 52, 52)                     # 掩膜顶满整张贴片（没有外圈）：退回深灰
    middle = len(picks) // 2
    return tuple(int(sorted(channel)[middle]) for channel in zip(*picks))  # type: ignore[return-value]


def _pupil_seed(
    dark: bytearray, inside: bytearray, width: int, height: int,
    local_x: float, local_y: float, radius: float,
) -> Optional[int]:
    """在贴片里找「眼珠」那颗种子：圆心是深色就最好，否则取半径内离圆心最近的深色像素。"""
    cx, cy = int(round(local_x)), int(round(local_y))
    if 0 <= cx < width and 0 <= cy < height and dark[cy * width + cx]:
        return cy * width + cx
    reach = int(round(radius * 1.10)) + 1
    best: Optional[int] = None
    best_dist = 0
    for y in range(max(0, cy - reach), min(height, cy + reach + 1)):
        for x in range(max(0, cx - reach), min(width, cx + reach + 1)):
            index = y * width + x
            if not (dark[index] and inside[index]):
                continue
            dist = (x - cx) * (x - cx) + (y - cy) * (y - cy)
            if best is None or dist < best_dist:
                best, best_dist = index, dist
    return best


def _fill_blob(
    dark: bytearray, inside: bytearray, width: int, height: int, seed: int
) -> Optional[bytearray]:
    """从种子灌出「眼珠」那一整块，并把它**内部的亮洞（高光）一起填实**。

    只取连通的那一块（不是圆里所有深色像素）：眼线、睫毛就算落在圆内也不会被带走。
    填洞是因为眼珠上通常有一点高光，不填的话那颗高光会留在底图上不动，很怪。
    """
    if not (0 <= seed < width * height) or not dark[seed]:
        return None
    blob = bytearray(width * height)
    stack = [seed]
    blob[seed] = 1
    while stack:
        index = stack.pop()
        x, y = index % width, index // width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            neighbour = ny * width + nx
            if dark[neighbour] and not blob[neighbour] and inside[neighbour]:
                blob[neighbour] = 1
                stack.append(neighbour)

    # 补洞：从贴片四边往里灌"外面"，灌不到、又不是眼珠的，就是被眼珠包住的高光
    outside = bytearray(width * height)
    queue = deque()
    for x in range(width):
        for y in (0, height - 1):
            index = y * width + x
            if not blob[index] and not outside[index]:
                outside[index] = 1
                queue.append(index)
    for y in range(height):
        for x in (0, width - 1):
            index = y * width + x
            if not blob[index] and not outside[index]:
                outside[index] = 1
                queue.append(index)
    while queue:
        index = queue.popleft()
        x, y = index % width, index // width
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if not (0 <= nx < width and 0 <= ny < height):
                continue
            neighbour = ny * width + nx
            if blob[neighbour] or outside[neighbour]:
                continue
            outside[neighbour] = 1
            queue.append(neighbour)
    for index in range(width * height):
        if not outside[index]:
            blob[index] = 1
    return blob


def _iris_blob(
    lit: bytearray, inside: bytearray, width: int, height: int,
    local_x: float, local_y: float, radius: float,
) -> Optional[bytearray]:
    """从圆心附近最深的那一坨灌出「眼珠」，返回那块二值掩膜。

    只认**从种子连通出去**的那一块：眼线、睫毛、眼睑就算落在圆里、甚至和眼珠贴着，
    也不会被带进来（老版是整圆一起收，才抠出整只眼）。
    """
    seed = _pupil_seed(lit, inside, width, height, local_x, local_y, radius)
    if seed is None:
        return None
    return _fill_blob(lit, inside, width, height, seed)


def _iris_center(blob: bytearray, width: int) -> Tuple[float, float, float]:
    """眼珠那一坨的质心 + 等效半径（当成面积相同的圆来量）。"""
    count = sum(blob)
    if not count:
        return 0.0, 0.0, 0.0
    sum_x = sum_y = 0
    for index, value in enumerate(blob):
        if value:
            sum_x += index % width
            sum_y += index // width
    return sum_x / float(count), sum_y / float(count), math.sqrt(count / math.pi)


def pupil_patch(
    base: Image.Image, cx: float, cy: float, radius: float
) -> Tuple[Tuple[int, int, int, int], Image.Image, float]:
    """圈出**眼珠本身**（虹膜 + 瞳孔 + 里面的高光），返回 (贴片框, 羽化过的掩膜, 眼珠半径)。

    这是"眼睛跟着鼠标转"的关键，分两步把范围收紧：

    1. 先只认圆心附近**最深的那一坨**（EYE_PUPIL_CORE）——那就是眼珠；
    2. 再按它的等效半径撑成一个虹膜大小的圆片（卡在 EYE_PUPIL_MIN/MAX 之间）。

    老版拿的是"圆内所有深色"，而深色一多（眼线 + 眼睑 + 虹膜连成一整块）抠出来的
    就是**整只眼睛**，鼠标一动像有人把眼睛推来推去。现在眼线 / 眼睑 / 眼白 / 皮肤
    一律留在底图上不动，动起来的只有"眼珠在眼眶里转"。
    """
    box, local_x, local_y = _eye_box(base, cx, cy, radius)
    piece = base.convert("RGBA").crop(box)
    width, height = piece.size
    pixels = piece.load()
    reach = radius * 1.10                    # 眼珠不会超出这个圈
    core = bytearray(width * height)         # 最深那一坨：瞳孔 + 深色虹膜
    dark = bytearray(width * height)         # 深色整体：还要靠它把虹膜外圈补齐
    inside = bytearray(width * height)
    reach_sq = reach * reach
    for y in range(height):
        row = y * width
        dy = y - local_y
        for x in range(width):
            dx = x - local_x
            if dx * dx + dy * dy > reach_sq:
                continue
            inside[row + x] = 1
            r, g, b, a = pixels[x, y]
            if a < 200:
                continue
            value = _luma(r, g, b)
            if value <= EYE_PUPIL_CORE:
                core[row + x] = 1
            if value <= EYE_PUPIL_DARK:
                dark[row + x] = 1

    blob = _iris_blob(core, inside, width, height, local_x, local_y, radius)
    if blob is None:
        # 核太浅（手动 --eyes 指到了浅色上）：退一步，用深色那一坨
        blob = _iris_blob(dark, inside, width, height, local_x, local_y, radius)
    mask = Image.new("L", (width, height), 0)
    if blob is None:
        # 连深色都找不到：就当一个实心小圆，至少还能动
        iris = max(2.0, radius * EYE_PUPIL_MAX)
        ImageDraw.Draw(mask).ellipse(
            (local_x - iris, local_y - iris, local_x + iris, local_y + iris), fill=255
        )
    else:
        center_x, center_y, iris = _iris_center(blob, width)
        iris = max(radius * EYE_PUPIL_MIN, min(radius * EYE_PUPIL_MAX, iris * EYE_PUPIL_GROW))
        iris_sq = iris * iris
        for y in range(height):
            row = y * width
            dy = y - center_y
            if dy * dy > iris_sq:
                continue
            for x in range(width):
                dx = x - center_x
                if dx * dx + dy * dy <= iris_sq and dark[row + x]:
                    mask.putpixel((x, y), 255)
        # 一闭一开：把眼珠里那颗高光（亮洞）也圈进来，让它跟着眼珠动、不留在原地
        mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    return (
        box,
        mask.filter(ImageFilter.GaussianBlur(max(0.5, iris * EYE_FEATHER * EYE_PUPIL_FEATHER))),
        iris,
    )


def blank_eyes(base: Image.Image, eyes: Sequence[Tuple[float, float, float]]) -> Image.Image:
    """底图里把眼珠抹掉（用眼珠外圈的颜色糊上）——动效帧用的就是这一张。

    只抹**眼珠那一块**，眼白 / 眼睑 / 眼线 / 皮肤一概不动；渲染器再把眼珠层盖回来，
    静止时看着和原图一样，动起来则是眼珠在眼眶里挪。
    """
    out = base.convert("RGBA")
    for cx, cy, radius in eyes:
        box, mask, iris = pupil_patch(out, cx, cy, radius)
        if iris <= 0.0:
            continue
        color = _patch_ring_color(out, box, mask, max(1.5, iris * 0.45))
        region = out.crop(box)
        out.paste(_gradient_fill(region, mask, color, color, iris * 0.35), (box[0], box[1]))
    return out


def build_eye_layer(base: Image.Image, eyes: Sequence[Tuple[float, float, float]]) -> Image.Image:
    """把两只**眼珠**抠成独立一层（透明底、画布对齐），渲染时再叠回帧上。"""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    source = base.convert("RGBA")
    for cx, cy, radius in eyes:
        box, mask, _ = pupil_patch(source, cx, cy, radius)
        piece = source.crop(box)
        piece.putalpha(mask)
        layer.alpha_composite(piece, (box[0], box[1]))
    return layer


def eye_patches(
    base: Image.Image, eyes: Sequence[Tuple[float, float, float]]
) -> List[float]:
    """每只眼睛会跟着鼠标挪的那一小块半径（= 眼珠半径）——挪多远按它算。

    按**眼珠**算而不是整只眼：眼珠比眼眶小得多，用整只眼的半径去乘比例，小眼睛
    会一下子挪出眼眶（老版就是这么怼到眼睑上的）。
    """
    return [pupil_patch(base, cx, cy, radius)[2] for cx, cy, radius in eyes]



def write_eye_assets(
    folder: Path,
    canvas: int,
    layer: Image.Image,
    eyes: Sequence[Tuple[float, float, float]],
    travel: float,
    warp_table: Dict[str, List[Tuple[float, ...]]],
) -> None:
    """把眼珠层 + 元数据写进资产目录（渲染器就靠这两个文件认识眼睛）。"""
    folder.mkdir(parents=True, exist_ok=True)
    layer.save(folder / EYE_LAYER_FILE, format="PNG")
    meta = {
        "canvas": canvas,
        "travel": round(float(travel), 3),
        "eyes": [[round(cx, 2), round(cy, 2), round(r, 2)] for cx, cy, r in eyes],
        "warp": {
            name: [[round(value, 6) for value in matrix] for matrix in matrices]
            for name, matrices in warp_table.items()
        },
    }
    (folder / EYE_META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def clear_part_assets(folder: Path, names: Sequence[str]) -> int:
    """把上一版残留的部件层删掉（形象换了 / 这次没找到部件），免得盖在不该盖的地方。"""
    removed = 0
    for name in names:
        path = folder / name
        if path.exists():
            path.unlink()
            removed += 1
    return removed


def write_part_preview(panels: Sequence[Image.Image], target: Path, note: str) -> None:
    """把几张贴片并排存一张检查图（左→右），方便肉眼验收。"""
    gap = 8
    width = sum(panel.width for panel in panels) + gap * (len(panels) - 1)
    height = max(panel.height for panel in panels)
    sheet = Image.new("RGBA", (width, height), (24, 28, 38, 255))
    x = 0
    for panel in panels:
        sheet.alpha_composite(panel.convert("RGBA"), (x, 0))
        x += panel.width + gap
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(target, format="PNG")
    print(f"检查图：{rel(target)}（{note}）")


def write_eye_preview(
    base: Image.Image,
    layer: Image.Image,
    blanked: Image.Image,
    eyes: Sequence[Tuple[float, float, float]],
    target: Path,
    irises: Sequence[float] = (),
) -> None:
    """把“原图 + 标记圈”“抠出来的眼珠层”“抹掉眼珠后的底图”并排存一张，方便肉眼验收。

    红圈 = 找眼睛时认下的「整只眼」范围；绿圈 = 真正会跟着鼠标挪的那一块（眼珠）。
    **绿圈要明显比红圈小**——两个一样大就说明整只眼都被抠进去了，鼠标一动整只眼在滑。
    """
    marked = base.convert("RGBA").copy()
    draw = ImageDraw.Draw(marked)
    for index, (cx, cy, radius) in enumerate(eyes):
        reach = radius * 1.06
        draw.ellipse((cx - reach, cy - reach, cx + reach, cy + reach), outline=(255, 60, 60, 255), width=2)
        iris = irises[index] if index < len(irises) else 0.0
        if iris > 0.0:
            draw.ellipse((cx - iris, cy - iris, cx + iris, cy + iris), outline=(60, 235, 120, 255), width=2)
    write_part_preview(
        [marked, layer, blanked],
        target,
        "左=原图+红圈(整只眼)/绿圈(会动的眼珠)，中=眼珠层，右=抹掉眼珠后",
    )


def write_hand_preview(
    base: Image.Image,
    layer: Image.Image,
    blanked: Image.Image,
    rect: Sequence[int],
    target: Path,
) -> None:
    """把“原图 + 红框”“抠出来的食指层”“抹掉食指后的底图”并排存一张，方便肉眼验收。"""
    marked = base.convert("RGBA").copy()
    draw = ImageDraw.Draw(marked)
    draw.rectangle(
        (int(rect[0]), int(rect[1]), int(rect[2]) - 1, int(rect[3]) - 1),
        outline=(255, 60, 60, 255),
        width=2,
    )
    write_part_preview([marked, layer, blanked], target, "左=原图+红框，中=食指层，右=抹掉食指后")


def write_hand_assets(
    folder: Path,
    canvas: int,
    layer: Image.Image,
    rect: Sequence[int],
    lift: float,
    period: float,
    warp_table: Dict[str, List[Tuple[float, ...]]],
) -> None:
    """把食指层 + 元数据写进资产目录（渲染器靠它做“轻轻敲下巴”）。"""
    folder.mkdir(parents=True, exist_ok=True)
    layer.save(folder / HAND_LAYER_FILE, format="PNG")
    meta = {
        "canvas": canvas,
        "lift": round(float(lift), 3),
        "period": round(float(period), 3),
        "box": [int(value) for value in rect],
        "warp": {
            name: [[round(value, 6) for value in matrix] for matrix in matrices]
            for name, matrices in warp_table.items()
        },
    }
    (folder / HAND_META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def parse_box(text: str) -> Tuple[int, int, int, int]:
    """手动指定那只手：左,上,右,下（画布像素坐标）。"""
    parts = [part for part in re.split(r"[,\s]+", (text or "").strip()) if part]
    if len(parts) != 4:
        raise ValueError("--hand-box 要写成 左,上,右,下（画布像素坐标，4 个数）")
    left, top, right, bottom = (int(float(part)) for part in parts)
    if right - left < 4 or bottom - top < 4:
        raise ValueError("--hand-box 框太小了（宽高至少 4 像素）")
    return (left, top, right, bottom)


def find_hand(
    base: Image.Image,
    eyes: Sequence[Tuple[float, float, float]] = (),
) -> Optional[Tuple[int, int, int, int]]:
    """找出搭在下巴上的那只手，返回「食指那一段」的方框（画布像素：左,上,右,下）。

    白手套是这个形象里最大块的白：垂在身侧的那只手和搭下巴的那只手都是白的，
    位置更高的那只是搭下巴的。再从它的最上沿往下切一截，就是竖起来的那根食指。
    眼白也是白的，会先按 find_eyes 的结果挖掉——不然手指可能和眼白连成一块。
    找不到就返回 None（那就不敲了，形象照常用）。
    """
    rgba = base.convert("RGBA")
    width, height = rgba.size
    box = rgba.getbbox() or (0, 0, width, height)
    top = int(box[1] + (box[3] - box[1]) * HAND_HEAD_RATIO)
    _, bright = _color_masks(rgba, top, box[3])

    for cx, cy, radius in eyes:
        reach = int(round(float(radius) * HAND_EYE_CLEAR))
        for y in range(max(0, int(cy) - reach), min(height, int(cy) + reach + 1)):
            row = y * width
            for x in range(max(0, int(cx) - reach), min(width, int(cx) + reach + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= reach * reach:
                    bright[row + x] = 0

    area = float(width * height)
    blobs = [blob for blob in _blob_indices(bright, width, height) if len(blob) >= HAND_MIN_AREA * area]
    if not blobs:
        return None
    blobs.sort(key=len, reverse=True)

    # 两只手套里挑「最上面那一点离形象中线最近」的那只：搭在下巴上的手一定是举在
    # 脸下面的，垂在身侧的那只会偏到一边去。只比高度不够稳——垂着的手可能反而更高。
    center_x = (box[0] + box[2]) / 2.0
    pick: Optional[List[int]] = None
    pick_top: Optional[int] = None
    best: Optional[float] = None
    for blob in blobs[:2]:
        blob_top = min(index // width for index in blob)
        top_xs = [index % width for index in blob if index // width <= blob_top + 1]
        score = abs(sum(top_xs) / float(len(top_xs)) - center_x)
        if best is None or score < best:
            pick, best, pick_top = blob, score, blob_top
    if not pick or pick_top is None:
        return None

    ys = [index // width for index in pick]
    hand_height = max(ys) - min(ys) + 1
    finger_bottom = int(pick_top + hand_height * HAND_TOP_RATIO)
    finger = [index for index in pick if index // width <= finger_bottom]
    if not finger:
        return None

    finger_xs = [index % width for index in finger]
    finger_ys = [index // width for index in finger]
    pad = max(2, int(round((max(finger_ys) - min(finger_ys) + 1) * HAND_PAD_RATIO)))
    return (
        max(0, min(finger_xs) - pad),
        max(0, min(finger_ys) - pad),
        min(width, max(finger_xs) + 1 + pad),
        min(height, max(finger_ys) + 1 + pad),
    )


def _row_band(image: Image.Image, box: Sequence[int], above: bool) -> Image.Image:
    """取框**外面**紧贴着的那一行（上面一行或下面一行），抹洞时按列拉伸用。"""
    x0, y0, x1, y1 = (int(value) for value in box)
    y = max(0, y0 - 1) if above else min(image.height - 1, y1)
    return image.crop((x0, y, x1, y + 1))


def _column_fill(
    region: Image.Image,
    mask: Image.Image,
    top_row: Image.Image,
    bottom_row: Image.Image,
    blur: float,
) -> Image.Image:
    """把「框上面那一行」和「框下面那一行」**按列**插值糊进洞里。

    以前是「上下各取一个颜色、拉一条竖渐变」：横向的明暗全丢了，手指抬起来露出这一块
    时就是一团和周围不搭的抹痕（现场那根棕色竖条就是这么来的）。按列插值保留横向结构——
    下巴的阴影、脖子和衣服的边界都留在原来的位置，露出来才像是本来就在手指后面的东西。
    """
    width, height = region.size
    top = top_row.convert("RGBA").resize((width, 1), Image.NEAREST).load()
    bottom = bottom_row.convert("RGBA").resize((width, 1), Image.NEAREST).load()
    filled = Image.new("RGBA", region.size, (0, 0, 0, 0))
    pixels = filled.load()
    for y in range(height):
        ratio = (y + 0.5) / float(height)
        for x in range(width):
            above, below = top[x, 0], bottom[x, 0]
            pixels[x, y] = (
                int(above[0] + (below[0] - above[0]) * ratio),
                int(above[1] + (below[1] - above[1]) * ratio),
                int(above[2] + (below[2] - above[2]) * ratio),
                255,
            )
    softened = filled.filter(ImageFilter.GaussianBlur(max(0.6, float(blur))))
    return Image.composite(softened, region, mask)


def _gradient_fill(
    region: Image.Image,
    mask: Image.Image,
    top_color: Tuple[int, int, int],
    bottom_color: Tuple[int, int, int],
    blur: float,
) -> Image.Image:
    """在 mask 圈出来的地方补一块「上浅下深」的色块，再糊一层柔光接住周围的明暗。

    上下同色时就退化成一块纯色（眼白那个补法就是这种情况）。
    """
    width, height = region.size
    strip = Image.new("RGBA", (1, max(1, height)))
    for y in range(max(1, height)):
        ratio = y / float(max(1, height - 1))
        strip.putpixel(
            (0, y),
            tuple(
                int(top_color[channel] * (1.0 - ratio) + bottom_color[channel] * ratio)
                for channel in range(3)
            ) + (255,),
        )
    gradient = strip.resize(region.size)
    filled = Image.composite(gradient, region, mask)
    softened = filled.filter(ImageFilter.GaussianBlur(max(0.6, float(blur))))
    return Image.composite(softened, region, mask)


def _hand_masks(size: Tuple[int, int], pad: float, hole: bool) -> Image.Image:
    """手那一块贴片的形状：一个圆角矩形（比食指那段略大），边上羽化一下。

    `hole` 是「挖补」用还是「部件层」用——层要比洞大一圈，静止时才能严丝合缝盖回来。
    """
    width, height = size
    inset = pad * (0.55 if hole else 0.30)
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (inset, inset, width - 1 - inset, height - 1 - inset),
        radius=max(1.0, min(width, height) * 0.30),
        fill=255,
    )
    return mask.filter(ImageFilter.GaussianBlur(max(0.6, pad * 0.45)))


def blank_hand(base: Image.Image, rect: Sequence[int]) -> Image.Image:
    """把手/食指那一块从底图里抹掉（按框上下的两行颜色补）——动效帧用的就是这一张。"""
    out = base.convert("RGBA")
    box = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    region = out.crop(box)
    pad = max(2.0, min(region.width, region.height) * HAND_PAD_RATIO * 0.5)
    filled = _column_fill(
        region,
        _hand_masks(region.size, pad, True),
        _row_band(out, box, True),
        _row_band(out, box, False),
        pad * 0.8,
    )
    out.paste(filled, (box[0], box[1]))
    return out


def build_hand_layer(base: Image.Image, rect: Sequence[int]) -> Image.Image:
    """把食指那一段抠成独立一层（透明底、画布对齐），运行时叠回帧上轻轻敲。"""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    box = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    piece = base.convert("RGBA").crop(box)
    pad = max(2.0, min(piece.width, piece.height) * HAND_PAD_RATIO * 0.5)
    piece.putalpha(_hand_masks(piece.size, pad, False))
    layer.alpha_composite(piece, (box[0], box[1]))
    return layer


# ---------- 部件骨架：让动作真的是「头在点 / 手在挥」 ----------
#
# 早先所有帧都是**整张图**做仿射（缩放 / 位移 / 旋转），所以「点头」看着就是整体上下抖——
# 动是动了，但看不出"头在点"。这里把形象拆成三块，各绕各的关节转：
#
#     躯干    头 / 手臂挖掉之后剩下的底图（挖出来的洞按旁边的颜色补上）
#     头      绕**脖子**的枢轴：点头往下沉、摇头左右转、歪头
#     手臂    绕**肩关节**：抬手 / 挥手 / 欢呼（垂在身侧的那只）
#
# 每一帧 = 躯干 → 摆好头 → 摆好手臂 → 再套原来那套整体变换（呼吸 / 弹跳照旧）。
# 只有写了 `rig_keys` 的动作才走这条路；没写的（待机 / 说话 / 情绪那种）跟以前一模一样。
#
# 下面的位置全部是**画布比例**（跟别的部件一个口径），换 --size 不会错位。
# 这套比例是照着**当前这个形象**量出来的（3D 小人：头占上半截、一只手搭在下巴上、
# 另一只手垂在身侧）。换一张完全不同的图，部件会切歪——那就加 `--no-rig`：
# 写了 rig_keys 的动作会自动退回"整体抖"，一帧都不会崩。
RIG_PARTS = ("head", "arm")

RIG_HEAD_CENTER = (0.410, 0.3125)                  # 头的外接椭圆（含耳朵）中心
RIG_HEAD_RADIUS = (0.227, 0.266)                   # 半径：下沿落在下巴（y≈0.58）
RIG_HEAD_FEATHER = 0.010                           # 掩膜羽化
RIG_NECK = (0.406, 0.586)                          # 脖子的枢轴：点头 / 摇头都绕它
RIG_GLOVE_BOX = (0.375, 0.547, 0.594, 0.801)       # 搭在下巴上那只手：**不跟头动**
RIG_NECK_FILL = (0.289, 0.461, 0.547, 0.594)       # 挖头之后要补色的那一段（脖子 / 领口）
RIG_ARM_BOX = (0.164, 0.5625, 0.344, 0.875)        # 垂在身侧那只手：袖子 + 手套
RIG_ARM_FEATHER = 0.0137
RIG_SHOULDER = (0.3125, 0.613)                     # 肩关节
RIG_BODY_STRIP = (0.227, 0.586, 0.344, 0.883)      # 抬手之后露出来的躯干左边（要补色）
RIG_ERASE_MARGIN = 3       # 挖洞比部件层大几圈（3 = 一圈）：免得边上留半透明残影


def _rig_box(size: Tuple[int, int], box: Sequence[float]) -> Tuple[int, int, int, int]:
    """比例 → 画布像素（坐标）。"""
    side = float(max(1, size[0]))
    left, top, right, bottom = (float(value) * side for value in box)
    return (int(round(left)), int(round(top)), int(round(right)), int(round(bottom)))


def _rig_span(size: Tuple[int, int], value: float) -> float:
    """比例 → 画布像素（长度 / 半径）。"""
    return float(max(1, size[0])) * float(value)


def _rig_point(size: Tuple[int, int], point: Sequence[float]) -> Tuple[float, float]:
    """比例 → 画布像素（一个点）。"""
    side = float(max(1, size[0]))
    return (float(point[0]) * side, float(point[1]) * side)


def _ellipse_mask(
    size: Tuple[int, int],
    center: Sequence[float],
    radius: Sequence[float],
    feather: float,
    exclude: Optional[Tuple[int, int, int, int]] = None,
) -> Image.Image:
    """头那一块用的掩膜：一个椭圆（可以再挖掉一块，比如搭在下巴上的手），边上羽化。"""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        (
            center[0] - radius[0], center[1] - radius[1],
            center[0] + radius[0], center[1] + radius[1],
        ),
        fill=255,
    )
    if exclude:
        draw.rounded_rectangle(exclude, radius=max(2, int(radius[0] * 0.18)), fill=0)
    return mask.filter(ImageFilter.GaussianBlur(max(0.6, float(feather))))


def _round_mask(size: Tuple[int, int], box: Sequence[float], feather: float) -> Image.Image:
    """手臂那一块用的掩膜：圆角矩形 + 羽化。"""
    left, top, right, bottom = (float(value) for value in box)
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=max(2.0, min(right - left, bottom - top) * 0.16),
        fill=255,
    )
    return mask.filter(ImageFilter.GaussianBlur(max(0.6, float(feather))))


def _cut_layer(base: Image.Image, mask: Image.Image) -> Image.Image:
    """按掩膜从底图上抠出一层（透明底、和画布对齐）。"""
    layer = base.copy()
    layer.putalpha(ImageChops.multiply(base.getchannel("A"), mask))
    return layer


def _erase(base: Image.Image, mask: Image.Image) -> Image.Image:
    """挖洞。洞比部件层**略大一圈**：部件贴回去时自己的羽化边正好盖住洞沿，
    不会在边上留下一圈半透明的残影（残影叠在深色桌面上就是一道灰边）。"""
    wide = mask.filter(ImageFilter.MaxFilter(max(3, RIG_ERASE_MARGIN | 1)))
    out = base.copy()
    out.putalpha(ImageChops.multiply(base.getchannel("A"), ImageChops.invert(wide)))
    return out


def _part_matrix(
    pivot: Sequence[float], angle: float = 0.0, dx: float = 0.0, dy: float = 0.0, scale: float = 1.0
) -> Tuple[float, ...]:
    """部件「绕自己的关节转 × 位移」的矩阵 —— **输出 → 源图**（PIL 的 transform 要的口径）。

    正向是「绕 pivot 旋转 angle 度（正值 = 顺时针，跟 affine_of 一个约定）→ 缩放 → 平移」。
    """
    theta = math.radians(float(angle))
    cos, sin = math.cos(theta), math.sin(theta)
    inv = 1.0 / max(0.05, float(scale))
    a, b = cos * inv, sin * inv
    d, e = -sin * inv, cos * inv
    px, py = float(pivot[0]), float(pivot[1])
    c = px - a * (px + dx) - b * (py + dy)
    f = py - d * (px + dx) - e * (py + dy)
    return (a, b, c, d, e, f)


def _affine_mul(first: Tuple[float, ...], second: Tuple[float, ...]) -> Tuple[float, ...]:
    """先做 first、再做 second（矩阵都是「输出→源」，所以这一层是"反着套"）。"""
    a1, b1, c1, d1, e1, f1 = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a2 * a1 + b2 * d1, a2 * b1 + b2 * e1, a2 * c1 + b2 * f1 + c2,
        d2 * a1 + e2 * d1, d2 * b1 + e2 * e1, d2 * c1 + e2 * f1 + f2,
    )


def _is_glove(pixel) -> bool:
    """白手套 / 高光：又亮又几乎没颜色——补色取样时要跳过。"""
    r, g, b, a = pixel[:4]
    return a > 150 and min(r, g, b) > 170 and (max(r, g, b) - min(r, g, b)) < 30


def _is_skin(pixel) -> bool:
    """皮肤：暖、亮、**蓝通道也不低**（下巴、脸颊就在那条缝右边，取样时同样要跳过）。

    靠蓝通道跟黄色衬衫分开：衬衫是 (250, 200, 60) 那种，蓝很低；皮肤蓝在 150 以上。
    """
    r, g, b, a = pixel[:4]
    return a > 150 and r > 170 and r - b > 26 and b > 140


def _body_side_fill(body: Image.Image, strip: Sequence[float]) -> Image.Image:
    """抬手之后露出来的躯干左边：拿**右边那一列**的颜色往左涂，外沿压暗一点。

    右边那一列可能是搭下巴的白手套或下巴的皮肤（它们就在这条缝的右边），所以取样要
    跳过手套和皮肤、接着往右找，找到衬衫 / 裤子那种有颜色的像素再涂——不然会补出
    一条白带子或者一块肉色（都真踩过）。找不到就接着用上一行的颜色往下涂。
    """
    x0, y0, x1, y1 = (int(round(value)) for value in strip)
    filled = Image.new("RGBA", body.size, (0, 0, 0, 0))
    fp = filled.load()
    src = body.load()
    colors: Dict[int, Tuple[int, int, int]] = {}
    for y in range(y0, y1):
        color: Optional[Tuple[int, int, int]] = None
        for x in range(x1, min(body.width, x1 + int(body.width * 0.375))):
            pixel = src[x, y]
            if pixel[3] > 150 and not _is_glove(pixel) and not _is_skin(pixel):
                color = (pixel[0], pixel[1], pixel[2])
                break
        if color is None:
            color = colors.get(y - 1)
        if color is None:
            continue
        colors[y] = color
        for x in range(x0, x1):
            k = 0.84 + 0.16 * (x - x0) / max(1, x1 - x0)   # 外沿暗一点，像身子侧面的转折
            fp[x, y] = (int(color[0] * k), int(color[1] * k), int(color[2] * k), 255)
    filled = filled.filter(ImageFilter.GaussianBlur(max(0.6, body.width * 0.0047)))
    clip = _round_mask(body.size, (x0, y0, x1, y1), max(0.6, body.width * 0.0059))
    return Image.composite(filled, body, clip)


def _neck_fill(body: Image.Image, box: Sequence[float]) -> Image.Image:
    """挖掉头之后，脖子 / 领口那一段按**下面那几行**的颜色往上接一段：

    头往下一低，靠下的位置就是它在盖着；没有这一层补色，头一动就会露出一个洞。
    """
    x0, y0, x1, y1 = (int(round(value)) for value in box)
    out = body.copy()
    pixels = out.load()
    src = body.load()
    reach = int(round(max(4, body.height * 0.10)))
    for x in range(x0, x1):
        column = [y for y in range(y0, min(body.height, y1 + reach)) if src[x, y][3] > 150]
        if not column:
            continue
        top = min(column)
        color = src[x, top][:3]
        for y in range(max(0, top - reach), top):
            fade = max(0.0, min(1.0, 1.0 - (top - y) / float(reach + 2)))
            if fade > 0:
                pixels[x, y] = color + (int(round(fade * 240)),)
    return out.filter(ImageFilter.GaussianBlur(max(0.4, body.width * 0.0023)))


class Rig:
    """一张底图 → 一副能摆姿势的骨架（躯干 + 会点头的头 + 会挥手的手臂）。

    枢轴和掩膜的位置都在上面那堆 RIG_* 比例里；换形象改那几行（或者 --no-rig）。
    """

    def __init__(self, base: Image.Image):
        self.size = base.size
        self.neck = _rig_point(base.size, RIG_NECK)
        self.shoulder = _rig_point(base.size, RIG_SHOULDER)
        self.head_mask = _ellipse_mask(
            base.size,
            _rig_point(base.size, RIG_HEAD_CENTER),
            [_rig_span(base.size, value) for value in RIG_HEAD_RADIUS],
            _rig_span(base.size, RIG_HEAD_FEATHER),
            exclude=_rig_box(base.size, RIG_GLOVE_BOX),
        )
        self.arm_mask = _round_mask(
            base.size,
            _rig_box(base.size, RIG_ARM_BOX),
            _rig_span(base.size, RIG_ARM_FEATHER),
        )
        self.head = _cut_layer(base, self.head_mask)
        self.arm = _cut_layer(base, self.arm_mask)
        torso = _neck_fill(_erase(base, self.head_mask), _rig_box(base.size, RIG_NECK_FILL))
        self.torso = _body_side_fill(
            _erase(torso, self.arm_mask), _rig_box(base.size, RIG_BODY_STRIP)
        )

    def head_matrix(self, head: Mapping[str, float]) -> Tuple[float, ...]:
        """头这一帧的变换（绕脖子）——输出→源。"""
        return _part_matrix(
            self.neck,
            angle=float(head.get("angle", 0.0)),
            dx=float(head.get("dx", 0.0)),
            dy=float(head.get("dy", 0.0)),
            scale=float(head.get("scale", 1.0)),
        )

    def arm_matrix(self, arm: Mapping[str, float]) -> Tuple[float, ...]:
        """手臂这一帧的变换（绕肩关节）——输出→源。"""
        return _part_matrix(
            self.shoulder,
            angle=float(arm.get("angle", 0.0)),
            dx=float(arm.get("dx", 0.0)),
            dy=float(arm.get("dy", 0.0)),
            scale=float(arm.get("scale", 1.0)),
        )

    def pose(
        self,
        head: Optional[Mapping[str, float]] = None,
        arm: Optional[Mapping[str, float]] = None,
    ) -> Image.Image:
        """摆一帧（还没套整体变换）：躯干 → 头 → 手臂。两个都不给 = 中立姿势。"""
        frame = self.torso.copy()
        frame.alpha_composite(
            self.head
            if not head
            else self.head.transform(
                self.size, Image.AFFINE, self.head_matrix(head),
                resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0),
            )
        )
        frame.alpha_composite(
            self.arm
            if not arm
            else self.arm.transform(
                self.size, Image.AFFINE, self.arm_matrix(arm),
                resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0),
            )
        )
        return frame

    def eye_matrix(
        self, body: Tuple[float, ...], head: Optional[Mapping[str, float]]
    ) -> Tuple[float, ...]:
        """这一帧要写进 eye_layer.json 的矩阵（**眼珠跟着头走**）。

        帧是「先在底图上摆部件、再整体变换」出来的：p帧 = M(H(p源))，所以反过来的映射
        是「先 M⁻¹ 再 H⁻¹」。顺序写反了眼珠就会飘到额头上去（真踩过）。
        """
        matrix = tuple(body)
        if head:
            matrix = _affine_mul(matrix, self.head_matrix(head))
        return matrix


# ---------- 动效 ----------

def affine_of(
    size: Tuple[int, int],
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    dx: float = 0.0,
    dy: float = 0.0,
    angle: float = 0.0,
) -> Tuple[float, float, float, float, float, float]:
    """算出「原图 → 这一帧」的仿射矩阵 (a,b,c,d,e,f)：x' = a·x + b·y + c，y' = d·x + e·y + f。

    和 warp() 共用一个来源，所以存进 eye_layer.json 的矩阵一定和帧对得上，
    眼珠层套同一个变换就不会在身体起伏的时候从眼眶里飘出去。
    """
    width, height = size
    cx, cy = width / 2.0, height / 2.0
    theta = math.radians(angle)
    cos, sin = math.cos(theta), math.sin(theta)
    sx, sy = max(0.05, scale_x), max(0.05, scale_y)
    a, b = cos / sx, sin / sx
    d, e = -sin / sy, cos / sy
    c = cx - a * (cx + dx) - b * (cy + dy)
    f = cy - d * (cx + dx) - e * (cy + dy)
    return (a, b, c, d, e, f)


def warp(
    base: Image.Image,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    dx: float = 0.0,
    dy: float = 0.0,
    angle: float = 0.0,
    matrix: Optional[Tuple[float, ...]] = None,
) -> Image.Image:
    """把图整体缩放/位移/旋转一点（围绕中心），用来一帧一帧地做出动感。"""
    size = base.size
    affine = tuple(matrix) if matrix else affine_of(size, scale_x, scale_y, dx, dy, angle)
    return base.transform(
        size,
        Image.AFFINE,
        affine,
        resample=Image.BICUBIC,
        fillcolor=(0, 0, 0, 0),
    )


# 每套动作的参数：呼吸（横向鼓 / 纵向压）、弹跳幅度、左右漂移、摇摆角度、说话时的小抖动
# 注意：渲染器**不会再叠**程序化弹跳了（见 sprite.py 的 _draw_frames），
# 帧之间的过渡靠交叉淡化补平，所以这里的幅度就是最终看到的幅度。
#
# 口径：「只在肉眼范围内有一点起伏」——一整套转完一圈（ui.anim_period 默认 30 秒），
# 待机的头顶只挪 1px、脚底 3px、身高变化 2px（256 画布；挂件 pet_size=168 时再 ×0.66），
# 看着像在轻轻呼吸，而不是挂在桌边一直喘。情绪那几套留更多弹跳（那是表情，不是呼吸），
# 但呼吸分量和待机一个量级。
# 想看出明显的动感：把 idle 那行乘 3 就是最早那版（脚底 10px / 身高 9px，太喘了）。
# 改完要重新生成帧才生效：python tools/make_pet.py assets/source/pet_new.png
MOTIONS: Dict[str, Dict[str, float]] = {
    "idle": {"breathe_x": 0.003, "breathe_y": 0.006, "bob": 0.007, "drift": 0.001, "sway": 0.6, "jitter": 0.0},
    "talk": {"breathe_x": 0.005, "breathe_y": 0.008, "bob": 0.016, "drift": 0.002, "sway": 1.2, "jitter": 0.010},
    "happy": {
        # 开心：不是"整体上下抖"，而是**轻轻一跳一跳 + 头跟着晃**——
        # 待机切到开心（或者菜单里夸它一下）就是"看着就高兴"的样子。
        "rig_keys": [
            {"head": {"dy": 0.000, "angle": 0.0}},
            {"dy": -0.008, "scale": 1.008, "squash": -0.008, "head": {"dy": -0.005, "angle": -3.0}},
            {"dy": -0.014, "scale": 1.014, "squash": -0.014, "head": {"dy": -0.009, "angle": -4.5}},
            {"dy": -0.004, "scale": 1.004, "head": {"dy": -0.002, "angle": -1.0}},
            {"dy": 0.005, "scale": 0.994, "squash": 0.008, "head": {"dy": 0.003, "angle": 2.5}},
            {"dy": -0.002, "scale": 1.002, "head": {"dy": -0.001, "angle": -2.0}},
        ],
    },
    "excited": {"breathe_x": 0.010, "breathe_y": 0.007, "bob": 0.036, "drift": 0.005, "sway": 2.2, "jitter": 0.014},
    "speechless": {"breathe_x": 0.002, "breathe_y": 0.006, "bob": 0.005, "drift": 0.001, "sway": 1.0, "jitter": 0.0},
    "curious": {"breathe_x": 0.003, "breathe_y": 0.006, "bob": 0.008, "drift": 0.005, "sway": 3.0, "jitter": 0.0},
    "smirk": {"breathe_x": 0.003, "breathe_y": 0.006, "bob": 0.008, "drift": 0.002, "sway": 0.8, "jitter": 0.0},
    # 下面三种跟着 pet/mood.py 一起加的（惊讶 / 生气 / 难过）：手写关键帧 + 一个小符号。
    # 为什么情绪也值得手写：图片帧模式下，情绪"长什么样"全靠这套帧本身
    # （MOOD_STYLE 里的眉眼只对内置矢量形象生效），所以动作得写足。
    "surprised": {                      # 惊魂未定：发颤 + 稍微撑大（配 sprite 的 0.10 tempo）
        "keys": [                       # 幅度压着点：情绪帧要跟待机共用一个内容框
            {"scale": 1.02, "dy": -0.006, "tilt": -1},
            {"scale": 1.035, "dy": -0.016, "tilt": 2},
            {"scale": 1.01, "dy": -0.004, "tilt": -2},
            {"scale": 1.03, "dy": -0.012, "tilt": 1},
        ],
        "deco": "!",
    },
    "angry": {                          # 生气：两边挣 + 快抖（0.06 tempo = 1.8 秒一圈）
        "keys": [
            {"tilt": -5, "dx": -0.010, "squash": 0.030},
            {"tilt": 5, "dx": 0.010, "squash": 0.030},
            {"tilt": -4, "dx": -0.008, "dy": 0.008},
            {"tilt": 4, "dx": 0.008, "dy": 0.008},
        ],
        "deco": "anger",
    },
    "sad": {                            # 难过：低头、缩一点、慢慢往下沉
        "keys": [
            {"dy": 0.010, "scale": 0.995, "tilt": -2},
            {"dy": 0.022, "scale": 0.980},
            {"dy": 0.014, "scale": 0.990, "tilt": 2},
            {"dy": 0.020, "scale": 0.985},
        ],
        "deco": "sad",
    },
}


def _key_params(keys: Sequence[Mapping[str, float]], t: float) -> Dict[str, float]:
    """按 0~1 的进度在手写关键帧之间线性插值（插完那一圈会接回第一帧）。

    注意 `scale` 的空缺值是 **1.0**（原大小），别的量才是 0——
    不然"这一组只写了 dy、下一组写了 scale"会插出 scale=0 来，
    那一帧就缩成一个点了（真踩过）。
    """
    count = len(keys)
    if count == 1:
        return dict(keys[0])

    def value(frame: Mapping[str, float], name: str) -> float:
        return float(frame.get(name, 1.0 if name == "scale" else 0.0))

    position = t * count
    index = int(position) % count
    frac = position - int(position)
    first, second = keys[index], keys[(index + 1) % count]
    names = set(first) | set(second)
    return {
        name: value(first, name) + (value(second, name) - value(first, name)) * frac
        for name in names
    }


def _key_progress(index: int, count: int, loop: bool) -> float:
    """关键帧采样点：loop=True 铺满一圈（最后一帧接回第一帧），
    loop=False 铺在 0~1 上（含头含尾，所以最后一组写"回中立"，播完落回待机不会硬切）。"""
    return index / max(1, count if loop else count - 1)


def _keys_affine(
    keys: Sequence[Mapping[str, float]],
    index: int,
    count: int,
    size: int,
    loop: bool = True,
) -> Tuple[float, ...]:
    """手写关键帧 → 这一帧的仿射矩阵（整体那一路：呼吸 / 弹跳 / 摇摆）。"""
    t = _key_progress(index, count, loop)
    params = _key_params(keys, t)
    scale = params.get("scale", 1.0)
    squash = params.get("squash", 0.0)
    # 压扁是"纵向压、横向鼓"：体积看着才守恒，像真的蹲了一下
    scale_x = scale * (1.0 + squash * 0.5)
    scale_y = scale * (1.0 - squash)
    return affine_of(
        (size, size),
        scale_x=scale_x,
        scale_y=scale_y,
        dx=params.get("dx", 0.0) * size,
        dy=params.get("dy", 0.0) * size,
        angle=params.get("tilt", 0.0),
    )


def _sub_keys(keys: Sequence[Mapping[str, object]], name: str) -> List[Dict[str, float]]:
    """从 `rig_keys` 里挑出**一层**的参数：name 空 = 整体那一路（去掉 head / arm）。

    挑出来的是一串「只有数字」的字典，正好能直接喂给 `_key_params` 插值。
    """
    out: List[Dict[str, float]] = []
    for frame in keys:
        if name:
            part = frame.get(name) or {}
            out.append({str(k): float(v) for k, v in dict(part).items()})  # type: ignore[arg-type]
        else:
            out.append({
                str(k): float(v)  # type: ignore[arg-type]
                for k, v in frame.items()
                if k not in RIG_PARTS
            })
    return out


def rig_frame_matrices(
    spec: Mapping[str, object], index: int, count: int, size: int
) -> Tuple[Tuple[float, ...], Dict[str, float], Dict[str, float]]:
    """`rig_keys` → 这一帧的（整体变换矩阵, 头参数, 手臂参数）。

    三层各插各的：整体那路还是 dx/dy/scale/squash/tilt 那一套，头 / 手臂是
    `{"dy": …, "dx": …, "angle": …, "scale": …}`（dy/dx 是画布比例，angle 是度）。
    """
    keys = spec.get("rig_keys") or []
    loop = bool(spec.get("keys_loop", True))
    t = _key_progress(index, count, loop)
    body = _keys_affine(_sub_keys(keys, ""), index, count, size, loop)
    head = _key_params(_sub_keys(keys, "head"), t) if keys else {}
    arm = _key_params(_sub_keys(keys, "arm"), t) if keys else {}
    # 头 / 手臂的 dy、dx 也按画布比例写，换算成像素再交给 _part_matrix
    for params in (head, arm):
        if "dy" in params:
            params["dy"] = params["dy"] * size
        if "dx" in params:
            params["dx"] = params["dx"] * size
    return body, head, arm


def motion_affine(spec: Dict[str, object], index: int, count: int, size: int) -> Tuple[float, ...]:
    """第 index 帧的仿射矩阵（build_frames 和 eye_layer.json 用的都是这一份）。

    spec 里给了 `keys`（手写关键帧）就按关键帧插值——`keys_loop` 决定是"转一圈"
    还是"走一遍"；只给了 `rig_keys`（逐部件那套）但**没有骨架**时，退回只套它里面的
    整体参数（幅度小一点，动作不至于消失）；都没给就退回原来那套正弦抖（呼吸 / 摇摆 / 抖动）。
    """
    keys = spec.get("keys")
    if keys:
        return _keys_affine(
            keys, index, count, size, loop=bool(spec.get("keys_loop", True))
        )
    rig_keys = spec.get("rig_keys")
    if rig_keys:
        return _keys_affine(
            _sub_keys(rig_keys, ""), index, count, size, bool(spec.get("keys_loop", True))
        )
    phase = 2 * math.pi * index / count
    wave = math.sin(phase)
    lift = max(0.0, wave) ** 1.2          # 弹跳只往上，落地那半圈像"踩在地上"
    squash = spec.get("bob", 0.0) * 0.35 * lift

    sx = 1.0 + spec.get("breathe_x", 0.0) * wave + squash * 0.5
    sy = 1.0 - spec.get("breathe_y", 0.0) * wave - squash
    dy = -spec.get("bob", 0.0) * size * lift
    if spec.get("jitter"):
        dy += spec["jitter"] * size * math.sin(phase * 3.0)   # 说话时下巴动得多一点
    dx = spec.get("drift", 0.0) * size * math.sin(phase + math.pi)
    angle = spec.get("sway", 0.0) * math.sin(phase + math.pi / 2) * 0.5
    return affine_of((size, size), scale_x=sx, scale_y=sy, dx=dx, dy=dy, angle=angle)


def motion_tables(
    count: int, size: int, rig: Optional["Rig"] = None
) -> Tuple[Dict[str, List[Tuple[float, ...]]], Dict[str, List[Tuple[float, ...]]]]:
    """每套动作、每一帧的仿射矩阵——分**眼珠**和**食指**两张表。

    这两张以前是一张：整张图一起动，眼珠和食指套同一个矩阵就够了。现在头和手臂各动
    各的，两张表就分家了——眼珠**跟着头**（不然它会飘到额头上去），食指跟着**躯干**
    （它长在手上、手挂在身子上，不跟着头转）。

    这里必须**连串门动作那几套也算进去**：少一套的话，做那个动作时眼珠层和食指层
    找不到对应的矩阵，渲染器会干脆不画它们（人一动，眼睛和手就没了）。
    """
    specs: Dict[str, Dict[str, object]] = dict(MOTIONS)  # type: ignore[arg-type]
    specs.update(play_mod.motions())      # 串门动作（见 pet/play.py）
    specs.update(states_mod.motions())    # 日常状态 / 界面互动（见 pet/states.py）

    eye: Dict[str, List[Tuple[float, ...]]] = {}
    hand: Dict[str, List[Tuple[float, ...]]] = {}
    for name, spec in specs.items():
        if spec.get("rig_keys") and rig is not None:
            eye_rows: List[Tuple[float, ...]] = []
            hand_rows: List[Tuple[float, ...]] = []
            for index in range(count):
                body, head, _arm = rig_frame_matrices(spec, index, count, size)
                eye_rows.append(rig.eye_matrix(body, head))
                hand_rows.append(body)
            eye[name], hand[name] = eye_rows, hand_rows
        else:
            rows = [motion_affine(spec, index, count, size) for index in range(count)]
            eye[name] = rows
            hand[name] = rows
    return eye, hand


# ---------- 帧上的小提示符号（让动作更好读）----------

#: 符号 → 颜色（和 pet/mood.py 的主题色一个调子）
DECO_COLORS: Dict[str, Tuple[int, int, int]] = {
    "spark": (255, 209, 102),   # 暖黄星星：开心 / 打招呼 / 加油
    "?": (169, 227, 75),        # 黄绿问号：疑惑
    "!": (255, 180, 84),        # 橙黄叹号：被戳
    "anger": (255, 107, 107),   # 红色怒气
    "sad": (127, 168, 255),     # 蓝汗滴
}


def _star(draw: "ImageDraw.ImageDraw", cx: float, cy: float, radius: float, fill) -> None:
    """四角星（八个点画出来的，比圆点更像"闪一下"）。"""
    points = []
    for step in range(8):
        length = radius if step % 2 == 0 else radius * 0.32
        angle = math.radians(step * 45 - 90)
        points.append((cx + length * math.cos(angle), cy + length * math.sin(angle)))
    draw.polygon(points, fill=fill)


def paint_deco(target: Image.Image, name: str, index: int, count: int) -> None:
    """在**已经摆好姿势**的那一帧上，画一个小小的提示符号。

    位置都挑在人物脑袋附近、且**落在内容框里面**：这样它只是让动作更好读，
    不会把内容框撑大（撑大了，站着不动的时候整个人会跟着缩小，得不偿失）。
    符号跟着帧号轻轻浮一下，看着像冒出来的，而不是贴上去的。
    """
    size = target.width
    color = DECO_COLORS.get(name, (255, 255, 255))
    fill = (color[0], color[1], color[2], 235)
    stroke = max(2, int(round(size * 0.016)))
    bob = math.sin(2 * math.pi * (index / max(1, count))) * size * 0.012

    overlay = Image.new("RGBA", target.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    if name == "spark":
        cx, cy = size * 0.24, size * 0.16 + bob
        _star(draw, cx, cy, size * 0.055, fill)
        _star(draw, cx + size * 0.070, cy - size * 0.052, size * 0.030, fill)
    elif name == "?":
        cx, cy = size * 0.60, size * 0.17 + bob
        r = size * 0.038
        draw.arc([cx - r, cy - r, cx + r, cy + r], 150.0, 40.0, fill=fill, width=stroke)
        draw.line([(cx + r * 0.60, cy + r * 0.85), (cx, cy + r * 1.55)], fill=fill, width=stroke)
        dot = max(1.5, size * 0.009)
        tip = cy + r * 2.0
        draw.ellipse([cx - dot, tip - dot, cx + dot, tip + dot], fill=fill)
    elif name == "!":
        cx, cy = size * 0.60, size * 0.16 + bob
        r = size * 0.045
        draw.line([(cx, cy - r), (cx, cy + r * 0.45)], fill=fill, width=stroke)
        dot = max(1.5, size * 0.010)
        tip = cy + r * 1.05
        draw.ellipse([cx - dot, tip - dot, cx + dot, tip + dot], fill=fill)
    elif name == "anger":
        # 太阳穴上那两撮"怒气"：四根短线围成一朵
        for px, py in ((0.28, 0.20), (0.58, 0.18)):
            cx, cy = size * px, size * py + bob
            r = size * 0.028
            for angle in (45.0, 135.0, 225.0, 315.0):
                rad = math.radians(angle)
                draw.line(
                    [
                        (cx + math.cos(rad) * r * 0.40, cy + math.sin(rad) * r * 0.40),
                        (cx + math.cos(rad) * r, cy + math.sin(rad) * r),
                    ],
                    fill=fill,
                    width=max(2, int(round(size * 0.013))),
                )
    elif name == "sad":
        cx, cy = size * 0.34, size * 0.44 + bob
        r = size * 0.020
        draw.ellipse([cx - r, cy - r * 0.7, cx + r, cy + r * 1.3], fill=fill)
        draw.polygon(
            [(cx, cy - r * 2.0), (cx - r * 0.85, cy - r * 0.1), (cx + r * 0.85, cy - r * 0.1)],
            fill=fill,
        )
    target.alpha_composite(overlay)


def build_frames(
    base: Image.Image,
    spec: Dict[str, object],
    count: int = FRAME_COUNT,
    size: int = 256,
    rig: Optional["Rig"] = None,
) -> List[Image.Image]:
    """生成一整套帧。

    * 写了 `rig_keys`（**逐部件**的手写关键帧）而且骨架可用：先在底图上把头和手臂
      摆到这一帧的姿势，再套整体变换——这才是「头在点 / 手在挥」；
    * 只写了抖法（呼吸 / 抖动那种）：sin 走满一整圈，循环播放不会跳帧；
    * 写了 `keys`（手写关键帧）：按关键帧摆姿势，采成 count 帧；
    * `deco` 有值就再画一个小提示符号——**画在摆好姿势之后**，
      所以符号不会跟着身体一起被拉扁。

    没有骨架（`--no-rig`，或者换了形象不适用）时，`rig_keys` 会自动退回"只套整体变换"：
    动作幅度小一点，但一帧都不会崩。
    """
    keys = spec.get("rig_keys")
    if keys and rig is not None:
        frames: List[Image.Image] = []
        for index in range(count):
            body, head, arm = rig_frame_matrices(spec, index, count, size)
            frames.append(warp(rig.pose(head=head, arm=arm), matrix=body))
    else:
        frames = [
            warp(base, matrix=motion_affine(spec, index, count, size))
            for index in range(count)
        ]
    deco = str(spec.get("deco") or "")
    if deco:
        for index, frame in enumerate(frames):
            paint_deco(frame, deco, index, count)
    return frames


def write_frames(folder: Path, name: str, frames: Sequence[Image.Image]) -> List[Path]:
    """写一套帧（先清掉旧的同名帧，免得上一版形象残留）。"""
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob(f"{name}_*.png"):
        stale.unlink()
    written: List[Path] = []
    for index, frame in enumerate(frames):
        target = folder / f"{name}_{index:02d}.png"   # 补零：idle_10 才会排在 idle_02 后面
        frame.save(target, format="PNG")
        written.append(target)
    return written


def describe(base: Image.Image, label: str) -> Tuple[float, float]:
    """打印一眼就能判断对错的统计：透明占比、实体占比、内容框。"""
    total = max(1, base.width * base.height)
    histogram = base.getchannel("A").histogram()
    clear = sum(histogram[:8]) / total
    solid = sum(histogram[248:]) / total
    print(
        f"  {label}: {base.width}x{base.height}，透明 {clear * 100:.1f}%，"
        f"实体 {solid * 100:.1f}%，内容框 {base.getbbox()}"
    )
    return clear, solid


def demo_character(size: int = 512) -> Image.Image:
    """代码画的占位形象（黄圆脸 + 大眼 + 眉毛 + 白手套 + 蓝鞋）。

    只为"还没有原图时也能验证抠图和动效"用，不是要替你的图。
    """
    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    cx, cy = size / 2, size * 0.45
    r = size * 0.33
    edge = max(2, size // 170)

    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r], fill=(255, 205, 40), outline=(226, 168, 20), width=edge
    )
    for sign in (-1, 1):
        ex = cx + sign * r * 0.42
        ey = cy - r * 0.10
        ew, eh = r * 0.30, r * 0.34
        draw.ellipse([ex - ew, ey - eh, ex + ew, ey + eh], fill=(250, 250, 250))
        draw.ellipse(
            [ex - ew * 0.55, ey - eh * 0.45, ex + ew * 0.55, ey + eh * 0.70],
            fill=(58, 44, 30),
        )
        draw.line(
            [ex - ew, ey - eh - r * 0.20, ex + ew, ey - eh - r * 0.13],
            fill=(120, 78, 30),
            width=max(3, size // 95),
        )
    draw.arc(
        [cx - r * 0.44, cy + r * 0.08, cx + r * 0.44, cy + r * 0.60],
        20,
        160,
        fill=(120, 78, 30),
        width=max(3, size // 95),
    )
    # 抬起的手（白手套）
    draw.ellipse(
        [cx - r * 1.08, cy + r * 0.30, cx - r * 0.42, cy + r * 0.98],
        fill=(252, 252, 252),
        outline=(212, 212, 212),
        width=edge,
    )
    # 搭在下巴上的那只手：拳头 + 竖起来的食指（用来试“食指轻轻敲下巴”）
    draw.ellipse(
        [cx + r * 0.30, cy + r * 0.55, cx + r * 0.88, cy + r * 1.00],
        fill=(252, 252, 252),
        outline=(212, 212, 212),
        width=edge,
    )
    finger_w = r * 0.155
    finger_x = cx + r * 0.47
    draw.rounded_rectangle(
        [finger_x - finger_w, cy + r * 0.34, finger_x + finger_w, cy + r * 0.68],
        radius=finger_w,
        fill=(252, 252, 252),
        outline=(212, 212, 212),
        width=edge,
    )
    # 鞋子（蓝）
    draw.rounded_rectangle(
        [cx - r * 0.58, cy + r * 1.00, cx + r * 0.12, cy + r * 1.46],
        radius=size * 0.028,
        fill=(72, 112, 190),
    )
    draw.rounded_rectangle(
        [cx + r * 0.04, cy + r * 1.10, cx + r * 0.64, cy + r * 1.50],
        radius=size * 0.028,
        fill=(72, 112, 190),
    )
    return image


# 源图太小的兜底：短边小于 UPSCALE_MIN_SIDE 就先放大，免得后面缩上去糊成一团
UPSCALE_MIN_SIDE = 160
UPSCALE_TARGET = 320


def auto_upscale_factor(image: Image.Image) -> float:
    """短边小于 160 时给出"放大到短边约 320"的倍数（1.0 表示不用放）。"""
    side = min(image.width, image.height)
    if side >= UPSCALE_MIN_SIDE or side <= 0:
        return 1.0
    return max(1.0, min(8.0, UPSCALE_TARGET / float(side)))


def upscale_source(image: Image.Image, factor: float) -> Image.Image:
    """LANCZOS 放大 + 轻锐化：比让后面直接缩上去清楚一点（不会凭空长出细节）。"""
    if factor <= 1.01:
        return image
    size = (
        max(1, int(round(image.width * factor))),
        max(1, int(round(image.height * factor))),
    )
    bigger = image.convert("RGBA").resize(size, Image.LANCZOS)
    return bigger.filter(ImageFilter.UnsharpMask(radius=1.6, percent=70, threshold=3))


# ---------- 命令行 ----------

def rel(path: Path) -> str:
    """相对项目根显示；在外面（比如临时目录）就原样显示，免得好端端报个异常。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _safe_console() -> None:
    """让提示语在中文 Windows 的 GBK 控制台里不至于把程序打断。

    `⚠` 这类字符 GBK 编不出来，控制台编码不变的话 print 会直接抛
    UnicodeEncodeError 把整个脚本打断（踩过）。优先复用 pet.make_console_safe；
    万一 import 不进来（比如把这个脚本单拷出去用）就自己兜一份最简的。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from pet import make_console_safe
    except Exception:
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is None:
                continue
            try:
                reconfigure(errors="replace")
            except Exception:
                pass
        return
    make_console_safe()



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make-pet",
        description="把一张人物图做成桌宠帧：抠背景 → 裁边 → 缩放 → 生成会动的帧。",
    )
    parser.add_argument("source", nargs="?", help=f"源图片；默认 {DEFAULT_SOURCE.relative_to(ROOT)}")
    parser.add_argument("--out", help=f"帧输出目录；默认 {ASSETS_PET.relative_to(ROOT)}")
    parser.add_argument("--size", type=int, default=256, help="每帧边长，默认 256")
    parser.add_argument(
        "--frames",
        type=int,
        default=FRAME_COUNT,
        help=f"每套几帧，默认 {FRAME_COUNT}（越多越顺，别超过 {MAX_FRAMES}）",
    )
    parser.add_argument(
        "--shade",
        type=float,
        default=0.5,
        help="背景上柔和阴影（3D 渲染图常见的地面投影）的判定门槛：暗到背景的多少倍"
        "还认。色相和背景一样、只是整体变暗的那块就是靠它抠掉的（默认 0.5，0 = 关掉）",
    )
    parser.add_argument(
        "--strict",
        type=float,
        default=12,
        help="封闭在人物里面的背景（比如两腿之间那道缝）用的严格色差门槛：比 --tol 严得多，"
        "只有几乎和背景一模一样的像素才算（默认 12，0 = 不清理）",
    )
    parser.add_argument(
        "--tol", type=int, default=26, help="背景色差阈值：背景越花调越大（默认 26）"
    )
    parser.add_argument(
        "--chroma",
        type=float,
        default=0.55,
        help="背景\"够不够有颜色\"的闸：候选像素的彩度至少要达到背景彩度的这个比例，"
        "背景是暖色而人物身上有白手套/白衬衫时靠它分开（默认 0.55，0 = 关掉）",
    )
    parser.add_argument("--feather", type=float, default=0.8, help="边缘柔化程度，0 = 硬边（默认 0.8）")
    parser.add_argument(
        "--heal",
        type=int,
        default=4,
        help="把人物身上细到几像素的透明裂缝补掉（抠图时背景从耳缝/下巴缝渗进去留下的，"
        "缩放后会变成一圈脏边）；这是允许补的最大缝宽，0 = 不补（默认 4）",
    )
    parser.add_argument("--bg", help="手动指定背景色，如 255,255,255（默认从四边自动猜）")
    parser.add_argument(
        "--upscale",
        type=float,
        default=0.0,
        help="源图太小就手动指定放大倍数（默认自动：短边小于 160 就放大到约 320）",
    )
    parser.add_argument("--no-upscale", action="store_true", help="不做任何放大，原样抠图")
    parser.add_argument(
        "--min-blob",
        type=int,
        default=48,
        help="小于这么多像素的碎块直接抹掉（截图边框容易留下小黑点；0 = 不按绝对大小清理，默认 48）",
    )
    parser.add_argument(
        "--min-ratio",
        type=float,
        default=0.02,
        help="碎块的相对门槛：比「最大那块（人物本体）× 这个比例」还小的都抹掉，"
        "背景上的闪光/水印就靠它清干净（默认 0.02 = 2%%，0 = 关掉）",
    )
    parser.add_argument("--demo", action="store_true", help="不用原图，用代码画的占位形象演示一遍")
    parser.add_argument("--install", action="store_true", help="配合 --demo：把占位帧也装进 assets/pet")
    parser.add_argument("--cutout", help="把抠好的图另存一份方便检查，比如 assets/source/cut.png")
    parser.add_argument("--clear", action="store_true", help="删掉 assets/pet 里的帧，换回内置矢量形象")
    parser.add_argument(
        "--eyes",
        help="手动指定眼珠（自动找不到时用）：左x,左y,左r,右x,右y,右r，画布像素坐标",
    )
    parser.add_argument(
        "--no-eyes",
        action="store_true",
        help="不做“眼珠跟鼠标”（默认会自动找眼睛，找到就装上）",
    )
    parser.add_argument(
        "--eye-travel",
        type=float,
        default=EYE_TRAVEL,
        help=f"眼珠最多挪多远（按眼珠半径的比例，默认 {EYE_TRAVEL}；调大更像东张西望）",
    )
    parser.add_argument(
        "--eye-preview",
        default=str(EYE_PREVIEW),
        help=f"眼睛检查图存哪儿（默认 {EYE_PREVIEW.relative_to(ROOT)}，留空就不存）",
    )
    parser.add_argument(
        "--no-rig",
        action="store_true",
        help="不要“部件骨架”（头绕脖子点、手臂绕肩挥）。默认会给形象拆出这几块，"
        "写了逐部件关键帧的动作（点头 / 摇头 / 挥手…）才动得起来；"
        "换了一张完全不同的图、部件切歪了，就加上它退回“整体抖”",
    )
    parser.add_argument(
        "--hand-box",
        help="手动指定搭在下巴上的那只手（自动找不到时用）：左,上,右,下，画布像素坐标",
    )
    parser.add_argument(
        "--no-hand",
        action="store_true",
        help="不要“食指轻轻敲下巴”（默认会自动找那只手，找到就装上）",
    )
    parser.add_argument(
        "--tap-lift",
        type=float,
        default=TAP_LIFT,
        help=f"食指敲一下抬多高（画布像素，默认 {TAP_LIFT}；调大敲得更明显）",
    )
    parser.add_argument(
        "--tap-period",
        type=float,
        default=TAP_PERIOD,
        help=f"敲一下几秒一轮（默认 {TAP_PERIOD} 秒；调大更慢更轻）",
    )
    parser.add_argument(
        "--hand-preview",
        default=str(HAND_PREVIEW),
        help=f"手的检查图存哪儿（默认 {HAND_PREVIEW.relative_to(ROOT)}，留空就不存）",
    )
    return parser


def main(argv=None) -> int:
    _safe_console()
    args = build_parser().parse_args(argv)

    if args.clear:
        removed = [path for path in ASSETS_PET.glob("*.png")]
        for path in removed:
            path.unlink()
        extra = clear_part_assets(ASSETS_PET, (EYE_META_FILE, HAND_META_FILE))
        print(
            f"已删掉 {len(removed)} 张帧{'和部件层' if extra else ''}，"
            "形象回到内置矢量版本（重启挂件后生效）。"
        )
        return 0

    if args.demo:
        source_image = demo_character(max(256, args.size * 2))
        out_dir = Path(args.out) if args.out else (ASSETS_PET if args.install else DEMO_DIR)
        print("这次用的是代码画的占位形象（不是你的图）。")
        if not args.install:
            print(f"只写进 {rel(out_dir)}，想装进挂件就再加 --install。")
    else:
        path = Path(args.source) if args.source else DEFAULT_SOURCE
        if not path.exists():
            print(f"找不到图片：{path}")
            print(f"把人物图存到 {DEFAULT_SOURCE.relative_to(ROOT)} 再跑一次；")
            print("或者先看看动效长什么样：python tools/make_pet.py --demo")
            return 1
        try:
            source_image = Image.open(path)
        except Exception as exc:
            print(f"打不开这张图：{exc}")
            return 1
        out_dir = Path(args.out) if args.out else ASSETS_PET
        print(f"读入：{path}（{source_image.width}x{source_image.height}，{source_image.mode}）")

        factor = 1.0 if args.no_upscale else (float(args.upscale) if args.upscale else 0.0)
        if not factor:
            factor = auto_upscale_factor(source_image)
        if factor > 1.01:
            before = source_image.size
            source_image = upscale_source(source_image, factor)
            print(
                f"源图只有 {before[0]}x{before[1]}，先放大 {factor:.1f} 倍到 "
                f"{source_image.width}x{source_image.height} 再抠图（不想要就加 --no-upscale）"
            )

    background = None
    if args.bg:
        try:
            background = tuple(int(part) for part in args.bg.replace("，", ",").split(","))[:3]
            if len(background) != 3:
                raise ValueError
        except Exception:
            print("--bg 要写成 255,255,255 这样")
            return 1

    cut, used_bg = cut_background(
        source_image,
        tolerance=max(0, args.tol),
        feather=max(0.0, args.feather),
        background=background,
        chroma=max(0, args.chroma),
        shade=max(0.0, float(args.shade)),
        strict=max(0.0, float(args.strict)),
    )
    print(f"背景色：rgb{used_bg}，容差 {args.tol}，边缘柔化 {args.feather}")

    cut, dropped = despeckle(cut, int(args.min_blob), float(args.min_ratio))
    if dropped:
        print(f"顺手抹掉 {dropped} 个碎块像素（比 {args.min_blob} 像素还小、或者不到本体的 {args.min_ratio:.1%}）")

    base = fit_canvas(cut, size=max(48, args.size))

    if args.heal > 0:
        base, healed = heal_slits(base, max_width=max(0, int(args.heal)))
        if healed:
            print(
                f"补掉 {healed} 个像素的细缝（抠图时背景从耳缝/下巴缝里渗进去留下的，"
                "不补的话缩放后会变成一圈脏边）"
            )

    clear_ratio, solid_ratio = describe(base, "抠好的形象")
    if clear_ratio < 0.05:
        print("⚠ 透明区太少，背景可能没删掉：把 --tol 调大（比如 40），或者用 --bg 手动指定背景色")
    if solid_ratio < 0.05:
        print("⚠ 实体区太少，人物可能被当成背景删掉了：把 --tol 调小（比如 12）")

    if args.cutout:
        target = Path(args.cutout)
        target.parent.mkdir(parents=True, exist_ok=True)
        base.save(target)
        print(f"另存抠图：{target}")

    count = min(MAX_FRAMES, max(2, int(args.frames)))
    if count != args.frames:
        print(f"（帧数按 {count} 来：2~{MAX_FRAMES} 之间，渲染器才播得动）")

    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 眼睛：抠出眼珠层、把底图里的眼珠抹掉，运行时跟着鼠标看 ----
    # 眼睛和手都在放大 PART_DETECT_SCALE 倍的画布上找：3D 渲染缩到 256 之后，眼白
    # 只剩下一个像素、而且未必和瞳孔在同一行；放大一圈再做判据才稳（几何完全一样，
    # 只是大一圈，坐标除回来就行）。
    detect = fit_canvas(cut, size=max(48, args.size) * PART_DETECT_SCALE)
    zoom = float(PART_DETECT_SCALE)

    eyes: List[Tuple[float, float, float]] = []
    if not args.no_eyes:
        try:
            eyes = (
                parse_eyes(args.eyes)
                if args.eyes
                else [(cx / zoom, cy / zoom, r / zoom) for cx, cy, r in find_eyes(detect)]
            )
        except ValueError as exc:
            print(f"⚠ {exc}")
            return 1
        if not eyes:
            print("⚠ 没找到眼睛（眼白不够白 / 戴着眼镜？），这个形象就不跟鼠标了。")
            print("  想手动指：--eyes 左x,左y,左r,右x,右y,右r（画布像素坐标）")

    # ---- 手：抠出搭在下巴上的食指，运行时让它轻轻敲 ----
    hand: Optional[Tuple[int, int, int, int]] = None
    if not args.no_hand:
        try:
            if args.hand_box:
                hand = parse_box(args.hand_box)
            else:
                # 找手之前要先把眼睛挖掉，所以把眼睛换算到放大画布上一起传进去
                big = find_hand(detect, [(cx * zoom, cy * zoom, r * zoom) for cx, cy, r in eyes])
                if big:
                    hand = (
                        int(round(big[0] / zoom)),
                        int(round(big[1] / zoom)),
                        int(round(big[2] / zoom)),
                        int(round(big[3] / zoom)),
                    )
        except ValueError as exc:
            print(f"⚠ {exc}")
            return 1
        if hand is None:
            print("⚠ 没找到搭在下巴上的那只手，就不敲下巴了。")
            print("  想手动指：--hand-box 左,上,右,下（画布像素坐标）")

    frames_base = base
    blanked_eyes: Optional[Image.Image] = blank_eyes(base, eyes) if eyes else None
    if blanked_eyes is not None:
        frames_base = blanked_eyes
    blanked_hand: Optional[Image.Image] = None
    if hand is not None:
        blanked_hand = blank_hand(frames_base, hand)
        frames_base = blanked_hand

    # ---- 骨架：把形象拆成「躯干 / 头 / 手臂」，头绕脖子点、手臂绕肩挥 ----
    # 必须等眼珠和食指都抹掉之后再拆：不然瞳孔会被烤进头那一层，运行时又叠一层眼珠，
    # 鼠标一动就是"四只眼"。
    rig: Optional[Rig] = None
    if not args.no_rig:
        rig = Rig(frames_base)
        print(
            "骨架：头绕脖子转（脖子 "
            f"{tuple(int(round(v)) for v in rig.neck)}）、手臂绕肩转（肩 "
            f"{tuple(int(round(v)) for v in rig.shoulder)}）"
            "—— 点头 / 摇头 / 挥手这些动作是真的在动关节"
        )

    # 每帧的变换矩阵：眼珠跟**头**走、食指跟**躯干**走，所以是两张表
    eye_warps, hand_warps = motion_tables(count, base.width, rig)

    if eyes:
        layer = build_eye_layer(base, eyes)
        irises = eye_patches(base, eyes)
        travel = max(0.0, float(args.eye_travel)) * (sum(irises) / float(len(irises)))
        write_eye_assets(out_dir, base.width, layer, eyes, travel, eye_warps)
        if args.eye_preview:
            write_eye_preview(base, layer, blanked_eyes, eyes, Path(args.eye_preview), irises)
        where = "  ".join(f"({cx:.0f},{cy:.0f}) r={r:.1f}" for cx, cy, r in eyes)
        print(f"眼睛：{where}；会跟着鼠标动的**眼珠**半径 " +
              " / ".join(f"{value:.1f}" for value in irises) + "（眼线、眼睑留在底图不动）")
        print(f"      眼珠最多挪 ±{travel:.1f} 像素（画布上），会跟着鼠标看")
    elif clear_part_assets(out_dir, (EYE_LAYER_FILE, EYE_META_FILE)):
        print("已清掉上一版的眼珠层（这个形象没有眼睛）。")

    if hand is not None:
        layer = build_hand_layer(base, hand)
        lift = max(0.0, float(args.tap_lift))
        period = max(0.4, float(args.tap_period))
        write_hand_assets(out_dir, base.width, layer, hand, lift, period, hand_warps)
        if args.hand_preview:
            write_hand_preview(base, layer, blanked_hand, hand, Path(args.hand_preview))
        print(f"手：食指那一段 {tuple(int(v) for v in hand)}，每 {period:.1f} 秒轻轻敲两下下巴")
    elif clear_part_assets(out_dir, (HAND_LAYER_FILE, HAND_META_FILE)):
        print("已清掉上一版的食指层（这个形象没有手）。")

    total = 0
    for name, spec in MOTIONS.items():
        total += len(
            write_frames(
                out_dir,
                name,
                build_frames(frames_base, spec, count=count, size=frames_base.width, rig=rig),
            )
        )
    print(f"写好 {total} 帧（{len(MOTIONS)} 套动作 × {count} 帧）→ {rel(out_dir)}")

    # 串门时两只一起玩的那几个动作（见 pet/play.py）：动作名就是帧名前缀，
    # 加载规则和 idle_* / talk_* 一模一样（pet/sprite.py 的 frames_acts）。
    act_specs = play_mod.motions()
    act_total = 0
    for name, spec in act_specs.items():
        act_total += len(
            write_frames(
                out_dir,
                name,
                build_frames(frames_base, spec, count=count, size=frames_base.width, rig=rig),
            )
        )
    if act_total:
        print(f"外加串门动作 {act_total} 帧（{len(act_specs)} 个动作 × {count} 帧）——"
              "两只一起玩时按动作进度播")

    # 日常状态 / 界面互动（见 pet/states.py）：走路 / 坐下 / 睡觉 / 挥手…
    # 帧名同样是状态名，渲染器跟串门动作共用一张表（pet/sprite.py 的 frames_acts）。
    pose_specs = states_mod.motions()
    pose_total = 0
    for name, spec in pose_specs.items():
        pose_total += len(
            write_frames(
                out_dir,
                name,
                build_frames(frames_base, spec, count=count, size=frames_base.width, rig=rig),
            )
        )
    if pose_total:
        print(f"再加日常状态 {pose_total} 帧（{len(pose_specs)} 条 × {count} 帧）——"
              "走路 / 坐下 / 睡觉那种一直循环，挥手 / 点头那种播一遍就停")
    print("重启挂件后生效（形象是启动时加载的）。想反悔：python tools/make_pet.py --clear")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


