"""形象/气泡预览图：不开窗口，直接把渲染结果存成 PNG。

    python tools/render_preview.py preview.png

输出一张深色背景的对比图：几套表情 + 一个吐槽气泡，**图上不写一个字**。
情绪不上界面——不写「开心/无语」这种标签、不画小胶囊，也不写「气泡示例」这类图注；
情绪只体现在表情和气泡边框色上，所以这张预览图就是最终看到的样子。
如果你放了自己的 assets/pet/*.png 帧（包括 happy_0.png 这类情绪帧），预览的就是你自己的形象。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPoint, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from pet import make_console_safe  # noqa: E402
from pet.config import ASSETS_DIR, Config  # noqa: E402
from pet.sprite import MOOD_STYLE, PetRenderer  # noqa: E402
from pet.window import BubbleWindow  # noqa: E402

CELL = 190
BG = QColor("#1B2030")


def main() -> int:
    make_console_safe()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("preview.png")
    if not out.is_absolute():
        out = Path(__file__).resolve().parent.parent / out

    app = QApplication(sys.argv[:1])
    cfg = Config()
    renderer = PetRenderer(ASSETS_DIR / "pet")

    columns = ["idle", *MOOD_STYLE.keys()]
    bubble_text = "这操作我真没看懂，你确定这是王者不是抽奖？"
    bubble_mood = "speechless"
    bubble = BubbleWindow(cfg)
    bubble.prepare(bubble_text, bubble_mood)
    bubble_w = max(280, bubble.width())

    width = max(CELL * len(columns) + 40, bubble_w + 40)
    height = CELL + bubble.height() + 40
    canvas = QPixmap(width, height)
    canvas.fill(BG)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    for index, name in enumerate(columns):
        x = 20 + index * CELL
        talking = name in ("happy", "excited")
        renderer.draw(
            painter,
            QRectF(x, 12, CELL - 20, CELL - 24),
            1.6,
            talking=talking,
            thinking=False,
            blink=False,
            mood="" if name == "idle" else name,
        )

    bubble.render(painter, QPoint(20, CELL + 4))
    painter.end()

    canvas.save(str(out))
    print(f"已生成 {out}（{width}x{height}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
