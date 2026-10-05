"""所见即所得的形象设计器：左边调，右边就是你。

    python tools/design_pet.py             # 从 config.json 里的 ui.pet_style 开始
    python tools/design_pet.py mint        # 从「薄荷」这个预设开始
    python tools/design_pet.py --no-config # 纯试玩：不读也不写 config.json

为什么要有它：内置的矢量形象以前是写死的（颜色是 pet/sprite.py 里的常量、机身固定是那坨
史莱姆）。想换样子只有两条路——手画 396 张帧，或者拿照片贴上去。现在「长相」抽成了
pet/style.py 里的一份数据（颜色 / 身形 / 五官 / 触角 / 腮红…），可这堆十六进制颜色
写在 config.json 里谁知道会长成什么样。所以这里把这些数据摆成能拧的旋钮，改一下右边就变。

三件事，各有一条路：

1. **边调边看**（右边那几格）：调什么都没关系，改完立刻重画。窗口里还站着一排情绪样张
   （开心 / 无语 / 激动 / 好奇），因为「设计器挑的眼睛和嘴只替换待机那一格」——除非把
   「情绪也用这套表情」勾上，那时候喜怒哀乐全照你挑的画。
2. **让真正的桌宠换上**：点「保存到配置」写进 `ui.pet_style`。在挂件里点「设计我的形象…」
   打开的这一份会**当场**给桌宠换装（见 pet/app.py 的 open_designer），不用重启。
   如果你是从命令行单独开的这个窗口，就只能改文件，重启挂件才生效。
3. **要图片帧就走「导出源图」**：存一张 768×768 的白底 PNG 到 `assets/source/pet_design.png`，
   再 `python tools/make_pet.py assets/source/pet_design.png` 就得到整套动作帧
   （抠背景那一步认白底；这里故意关掉地面阴影——那团灰会被当成背景的一部分吃掉）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pet import make_console_safe  # noqa: E402
from pet import mood as mood_mod  # noqa: E402
from pet import style as style_mod  # noqa: E402
from pet.config import ASSETS_DIR, Config  # noqa: E402
from pet.sprite import PetRenderer  # noqa: E402

#: 预览格用多大的画布（方的那一格）；情绪样张小一点，一排摆得下
LIVE_SIZE = 208
THUMB_SIZE = 96
#: 交给 make_pet 的源图：大一点，抠出来的边更细（缩到 256 的帧上，边上的毛刺就看不出了）
EXPORT_SIZE = 768
#: 预览格的底色：跟挂件所在的深色桌面不是一回事，但看颜色搭不搭够用了
BACKDROP = "#191E2B"
#: 情绪样张挑这几张摆：正好覆盖四类表情（弯月眼 / 一条线 / 大眼 / 瞟一眼）
SAMPLE_MOODS = ("happy", "speechless", "excited", "curious")


def installed_frames() -> bool:
    """assets/pet 里装没装图片帧（装了的话，"保存到配置"不一定马上看得出来）。"""
    folder = ASSETS_DIR / "pet"
    return bool(folder.is_dir() and next(folder.glob("idle_*.png"), None))


class PetPreview(QWidget):
    """一格预览：`mood` 决定表情，`live=True` 的那一格会动（其余停在原地好对比）。"""

    def __init__(
        self,
        renderer: PetRenderer,
        mood: str = "",
        size: int = THUMB_SIZE,
        live: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.renderer = renderer
        self.mood = mood
        self.live = live
        self.t = 0.35
        self.setFixedSize(size, size)
        self.setToolTip("待机（会动）" if live else mood_mod.label(mood))

    def tick(self, t: float) -> None:
        """整点推进动画；不动的格子连重画都不用。"""
        if not self.live:
            return
        self.t = t
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = QRectF(0, 0, self.width(), self.height())
        painter.fillRect(rect, QColor(BACKDROP))
        # talking 一律 False：说话那套快得多，预览里晃得看不清颜色
        self.renderer.draw(painter, rect, self.t, talking=False, mood=self.mood)
        painter.end()


class PetDesigner(QWidget):
    """设计器窗口。可以直接 show()，也能被挂件从菜单里叫出来（见 pet/app.py 的 open_designer）。"""

    styleChanged = Signal(object)   # 每次改动：让挂件当场换装
    saved = Signal(object)          # 存进 config.json 之后

    def __init__(
        self,
        style: Optional[style_mod.PetStyle] = None,
        cfg: Optional[Config] = None,
        frames_installed: bool = False,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.cfg = cfg
        self.frames_installed = bool(frames_installed)
        self.style = style or style_mod.PetStyle()
        self._colors: Dict[str, str] = {}
        self._color_buttons: Dict[str, QPushButton] = {}
        self._spins: Dict[str, QDoubleSpinBox] = {}
        self._flags: Dict[str, QCheckBox] = {}
        self._loading = True          # 铺控件的时候别触发 _push（那会来回打架）
        self._t = 0.35

        self.setWindowTitle("设计我的形象")
        self.renderer = PetRenderer(ASSETS_DIR / "pet", prefer_vector=True)
        self.renderer.set_style(self.style)

        self._build_ui()
        self._sync(self.style)
        self._loading = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)
        self._tick()

    # ---------- 搭界面 ----------

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.addWidget(self._build_controls(), 0)
        outer.addWidget(self._build_previews(), 1)
        self.resize(900, 660)

    def _build_controls(self) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)

        # 「配色」：挑预设最快，想自己调就六个色块一个一个点
        preset_group = QGroupBox("配色")
        preset_row = QVBoxLayout(preset_group)
        self._preset = QComboBox()
        for name in style_mod.PRESET_ORDER:
            self._preset.addItem(str(style_mod.PRESETS[name].get("label") or name), name)
        self._preset.activated.connect(lambda *_: self._load_preset(self._preset.currentData()))
        preset_row.addWidget(self._preset)
        grid = QGridLayout()
        swatches = (
            ("body_top", "身子（上）"),
            ("body_bottom", "身子（下）"),
            ("outline", "描边"),
            ("eye", "眼珠"),
            ("blush", "腮红"),
            ("accent", "触角光"),
        )
        for index, (field, text) in enumerate(swatches):
            self._colors[field] = "#FFFFFF"
            button = QPushButton()
            button.clicked.connect(lambda *_, name=field: self._pick_color(name))
            self._color_buttons[field] = button
            grid.addWidget(QLabel(text), index // 3, (index % 3) * 2)
            grid.addWidget(button, index // 3, (index % 3) * 2 + 1)
        preset_row.addLayout(grid)
        column.addWidget(preset_group)

        # 「身形和五官」：身形是四种画法，眼睛/嘴巴挑「待机时」的样子
        shape_group = QGroupBox("身形和五官")
        form = QGridLayout(shape_group)
        self._shape = self._choice(style_mod.BODY_SHAPES, style_mod.BODY_SHAPE_LABELS)
        self._eyes = self._choice(style_mod.EYE_KINDS, style_mod.EYE_LABELS)
        self._mouth = self._choice(style_mod.MOUTH_KINDS, style_mod.MOUTH_LABELS)
        form.addWidget(QLabel("身形"), 0, 0)
        form.addWidget(self._shape, 0, 1)
        form.addWidget(QLabel("眼睛"), 1, 0)
        form.addWidget(self._eyes, 1, 1)
        form.addWidget(QLabel("嘴巴"), 2, 0)
        form.addWidget(self._mouth, 2, 1)
        form.addWidget(QLabel("身宽"), 3, 0)
        form.addWidget(self._number("body_width", style_mod.BODY_W_MIN, style_mod.BODY_W_MAX), 3, 1)
        form.addWidget(QLabel("身高"), 4, 0)
        form.addWidget(self._number("body_height", style_mod.BODY_H_MIN, style_mod.BODY_H_MAX), 4, 1)
        column.addWidget(shape_group)

        # 「小细节」：这几个开关最影响"像不像原来那只"
        detail_group = QGroupBox("小细节")
        details = QVBoxLayout(detail_group)
        for field, text in (
            ("blush_on", "腮红"),
            ("antenna", "头顶的触角"),
            ("highlight", "身上那点高光"),
            ("shadow", "脚下的影子（导出源图时会自动关掉）"),
            ("face_locked", "情绪也用这套表情（不然只有待机照它画）"),
        ):
            check = QCheckBox(text)
            check.toggled.connect(lambda *_: self._push())
            self._flags[field] = check
            details.addWidget(check)
        self._prefer_vector = QCheckBox("装了图片帧也先用这套（重启生效）")
        self._prefer_vector.setToolTip(
            "写到配置的 ui.prefer_vector：assets/pet 里就算有帧，也先画设计器这套"
        )
        # 从配置文件里读初值：不然用户明明开着它，一保存反倒被这边没勾的框写回 false
        self._prefer_vector.setChecked(bool(getattr(self.cfg.ui, "prefer_vector", False)) if self.cfg else False)
        details.addWidget(self._prefer_vector)
        column.addWidget(detail_group)

        buttons = QHBoxLayout()
        random_button = QPushButton("随机一套")
        random_button.clicked.connect(lambda *_: self._load(style_mod.random_style()))
        buttons.addWidget(random_button)
        reset_button = QPushButton("用回预设")
        reset_button.clicked.connect(lambda *_: self._load_preset(self.style.preset))
        buttons.addWidget(reset_button)
        column.addLayout(buttons)

        export_button = QPushButton("导出源图（交给 make_pet 生成帧）")
        export_button.clicked.connect(lambda *_: self.export_source())
        column.addWidget(export_button)
        save_button = QPushButton("保存到配置")
        save_button.clicked.connect(lambda *_: self.save_to_config())
        column.addWidget(save_button)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        column.addWidget(close_button)

        self._hint = QLabel("改一下右边就变；满意了点「保存到配置」。")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color:#9FB3D9;")
        column.addWidget(self._hint)
        self._problems = QLabel("")
        self._problems.setWordWrap(True)
        self._problems.setStyleSheet("color:#FFC46B;")
        column.addWidget(self._problems)
        column.addStretch(1)
        return box

    def _choice(self, options: Sequence[str], labels: Dict[str, str]) -> QComboBox:
        """一个下拉框：值还是那个值（"slime"/"round"…），显示的是中文名。"""
        combo = QComboBox()
        for value in options:
            combo.addItem(labels.get(value) or value or "（空）", value)
        combo.activated.connect(lambda *_: self._push())
        return combo

    def _number(self, field: str, low: float, high: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(0.01)
        spin.setDecimals(2)
        spin.valueChanged.connect(lambda *_: self._push())
        self._spins[field] = spin
        return spin

    def _build_previews(self) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)

        self._live = PetPreview(self.renderer, mood="", size=LIVE_SIZE, live=True)
        self._views: List[PetPreview] = [self._live]
        column.addWidget(self._live, 0, Qt.AlignmentFlag.AlignHCenter)

        strip = QHBoxLayout()
        for name in SAMPLE_MOODS:
            cell = QVBoxLayout()
            view = PetPreview(self.renderer, mood=name, size=THUMB_SIZE)
            self._views.append(view)
            cell.addWidget(view, 0, Qt.AlignmentFlag.AlignHCenter)
            caption = QLabel(mood_mod.label(name))
            caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            caption.setStyleSheet("color:#9FB3D9; font-size:11px;")
            cell.addWidget(caption)
            strip.addLayout(cell)
        column.addLayout(strip)
        column.addStretch(1)

        # 为什么专门摆这几张：设计器挑的眼睛/嘴巴默认只替换"待机"那一格，
        # 情绪还是 MOOD_STYLE 那套老表情。不摆出来，用户会以为自己没改成功。
        note = QLabel(
            "这一排是情绪样张：挑的眼睛 / 嘴巴默认只管「待机」，\n"
            "想连喜怒哀乐一起换，把左边「情绪也用这套表情」勾上。"
        )
        note.setStyleSheet("color:#7E8BA8; font-size:11px;")
        column.addWidget(note)
        return box

    def _tick(self) -> None:
        """每 33 毫秒把动画往前推一点（跟挂件里那份节奏差不多）。"""
        self._t += 0.11
        for view in self._views:
            view.tick(self._t)

    # ---------- 控件 <-> 长相 ----------

    def _sync(self, style: style_mod.PetStyle) -> None:
        """把一套长相铺到控件上。中间那堆 `setValue` 会一路触发 `_push`，所以先拦住。"""
        was = self._loading
        self._loading = True
        self.style = style
        self._preset.setCurrentIndex(max(0, self._preset.findData(style.preset)))
        self._shape.setCurrentIndex(max(0, self._shape.findData(style.body_shape)))
        self._eyes.setCurrentIndex(max(0, self._eyes.findData(style.eyes)))
        self._mouth.setCurrentIndex(max(0, self._mouth.findData(style.mouth)))
        self._spins["body_width"].setValue(float(style.body_width))
        self._spins["body_height"].setValue(float(style.body_height))
        for field in self._colors:
            self._colors[field] = str(getattr(style, field)).upper()
            self._refresh_swatches(field)
        for field, check in self._flags.items():
            check.setChecked(bool(getattr(style, field)))
        self.renderer.set_style(style)
        self._refresh_problems()
        self._loading = was
        for view in self._views:
            view.update()

    def _push(self) -> None:
        """控件 -> 长相 -> 重画 -> 通知挂件。改哪儿都走这一条路，省得有地方漏了。"""
        if self._loading:
            return
        data: Dict[str, object] = {
            "preset": self._preset.currentData(),
            "body_shape": self._shape.currentData(),
            "eyes": self._eyes.currentData(),
            "mouth": self._mouth.currentData(),
            "body_width": self._spins["body_width"].value(),
            "body_height": self._spins["body_height"].value(),
        }
        data.update(self._colors)
        data.update({field: check.isChecked() for field, check in self._flags.items()})
        self._loading = True
        self.style = style_mod.PetStyle.from_dict(data)
        self._loading = False
        self.renderer.set_style(self.style)
        self._refresh_problems()
        for view in self._views:
            view.update()
        self.styleChanged.emit(self.style)

    def _load(self, style: style_mod.PetStyle) -> None:
        """整份换掉（随机一套 / 换预设 / 用回预设都走这儿）。"""
        self._sync(style)
        self._push()

    def _load_preset(self, name: Optional[str]) -> None:
        self._load(style_mod.PetStyle.from_preset(str(name or style_mod.DEFAULT_PRESET)))

    def _pick_color(self, field: str) -> None:
        chosen = QColorDialog.getColor(QColor(self._colors[field]), self, "挑个颜色")
        if not chosen.isValid():
            return
        self._colors[field] = chosen.name(QColor.NameFormat.HexRgb).upper()
        self._refresh_swatches(field)
        self._push()

    def _refresh_swatches(self, field: str) -> None:
        """色块自己就是这个颜色，字压在中间——不用点开也知道选的是什么。"""
        color = QColor(self._colors[field])
        ink = "#101018" if color.lightness() > 140 else "#FFFFFF"
        button = self._color_buttons[field]
        button.setText(self._colors[field])
        button.setStyleSheet(
            f"background:{self._colors[field]}; color:{ink};"
            "border:1px solid #46506B; border-radius:6px; padding:4px 6px;"
        )

    def _refresh_problems(self) -> None:
        rows = list(self.style.problems)
        self._problems.setText(("配置里这几项没认出来，已经用预设的值顶上：\n" + "\n".join(rows)) if rows else "")

    # ---------- 两条出路：写进配置 / 导出源图 ----------

    def save_to_config(self) -> None:
        """写进 config.json 的 `ui.pet_style`（顺手把 prefer_vector 一起写）。"""
        cfg = self.cfg or Config.load()
        cfg.ui.pet_style = self.style.to_config_dict()
        cfg.ui.prefer_vector = self._prefer_vector.isChecked()
        path = cfg.save()
        if not path:
            self._hint.setText("配置写不进去（看看 config.json 是不是被别的程序占着），长相只在这次运行里有效。")
            return
        self.cfg = cfg
        note = "已经记在你的配置文件里了"
        if self.frames_installed and not cfg.ui.prefer_vector:
            note += "；不过 assets/pet 里还装着图片帧，桌宠照旧播帧——想让它当场上场，就勾上上面那个「装了图片帧也先用这套」"
        self._hint.setText(f"{note}：{path}")
        print(f"[design] 长相已写进 {path}：{self.style.describe()}")
        self.saved.emit(self.style)

    def export_source(self, out: Optional[Path] = None) -> None:
        """画一张大图存成源图，接着交给 tools/make_pet.py 生成整套帧。

        白底：make_pet 抠背景是从四边往里泛洪，白底最不容易误伤身上的浅色。
        关掉地面阴影（`shadow=False`）——那团半透明灰会被抠图当成背景的一部分吃掉。

        `out` 只有测试会传（存到临时目录里，别动真源图）；界面上走默认路径。
        """
        out = Path(out) if out is not None else ROOT / "assets" / "source" / "pet_design.png"
        source_style = self.style.with_changes(shadow=False)
        renderer = PetRenderer(ASSETS_DIR / "pet", prefer_vector=True)
        renderer.set_style(source_style)
        canvas = QPixmap(EXPORT_SIZE, EXPORT_SIZE)
        canvas.fill(QColor("#FFFFFF"))
        painter = QPainter(canvas)
        renderer.draw(painter, QRectF(0, 0, EXPORT_SIZE, EXPORT_SIZE), 1.15, talking=False)
        painter.end()
        out.parent.mkdir(parents=True, exist_ok=True)
        if not canvas.save(str(out)):
            self._hint.setText(f"这张源图没存下来：{out}")
            return
        command = "python tools/make_pet.py assets/source/pet_design.png"
        self._hint.setText(f"源图存好了：{out}\n接着跑：{command}")
        print(f"[design] 源图已存到 {out}（{EXPORT_SIZE}×{EXPORT_SIZE}，白底、没画地面阴影）")
        print(f"[design] 接着跑：{command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="设计桌宠长什么样：左边调，右边就是你（改完点「保存到配置」）"
    )
    parser.add_argument("preset", nargs="?", default="", help="从哪个预设开始（默认读配置里的 ui.pet_style）")
    parser.add_argument("--no-config", action="store_true", help="纯试玩：不读也不写 config.json")
    args = parser.parse_args(argv)

    make_console_safe()
    app = QApplication(sys.argv[:1])
    cfg = None if args.no_config else Config.load()
    if args.preset:
        start = style_mod.PetStyle.from_preset(args.preset)
    elif cfg is not None:
        start = style_mod.PetStyle.from_config(cfg.ui)
    else:
        start = style_mod.PetStyle.from_preset()
    designer = PetDesigner(style=start, cfg=cfg, frames_installed=installed_frames())
    designer.show()
    print(f"[design] 当前长相：{designer.style.describe()}")
    if cfg is None:
        print("[design] --no-config：这次改的不写进 config.json")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
