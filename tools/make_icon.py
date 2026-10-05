"""生成程序图标：`assets/app.ico`（exe、安装包、开始菜单都用它）。

    python tools/make_icon.py            写 assets/app.ico
    python tools/make_icon.py out.ico    写到别处

形象是**画出来的**（见 pet/sprite.py），项目里没有现成图片，所以这里让它按几个尺寸
各画一遍，再交给 Pillow 打包成一个多尺寸 .ico：16/24 是任务栏和小图标，32/48 是桌面
和资源管理器，128/256 是安装包和高分屏。

托盘图标**不用**这个文件——托盘那个是运行时现画的（PetRenderer.icon()），
换形象不用重新生成 ico；只有换 exe 图标时才需要重跑这个脚本。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 只画图，别闪窗口

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet.config import ASSETS_DIR  # noqa: E402
from pet.sprite import PetRenderer  # noqa: E402

#: .ico 里要装的尺寸：Windows 从 16（任务栏）一直用到 256（大图标 / 安装包）
SIZES = (16, 24, 32, 48, 64, 128, 256)


def main(argv=None) -> int:
    args = [str(arg) for arg in (sys.argv[1:] if argv is None else argv)]
    dest = Path(args[0]) if args else (ASSETS_DIR / "app.ico")
    if not dest.is_absolute():
        dest = Path(__file__).resolve().parent.parent / dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    QApplication(sys.argv[:1])
    renderer = PetRenderer(ASSETS_DIR / "pet", crossfade=False)
    if not renderer.uses_frames:
        print(f"[icon] 注意：{(ASSETS_DIR / 'pet')} 里没有帧，用的是内置画法")

    with tempfile.TemporaryDirectory() as tmp:
        base = None
        for size in SIZES:
            path = Path(tmp) / f"icon{size}.png"
            renderer.icon(size).save(str(path), "PNG")
            image = Image.open(path).convert("RGBA")
            if size == max(SIZES):
                base = image
            print(f"[icon] 画好 {size}x{size}")
        if base is None:                      # 只可能发生在 SIZES 被改空的时候
            print("[icon] 没画出任何尺寸，检查 SIZES")
            return 1
        base.save(dest, format="ICO", sizes=[(size, size) for size in SIZES])

    with Image.open(dest) as check:
        got = sorted(check.ico.sizes()) if hasattr(check, "ico") else []
    print(f"[icon] 写好 {dest}（{dest.stat().st_size / 1024:.1f} KB，装了 {len(got)} 个尺寸：{got}）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
