"""把一张人脸照片贴到桌宠形象的脸上，做出「你的脸 + 黄豆的身体」。

用法：
    python tools/paste_face.py assets/source/my_face.jpg        # 生成 assets/source/pet_src_face.png
    python tools/paste_face.py 我的照片.png --install           # 顺便生成整套帧装进 assets/pet
    python tools/paste_face.py 我的照片.png --preview 对比.png  # 存一张放大对比图，看看贴得正不正
    python tools/paste_face.py 我的照片.png --face-box 80,40,240,300   # 手动框出照片里脸的位置
    python tools/paste_face.py 我的照片.png --zoom 1.05 --shift 0,-0.06  # 脸再大一点、往上挪一点

做法（不用人脸识别，靠颜色 + 几何就能贴准）：
    1. 在底图（默认 assets/source/pet_cut.png，就是黄豆本人）里找那个黄色脑袋——
       挑出黄色像素、取最大的连通块，得到圆心与半径；
    2. 从照片里裁一块正方形（默认取中间 2/3，脸的中心稍微偏下一点），裁成圆形并羽化一圈边；
    3. 把圆形人脸缩到脑袋里（默认占脑袋直径的 96%，外面留一圈黄边），贴上去；
    4. 存成新的源图，直接喂给 tools/make_pet.py 就能生成会动的帧。

底图默认先放大 2 倍再贴（--scale）：贴完再交给 make_pet 缩回 256，
人脸是「照片直接缩到目标尺寸」，不会被放大糊掉。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = ROOT / "assets" / "source" / "pet_cut.png"
DEFAULT_OUT = ROOT / "assets" / "source" / "pet_src_face.png"

# 默认裁框：按「人像自拍」调的——脸占照片短边的 2/3，中心稍微偏下一点
# （自拍里额头常常顶到画面上边，脸的实际中心在中间偏下）。别的构图就用
# --crop-ratio / --center 或直接 --face-box 框一下。
DEFAULT_CROP_RATIO = 0.67
DEFAULT_CENTER = (0.485, 0.574)

# 黄色脑袋的判定：红高、绿中高、蓝低，且红蓝差距明显（黄豆就是这个色）
YELLOW = dict(r_min=180, g_min=140, b_max=140, rb_gap=60, alpha_min=40)


def find_head(image: Image.Image) -> Tuple[float, float, float]:
    """找出黄色脑袋：返回 (圆心 x, 圆心 y, 半径)。找不到就抛 ValueError。"""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    mask = bytearray(width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if (
                a > YELLOW["alpha_min"]
                and r > YELLOW["r_min"]
                and g > YELLOW["g_min"]
                and b < YELLOW["b_max"]
                and (r - b) > YELLOW["rb_gap"]
            ):
                mask[row + x] = 1

    seen = bytearray(width * height)
    best: List[int] = []
    for start in range(width * height):
        if not mask[start] or seen[start]:
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
        if len(blob) > len(best):
            best = blob

    if not best:
        raise ValueError("底图里没找到黄色脑袋，用 --head / --head-radius 手动指定吧")
    xs = [index % width for index in best]
    ys = [index // width for index in best]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    radius = max(max(xs) - min(xs), max(ys) - min(ys)) / 2.0
    return cx, cy, radius


def parse_pair(text: str) -> Tuple[float, float]:
    parts = [part for part in text.replace("，", ",").split(",") if part.strip() != ""]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("要写成 1,2 这样")
    return float(parts[0]), float(parts[1])


def parse_box(text: str) -> Tuple[int, int, int, int]:
    parts = [part for part in text.replace("，", ",").split(",") if part.strip() != ""]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--face-box 要写成 x,y,宽,高（原图像素）")
    values = [int(round(float(part))) for part in parts]
    return values[0], values[1], values[2], values[3]


def rel(path: Path) -> str:
    """能显示成相对项目根的路径就显示相对路径，不然就原样（绝对路径也要能用）。"""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def crop_square(
    photo: Image.Image,
    box: Optional[Tuple[int, int, int, int]],
    ratio: float,
    center: Tuple[float, float],
) -> Image.Image:
    """从照片里裁一块正方形：给了 --face-box 就用它，否则按比例取中间一块。"""
    image = photo.convert("RGB")
    if box:
        x, y, w, h = box
        side = max(8, min(w, h))
        cx = x + w / 2.0
        cy = y + h / 2.0
    else:
        side = max(8, int(round(min(image.width, image.height) * ratio)))
        cx = image.width * center[0]
        cy = image.height * center[1]
    left = int(round(cx - side / 2.0))
    top = int(round(cy - side / 2.0))
    return image.crop((left, top, left + side, top + side))


def circle_mask(size: int, feather: float = 0.02, supersample: int = 4) -> Image.Image:
    """圆形遮罩：先在 4 倍画布上画圆再缩回来，边缘天然抗锯齿；再轻轻模糊一圈。"""
    big = size * supersample
    mask = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(mask)
    pad = max(1, big // 128)
    draw.ellipse((pad, pad, big - 1 - pad, big - 1 - pad), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    if feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(max(0.5, size * feather)))
    return mask


def paste_face(
    base: Image.Image,
    photo: Image.Image,
    head: Tuple[float, float, float],
    box: Optional[Tuple[int, int, int, int]],
    ratio: float,
    center: Tuple[float, float],
    zoom: float,
    shift: Tuple[float, float],
    feather: float,
) -> Image.Image:
    """把圆形人脸贴进脑袋圆里（默认留一圈黄边，看起来像「黄豆长了你的脸」）。"""
    cx, cy, radius = head
    face_size = max(8, int(round(radius * 2.0 * zoom)))
    square = crop_square(photo, box, ratio, center)
    face = square.resize((face_size, face_size), Image.LANCZOS).convert("RGBA")
    face.putalpha(circle_mask(face_size, feather))

    canvas = base.convert("RGBA").copy()
    left = int(round(cx - face_size / 2.0 + shift[0] * radius))
    top = int(round(cy - face_size / 2.0 + shift[1] * radius))
    canvas.alpha_composite(face, (left, top))
    return canvas


def write_preview(
    before: Image.Image,
    after: Image.Image,
    head: Tuple[float, float, float],
    out: Path,
    zoom: int = 3,
) -> None:
    """存一张放大对比图：左边原脑袋、右边贴好的脑袋。"""
    cx, cy, radius = head
    side = int(radius * 2.4)
    box = (int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2))
    tiles = [before.convert("RGBA").crop(box), after.convert("RGBA").crop(box)]
    sizes = [(tile.width * zoom, tile.height * zoom) for tile in tiles]
    width = sum(size[0] for size in sizes) + 8 * (len(sizes) + 2)
    height = max(size[1] for size in sizes) + 16
    canvas = Image.new("RGBA", (width, height), (24, 28, 38, 255))
    x = 16
    for tile, size in zip(tiles, sizes):
        canvas.alpha_composite(tile.resize(size, Image.NEAREST), (x, 8))
        x += size[0] + 16
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print(f"对比图（左：原脑袋　右：贴好的人脸，放大 {zoom} 倍）→ {out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把一张人脸照片贴到桌宠形象的脸上",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("face", nargs="?", default=str(ROOT / "assets" / "source" / "my_face.jpg"),
                        help="人脸照片（默认 assets/source/my_face.jpg）")
    parser.add_argument("--base", default=str(DEFAULT_BASE), help="底图，默认 assets/source/pet_cut.png（黄豆抠好的图）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出的源图，默认 assets/source/pet_src_face.png")
    parser.add_argument("--face-box", type=parse_box, default=None, help="照片里脸的框：x,y,宽,高（原图像素）")
    parser.add_argument("--crop-ratio", type=float, default=DEFAULT_CROP_RATIO, help="没给 --face-box 时，裁正方形占照片短边的比例")
    parser.add_argument("--center", type=parse_pair, default=DEFAULT_CENTER, help="裁正方形的中心（占照片宽高的比例）")
    parser.add_argument("--head", type=parse_pair, default=None, help="手动指定脑袋圆心（占底图宽高的比例）")
    parser.add_argument("--head-radius", type=float, default=None, help="手动指定脑袋半径（占底图短边的比例）")
    parser.add_argument("--zoom", type=float, default=0.96, help="人脸占脑袋直径的比例，1.0 就是贴满")
    parser.add_argument("--shift", type=parse_pair, default=(0.0, 0.0), help="人脸相对脑袋圆心的偏移（按半径算）")
    parser.add_argument("--feather", type=float, default=0.02, help="圆边羽化，按半径的比例")
    parser.add_argument("--scale", type=float, default=2.0, help="底图先放大几倍再贴（贴完交给 make_pet 缩回 256）")
    parser.add_argument("--preview", default=None, help="存一张放大对比图，看看贴得正不正")
    parser.add_argument("--install", action="store_true", help="顺手跑 tools/make_pet.py，把帧装进 assets/pet")
    parser.add_argument("--frames", type=int, default=12, help="--install 时每套动作生成几帧")
    return parser


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    _safe_console()
    args = build_parser().parse_args(argv)

    face_path = Path(args.face)
    if not face_path.exists():
        print(f"找不到照片：{face_path}")
        print("把照片存到这个路径再跑一次，或者直接把路径传给命令：")
        print("  python tools/paste_face.py 你的照片.jpg --install")
        return 1
    base_path = Path(args.base)
    if not base_path.exists():
        print(f"找不到底图：{base_path}")
        print("底图默认是 assets/source/pet_cut.png（黄豆抠好的图）；用 --base 指一张也行。")
        return 1

    base = Image.open(base_path).convert("RGBA")
    photo = Image.open(face_path)
    print(f"底图：{rel(base_path)}（{base.width}x{base.height}）")
    print(f"照片：{rel(face_path)}（{photo.width}x{photo.height}，{photo.mode}）")

    scale = max(1.0, float(args.scale))
    if scale > 1.01:
        base = base.resize((int(base.width * scale), int(base.height * scale)), Image.LANCZOS)

    try:
        cx, cy, radius = find_head(base)
    except ValueError as exc:
        print(f"⚠ {exc}")
        return 1
    if args.head:
        cx, cy = base.width * args.head[0], base.height * args.head[1]
    if args.head_radius:
        radius = min(base.width, base.height) * args.head_radius
    print(f"脑袋：圆心 ({cx:.1f}, {cy:.1f})，半径 {radius:.1f}（{base.width}x{base.height} 的画布上）")

    before = base.copy()
    composed = paste_face(
        base, photo, (cx, cy, radius), args.face_box, args.crop_ratio, args.center,
        args.zoom, args.shift, max(0.0, args.feather),
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    composed.save(out_path)
    print(f"写好源图：{rel(out_path)}（{composed.width}x{composed.height}）")

    if args.preview:
        write_preview(before, composed, (cx, cy, radius), Path(args.preview))

    if not args.install:
        print("下一步二选一：")
        print(f"  先看效果：python tools/paste_face.py {args.face} --preview 对比.png")
        print(f"  直接装进挂件：python tools/paste_face.py {args.face} --install")
        return 0

    import subprocess
    import sys

    command = [
        sys.executable, str(ROOT / "tools" / "make_pet.py"), str(out_path),
        "--frames", str(max(2, int(args.frames))),
    ]
    print("跑 make_pet 生成动效帧：python tools/make_pet.py " + " ".join(command[2:]))
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        print("make_pet 没跑成功；源图已经存好了，可以手动再跑一次。")
        return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
