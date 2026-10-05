"""冒烟测试：把每个模块都真跑一遍，改完代码可以快速自查。

    $env:QT_QPA_PLATFORM="offscreen"; python tools/smoke_test.py

offscreen 模式下不会真的弹窗口，但截屏、OCR、热键（会合成一次按键）、模型调用都是真的。
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试一律跑在一个临时"家"里（PET_HOME）：几个测试会临时构造没有来源文件的 `Config()`，
# 它算出来的 memory.json / data/ 默认落在"家"里——不设这一条就落在**项目根**，
# 真机上踩过：跑一趟冒烟，项目的 data/memory/archive.jsonl 里多出一份回填、
# memory.json 也可能被测试写脏。设了它，`paths.user_dir()` 指向临时目录，
# 项目里的真配置 / 真记忆全程不碰（专门验路径规则的那条测试会自己把环境恢复回来）。
SMOKE_HOME = tempfile.mkdtemp(prefix="pet-smoke-")
os.environ["PET_HOME"] = SMOKE_HOME

from PIL import Image  # noqa: E402
from PySide6.QtCore import QEvent, QEventLoop, QObject, QPoint, QPointF, QRect, QRectF, QTimer, Qt, Slot  # noqa: E402
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPixmap, QRegion  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidgetAction  # noqa: E402

from pet import capture, care, corpus, dialog, doing, episode, foreground, friends, humanstyle, keyinfo, make_console_safe, memarchive, mood, net, paths, persona, play, proactive, progress, scene, states, taste, watchlog, webstudy, wincap, winfind, zh  # noqa: E402
from pet.asr import SpeechReader  # noqa: E402
from pet.chatpanel import ChatPanel  # noqa: E402
from pet.config import ASSETS_DIR, Config, apply_override  # noqa: E402
from pet.friendpanel import FriendPanel  # noqa: E402
from pet.friends import Guest  # noqa: E402
from pet.guest import GuestWindow  # noqa: E402
from pet.hotkey import HotkeyListener, format_hotkey, parse_hotkey  # noqa: E402
from pet.memory import Episode, Memory, archive_path_for, extract_tags, topic_candidates  # noqa: E402
from pet.ocr import TextReader, clean_lines  # noqa: E402
from pet.overlay import RegionPicker  # noqa: E402
from pet.proactive import ProactivePolicy  # noqa: E402
from pet.sprite import IDENTITY, MOOD_STYLE, PetRenderer, _tap_matrix  # noqa: E402
from pet.vlm import VisionClient  # noqa: E402
from pet.window import PetWindow  # noqa: E402
from pet.worker import AnalysisWorker  # noqa: E402

FAILURES = []
tmpdir = ""


def check(name, func):
    try:
        func()
    except Exception as exc:  # noqa: BLE001
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        where = f"（{Path(frame.filename).name}:{frame.lineno} {frame.line}）"
        FAILURES.append(f"{name}: {exc!r} {where}")
        print(f"[FAIL] {name}: {exc!r} {where}")
        return False
    print(f"[ok]   {name}")
    return True


def _local_ts(hour: int, minute: int = 0) -> float:
    """2026-01-05 当天本地时间某点的 epoch 秒（测 clock_text 用，不受跑测试的时刻影响）。"""
    return time.mktime((2026, 1, 5, int(hour), int(minute), 0, 0, 0, -1))


def _press_hotkey(modifiers: int, vk: int) -> None:
    """合成一次按键（Windows）：按下修饰键 + 主键，再按反序松开。"""
    user32 = ctypes.windll.user32
    vk_control, vk_alt, vk_shift, vk_win = 0x11, 0x12, 0x10, 0x5B
    KEYUP = 0x0002
    down = []
    for flag, code in ((0x0002, vk_control), (0x0001, vk_alt), (0x0004, vk_shift), (0x0008, vk_win)):
        if modifiers & flag:
            down.append(code)
    for code in down:
        user32.keybd_event(code, 0, 0, 0)
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYUP, 0)
    for code in reversed(down):
        user32.keybd_event(code, 0, KEYUP, 0)


class _HotkeyProbe(QObject):
    """收跨线程信号的接收方。

    直接 connect 到 lambda 会被当成"在发信号的线程里直接调用"，
    用一个住在主线程的 QObject 才会走排队（Queued）连接。
    """

    def __init__(self):
        super().__init__()
        self.actions = []

    @Slot(str)
    def on_triggered(self, action: str) -> None:
        self.actions.append(action)


def _send_mouse(widget, kind, pos, button=Qt.MouseButton.LeftButton) -> None:
    """给挂件 / 框选界面塞一个合成鼠标事件（offscreen 下也能走真的 mouseXXX 处理器）。"""
    event = QMouseEvent(
        kind,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        button,
        button,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def _key_event(key: Qt.Key) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def _text_image(text: str, size=(900, 220)):
    """造一张白底黑字的大字图，用来验证 OCR 真的能认字。"""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=72)
    except TypeError:  # 老版本 Pillow 不支持 size 参数
        font = ImageFont.load_default()
    draw.text((30, size[1] // 3), text, fill="black", font=font)
    return image


def main() -> int:
    make_console_safe()
    print(f"平台插件：{os.environ.get('QT_QPA_PLATFORM', '(默认)')}")
    app = QApplication(sys.argv[:1])
    global tmpdir
    tmpdir = tempfile.mkdtemp(prefix="screen-pet-smoke-")

    frame = None

    def test_capture():
        nonlocal frame
        frame = capture.grab(None)
        assert frame.width > 0 and frame.height > 0, "截屏尺寸异常"
        small = capture.shrink(frame, 720)
        assert small.width <= 720
        digest = capture.dhash(small)
        assert 0 <= digest < 2 ** 64
        assert len(capture.to_jpeg_base64(small, 70)) > 100
        assert 0.0 <= capture.brightness(small) <= 1.0
        assert capture.dominant_hue(small) in {"bright", "dark", "green", "warm", "cool", "gray"}
        assert capture.hash_distance(digest, digest) == 0.0
        print(f"      {len(capture.list_monitors())} 块屏幕，画面 {small.width}x{small.height}")

    def test_config():
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            cfg = Config.load(missing)  # 不存在 -> 全默认
            assert cfg.provider == "mock"
            assert cfg.endpoint.endswith("/chat/completions")

            path = Path(tmp) / "cfg.json"
            # 长相（颜色 / 身形 / 五官）：默认整节不写 = 用内置的 classic 预设（见 pet/style.py）
            assert Config().ui.pet_style == {} and Config().ui.prefer_vector is False
            cfg.ui.pet_size = 111
            cfg.ui.pet_style = {"preset": "mint", "eyes": "arc"}
            cfg.ui.prefer_vector = True
            cfg.save(path)
            again = Config.load(path)
            assert again.ui.pet_size == 111
            assert again.capture.interval_sec == cfg.capture.interval_sec
            assert again.ui.pet_style == {"preset": "mint", "eyes": "arc"}, again.ui.pet_style
            assert again.ui.prefer_vector is True

        for name in ("zhipu", "dashscope", "siliconflow", "openai", "ollama", "mock"):
            probe = Config()
            probe.provider = name
            probe.apply_preset()
            assert probe.model, f"{name} 没有默认模型"

        # 临时构造的配置没有来源文件，不给路径就不该落盘（防止手滑覆盖 config.json）
        assert Config().save() is None

        # 「隐身时自己上网学」不再摆开关（右键菜单里那项已经撤了）：默认就得是开着
        assert Config().study.enabled is True, "隐身学习应该默认开着（不摆开关）"

    def test_cli_override():
        """命令行临时参数不能被写回配置文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            base = Config()
            base.ui.vertical_ratio = 0.42
            base.save(path)

            cfg = Config.load(path)
            apply_override(cfg, "ui.vertical_ratio", 0.9)
            apply_override(cfg, "capture.interval_sec", 0.5)
            apply_override(cfg, "hotkey.region", "ctrl+alt+f9")
            assert cfg.ui.vertical_ratio == 0.9
            assert cfg.capture.interval_sec == 0.5
            assert cfg.hotkey.region == "ctrl+alt+f9"

            cfg.save()
            saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["ui"]["vertical_ratio"] == 0.42, saved["ui"]
        assert saved["capture"]["interval_sec"] == base.capture.interval_sec, saved["capture"]
        assert saved["hotkey"]["region"] == base.hotkey.region, saved["hotkey"]

    def test_pet_style():
        """长相（颜色 / 身形 / 五官）：配置怎么读、坏值退回哪儿、矢量形象真照它画。

        这一条盯的是"用户自己调完，桌宠当场变成那个样"：`ui.pet_style` → `PetStyle`
        → `PetRenderer.set_style` → 画出来的颜色。以前那六个颜色是写死在
        pet/sprite.py 里的常量，现在换成一份数据，所以得验一遍**数据真的落到画面上**。
        """
        from pet import style as style_mod

        # ① 配置里不写 = 出厂那套（跟写死的那一版一模一样，老用户看到的长相一点没变）
        blank = style_mod.PetStyle.from_config(Config().ui)
        assert blank.problems == [], blank.problems
        assert blank.preset == style_mod.DEFAULT_PRESET
        assert (blank.body_top, blank.body_bottom, blank.outline) == ("#CFF3FF", "#5FB8EE", "#2A7FB8")
        assert blank.body_shape == "slime" and blank.body_width == style_mod.BODY_W_DEFAULT

        # ② 只写想改的那几项：没写的从预设取；整节只写一个预设名也认
        tweaked = style_mod.PetStyle.from_dict({"preset": "mint", "eyes": "arc", "body_height": 0.8})
        assert tweaked.problems == [], tweaked.problems
        assert (tweaked.body_top, tweaked.body_bottom) == ("#DFF7EA", "#68CFA3"), "换了预设，颜色没跟着走"
        assert tweaked.eyes == "arc" and tweaked.body_height == 0.8
        assert tweaked.body_shape == "slime", "预设里没写的项该回到出厂值（不是 KeyError）"

        class _Ui:                      # 只写一个预设名的那种配置长这样
            pet_style = "night"

        named = style_mod.PetStyle.from_config(_Ui())
        assert named.problems == [], named.problems
        assert (named.body_shape, named.body_top) == ("drop", "#3B4A6B")

        # ③ 坏值一律退回预设，并留一句话（不许静默——不然用户以为改的那行没生效）
        broken = style_mod.PetStyle.from_dict(
            {"preset": "没有这个预设", "body_top": "纯红", "eyes": "眯眼", "body_width": 99, "多写的一项": 1}
        )
        assert len(broken.problems) == 5, broken.problems
        assert broken.preset == style_mod.DEFAULT_PRESET
        assert broken.body_top == "#CFF3FF" and broken.eyes == ""
        assert broken.body_width == style_mod.BODY_W_MAX, "超出范围的数字该收到边界上"
        assert style_mod.PetStyle.from_dict(None).preset == style_mod.DEFAULT_PRESET

        # ④ 写回配置只写"和预设不一样的那几项"：以后预设本身微调也能跟着走
        assert tweaked.to_config_dict() == {"preset": "mint", "eyes": "arc", "body_height": 0.8}, \
            tweaked.to_config_dict()
        assert style_mod.PetStyle.from_preset("mint").to_config_dict() == {"preset": "mint"}

        # ⑤ 矢量形象真照这份定义画：挑个大红的身子，画完得在图上找得到
        loud = style_mod.PetStyle.from_dict(
            {"body_top": "#FF0000", "body_bottom": "#FF0000", "outline": "#FF0000", "shadow": False}
        )
        renderer = PetRenderer(ASSETS_DIR / "pet", prefer_vector=True)   # 装了图片帧也先画矢量
        assert renderer.uses_frames is False, "prefer_vector 开着，不该去读图片帧"
        renderer.set_style(loud)
        assert renderer.pet_style is loud
        canvas = QPixmap(128, 128)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        renderer.draw(painter, QRectF(0, 0, 128, 128), 0.35, talking=False)
        painter.end()
        image = canvas.toImage()
        reds = sum(
            1
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).red() > 200 and image.pixelColor(x, y).green() < 90
        )
        assert reds > 200, f"换了大红的身子，画出来却没见着红色（{reds} 个像素）"
        print(f"      长相：{loud.describe()}（矢量上色 {reds} 个像素）")

    def test_pet_designer():
        """设计器（tools/design_pet.py）：拧一下旋钮 → 长相跟着变 → 能存进配置 / 能导出源图。

        这是"用户自己调桌宠"那半边：界面上的每个控件都得真的接到长相上（漏一个，
        用户拧完没反应）。存配置写的是**临时目录**里的配置，碰不到用户那份。
        """
        from pet import style as style_mod
        from tools.design_pet import EXPORT_SIZE, PetDesigner, installed_frames

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "designer-cfg.json"
            cfg = Config()
            cfg.save(path)
            designer = PetDesigner(
                style=style_mod.PetStyle.from_config(cfg.ui),
                cfg=Config.load(path),
                frames_installed=installed_frames(),
            )
            designer.show()
            app.processEvents()

            def pick(combo, value):
                """像用户那样挑一项——`activated` 才是"人点了"，setCurrentIndex 不发这个信号。"""
                index = combo.findData(value)
                assert index >= 0, f"{value} 不在下拉框里"
                combo.setCurrentIndex(index)
                combo.activated.emit(index)

            try:
                # ① 身形 / 眼睛 / 嘴 / 开关 / 身宽身高：每个控件都得改到长相上
                pick(designer._shape, "drop")
                pick(designer._eyes, "arc")
                pick(designer._mouth, "grin")
                designer._flags["blush_on"].setChecked(False)
                designer._spins["body_height"].setValue(0.9)
                app.processEvents()
                assert designer.style.body_shape == "drop", "换了身形，长相没跟着变"
                assert designer.style.eyes == "arc" and designer.style.mouth == "grin"
                assert designer.style.blush_on is False and designer.style.body_height == 0.9
                assert designer.style.problems == [], designer.style.problems
                assert designer.renderer.pet_style is designer.style, "预览没跟着换长相"

                # ② 「用回预设」：整份换掉（顺带把预设的颜色也带回来）
                designer._load_preset("peach")
                app.processEvents()
                assert designer.style.preset == "peach" and designer.style.body_top == "#FFE3EC"
                assert designer._color_buttons["body_top"].text() == "#FFE3EC", "色块没刷成预设的颜色"

                # ③ 保存到配置：写的就是 config.json 的 ui.pet_style（只写和预设不一样的项）
                pick(designer._shape, "round")
                designer.save_to_config()
                app.processEvents()
                saved = Config.load(path)
                assert saved.ui.pet_style == {"preset": "peach", "body_shape": "round"}, saved.ui.pet_style
                assert saved.ui.prefer_vector is False

                # ④ 导出源图：就是交给 `make_pet.py` 的那张（存临时目录，别碰真源图）
                source_png = Path(tmp) / "pet_design.png"
                designer.export_source(source_png)
                app.processEvents()
                assert source_png.is_file(), "源图没导出"
                with Image.open(source_png) as image:
                    assert image.size == (EXPORT_SIZE, EXPORT_SIZE), image.size
                    assert image.getpixel((2, 2))[:3] == (255, 255, 255), "角落不是纯白，抠背景会误伤身上的浅色"
                    gray = image.convert("L")
                ink = sum(gray.histogram()[:250])          # 非白像素 = 画上去的东西
                assert ink > 5000, f"这张源图上几乎没画出东西（非白像素 {ink}）"
                # 地面阴影是故意关掉的：脚下那圈得是干净的白。
                # make_pet 抠背景是从四边往里泛洪，留一团半透明灰在那儿会被一起抠掉。
                # （影子开着的话，这一条会一直压到 y≈660，正好落在这段里。）
                foot = gray.crop((0, int(EXPORT_SIZE * 0.85), EXPORT_SIZE, EXPORT_SIZE)).getextrema()
                assert foot == (255, 255), f"脚下那圈不干净（{foot}），导出时是不是把地面阴影也画上了"
            finally:
                designer.close()
                app.processEvents()
        print("      设计器：控件 → 长相 → 配置 → 源图，一路都通")

    # ---------- ② 情绪状态机 ----------

    def test_mood():
        assert mood.split_mood("[无语] 这操作我真看不懂") == mood.Comment("这操作我真看不懂", "speechless")
        assert mood.split_mood("[开心]笑死").mood == "happy"
        assert mood.split_mood("这也太离谱了吧").mood == "speechless"   # 没标签时按关键词兜底
        assert mood.split_mood("为什么啊").mood == "curious"
        assert mood.split_mood("[不认识的标签] 嗯").mood == mood.DEFAULT_MOOD
        assert mood.normalize("happy") == "happy"
        assert mood.label("excited") == "激动"
        assert mood.color("happy").startswith("#")
        assert mood.rate_delta("excited") > 0
        # 新加的三种：惊讶从 excited 里单拎出来（"震惊/吃惊"也算），生气 / 难过 是全新的。
        # 它们各自还会带一个动作，见 pet/states.py 的 MOOD_ACTIONS
        assert mood.split_mood("[惊讶] 不是吧这也行").mood == "surprised"
        assert mood.split_mood("[生气] 这也太过分了").mood == "angry"
        assert mood.split_mood("[难过] 可惜了").mood == "sad"
        assert mood.normalize("震惊") == "surprised"
        assert mood.label("surprised") == "惊讶"
        assert mood.label("angry") == "生气"
        assert mood.label("sad") == "难过"
        for name in mood.mood_names():
            assert mood.emoji(name) and mood.label(name)

    # ---------- ②.5 对话类型（记录 / 分析 / 学习，私下分类，不上界面）----------

    def test_dialog():
        assert dialog.classify("我在看王者荣耀的直播") == dialog.RECORD
        assert dialog.classify("这操作也太离谱了吧") == dialog.ANALYZE
        assert dialog.classify("这个英雄为什么这么强？") == dialog.LEARN
        assert dialog.classify("教我一下这个怎么弄") == dialog.LEARN
        # 字面看不出来时看情绪兜底；空句子一律算「记录」（最稳妥）
        assert dialog.classify("嗯", mood="curious") == dialog.LEARN
        assert dialog.classify("", mood="excited") == dialog.RECORD
        # 判不出来也别乱猜：默认落在「记录」
        assert dialog.classify("那天的风挺大", mood="") == dialog.RECORD

        assert dialog.label(dialog.LEARN) == "学习"
        assert dialog.label("不认识的类型") == "记录"
        for kind in dialog.KINDS:
            assert dialog.hint(kind), f"{kind} 这一类没有接话提醒"
        assert dialog.tally({dialog.RECORD: 3, dialog.ANALYZE: 5}) == "记录 3 / 分析 5 / 学习 0"

        # 分布提醒：样本不够不说话；某一类过半才开口
        assert dialog.mix_hint({dialog.LEARN: 2}) == ""
        assert "答案" in dialog.mix_hint({dialog.LEARN: 5, dialog.RECORD: 1})
        assert dialog.mix_hint({dialog.RECORD: 3, dialog.ANALYZE: 3}) == ""
        print(f"      记录/分析/学习三类：{dialog.tally({dialog.RECORD: 3, dialog.ANALYZE: 5, dialog.LEARN: 2})}")

    # ---------- ① 弹幕/字幕 OCR ----------

    def test_ocr_lines():
        lines = clean_lines(["抖 音 精 选", "、", "ooo", "", "王者荣耀", "王者荣耀", "12", "Enter your key"])
        assert "抖音精选" in lines, lines          # 中文之间被 OCR 拆出来的空格要合并
        assert lines.count("王者荣耀") == 1, lines
        assert "、" not in lines and "ooo" not in lines, lines
        assert "Enter your key" in lines, lines

    def test_ocr_engine():
        cfg = Config()
        cfg.ocr.backend = "winocr"
        reader = TextReader(cfg, Path(tmpdir))
        if not reader.available():
            print("      这台机器没有可用的 OCR 后端，跳过")
            return
        image = _text_image("HELLO 12345")
        result = reader.read(image, 0xABCDEF)
        assert result.ok, result.error
        assert result.lines, "OCR 一张大号白底黑字居然没识别出任何东西"
        joined = " ".join(result.lines).upper().replace(" ", "")
        assert "12345" in joined or "HELLO" in joined, result.lines
        cached = reader.read(image, 0xABCDEF)
        assert cached.lines == result.lines and cached.elapsed == 0.0

    # ---------- ①' 画面关键信息（台标 / 节目名 / 集数 / 话题人名）----------

    def test_keyinfo():
        """就是你圈出来的那一幕：抖音 + 浙江卫视 + 奔跑吧第十四季 + 范丞丞 / 孟子义。"""
        lines = [
            "抖音精选", "搜索", "关注", "我的", "直播", "下一集", "发一条友好的弹幕吧",
            "浙江卫视", "范丞丞", "来了",
            "第164集: #奔跑吧第十四季第十一期（10） #跑男 #范丞丞 #孟子义",
            "3:10 / 12:26",
            "合集：奔跑吧第十四季 | 更新至第187集",
            "京ICP备16016397号-3",
            "2026 © 抖音",
        ]
        info = keyinfo.extract(lines, window="抖音精选")
        assert "浙江卫视" in info.platforms, info.platforms      # 台标（你画红线的那个）
        assert "抖音" in info.platforms, info.platforms
        assert "奔跑吧" in info.shows, info.shows                # 「第 N 季」前面那截就是节目名
        assert "第187集" in info.episode and "3:10 / 12:26" in info.episode, info.episode
        assert "范丞丞" in info.tags and "孟子义" in info.tags, info.tags
        assert info.people() == ["范丞丞", "孟子义"], info.people()
        assert "来了" in info.subtitles, info.subtitles
        # 台标不许又被当成字幕说一遍，界面词/水印也不许混进字幕
        assert "浙江卫视" not in info.subtitles and "搜索" not in info.subtitles, info.subtitles
        block = info.block()
        assert block.startswith("画面关键信息") and "台标/平台：浙江卫视、抖音" in block, block
        print(f"      认出：{info.line()}")

        # 认不出来就必须是**空的**——宁可什么都不加，也不能编
        nothing = keyinfo.extract(["发送", "12", "、", "oooo", "设置", ""], window="Visual Studio Code")
        assert nothing.is_empty() and nothing.block() == "", nothing
        assert keyinfo.extract([], window="").is_empty()
        # 窗口标题里也常常直接写着答案
        assert keyinfo.extract([], window="哔哩哔哩 - 我的世界").platforms == ["哔哩哔哩"]

        # 游戏 / 应用名照表认（ASCII 别名要整词命中，别在英文句子里乱撞）
        game = keyinfo.extract(["FC ONLINE", "佩德里", "巴塞罗那"], window="FC ONLINE")
        assert game.apps == ["FC ONLINE"], game.apps
        assert "佩德里" in game.subtitles, game.subtitles
        assert keyinfo.extract(["正在加载 wow 文件"]).apps == [], "wow 不该在英文句子里命中"
        assert keyinfo.extract(["他在玩 王者荣耀"]).apps == ["王者荣耀"]

        # 代码里的 # 注释不是话题标签（桌宠也会盯着编辑器看）
        code = keyinfo.extract([
            "def system_prompt(cfg):",
            "    x = 1  #TODO",           # 代码注释里带 ASCII：要挡掉
            "#define MAX 8",            # C 宏同理
            "    # harness_done 收尾",    # # 后面是空格，本来就不算标签
        ])
        assert code.tags == [], code.tags

        # 源码 / 文档里的长行不算台标——桌宠也会盯着编辑器看，
        # 屏幕上摆着这份源码时，"浙江卫视"到处都是，不能就当他真在看浙江卫视
        editor = keyinfo.extract([
            '    ("浙江卫视", ("浙江卫视", "浙江台")),',
            'assert "浙江卫视" in info.platforms, info.platforms',
        ], window="keyinfo.py - Visual Studio Code")
        assert editor.platforms == [], editor.platforms
        # 但独立成行的台标照样认（视频里那块 logo 就是这样）
        assert keyinfo.extract(["浙江卫视"]).platforms == ["浙江卫视"]
        assert keyinfo.extract(["浙江卫视《奔跑吧》第十四季"]).platforms == ["浙江卫视"]

    def test_keyinfo_prompt():
        """本地认出来的关键信息要真进提示词：摆在原始 OCR 前面，还得叫模型优先信它。"""
        key = keyinfo.extract(["浙江卫视", "#奔跑吧第十四季"], window="抖音")
        cfg = Config()
        prompt = persona.user_prompt(cfg, ocr_text=key.block() + "\n\n浙江卫视 / 抖音 / 来了")
        assert "优先信它" in prompt
        body = prompt.split("画面上的文字与关键信息", 1)[1]
        assert body.index("· 台标/平台") < body.index("浙江卫视 / 抖音 / 来了"), body
        # 看片笔记那一轮也要按台标/节目名记
        notes = persona.absorb_format_block()
        assert "台标" in notes and "话题标签" in notes, notes
        # 陪看时的系统提示词得教它主动去找标志和名字
        system = persona.system_prompt(cfg)
        assert "先认**标志和名字**" in system and "台标" in system, "系统提示词没教它去找标志"

    # ---------- ③ 长期记忆 ----------

    def test_memory():
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            base = Config()
            base.memory.path = str(Path(tmp) / "memory.json")
            base.save(path)
            cfg = Config.load(path)

            mem = Memory(cfg)
            assert mem.path == Path(tmp) / "memory.json", mem.path
            assert mem.context() == ""
            assert not mem.save()  # 没有改动就不落盘

            tags = mem.add("这波操作太牛了", "excited", context="王者荣耀 抖音精选")
            assert "游戏" in tags, tags
            assert mem.comment_count == 1
            assert mem.entries[-1].dialog == dialog.ANALYZE, mem.entries[-1]
            assert dict(mem.dialog_mix()) == {dialog.ANALYZE: 1}
            mem.add("我在看这支视频", dialog=dialog.RECORD)
            assert mem.dialog[dialog.RECORD] == 1 and mem.dialog[dialog.ANALYZE] == 1
            assert mem.save(force=True) and mem.path.exists()

            again = Memory(Config.load(path))
            assert again.comment_count == 2 and len(again.entries) == 2
            assert "游戏" in dict(again.top_tags())
            # 对话类型跟着记录一起存盘、读回来还是那一类（私下统计，不上界面）
            assert again.entries[-1].dialog == dialog.RECORD, again.entries[-1]
            assert again.dialog[dialog.ANALYZE] == 1 and again.dialog[dialog.RECORD] == 1
            # 分布够明显时，上下文里会多一句"接下来该怎么说"（私下提醒，不进界面）
            for _ in range(5):
                again.add("这操作绝了", "excited")
            assert "情绪" in again.context(), again.context()
            assert again.apply_summary("画像：爱看游戏集锦，对抽卡完全无感。\n标签：游戏, 集锦, 短视频")
            assert "爱看游戏集锦" in again.profile
            assert "集锦" in dict(again.top_tags())
            assert "记得关于这个观众" in again.context()

            again.comment_count = 30
            again.profile_updated_at = 0.0
            assert again.needs_summary()
            assert "画像" in again.summary_prompt()

            again.clear()
            assert again.comment_count == 0 and not again.entries

        assert "游戏" in extract_tags("王者荣耀五杀")
        assert extract_tags("") == []

        # ---- 进阶：能拿去主动搭话的话头（只读记忆，不写）----
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            base = Config()
            base.memory.path = str(Path(tmp) / "memory.json")
            base.save(path)
            mem = Memory(Config.load(path))

            assert mem.topics() == [], "还没记忆就已经有话头了"
            for _ in range(3):
                mem.add("又是王者荣耀", "excited", context="王者荣耀")
            before = list(mem.entries)
            picked = mem.topics()
            assert picked, "看了四遍王者荣耀还说没话头"
            assert picked[0].label == "王者荣耀", picked           # 具体作品名排在笼统类别前
            assert picked[0].count >= 3 and "王者荣耀" in picked[0].hint
            assert "游戏" in [t.label for t in picked], picked      # 类别也在，只是排后面
            assert mem.topics() and list(mem.entries) == before, "挑话头不该写记忆"

            # 出现次数不够 / 太笼统的标签，都不该拿来搭话
            assert topic_candidates([Episode(1.0, "只看到一次", "", ["猫猫"])], min_count=3) == []
            loud = [Episode(float(i), "刷多了", "", ["短视频"]) for i in range(5)]
            assert topic_candidates(loud, min_count=2) == [], "「短视频」这种太笼统的标签不该当话头"
            assert topic_candidates(loud, min_count=2, limit=4) == []

            # "最近"只看最近的记录，几百年前爱看的不算
            old = [Episode(1.0, "老早看的", "", ["王者荣耀"]) for _ in range(10)]
            fresh = [Episode(900.0 + i, "刚看的", "", ["猫猫"]) for i in range(10)]
            assert [t.label for t in topic_candidates(old + fresh, min_count=3, window=10)] == ["猫猫"]

    # ---------- ③′ 完整记忆存档（memory.json 会裁剪，存档一条都不丢）----------

    def test_memory_archive():
        """今天要的那件事：**记忆一直留着**——脑子那份可以裁、可以清，流水不动。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            base = Config()
            # 存档默认就跟着 memory.json 走（archive_path 留空 = 放在它旁边）
            assert base.memory.archive_path == "", "默认该是空串（跟着 memory.json 放）"
            assert base.memory.archive_enabled is True
            base.memory.path = str(Path(tmp) / "memory.json")
            base.memory.archive_path = str(Path(tmp) / "archive.jsonl")
            base.memory.max_entries = 3          # 故意掐得很小，好看见"裁剪"
            base.save(path)
            cfg = Config.load(path)

            mem = Memory(cfg)
            assert mem.archive.path == Path(tmp) / "archive.jsonl", mem.archive.path
            assert mem.stats()["archive"] == 0 and not mem.archive.count()

            for i in range(5):
                mem.add(f"第 {i} 条吐槽", "happy", context="王者荣耀")
            mem.save(force=True)
            # 脑子里那份被裁到 3 条；存档里 5 条一条不少
            assert len(mem.entries) == 3, mem.entries
            assert mem.archive.count() == 5, mem.archive.count()
            saved = json.loads(mem.path.read_text(encoding="utf-8"))
            assert [e["text"] for e in saved["entries"]] == ["第 2 条吐槽", "第 3 条吐槽", "第 4 条吐槽"]

            rows = list(mem.archive.records(kind=memarchive.EPISODE))
            assert [r["text"] for r in rows] == [f"第 {i} 条吐槽" for i in range(5)], rows
            assert rows[0]["tags"] and rows[0]["dialog"], rows[0]     # 标签 / 私下分类都得留着
            info = mem.archive.stats()
            assert info["episodes"] == 5 and info["lines"] == 5, info
            assert "王者荣耀" in dict(info["top_tags"]), info["top_tags"]
            assert "共 5 行" in mem.archive.report(), mem.archive.report()
            recent = mem.archive.tail(2)
            assert "第 4 条吐槽" in recent and "第 0 条吐槽" not in recent, recent

            # 画像也进流水（以后能翻到"它当时是这么理解我的"）
            assert mem.apply_summary("画像：爱看游戏集锦。\n标签：游戏, 集锦")
            kinds = [r["kind"] for r in mem.archive.records()]
            assert kinds.count(memarchive.PROFILE) == 1 and mem.archive.count() == 6, kinds
            assert "[画像]" in mem.archive.tail(1), mem.archive.tail(1)

            # 清空长期记忆：脑子里那份清了，**流水一行不动**
            mem.clear()
            assert not mem.entries and mem.comment_count == 0
            assert mem.archive.count() == 6, "清记忆把完整存档也牵连了"
            again = Memory(Config.load(path))
            assert again.archive.count() == 6, "重启后又把存档倒了一遍"

            # 写不进去也不能影响记忆本身（存档是顺带留一份，不是主路）
            blocked = Path(tmp) / "blocked-dir"
            blocked.mkdir()                       # 拿目录冒充存档文件：写必然失败
            broken = Memory(Config.load(path))
            broken.archive.path = blocked
            assert broken.archive.add_episode(Episode(1.0, "写不进去的一条")) is False
            assert broken.archive.count() == 0

        # 老用户第一次升级：存档还不存在时，把 memory.json 里现有的先倒进去
        with tempfile.TemporaryDirectory() as tmp:
            # archive_path 留空（默认）：存档就跟在 memory.json 旁边
            # （测试把记忆指到临时目录时，存档也落临时目录——绝不写脏项目根）
            plain = Config()
            plain.memory.path = str(Path(tmp) / "side" / "memory.json")
            beside = Memory(plain)
            expected = Path(tmp) / "side" / "data" / "memory" / "archive.jsonl"
            assert beside.archive.path == expected, beside.archive.path
            # 命令行（python -m pet.memarchive path）走的是同一个算法
            assert archive_path_for(plain) == expected, archive_path_for(plain)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            base = Config()
            base.memory.path = str(Path(tmp) / "memory.json")
            base.memory.archive_path = str(Path(tmp) / "old.jsonl")
            base.save(path)
            old = Memory(Config.load(path))
            old.archive.enabled = False          # 模拟"还没有存档"的老版本
            for i in range(3):
                old.add(f"以前的第 {i} 条", context="王者荣耀")
            old.save(force=True)
            assert not old.archive.path.exists()

            upgraded = Memory(Config.load(path))  # 升级后第一次启动
            assert upgraded.archive.count() == 3, upgraded.archive.count()
            assert [r["kind"] for r in upgraded.archive.records()] == [memarchive.EPISODE] * 3
            assert Memory(Config.load(path)).archive.count() == 3, "回填不该重复倒"

            # 记忆整个关掉时存档也停：config 里关了就该真关
            off = Config.load(path)
            off.memory.enabled = False
            quiet = Memory(off)
            before = quiet.comment_count          # 读回来的旧账还在，只是不许再添新的
            quiet.add("这条不该进任何地方")
            assert quiet.comment_count == before and not quiet.archive.on, "关掉记忆还在记"
            assert quiet.archive.add_episode(Episode(2.0, "也不该进")) is False
            assert quiet.archive.count() == 3, "关掉记忆还在往存档里写"

    # ---------- ④ 全局热键 ----------

    def test_hotkey_parse():
        assert parse_hotkey("ctrl+alt+m") == (0x0002 | 0x0001 | 0x4000, 0x4D)
        assert parse_hotkey("Ctrl+Shift+F9") == (0x0002 | 0x0004 | 0x4000, 0x78)
        assert parse_hotkey("m") is None            # 没有修饰键
        assert parse_hotkey("ctrl+") is None
        assert parse_hotkey("ctrl+abc") is None
        assert format_hotkey("ctrl+alt+m") == "Ctrl+Alt+M"
        assert format_hotkey("alt+f9") == "Alt+F9"

    def test_hotkey_live():
        """真注册一次热键，再合成按键，验证整条链路。（Ctrl+Alt+F9）"""
        if os.name != "nt":
            print("      非 Windows，跳过")
            return
        listener = HotkeyListener({"test": "ctrl+alt+f9"})
        probe = _HotkeyProbe()  # QObject 在主线程，跨线程信号才会被排队
        listener.triggered.connect(probe.on_triggered)
        listener.start()

        deadline = time.monotonic() + 3.0
        pressed_at = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            if not probe.actions and time.monotonic() >= pressed_at:
                _press_hotkey(0x0002 | 0x0001, 0x78)  # Ctrl + Alt + F9
                pressed_at = deadline + 1  # 只按一次
            app.processEvents()
            time.sleep(0.05)
            if probe.actions:
                break

        registered = list(listener.registered)
        listener.stop()
        listener.wait(2000)

        if not registered:
            raise AssertionError("热键没注册上（可能被别的程序占用了）")
        assert probe.actions == ["test"], f"注册了 {registered}，但没收到按键：{probe.actions}"

    # ---------- 整体组装 ----------

    def test_app_wiring():
        """把 ScreenPet 真装起来跑几秒，覆盖 界面↔线程↔气泡 的接线。"""
        from pet.app import ScreenPet

        # 一定用"从文件加载"的配置，否则退出时的保存会写回项目里的 config.json
        cfg_path = Path(tmpdir) / "app-cfg.json"
        base = Config()
        base.ocr.backend = "off"
        base.memory.path = str(Path(tmpdir) / "app-memory.json")
        base.capture.interval_sec = 1.0
        base.capture.cooldown_sec = 0.0
        base.provider = "mock"
        # 开着热键，用一组测试专用的组合键，避免和真在跑的挂件抢
        base.hotkey.enabled = True
        base.hotkey.region = "ctrl+alt+f10"
        base.hotkey.pause = "ctrl+alt+f11"
        base.hotkey.say = "ctrl+alt+f12"
        base.save(cfg_path)
        cfg = Config.load(cfg_path)
        assert cfg.config_path() == cfg_path

        companion = ScreenPet(cfg, app)
        seen = []
        companion.worker.comment.connect(seen.append)
        companion.start()
        started = time.monotonic()
        deadline = started + 6.0
        while time.monotonic() < deadline and not seen:
            app.processEvents()
            time.sleep(0.05)
        assert seen, "ScreenPet 装起来之后没吐出一句话"
        assert isinstance(seen[0], mood.Comment), seen[0]
        assert companion.worker.memory.comment_count >= 1

        # 端到端：合成 Ctrl+Alt+F10，看是不是真的弹出了"划观看范围"那个界面
        if os.name == "nt":
            deadline = time.monotonic() + 3.5
            pressed = False
            while time.monotonic() < deadline and companion._picker is None:
                if not pressed and time.monotonic() > started + 1.5:
                    _press_hotkey(0x0002 | 0x0001, 0x79)  # Ctrl + Alt + F10
                    pressed = True
                app.processEvents()
                time.sleep(0.05)
            assert companion._picker is not None, "按了划观看范围的热键，挂件没反应"
            companion._picker.close()      # 别把它留在屏幕上
            companion._picker = None

        # 端到端：串门那条链路也得是通的（抱抱 / 夸夸那两条热键已经并进主动搭话，撤掉了）
        seen.clear()
        companion.visit_now()
        assert companion.window._visiting is True, "没出发去别的屏幕"
        print("      出发去别的屏幕了（抱抱/夸夸已并进主动搭话）")

        # 收进托盘 / 放回来：挂件和后台线程得一起切（隐身期间不抓屏，见 worker.set_hidden）
        companion.hide()
        assert not companion.window.isVisible(), "隐身之后挂件还留在屏幕上"
        assert companion.worker._away.is_set(), "隐身了却没告诉后台线程"
        companion._toggle_visible(True)
        assert companion.window.isVisible(), "放回来之后挂件没显示"
        assert not companion.worker._away.is_set(), "放回来了后台还在隐身"
        print("      隐身↔放回来 这条接线是通的（worker 跟着一起切）")

        # 点一下挂件 = 聊 / 收：面板开着再点一下就收起来（挂件问 app，app 收面板）
        assert not companion.panel.isVisible()
        assert callable(companion.window.chat_open), "挂件没接上「面板开没开」那个问句"
        companion.toggle_chat()
        assert companion.panel.isVisible(), "点一下没把输入框叫出来"
        companion.toggle_chat()
        assert not companion.panel.isVisible(), "再点一下没收起来（还占着屏幕）"
        print("      点一下挂件：聊 ↔ 收 都通（面板开没开由 app 说了算）")

        # 「设计我的形象…」这条菜单：开出来 → 改一下**当场**换装 → 存进配置 → 关掉不留尾巴
        companion.open_designer()
        designer = companion._designer
        assert designer is not None, "点了「设计我的形象…」没把设计器叫出来"
        assert designer in companion.window.extra_occluders, "设计器开着，抓屏时却没躲开它"
        index = designer._shape.findData("drop")
        designer._shape.setCurrentIndex(index)
        designer._shape.activated.emit(index)
        app.processEvents()
        assert designer.style.body_shape == "drop"
        assert companion.renderer.pet_style.body_shape == "drop", "设计器改了身形，挂件没跟着换"
        designer.save_to_config()
        app.processEvents()
        saved = Config.load(cfg_path)
        assert saved.ui.pet_style.get("body_shape") == "drop", saved.ui.pet_style
        designer.close()
        deadline = time.monotonic() + 2.0
        while companion._designer is not None and time.monotonic() < deadline:
            app.processEvents()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)   # WA_DeleteOnClose 是延后删
            time.sleep(0.02)
        assert companion._designer is None, "设计器关掉了，app 还攥着它（下次抓屏会去问一个没了的窗口）"
        assert companion.window.extra_occluders == [companion.panel], companion.window.extra_occluders
        print("      设计我的形象：开出来 / 当场换装 / 存进配置 / 关掉清理引用，全通")

        companion.quit()


    def test_vlm_mock():
        cfg = Config()
        client = VisionClient(cfg)
        assert client.ready
        found = None
        for _ in range(6):  # mock 每 4 次会有一次沉默，多试几次
            found = client.describe(frame)
            if found is not None:
                break
        assert isinstance(found, mood.Comment), found
        assert found.text and found.mood in mood.mood_names()
        summary = client.summarize("随便什么提示词")
        assert "画像" in summary and "标签" in summary

        # 主动搭话走同一条假链路（含进阶的"翻记忆找话头"），照旧不许沉默
        for kind in (proactive.STILL_SCREEN, proactive.LONG_QUIET, proactive.MEMORY_TOPIC):
            line = client.nudge(kind, image=frame, topic="他最近老在看「王者荣耀」", clock="凌晨 1:20")
            assert isinstance(line, mood.Comment) and line.text, line
            assert line.kind == f"proactive:{kind}", line.kind

    def test_persona_prompt():
        """提示词要带上时间和话头——不然"这个点还看"和"你最近老在看"它都不知道。"""
        cfg = Config()
        system = persona.system_prompt(cfg)
        assert cfg.persona.name in system and "[沉默]" in system
        tail = persona.system_prompt(cfg, proactive=True)     # 主动开口那一轮，沉默这条路被堵上
        assert "不能回复 [沉默]" in tail and "别像在汇报状态" in tail

        plain = persona.user_prompt(cfg, window="斗鱼 - 王者荣耀", clock="凌晨 1:20")
        assert "现在时间：凌晨 1:20" in plain and "斗鱼" in plain

        chat = persona.proactive_prompt(
            cfg,
            proactive.MEMORY_TOPIC,
            note="已经 320 秒没说话",
            window="斗鱼 - 王者荣耀",
            topic="他最近老在看「王者荣耀」，你记录到 4 次",
            clock="凌晨 1:20",
        )
        assert "现在时间：凌晨 1:20" in chat
        assert "他最近老在看「王者荣耀」" in chat and "别原样念出来" in chat
        assert "不能回 [沉默]" in chat
        assert proactive.REASONS[proactive.MEMORY_TOPIC] in chat

        # 不带话头时不该出现那一段
        bare = persona.proactive_prompt(cfg, proactive.LONG_QUIET)
        assert "他最近老在看" not in bare and "不能回 [沉默]" in bare

    cfg = Config()
    cfg.capture.interval_sec = 1.0
    cfg.capture.cooldown_sec = 0.0
    # 测试跑出来的记忆 / 口味档案一律丢到临时目录，别写脏项目根
    cfg.memory.path = str(Path(tmpdir) / "outer-memory.json")
    cfg.taste.path = str(Path(tmpdir) / "outer-taste.json")

    renderer = PetRenderer(ASSETS_DIR / "pet")

    def test_renderer():
        assert not renderer.icon(32).isNull()
        for index, name in enumerate(MOOD_STYLE):
            pm = QPixmap(160, 160)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            renderer.draw(painter, QRectF(0, 0, 160, 160), 1.5, index % 2 == 0, False, False, name)
            painter.end()
            assert not pm.isNull()

        # 图片帧：缩放结果要缓存（每帧重缩放 + 亚像素绘制 = 肉眼看到的"闪"）
        if renderer.uses_frames:
            pm = QPixmap(160, 160)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            renderer.draw(painter, QRectF(0, 0, 160, 160), 0.0)
            cached = len(renderer._scaled_cache)
            renderer.draw(painter, QRectF(0, 0, 160, 160), 0.05)
            painter.end()
            assert cached > 0, "帧没有被缩放并缓存"
            assert len(renderer._scaled_cache) - cached <= 2, "每帧都在重新缩放，画面会抖"
            print(
                f"      图片帧 {len(renderer.frames_idle)} 张 / 呼吸一轮 {renderer.period}s / "
                f"交叉淡化 {'开' if renderer.crossfade else '关'}"
            )
        else:
            print("      没装图片帧，用内置矢量形象（跑 tools/make_pet.py 可以换成自己的图）")

        # 食指敲下巴：以前是「整块贴片往上挪」——贴片下沿压在指根/拳头上，一挪就在手和
        # 贴片之间裂开一条缝（补色那块露出来），现场看着就是**手在自己伸缩**。现在改成
        # 绕指根纵向拉伸：下沿原地不动、越靠近指尖抬得越多，底下永远连着。
        if renderer.uses_hand and renderer.hand_box:
            box = renderer.hand_box
            period, lift = renderer.hand_period, renderer.hand_lift
            top, bottom = float(box[1]), float(box[3])
            assert _tap_matrix(0.0, period, lift, box) == IDENTITY, "周期头上应当贴着下巴不动"

            samples = [
                (index / 40.0, _tap_matrix(period * index / 40.0, period, lift, box))
                for index in range(40)
            ]
            risen = 0.0
            for fraction, matrix in samples:
                a, b, c, d, e, f = matrix
                assert abs(a - 1.0) < 1e-9 and abs(b) < 1e-9 and abs(d) < 1e-9, \
                    f"食指只该纵向拉伸，{fraction:.2f} 处的矩阵是 {matrix}"
                assert abs(e * bottom + f - bottom) < 1e-6, \
                    f"指根那行动了（{fraction:.2f}），手和贴片之间就会露出缝——就是「手在伸缩」"
                y_top = e * top + f
                assert y_top <= top + 1e-9, f"只能往上抬，不能往下戳进下巴（{fraction:.2f}）"
                risen = max(risen, top - y_top)
            assert abs(risen - lift) < 0.35, f"抬起幅度该接近 --tap-lift：{risen:.2f} vs {lift}"
            tapping = sum(1 for _, matrix in samples if matrix != IDENTITY)
            assert 0 < tapping < len(samples), "一圈里既要有在敲的时候，也要有贴着下巴不动的时候"
            print(
                f"      食指敲下巴：每 {period:.1f}s 轻敲 2 下，抬起 {risen:.1f}px / 画布，"
                f"指根不动（{tapping}/{len(samples)} 个采样点在抬）"
            )

    window = None

    def test_window():
        nonlocal window
        window = PetWindow(cfg, renderer)
        window.show()
        window.say("测试一句话，看看气泡能不能出来", "excited")
        assert window.mood == "excited"
        assert window.occlusion_rect().width() > 0
        window.set_thinking(True)
        window.set_paused(True)
        window.set_paused(False)
        window.set_click_through(True)
        window.set_click_through(False)
        window.hideForCapture()
        window.showAfterCapture()
        assert window.cfg.capture.region is None
        window.say("换个表情", "speechless")
        assert window.mood == "speechless"

        # 日常状态 / 界面互动：按"它现在什么情况"切状态、互动时播一遍小动作（见 pet/states.py）
        window.set_thinking(False)
        assert window._pose == "", window._pose
        window.set_paused(True)
        assert window._pose == "sleep", window._pose           # 暂停 = 眯着了
        window.set_paused(False)
        assert window._pose == "", window._pose
        window.set_listening(True)
        assert window._pose == "look", window._pose            # 麦克风开着 = 竖着耳朵听
        window.set_thinking(True)
        assert window._pose == "look", f"「在听」应该压过「在想」：{window._pose}"
        window.set_listening(False)
        assert window._pose == "think", window._pose
        window.set_thinking(False)
        assert window._pose == "", window._pose
        window.react("click")
        assert window._act == "greet", window._act             # 单击 = 打招呼
        assert window._act_state(time.monotonic())[0] == "greet"
        window.react("double")
        assert window._act == "poke", window._act              # 双击 = 被戳
        window.react("这个互动不存在")                          # 认不出来就什么都不做
        window.set_pose("这个状态不存在")
        assert window._pose == "" and window._act == "poke", (window._pose, window._act)

        # 「逗它一下」：右键菜单里那排按钮（见 states.MENU_REACTIONS）必须**项项都有落点**——
        # 菜单上的字 → 信号 → 挂件把它演出来。这套动作以前做完了却没入口（菜单里没处点、
        # 情绪也带不出来），界面上永远看不到，等于白做；现在挨个点一遍，少接一个就报出来。
        menu = window._build_menu()
        labels = [action.text() for action in menu.actions()]
        # 分组标题是挂在 QWidgetAction 上的 QLabel（菜单一挂样式表，addSection 的标题
        # 就画不出字了，见 window._add_section），所以标题得从 defaultWidget 里问。
        titles = [
            action.defaultWidget().text()
            for action in menu.actions()
            if isinstance(action, QWidgetAction) and action.defaultWidget() is not None
        ]
        assert "逗它一下" in titles, f"右键菜单里没「逗它一下」那一节：{titles}"
        got = []
        window.reactionRequested.connect(got.append)
        for label, key in states.MENU_REACTIONS:
            assert label in labels, f"菜单里没有「{label}」"
            assert states.for_reaction(key) is not None, f"「{label}」指的 {key} 不是认得的动作"
            next(action for action in menu.actions() if action.text() == label).trigger()
            assert got[-1:] == [key], f"点「{label}」该报 {key}，实际 {got}"
            window._act, window._act_until = "", 0.0        # 让上一个动作算播完
            window.react(key)
            assert window._act == states.REACTIONS[key], (label, window._act)
        # 鼠标那两下新入口：停在它身上 → 挥手；拖起来放下 → 蹦一下
        window._act, window._act_until = "", 0.0
        window._hover_cool = 0.0
        window._on_hover_still()
        assert window._act == "wave", window._act               # 鼠标停在它身上 = 它跟你挥手
        window._act, window._act_until = "", 0.0
        window._on_hover_still()
        assert window._act == "", "刚挥过就不该再挥（冷却里不插队）"
        window._hover_cool = 0.0
        window._on_drop()
        assert window._act == "jump", window._act               # 放下 = 落地蹦一下
        print(f"      「逗它一下」{len(states.MENU_REACTIONS)} 项都能落到动作上 + 悬停挥手 / 放下蹦一下")

        # 抓到的画面是什么情绪，就顺手带个动作（见 states.MOOD_ACTIONS / PetWindow.play_mood）
        window._act, window._act_until = "", 0.0               # 先让上一个动作算播完
        window.play_mood("curious")
        assert window._act == "confused", window._act          # 好奇 → 疑惑
        window.play_mood("surprised")
        assert window._act == "confused", "正做着动作的时候不该被打断"
        window._act, window._act_until = "", 0.0
        window.play_mood("surprised")
        assert window._act == "poke", window._act              # 惊讶 → 被戳般惊跳
        window._act, window._act_until = "", 0.0
        window.play_mood("")                                   # 没情绪就什么都不做
        assert window._act == "", window._act

        # 右键菜单能真的建起来（把 exec 换成不打开展示的探针）：只留用户功能，没有测试项 / 说明行
        import pet.window as window_mod

        real_menu = window_mod.QMenu

        class _MenuProbe(real_menu):
            seen = []

            def exec(self, *args, **kwargs):  # 不真的弹出菜单
                _MenuProbe.seen = [(a.text(), a.isCheckable()) for a in self.actions()]
                return None

        window_mod.QMenu = _MenuProbe
        try:
            window._show_menu(QPoint(0, 0))
        finally:
            window_mod.QMenu = real_menu
        labels = [text for text, _ in _MenuProbe.seen]
        assert labels, "右键菜单一项都没有"
        chat = [text for text, checkable in _MenuProbe.seen if "主动搭话" in text]
        assert chat and all(
            checkable for text, checkable in _MenuProbe.seen if "主动搭话" in text
        ), f"主动搭话开关不在菜单里或是不可勾选：{chat}"
        assert any("退出" in text for text in labels), "菜单里连退出都没有"
        # 观看范围只留一个入口：进去自己拖一块，或者双击选整块屏（以前是两个菜单项）
        region_items = [text for text in labels if "观看范围" in text]
        assert len(region_items) == 1, f"观看范围应该只有一项，现在有 {region_items}"
        assert not any("只看一小块" in text or "整块屏都看" in text for text in labels), labels
        assert not any("静音" in text for text in labels), f"语音/静音已经去掉，菜单里还有：{labels}"
        # 这两项撤了：「把我钉在这儿」挪去 `Ctrl+Alt+L` 热键；「隐身时自己上网学」改成默认开着，
        # 不再摆开关（见 pet/window.py 的 _build_menu 与 pet/config.py 的 StudyConfig）。
        for gone in ("钉在", "钉住", "鼠标点不到我", "上网学", "隐身时自己"):
            assert not any(gone in text for text in labels), f"菜单里还留着「{gone}」：{labels}"
        assert not any(checkable for text, checkable in _MenuProbe.seen if "隐身" in text), labels
        # 「去别的屏幕逛逛」和「好友陪伴」并成一项「好友系统」（本机溜达挪进 FriendPanel 里了）
        assert any("好友系统" in text for text in labels), f"菜单里没有「好友系统」：{labels}"
        assert not any("去别的屏幕逛逛" in text for text in labels), labels
        assert not any("好友陪伴" in text for text in labels), labels
        assert not any("测试" in text for text in labels), f"菜单里还留着测试项：{labels}"
        assert not any(len(text) > 28 for text in labels), f"菜单里还留着大段说明：{labels}"
        # 「马上吐槽一句」只剩 `Ctrl+Alt+S` 热键；「打字跟我唠…」「说一句（语音）」合成
        # "点一下挂件"（见下面的点击测试）——菜单里三项都不该再有
        for gone in ("马上吐槽", "打字跟我唠", "说一句"):
            assert not any(gone in text for text in labels), f"菜单里还留着「{gone}」：{labels}"
        # 「设计我的形象…」：长相现调现看（见 app.open_designer），入口就在右键菜单里
        assert any("设计我的形象" in text for text in labels), f"右键菜单里没有「设计我的形象…」：{labels}"
        # 托盘让出来的那两项，得在这儿（右键菜单）找得到——托盘只管"放出来 / 收回去"
        for moved in ("打开记忆文件", "清除长期记忆"):
            assert any(moved in text for text in labels), f"右键菜单里少了「{moved}」：{labels}"
        # 「打开完整存档」是今天新加的：memory.json 会被裁剪，这份流水一条都不丢
        assert any("打开完整存档" in text for text in labels), f"右键菜单里少了「打开完整存档」：{labels}"
        print(
            f"      右键菜单 {len(labels)} 项，观看范围只留一个入口，含主动搭话开关，"
            "没有钉住 / 隐身学习那两个开关，无测试项 / 无长说明"
        )

        # 点一下挂件 = 想跟它说话：气泡先问一句 + 请求打开输入框
        said = []
        window.chatRequested.connect(lambda: said.append(True))
        _send_mouse(window, QEvent.Type.MouseButtonPress, QPoint(12, 12))
        _send_mouse(window, QEvent.Type.MouseButtonRelease, QPoint(12, 12))
        assert window._click_timer.isActive(), "点一下该等一个双击间隔，再决定说不说话"
        window._click_timer.stop()
        window._on_click()
        assert window._bubble._text == window_mod.CHAT_HI, window._bubble._text
        assert "想跟我聊些什么" in window._bubble._text, window._bubble._text
        assert said == [True], said
        # 拖它换位置不算"点了一下"：不说话、也不弹面板
        window._bubble.hide_bubble()
        said.clear()
        _send_mouse(window, QEvent.Type.MouseButtonPress, QPoint(12, 12))
        _send_mouse(window, QEvent.Type.MouseMove, QPoint(60, 60))
        _send_mouse(window, QEvent.Type.MouseButtonRelease, QPoint(60, 60))
        assert not window._click_timer.isActive(), "拖动被当成点了一下"
        assert not said, said
        # 双击（暂停 / 继续看）不许顺手把面板也点开
        paused = []
        window.pauseToggled.connect(lambda value: paused.append(value))
        _send_mouse(window, QEvent.Type.MouseButtonPress, QPoint(12, 12))
        _send_mouse(window, QEvent.Type.MouseButtonRelease, QPoint(12, 12))
        _send_mouse(window, QEvent.Type.MouseButtonDblClick, QPoint(12, 12))
        _send_mouse(window, QEvent.Type.MouseButtonRelease, QPoint(12, 12))
        assert paused == [not window.paused], paused
        assert not window._click_timer.isActive(), "双击那一下又去开面板了"
        # 再点一下（面板已经开着）= 收起来：**慢两下**是"聊完收工"，快两下才是双击（暂停）。
        # 收的时候不吭声——他是要把框收走，不是要再聊一句。
        window.chat_open = lambda: True          # 假装面板正开着
        window._bubble._text = ""
        said.clear()
        window._on_click()
        assert said == [True], said              # 照样发信号，收还是聊由 app 决定
        assert window._bubble._text == "", "要收起来的时候还说「想跟我聊些什么」"
        window.chat_open = None
        print("      点一下挂件：先问「想跟我聊些什么~」再弹出输入框；拖动 / 双击各归各")

        # 气泡里不再画情绪小标签（「好奇」那种胶囊）：情绪只驱动边框配色和表情。
        # 结构上的证据：有没有情绪，气泡的高度都一样（不给标签留位置），常量也没了。
        bubble = window_mod.BubbleWindow(cfg)
        assert bubble.prepare("就这？", "curious")
        with_mood = bubble.height()
        assert bubble.prepare("就这？", "")
        assert bubble.height() == with_mood, "气泡还留着情绪标签那一行的位置"
        assert not hasattr(window_mod, "CHIP_H"), "情绪标签的常量还留在 window.py 里"
        print("      气泡不再画情绪标签（情绪只走配色 + 表情），高度与有没有情绪无关")

    def test_picker():
        """观看范围就一个界面：拖一块 = 只看这块；不拖、双击（或按 F）= 整块屏。

        （以前是菜单里两个入口——"只看一小块地方…"和"整块屏都看"，现在合并成一个。）
        """
        picker = RegionPicker()
        pm = QPixmap(picker.size())
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        picker.render(painter, QPoint(0, 0))  # 直接走一遍 paintEvent，不真的全屏弹出来
        painter.end()

        # ① 拖一个矩形 → picked 给的是物理像素矩形
        picked = []
        picker.picked.connect(picked.append)
        _send_mouse(picker, QEvent.Type.MouseButtonPress, QPoint(120, 120))
        _send_mouse(picker, QEvent.Type.MouseMove, QPoint(460, 360))
        _send_mouse(picker, QEvent.Type.MouseButtonRelease, QPoint(460, 360))
        assert picked and isinstance(picked[0], dict), f"拖了一块，却没拿到观看范围：{picked}"
        assert int(picked[0]["width"]) > 0 and int(picked[0]["height"]) > 0, picked[0]
        assert set(picked[0]) == {"left", "top", "width", "height"}, picked[0]
        print(f"      拖出来一块 {picked[0]['width']}×{picked[0]['height']}（物理像素）")

        # ② 不拖、双击 → 整块屏
        whole = []
        picker2 = RegionPicker()
        picker2.wholeRequested.connect(lambda: whole.append(True))
        _send_mouse(picker2, QEvent.Type.MouseButtonDblClick, QPoint(400, 300))
        assert whole, "双击没有切到整块屏"

        # ③ 按 F 也是整块屏（键盘党的说法）
        whole_f = []
        picker3 = RegionPicker()
        picker3.wholeRequested.connect(lambda: whole_f.append(True))
        picker3.keyPressEvent(_key_event(Qt.Key.Key_F))
        assert whole_f, "按 F 没有切到整块屏"

        # ④ 不想选就 Esc 或者点一下右键 → 回一个 None（app 那边就当没选）
        for cancel in (
            lambda w: w.keyPressEvent(_key_event(Qt.Key.Key_Escape)),
            lambda w: _send_mouse(w, QEvent.Type.MouseButtonPress, QPoint(300, 300), Qt.MouseButton.RightButton),
        ):
            probe = RegionPicker()
            got = []
            probe.picked.connect(got.append)
            cancel(probe)
            assert got == [None], f"取消的路径应该回一个 None，现在是 {got}"

        # ⑤ 划得太小 = 手抖，不算数：什么都不发，界面留着让人重划
        shaky = RegionPicker()
        nothing = []
        shaky.picked.connect(nothing.append)
        _send_mouse(shaky, QEvent.Type.MouseButtonPress, QPoint(200, 200))
        _send_mouse(shaky, QEvent.Type.MouseButtonRelease, QPoint(205, 203))
        assert not nothing, f"划得太小不该有结果，现在是 {nothing}"
        print("      拖一块 = 只看这块，双击 / F = 整块屏，Esc / 右键 / 手抖 = 不改")

    def test_worker_cycle():
        cfg = Config()
        cfg.capture.interval_sec = 1.0
        cfg.capture.cooldown_sec = 0.0
        cfg.capture.region = None
        cfg.ocr.backend = "off"          # 循环测试不掺 OCR，省时间
        cfg.memory.path = str(Path(tmpdir) / "worker-memory.json")
        cfg.taste.path = str(Path(tmpdir) / "worker-taste.json")
        worker = AnalysisWorker(cfg)
        got = []
        worker.comment.connect(got.append)
        loop = QEventLoop()
        worker.comment.connect(lambda *_: loop.quit())
        worker.start()
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        worker.stop()
        worker.wait(3000)
        assert got, "6 秒内没有收到任何吐槽（mock 模式每帧都该有话说）"
        assert isinstance(got[0], mood.Comment), got[0]
        assert worker.memory.comment_count >= 1, "吐槽没有进长期记忆"
        worker.memory.save(force=True)
        assert Path(worker.memory.path).exists()
        print(f"      收到吐槽：{got[0].text}（{mood.label(got[0].mood)}）")

    def test_proactive_idle():
        """真调 GetLastInputInfo / GetTickCount64，确认"人还在不在"能测出来。"""
        if os.name != "nt":
            assert proactive.user_idle_seconds() is None
            return
        before = proactive._tick_ms()
        assert before is not None, "拿不到系统 tick（GetTickCount64 在 kernel32，不在 user32）"
        time.sleep(0.25)
        after = proactive._tick_ms()
        assert after is not None and after > before, f"系统 tick 没有前进：{before} -> {after}"
        idle = proactive.user_idle_seconds()
        assert isinstance(idle, float) and idle >= 0.0, idle
        print(f"      键鼠空闲 {idle:.1f} 秒（GetLastInputInfo 实测）")

    def test_proactive_policy():
        """假时钟把几个小时的时序在毫秒内跑完：该开口开口，不该开口闭嘴。"""
        probe = Config()
        pro = probe.proactive
        pro.still_after_sec = 100.0
        pro.quiet_after_sec = 200.0
        pro.away_after_sec = 300.0
        pro.cooldown_sec = 600.0
        pro.back_cooldown_sec = 60.0
        pro.max_per_hour = 3
        policy = ProactivePolicy(probe)
        t = 1000.0

        assert policy.observe(t, True, 1.0) is None, "刚打开就说话"
        t += 110
        nudge = policy.observe(t, False, 1.0)
        assert nudge and nudge.kind == proactive.STILL_SCREEN and not nudge.local, nudge
        t += 30
        assert policy.observe(t, False, 1.0) is None, "冷却期里又开口了"
        t += 700
        nudge = policy.observe(t, False, 400.0)
        assert nudge and nudge.kind == proactive.DOZING and nudge.local, nudge
        assert policy.state == proactive.STATE_AWAY
        t += 20
        assert policy.observe(t, False, 900.0) is None, "人都走了还自言自语"
        t += 120
        nudge = policy.observe(t, True, 0.5)
        assert nudge and nudge.kind == proactive.WELCOME_BACK, nudge
        assert policy.state == proactive.STATE_WATCHING
        t += 700
        assert policy.observe(t, False, 1.0) is None, "每小时次数上限没生效"
        t += 3600
        assert policy.observe(t, False, 1.0) is not None, "过了限制时间应该又能开口"
        print(f"      时序：静止→找话 / 走开→打盹 / 回来→招呼，共开口 {policy.count} 次")

        # 躺着看电影不算走开：键鼠一直不动，但画面一直在变
        movie = ProactivePolicy(probe)
        t = 1000.0
        movie.observe(t, True, 1.0)
        for _ in range(10):
            t += 60
            movie.observe(t, True, 900.0)   # 手不碰键鼠，但画面一直在动
            assert movie.state == proactive.STATE_WATCHING, "躺着看电影被判成人走了"

        # 关掉"问模型"就只剩免费台词
        pro.model_nudge = False
        cheap = ProactivePolicy(probe)
        t = 1000.0
        cheap.observe(t, True, 1.0)
        t += 110
        assert cheap.observe(t, False, 1.0) is None, "关了 model_nudge 还在问模型"

    def test_proactive_extras():
        """进阶版：深夜劝睡 + 翻长期记忆找话头（全是假时钟、假记忆，不碰网络）。"""
        probe = Config()
        pro = probe.proactive
        pro.still_after_sec = 100.0
        pro.quiet_after_sec = 200.0
        pro.away_after_sec = 300.0
        pro.cooldown_sec = 600.0
        pro.night_cooldown_sec = 3600.0
        pro.max_per_hour = 3

        # ---- 时段判定：深夜区间跨零点 ----
        assert proactive.in_night(23, 23, 6) and proactive.in_night(3, 23, 6)
        assert not proactive.in_night(12, 23, 6) and not proactive.in_night(6, 23, 6)
        assert proactive.in_night(9, 9, 18) and not proactive.in_night(18, 9, 18)
        assert not proactive.in_night(3, 6, 6), "起止一样就不该是深夜"
        assert proactive.time_phase(2, pro) == proactive.PHASE_LATE_NIGHT
        assert proactive.time_phase(14, pro) == proactive.PHASE_DAY
        assert proactive.time_phase(None, pro) == proactive.PHASE_DAY
        late = proactive.clock_text(_local_ts(1, 20))
        assert "凌晨" in late and "1:20" in late, late
        assert "下午" in proactive.clock_text(_local_ts(15, 5))
        assert "晚上" in proactive.clock_text(_local_ts(23, 40))
        assert "早上" in proactive.clock_text(_local_ts(7, 0))

        # ---- 深夜：同一时刻，说出口的该是"该睡了"，而不是"画面没动" ----
        night = ProactivePolicy(probe)
        t = 1000.0
        assert night.observe(t, True, 1.0, hour=2) is None, "刚打开挂件就唠叨"
        t += 110
        first = night.observe(t, False, 1.0, hour=2)
        assert first and first.kind == proactive.STILL_SCREEN, first   # 先照常吐槽画面
        t += 3700
        nudge = night.observe(t, False, 1.0, hour=2)                   # 沉默够久 + 深夜 → 劝睡
        assert nudge and nudge.kind == proactive.LATE_NIGHT and nudge.local, nudge
        assert night.stats()["phase"] == proactive.PHASE_LATE_NIGHT
        t += 110
        assert night.observe(t, False, 1.0, hour=2) is None, "深夜唠叨没走自己的冷却"
        print(f"      凌晨两点：{nudge.line}")

        # 关掉"按时间段说话"，深夜就只是普通搭话
        pro.time_aware = False
        plain = ProactivePolicy(probe)
        t = 1000.0
        plain.observe(t, True, 1.0, hour=2)
        t += 110
        nudge = plain.observe(t, False, 1.0, hour=2)
        assert nudge and nudge.kind == proactive.STILL_SCREEN, nudge
        pro.time_aware = True
        # 人走开了就别再劝睡了，先招呼一声
        away = ProactivePolicy(probe)
        t = 1000.0
        away.observe(t, True, 1.0, hour=2)
        t += 800
        nudge = away.observe(t, False, 900.0, hour=2)
        assert nudge and nudge.kind == proactive.DOZING, nudge

        # ---- 话头：从最近的记录里挑"他最近老在看"的东西 ----
        now = time.time()
        entries = [
            Episode(now - 60 * i, f"第 {i} 段", "excited", ["游戏", "王者荣耀"]) for i in range(4)
        ]
        entries.append(Episode(now, "偶尔刷到的", "curious", ["搞笑", "猫"]))
        topics = topic_candidates(entries, min_count=3)
        assert topics and topics[0].label == "王者荣耀", topics
        assert topics[0].count == 4 and "王者荣耀" in topics[0].hint

        chatty = ProactivePolicy(probe, topic_source=lambda: topics)
        t = 1000.0
        chatty.observe(t, True, 1.0)
        t += 210
        nudge = chatty.observe(t, True, 1.0)      # 画面在动，但它自己憋不住要开口
        assert nudge and nudge.kind == proactive.MEMORY_TOPIC, nudge
        assert "王者荣耀" in nudge.topic, nudge.topic
        t += 700
        again = chatty.observe(t, True, 1.0)
        assert again and again.kind == proactive.MEMORY_TOPIC, again
        assert "王者荣耀" not in again.topic, "同一个话头连着聊两次"
        assert chatty.stats()["topics_used"] == ["王者荣耀", "游戏"]
        print(f"      搭话话头：{nudge.topic} → {again.topic}")

        # 记忆还没攒够话头 → 退回普通的"你怎么不说话"
        empty = ProactivePolicy(probe, topic_source=lambda: [])
        t = 1000.0
        empty.observe(t, True, 1.0)
        t += 210
        nudge = empty.observe(t, True, 1.0)
        assert nudge and nudge.kind == proactive.LONG_QUIET, nudge

        # 记忆文件坏了也不能把"搭话"这条路堵死
        def boom():
            raise RuntimeError("记忆文件读坏了")

        broken = ProactivePolicy(probe, topic_source=boom)
        t = 1000.0
        broken.observe(t, True, 1.0)
        t += 210
        nudge = broken.observe(t, True, 1.0)
        assert nudge and nudge.kind == proactive.LONG_QUIET, nudge

        # 想关掉"翻记忆找话头"就一句话的事
        pro.topic_from_memory = False
        no_topic = ProactivePolicy(probe, topic_source=lambda: topics)
        t = 1000.0
        no_topic.observe(t, True, 1.0)
        t += 210
        nudge = no_topic.observe(t, True, 1.0)
        assert nudge and nudge.kind == proactive.LONG_QUIET, nudge
        pro.topic_from_memory = True

    def test_proactive_worker():
        """端到端：让它立刻主动说一句，看挂件侧真的收到，且不污染长期记忆。"""
        cfg = Config()
        cfg.capture.interval_sec = 0.5
        cfg.capture.cooldown_sec = 0.0
        cfg.capture.region = None
        cfg.ocr.backend = "off"
        cfg.memory.path = str(Path(tmpdir) / "nudge-memory.json")
        cfg.taste.path = str(Path(tmpdir) / "nudge-taste.json")
        worker = AnalysisWorker(cfg)
        got = []
        loop = QEventLoop()

        def on_comment(comment):
            got.append(comment)
            # 普通吐槽照常收着，等真的收到"主动搭话"再结束
            if getattr(comment, "kind", "").startswith("proactive:"):
                loop.quit()

        worker.comment.connect(on_comment)
        worker.start()
        QTimer.singleShot(800, worker.nudge_now)   # 不等冷却，直接要一句
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        worker.stop()
        worker.wait(3000)

        chats = [c for c in got if getattr(c, "kind", "").startswith("proactive:")]
        assert chats, f"没收到主动搭话，只收到：{[c.text for c in got]}"
        assert chats[0].text and chats[0].mood in mood.mood_names()
        spoken = [c.text for c in chats]
        kept = [e.text for e in worker.memory.entries]
        assert not any(text in kept for text in spoken), "主动搭话不该写进长期记忆"
        print(f"      主动搭话：{spoken[0]}（{chats[0].kind}）")

    def test_scene_parse():
        """5W1H 场景行：两行 / 场景在前 / 挤在一行 / 全是"未知" / 纯台词，都得拆对。"""
        cases = [
            ("[无语] 看不下去了\n场景：人物=主播｜事件=连抽十次没出金色｜时间=深夜｜地点=抽卡直播间｜原因=想抽当期角色", 5, "人物=主播"),
            ("场景：人物=猫｜事件=踩键盘\n[好奇] 这是啥", 2, "事件=踩键盘"),
            ("[吐槽] 这剪辑绝了 场景：人物=up主｜事件=疯狂跳剪", 2, "人物=up主"),
            ("[沉默]", 0, ""),
            ("场景：人物=未知｜事件=未知｜时间=未知｜地点=未知｜原因=未知", 0, ""),
            ("[激动] 五杀！！", 0, ""),
            # 现场那条（图一）：两行挤成一行，模型还把**格式骨架**当内容交了回来。
            # 骨架不是内容——它既不能进气泡，也不能当成"看懂了什么"记下来。
            ("这画面还挺酷的 / 场景：人物｜事件｜时间｜地点｜原因", 0, ""),
            # 按顺序摆的短句（提示词要的就是这个写法）：得跟五个字段对上号
            ("这画面还挺酷的 / 场景：白队｜等待出发｜未知｜未知｜未知", 2, "人物=白队"),
            # 连「场景：」都没写，直接粘一串骨架在台词后面
            ("[开心] 这画面还挺酷的 人物｜事件｜时间｜地点｜原因", 0, ""),
            # 骨架单独占一行（谁都没说）
            ("人物｜事件｜时间｜地点｜原因", 0, ""),
        ]
        for raw, filled, snippet in cases:
            speech, parsed = scene.split(raw)
            assert parsed.filled == filled, (raw, parsed.filled)
            assert snippet in parsed.line() or not snippet, (raw, parsed.line())
            assert "场景" not in speech, (raw, speech)   # 场景行绝不能被当台词念出来
        # 骨架绝不能留在气泡里，也不能留在记忆里（现场就是它漏进了气泡）
        leaked, parsed = scene.split("这画面还挺酷的 / 场景：人物｜事件｜时间｜地点｜原因")
        assert leaked == "这画面还挺酷的", leaked
        assert parsed.is_empty(), parsed.line()
        # 台词尾巴上的分隔符要剥干净（`…还挺酷的 / 场景：…` → `…还挺酷的`）
        assert scene.split("啊这 / 场景：白队｜等待出发")[0] == "啊这"
        # 单字字段（人=/事=/地=）也认，但普通台词里一个冒号不算场景
        _, short = scene.split("人=主播｜事=做饭｜地=厨房")
        assert short.filled == 3, short.line()
        _, plain = scene.split("他说：走吧，别看了")
        assert plain.is_empty(), plain.line()
        # 台词里出现"场景"两个字、后面又是正常句子：不许误切
        speech, parsed = scene.split("这个场景：人挺多的，气氛也好")
        assert speech == "这个场景：人挺多的，气氛也好", speech
        assert parsed.is_empty(), parsed.line()
        print(f"      场景示例：{scene.split(chr(10).join(['[好奇] 这是啥', '场景：人物=猫｜事件=踩键盘'] ))[1].brief()}")

    def test_chat_config():
        """chat / asr 两节配置：默认值、存盘读回、命令行覆盖都不丢。"""
        cfg = Config()
        assert cfg.chat.enabled and cfg.chat.max_chars > 0
        assert cfg.asr.enabled and cfg.asr.culture
        for spec in (cfg.hotkey.chat, cfg.hotkey.voice):
            assert parse_hotkey(spec), spec
        path = Path(tmpdir) / "chat-config.json"
        cfg.chat.max_chars = 66
        cfg.asr.seconds = 3.5
        cfg.save(path)
        again = Config.load(path)
        assert again.chat.max_chars == 66 and abs(again.asr.seconds - 3.5) < 0.01
        apply_override(again, "chat.max_chars", 12)
        assert again.chat.max_chars == 12 and again.restore_on_save["chat.max_chars"] == 66

    def test_chat_panel():
        """面板：打字回车能发出去、语音状态能切、收起时状态收干净。"""
        cfg = Config()
        panel = ChatPanel(cfg)
        sent = []
        asked = []
        panel.submitted.connect(lambda text: sent.append(text))
        panel.voiceRequested.connect(lambda: asked.append(True))
        panel.set_input("你在看啥呢？")
        panel._submit()
        assert sent == ["你在看啥呢？"], sent
        assert panel.input_text() == ""
        panel.show()
        panel.set_listening(True)
        assert panel.listening and not panel.mic.isEnabled()
        panel.set_listening(False)
        assert not panel.listening and panel.mic.isEnabled()
        panel.add_user("你好")
        panel.add_reply("诶，我在", "happy")
        # 面板里也不再写「[开心] 台词」：情绪只给这句话上色（私下分类见 pet/dialog.py）
        assert "诶，我在" in panel._rows[-1], panel._rows[-1]
        assert "[开心]" not in panel._rows[-1] and "开心" not in panel._rows[-1], panel._rows[-1]
        panel.add_notice("（测试提示）")
        panel._voice()
        assert asked == [True], asked
        panel.close_panel()
        assert not panel.isVisible() and not panel.listening
        panel.close()

    def test_asr_engine():
        """语音输入：列语音包 + 真听一次（没麦克风 / 没人说话也得正常返回，不许抛）。"""
        cfg = Config()
        cfg.asr.seconds = 1.0
        reader = SpeechReader(cfg)
        if not reader.available():
            print("      这台机器上用不了（非 Windows 或配置里关了），跳过")
            return
        engines = reader.recognizers()
        print(f"      语音包：{engines or '未列出'}")
        result = reader.listen(1.0)
        print(f"      听一次：text={result.text!r} engine={result.engine!r} 耗时 {result.elapsed:.1f}s")

    def test_worker_chat():
        """端到端：用户在面板里说一句，后台线程正面答一句（mock 就够）。"""
        cfg = Config()
        cfg.capture.interval_sec = 0.5
        cfg.capture.cooldown_sec = 0.0
        cfg.capture.region = None
        cfg.ocr.backend = "off"
        cfg.memory.path = str(Path(tmpdir) / "chat-memory.json")
        cfg.taste.path = str(Path(tmpdir) / "chat-taste.json")
        worker = AnalysisWorker(cfg)
        got = []
        loop = QEventLoop()

        def on_comment(comment):
            got.append(comment)
            if getattr(comment, "kind", "") == "chat":
                loop.quit()

        worker.comment.connect(on_comment)
        worker.start()
        QTimer.singleShot(300, lambda: worker.submit_user("你在看啥呢？"))
        QTimer.singleShot(6000, loop.quit)
        loop.exec()
        worker.stop()
        worker.wait(3000)

        chats = [c for c in got if getattr(c, "kind", "") == "chat"]
        assert chats, f"没等到回答：{[c.text for c in got]}"
        assert chats[0].text and chats[0].mood in mood.mood_names()
        # 对话不写进长期记忆：记忆里只记"它陪你看了什么"
        kept = [e.text for e in worker.memory.entries]
        assert not any(c.text in kept for c in chats), f"对话被写进记忆了：{kept}"
        # 他这句话私下被分了类（记录/分析/学习），并且换来了这一轮的接话提醒
        assert worker._turns[dialog.LEARN] == 1, dict(worker._turns)      # "你在看啥呢？"是提问
        assert worker._turn_hint == dialog.hint(dialog.LEARN), worker._turn_hint
        print(f"      他这句私下算「{dialog.label(dialog.LEARN)}」｜回答：{chats[0].text}（{chats[0].mood}）")

    # ---------- 真人聊天手感 / 反重复 / 锁定进程 ----------

    def test_humanstyle():
        """口语语料 + 相似度拦截：这两样是「对话不再单一」的底子。"""
        assert humanstyle.PATTERNS and humanstyle.SAMPLES
        assert len(humanstyle.patterns()) >= len(humanstyle.PATTERNS)
        assert len(humanstyle.samples()) >= len(humanstyle.SAMPLES)

        # 同一句换个字 → 算重复；说的事不一样 → 不算
        assert humanstyle.similarity("这操作我真看不懂", "这操作我真看不懂啊") > 0.62
        assert humanstyle.similarity("这操作我真看不懂", "哈哈这狗太逗了") < 0.2
        assert humanstyle.is_repeat("这操作我真看不懂啊", ["这操作我真看不懂"])
        assert not humanstyle.is_repeat("我服了这个裁判", ["这操作我真看不懂"])
        # 万能句（「这画面我真看不懂」）换个名词也算重复：第一次说没关系，再说就不行
        assert humanstyle.is_repeat("这画面我真看不懂", ["这操作我真看不懂"])
        assert not humanstyle.is_repeat("这操作我真看不懂", ["哈哈这狗太逗了"])

        # 每次都抽到不一样的角度/例句，提示词才不会年年一个味道
        blocks = {humanstyle.style_block() for _ in range(12)}
        assert len(blocks) > 1, "提示词每次一模一样，那还是会说同一句话"
        assert "接话" in humanstyle.chat_block()

        avoid = humanstyle.avoid_block(["这操作我真看不懂", "哈哈笑死"])
        assert "别再说了" in avoid and "这操作我真看不懂" in avoid
        assert humanstyle.avoid_block([]) == ""

        # 答非所问的头号形状：把他的问句换个字原样抛回来
        # （现场：用户问「你在干嘛呢」，气泡里回「他在干嘛呢？」——人称一换，一个字都没答）
        assert humanstyle.looks_like_question_echo("他在干嘛呢？", "你在干嘛呢")
        assert humanstyle.looks_like_question_echo("你在干嘛呢", "你在干嘛呢")
        assert not humanstyle.looks_like_question_echo("我在陪你看画面呢", "你在干嘛呢")
        assert not humanstyle.looks_like_question_echo("哈哈这狗太逗了", "你在干嘛呢")
        assert not humanstyle.looks_like_question_echo("", "你在干嘛呢")
        # 长句子一律不认（长度够就容易撞上，不敢断言是抄的）
        long_ask = "你这是要干什么呢我实在没看懂你说的是哪一件事"
        assert not humanstyle.looks_like_question_echo(long_ask, long_ask)

        # 自己往 data/chat_style.json 里加的语料也要能被读进来
        extra = humanstyle.user_corpus()
        assert set(extra) == {"dialogue", "patterns", "openers", "enders", "fillers", "banned"}
        print(
            f"      {len(humanstyle.patterns())} 种说话角度 / "
            f"{len(humanstyle.samples())} 段真人对话 / "
            f"{len(humanstyle.banned())} 个禁用书面词"
        )

    def test_persona_human():
        """提示词里得带上「像人一样说话」那一段；书面腔要被最后一道手洗掉。"""
        cfg = Config()
        system = persona.system_prompt(cfg)
        assert "朋友之间" in system or "真人" in system
        assert "语气词" in system
        assert "角度" in system
        assert "别再说了" in persona.system_prompt(cfg, avoid=["这操作我真看不懂"])
        assert "这操作我真看不懂" in persona.system_prompt(cfg, avoid=["这操作我真看不懂"])
        assert "接话的节奏" in persona.chat_system_prompt(cfg)
        assert "别再说了" in persona.chat_system_prompt(cfg, avoid=["哈哈笑死"])

        # 「他这句话是冲谁说的」：问桌宠自己 / 只是打个招呼，都得在提示词里说死
        # （现场：跟它说「你好」，它回「这集都第28集了」；问「你在干嘛呢」，它回「他在干嘛呢？」）
        assert "你自己" in persona.user_focus_note("你在干嘛呢")
        assert "抛回去" in persona.user_focus_note("你在干嘛呢")
        assert "打个招呼" in persona.user_focus_note("你好")
        assert persona.user_focus_note("这条视频好看吗") == ""
        chat_prompt = persona.chat_user_prompt(cfg, "你在干嘛呢", frames=1)
        assert "【用户跟你说】" in chat_prompt and "你在干嘛呢" in chat_prompt
        assert "回你自己" in chat_prompt, chat_prompt        # 他问的是你（桌宠），别答成「他」
        assert "打个招呼" in persona.chat_user_prompt(cfg, "你好", frames=1)
        # 主动搭话和「回他」是两码事：两条路的提示词得各自说清自己是哪一种
        chat_rules = persona.chat_system_prompt(cfg)
        assert "回他" in chat_rules and "不是你自己找话说" in chat_rules
        assert "不是「回他」" in persona.system_prompt(cfg, proactive=True)

        cleaned = persona.deformalize("总而言之，这操作堪称离谱。")
        assert "总而言之" not in cleaned and "堪称" not in cleaned
        assert not cleaned.endswith("。"), cleaned
        assert persona.deformalize("值得一提") == ""

    def test_repeat_guard():
        """防重复两道闸：worker 记着最近说过的话，mock 模式不被它卡死。"""
        cfg = Config()
        cfg.provider = "zhipu"      # 非 mock 才认真防重复
        worker = AnalysisWorker(cfg)
        assert not worker.is_repeat("这操作我真看不懂")
        worker._recent.append("这操作我真看不懂")
        assert worker.is_repeat("这操作我真看不懂啊")
        assert not worker.is_repeat("我服了这个裁判")
        worker._remember("哈哈这狗太逗了")
        assert len(worker._recent) == 2 and worker._history == ["哈哈这狗太逗了"]

        # mock（离线演示）的台词池就那几句，不能被防重复卡成哑巴
        mock_cfg = Config()
        mock_cfg.memory.path = str(Path(tmpdir) / "mem-repeat.json")
        mock_worker = AnalysisWorker(mock_cfg)
        mock_worker._recent.append("这操作我真看不懂")
        assert not mock_worker.is_repeat("这操作我真看不懂")

    def test_winfind():
        """按进程找窗口：纯 ctypes，拿到的东西得自洽。"""
        if not winfind.available():
            print("      非 Windows，跳过")
            return
        windows = winfind.list_windows()
        assert all(w.title for w in windows), windows
        assert all(w.width > 0 and w.height > 0 for w in windows), windows
        assert all(w.width >= winfind.MIN_SIDE for w in windows), windows
        named = [w for w in windows if w.process]
        assert named, "一个窗口的进程名都读不出来"
        apps = winfind.list_apps()
        names = [a.process.lower() for a in apps]
        assert names, "一个程序都没列出来"
        assert len(names) == len(set(names)), f"同一个程序列了两次：{names}"
        assert winfind.find_window("这程序肯定不存在.exe") is None
        assert winfind.find_window("") is None
        # 收起来的窗口也得列得出来，而且矩形取的是「还原之后」那块
        # （最小化时 GetClientRect 有的程序给 0：不给它换一把尺子，目标就被丢成"没找到窗口"）
        restored = [w for w in winfind.list_windows(include_minimized=True) if w.minimized]
        for info in restored:
            assert winfind.restored_rect(info.hwnd) == info.rect, ("该给还原后的矩形", info)
        if restored:
            print(f"      收起来的窗口 {len(restored)} 个，矩形取还原后的（比如 {restored[0].process}）")
        print(f"      可见窗口 {len(windows)} 个 / 程序 {len(apps)} 个，比如 {apps[0].label}")

    def test_target_process():
        """锁定进程：锁上就跟着窗口走，窗口不在就干脆别抓屏。"""
        cfg = Config()
        region = {"left": 10, "top": 20, "width": 300, "height": 200}
        cfg.capture.region = region
        worker = AnalysisWorker(cfg)
        assert worker.target_region() == region, "没锁进程时不该改变拍哪块"
        assert worker.target_state == "off"

        cfg.capture.target_process = "这程序肯定不存在.exe"
        assert worker.target_region() is None, "锁的程序不在了还在拍别的画面"
        assert worker.target_state == "missing"

        if winfind.available() and winfind.list_apps():
            picked = winfind.list_apps()[0]
            cfg.capture.target_process = picked.process
            cfg.capture.target_foreground_only = False    # 免得被"前台"这一条挡掉
            got = worker.target_region()
            assert got is None or (got["width"] > 0 and got["height"] > 0), got
            print(f"      拿 {picked.process} 试了一下：state={worker.target_state} rect={got}")

    def test_minimized_target_keeps_watching():
        """被盯的程序最小化了：监视不停——先问窗口能不能画，不行回放最后一眼（绝不拍屏幕）。"""
        cfg = Config()
        cfg.capture.target_process = "假装在看的程序.exe"
        # 记忆 / 存档都指到临时目录：测试绝不往项目里的 memory.json、data/ 写东西
        cfg.memory.path = str(Path(tmpdir) / "mem-minimized.json")
        worker = AnalysisWorker(cfg)
        region = {"left": 271, "top": 60, "width": 1370, "height": 929}
        # 最小化的窗口给的就是"还原之后"那块矩形（真机上实测过：1370×929 @271,60）
        info = winfind.WindowInfo(
            hwnd=4242,
            title="抖音-记录美好生活",
            process="假装在看的程序.exe",
            pid=99,
            left=271,
            top=60,
            width=1370,
            height=929,
            minimized=True,
        )
        real_find, real_grab, real_screen = winfind.find_window, wincap.grab, capture.grab
        grabbed, screened = [], []
        try:
            winfind.find_window = lambda *a, **k: info      # 锁定的那个窗口现在"收起来"了

            def _win_grab(hwnd, client_only=True, allow_minimized=False):
                grabbed.append((hwnd, client_only, allow_minimized))
                return None                                 # 实测：最小化时它画出来的是全黑

            def _screen_grab(reg):
                screened.append(reg)     # 那块地方此刻站着的是别的窗口：一帧都不许拍
                raise AssertionError("目标最小化时去拍屏幕了")

            wincap.grab = _win_grab
            capture.grab = _screen_grab

            got = worker.target_region()
            assert got == region, ("最小化时也得给一块矩形（还原后的），不然监视就断了", got)
            assert worker.target_state == "minimized"

            # 手上还没有"最后一眼"：这一轮真没得看——但也不许去拍屏幕
            assert worker._grab(got) is None
            assert worker._frame_stale, "回放的那一帧得标出来，主循环靠它跳过吐槽 / 吸收"
            assert not screened and grabbed, (screened, grabbed)
            assert grabbed[-1][2] is True, "最小化那条路要允许抓一把（万一这程序画得出来）"
            assert grabbed[-1][1] is False, "最小化时窗口矩形是还原尺寸，别拿它裁标题栏"

            # 手上有一张真画面（收起来之前抓到的）：回放它——它还"记得你看到哪儿了"
            shot = Image.new("RGB", (64, 36), (10, 20, 30))
            worker._last_shot = shot
            worker._min_tried_at = 0.0        # 别被"隔 MIN_GRAB_RETRY 秒再问一次"挡住
            assert worker._grab(got) is shot
            assert worker._frame_stale and not screened

            # 万一这程序最小化也画得出来（老式 GDI 那种）：拿到的就是真画面，照看
            fresh = Image.new("RGB", (80, 45), (200, 30, 30))
            wincap.grab = lambda hwnd, client_only=True, allow_minimized=False: fresh
            worker._min_tried_at = 0.0
            assert worker._grab(got) is fresh
            assert not worker._frame_stale, "窗口画得出来就不算回放"
            assert worker._last_shot is fresh

            # 提醒只说一遍（收起来的这段时间别 90 秒念一次）
            said = []
            worker.notice.connect(said.append)
            worker._min_noted = False
            worker._note_target_missing()
            worker._note_target_missing()
            assert len(said) == 1 and "最小化" in said[0], said
            print(f"      收起来时的说法：{said[0]}")
        finally:
            winfind.find_window, wincap.grab, capture.grab = real_find, real_grab, real_screen

    def test_window_capture():
        """直接抓窗口画面（被别的窗口盖住也能抓）+ 连拍拼图：这是"看懂视频"的地基。"""
        tiles = [Image.new("RGB", (160, 90), (i * 70, 40, 200 - i * 60)) for i in range(3)]
        sheet = capture.sequence_sheet(tiles, total_width=480, gap=6)
        assert sheet is not None and sheet.width > max(t.width for t in tiles), "分镜图没拼起来"
        assert not capture.is_blank(sheet), "分镜图被判成空白"
        assert capture.sequence_sheet([tiles[0]]) is tiles[0], "只有一张时不该拼"
        assert capture.is_blank(Image.new("RGB", (64, 64), (0, 0, 0))), "全黑图没被认出来"

        # 「只剩标题栏几个按钮」的假画面：最小化时 PrintWindow 交的就是这个
        # （is_blank 看"最亮-最暗差"，被那几个亮块骗过去，所以要另加一道）
        only_buttons = Image.new("RGB", (200, 120), (0, 0, 0))
        for x in range(6, 60):
            for y in range(6, 22):
                only_buttons.putpixel((x, y), (255, 255, 255))
        assert not capture.is_blank(only_buttons) and not wincap.is_blank(only_buttons), (
            "这张图本来就骗得过 is_blank（所以才要另加一道）"
        )
        assert wincap.looks_unpainted(only_buttons), "剩下的全是黑：该认成「没画出来」"
        assert wincap.looks_unpainted(Image.new("RGB", (64, 64), (0, 0, 0)))
        assert not wincap.looks_unpainted(Image.new("RGB", (200, 120), (90, 120, 200))), (
            "正常画面被误判成没画出来"
        )
        assert not wincap.looks_unpainted(Image.new("RGB", (200, 120), (20, 20, 24))), (
            "暗场景（但确实有内容）不算没画"
        )

        if not wincap.available():
            print("      非 Windows，跳过窗口直抓")
            return
        windows = [w for w in winfind.list_windows() if w.width > 320 and not w.minimized]
        if not windows:
            print("      没有可抓的窗口，跳过实测")
            return
        info = windows[0]
        shot = wincap.grab(info.hwnd, client_only=True)
        assert shot is None or not wincap.is_blank(shot), "抓到的窗口画面是空白"
        print(f"      抓 {info.process} 的窗口（{info.width}×{info.height}）→ {None if shot is None else shot.size}")

    def test_noise_guard():
        """模型偶尔把提示词块头 / 单字段场景当台词抄回来：得在解析层就堵掉。"""
        speech, parsed = scene.split("【你刚才说过】\n这节目挺有意思的\n场景：人物=选手｜事件=抽签")
        assert speech == "这节目挺有意思的", speech
        assert parsed.who == "选手" and parsed.what == "抽签", parsed

        speech, parsed = scene.split("时间=白天")
        assert speech == "" and parsed.when == "白天", (speech, parsed)

        # 模型把**格式骨架**抄回来当第二行（现场就是这么漏进气泡的）：
        # 它没有内容，但更不该被念出来——当场景行丢掉，台词照样留着。
        assert scene.looks_like_scene("人物｜事件｜时间｜地点｜原因")
        speech, parsed = scene.split("[开心] 这画面还挺酷的\n人物｜事件｜时间｜地点｜原因")
        assert speech == "[开心] 这画面还挺酷的", speech     # 情绪标签留给 mood.split_mood 剥
        assert "人物" not in speech and "原因" not in speech, speech
        assert parsed.is_empty(), parsed.line()

        assert persona.clean_reply("【现在】\n嗯这波稳了") == "嗯这波稳了"
        assert persona.clean_reply("【你刚才说过】") == ""
        assert persona.clean_reply("【你现在】") == ""
        assert persona.clean_reply("「哈哈这狗太逗了」") == "哈哈这狗太逗了"
        assert scene.looks_like_scene("人物=主播｜事件=抽卡")
        assert not scene.looks_like_scene("人：来来来，走了走了")

        # 小模型把提示词整行抄回来当台词（「如果画面基本还是同一件事，就别重复…」）
        prompt = (
            "【现在】\n"
            "画面上的文字（可能是弹幕/字幕/按钮，噪声多，挑有用的当素材，不要照念）：\n"
            "听到没\n"
            "如果画面基本还是同一件事，就别重复刚才的说法——换个角度，或者干脆沉默"
        )
        assert humanstyle.looks_like_echo(
            "如果画面基本还是同一件事，就别重复刚才的说法——换个角度，或者干脆沉默", (prompt,)
        )
        assert humanstyle.looks_like_echo(
            "画面上的文字（可能是弹幕/字幕/按钮，噪声多，挑有用的当素材，不要照念）：", (prompt,)
        )
        assert not humanstyle.looks_like_echo("这节目还挺有意思的", (prompt,))
        assert not humanstyle.looks_like_echo("哈哈", (prompt,))

    def test_client_parse():
        """模型文本 → 台词这条解析链得真跑一遍：曾因为跨模块函数名写错，整条分析线程被异常带走。"""
        cfg = Config()
        cfg.provider = "zhipu"          # 非 mock 才走真实解析（这里不联网，只喂文本）
        client = VisionClient(cfg)
        # 抄的这行直接从真提示词里取（写死字符串的话，改了提示词这测试就白测了）
        block = persona._scene_block("人物=两位女性｜事件=吃面")
        echo = [row for row in block[0].split("\n") if len(humanstyle.normalize(row)) >= 8][-1]
        client._echo_sources = (block[0],)
        assert client._to_comment(echo) is None              # 抄提示词（这里抄的是背景块）的那句要丢掉
        assert client._to_comment("【你刚才说过】") is None   # 只有块头没台词 = 这轮沉默
        assert client._to_comment("[沉默]") is None
        comment = client._to_comment("这赛道修得也太糙了\n场景：人物=选手｜事件=比赛")
        assert comment is not None and comment.text == "这赛道修得也太糙了", comment
        assert comment.scene is not None and comment.scene.who == "选手", comment.scene

    def test_narration_guard():
        """「某某和某某站在…前，似乎是在参与某个环节」这种解说词，一次都不该进气泡。

        现场那一句是真的（用户截图）：气泡里贴了一段镜头说明书，看着就像程序坏了。
        要的是零散的想法，不是把画面复述完整。
        """
        live = "范丞丞和郑恺站在两个显示时间为21:15的闹钟前，似乎是在参与某个环节，他们的表"
        assert humanstyle.looks_like_narration(live), "现场那句解说词没认出来"
        # 换个说法也一样：场面描述（谁和谁 + 在干嘛）也是解说
        assert humanstyle.looks_like_narration("王小明和李雷两个人一起走进教室，手里都拿着书")
        # 结尾落在名词化总结上也算
        assert humanstyle.looks_like_narration("他俩在屋里绕来绕去，我猜不透这是个什么环节")
        # 真人聊天那几句一个都不能误伤（短句永远安全）
        for chat in ("哈哈这波稳了", "我服了，他认真的？", "就这？", "诶这球才15？",
                     "他俩站那儿半天了，也不知道在等谁", "你别说，还挺好看的"):
            assert not humanstyle.looks_like_narration(chat), chat

        # 解析链上：这种句子要被拦成"这轮没说话"，并且把原因留给 worker
        cfg = Config()
        cfg.provider = "zhipu"          # 非 mock 才走真实解析（这里不联网，只喂文本）
        client = VisionClient(cfg)
        assert client._to_comment(live) is None
        assert client.last_drop == "narration", client.last_drop
        ok = client._to_comment("这俩人怎么还穿着睡衣")
        assert ok is not None and ok.text == "这俩人怎么还穿着睡衣", ok
        assert client.last_drop == "", client.last_drop        # 正常台词照过，别把闸做成哑巴

    def test_narration_retry():
        """被拦下之后不能就这么哑掉：worker 要**当场**再要一句，而且不许把请求翻倍。"""
        cfg = Config()
        cfg.memory.path = str(Path(tmpdir) / "mem-narr.json")
        cfg.taste.path = str(Path(tmpdir) / "taste-narr.json")
        worker = AnalysisWorker(cfg)
        assert AnalysisWorker.NARRATION_RETRY_GAP > 0
        assert worker._last_narration_at == 0.0

        asked = []

        def fake_think(image, small, digest, avoid=(), insist=False):
            asked.append(insist)
            return mood.Comment("这俩人怎么还穿着睡衣", "curious")

        worker._think = fake_think
        got = worker._narration_retry(None, None, 0)
        assert asked == [True], f"补要那轮必须走 insist（只回一句话）: {asked}"
        assert got is not None and got.text == "这俩人怎么还穿着睡衣", got
        # 冷却之内不再要（不然每帧都翻倍花接口钱）
        assert worker._narration_retry(None, None, 0) is None
        assert asked == [True], asked

        # 提示词那一侧：解说词这条禁令得同时出现在陪看相和补说那一轮
        system = persona.system_prompt(cfg)
        assert "不许写解说词" in system and "不许推测" in system, "系统提示词没禁解说词"
        insisted = persona.user_prompt(cfg, insist=True)
        assert "不许描述画面" in insisted and "只回一句话" in insisted, insisted
        assert "不许描述画面" not in persona.user_prompt(cfg), "平时那轮不该每帧都在催"

    def test_screen_caption_guard():
        """「电脑屏幕截图，包含…编辑器界面…」这种机器识图描述，一次都不该进气泡。

        现场那两句是真的（用户截图）：它在念"我收到了怎样一张图"——机器识别的过程，
        不是人该说的话。屏幕边上要的是个陪看的朋友，不是识图日志：认出来就整句丢掉，
        让 worker 当场再要一句（见 worker 的 last_drop 白名单）。
        """
        live = "电脑屏幕截图，包含Visual Studio Code编辑器界面和一些中文文本"
        live2 = "电脑桌面截图，Visual Studio Code和一些文件资源管理器中的物品…"
        assert humanstyle.looks_like_screen_caption(live), "现场那句识图描述没认出来"
        assert humanstyle.looks_like_screen_caption(live2), "现场那句桌面截图描述没认出来"
        # 换个说法也一样：屏幕上显示了什么 / 这是一张截图
        assert humanstyle.looks_like_screen_caption("屏幕上显示了一个代码编辑器窗口")
        assert humanstyle.looks_like_screen_caption("这是一张电脑截图，上面是代码")
        # 真人聊天那几句一个都不能误伤（短句、问句、反应词永远安全）
        for chat in ("哈哈这波稳了", "你截图给我看看", "就这？", "屏幕看得我眼睛疼",
                     "诶你刚才那个操作再来一遍", "不是吧哥们"):
            assert not humanstyle.looks_like_screen_caption(chat), chat

        # 解析链上：这种句子要被拦成"这轮没说话"，并且把原因留给 worker
        cfg = Config()
        cfg.provider = "zhipu"          # 非 mock 才走真实解析（这里不联网，只喂文本）
        client = VisionClient(cfg)
        assert client._to_comment(live) is None
        assert client.last_drop == "screen", client.last_drop
        ok = client._to_comment("这代码写得还挺认真")
        assert ok is not None and ok.text == "这代码写得还挺认真", ok
        assert client.last_drop == "", client.last_drop        # 正常台词照过，别把闸做成哑巴

        # 提示词那一侧也按住：别念截图
        assert "别提截图" in persona.system_prompt(cfg), "系统提示词没按住\"念截图\""

    def test_silence_contract():
        """输出格式的"沉默"契约：第一行永远在，要沉默就写 [沉默]——不许只交场景行。

        这是「它盯着屏幕一句话不说」的根因：以前提示词两头打架（格式要求两行 + 沉默时说
        "不要写场景行"），小模型干脆每轮只交第二行场景，日志里就是刷不完的
        「模型只回了场景没给台词」。
        """
        block = persona.format_block(True)
        assert "写 [沉默]" in block and "不允许空着" in block, block
        assert "只有场景行、没有第一行" in block, block
        # 第二行降级成"可有可无"：模型决定沉默时就不会再拿它交差（这是只交场景行的动机）
        assert "可有可无" in block, block
        # 对话那轮（allow_silence=False）本来就不许沉默，不能再提 [沉默] 让模型误解
        assert "[沉默]" not in persona.format_block(False)
        # 场景行照旧在（不能因为改契约就把 5W1H 弄丢），而且要写清"按什么顺序写"；
        # 同时必须明说这几个字段名不许原样抄进去——现场就是它被抄进了气泡
        assert "场景：" in block and "人物、事件、时间、地点、原因" in block, block
        assert "不许原样写进去" in block, block
        assert "场景：" in persona.format_block(False)
        # 关键：格式块里不能再有"现成的例句"——小模型会原样抄回来（抄例句当台词、
        # 抄示例场景当自己的场景：屏幕上明明是英雄联盟，它报的是"抽卡直播间"）
        for text in (block, persona.format_block(False)):
            assert "黑衣男主播" not in text, text
            assert "这操作我真看不懂" not in text, text

    def test_scene_block_is_background():
        """上一眼场景只留一句提醒，**5W1H 原文不再回喂**（回喂就会被整段抄回来当第二行）。"""
        rows = persona._scene_block("人物=两位女性｜事件=吃面")
        assert rows, "有场景时应该给一句提醒"
        block = rows[0]
        assert "提醒" in block and "别重复" in block, block
        assert "人物=" not in block and "两位女性" not in block, block   # 事实不再进提示词
        assert persona._scene_block("") == []
        assert "提醒" not in persona.user_prompt(Config(), scene="")
        assert "提醒" in persona.user_prompt(Config(), scene="人物=选手")

    def test_insist_prompt():
        """补说提示词：只在 insist=True 那一轮出现，而且把输出压成"只回一句话"。"""
        cfg = Config()
        plain = persona.user_prompt(cfg)
        urged = persona.user_prompt(cfg, insist=True)
        assert "只回一句话" in urged and "不要写场景行" in urged, urged
        assert "只回一句话" not in plain, plain
        # 系统提示词那一侧也要跟着改口（两边说法不一致时，小模型会挑松的那个执行）
        assert "只回一句话" in persona.system_prompt(cfg, insist=True)
        assert "只回一句话" not in persona.system_prompt(cfg)

    def test_insist_chain():
        """补说链路真的能到模型：describe(insist=True) 发出去的提示词里带着那句"只回一句话"。"""
        cfg = Config()
        cfg.provider = "zhipu"
        cfg.api_key = "dummy-for-parse-only"     # 只过 ready 这关，不发请求
        client = VisionClient(cfg)
        state = {"payload": None}

        def fake_post(payload, timeout=0.0):
            state["payload"] = payload
            client._echo_sources = VisionClient._payload_texts(payload)
            return "就这？"

        client._post = fake_post
        said = client.describe(frame, insist=True)      # 单行模式：只有台词，没有场景行
        assert said is not None and said.text == "就这？", said
        sent = "\n".join(str(text) for text in VisionClient._payload_texts(state["payload"]))
        assert "只回一句话" in sent
        # 平时那一轮不能带这句（不然每轮都在催，模型会开始硬凑话）
        client.describe(frame)
        sent = "\n".join(str(text) for text in VisionClient._payload_texts(state["payload"]))
        assert "只回一句话" not in sent

    def test_scene_reset_on_new_video():
        """换了视频要清掉"上一眼场景"：否则旧场景会被当成这一支的画面回喂回去（现场日志里就是这么错的）。"""
        cfg = Config()
        cfg.memory.path = str(Path(tmpdir) / "mem-scene.json")
        cfg.taste.path = str(Path(tmpdir) / "taste-scene.json")
        worker = AnalysisWorker(cfg)
        worker._last_scene = "人物=玩家｜事件=打野"
        small = capture.shrink(frame, 320)
        worker._absorb(frame, capture.dhash(small))
        assert worker._last_scene == "", worker._last_scene

    def test_insist_guard():
        """"补说"的闸门：安静够了才补，而且每 gap 秒最多补一次（不然沉默期请求量翻倍）。

        把 `capture.insist_after_quiet_sec` 调成 0 = 关掉补说。
        """
        cfg = Config()
        cfg.memory.path = str(Path(tmpdir) / "mem-insist.json")
        cfg.taste.path = str(Path(tmpdir) / "taste-insist.json")
        worker = AnalysisWorker(cfg)
        now = time.monotonic()

        worker._last_spoke_at = now            # 刚说过：不补
        worker._last_insist_at = 0.0
        assert worker._should_insist() is False

        worker._last_spoke_at = now - 100.0    # 安静很久了：补，而且这一段只补这一次
        worker._last_insist_at = 0.0
        assert worker._should_insist() is True
        assert worker._should_insist() is False

        worker._last_insist_at = now - 100.0   # 又安静过一轮：还能补
        worker._last_spoke_at = now - 100.0
        assert worker._should_insist() is True

        worker.cfg.capture.insist_after_quiet_sec = 0.0   # 关掉
        worker._last_spoke_at = now - 100.0
        worker._last_insist_at = 0.0
        assert worker._should_insist() is False

    def test_scene_only_and_quiet():
        """两种"没说话"都不许把场景行当台词，也不许抛异常（白说一轮 / 它自己选择沉默）。"""
        cfg = Config()
        cfg.provider = "zhipu"
        cfg.api_key = "dummy-for-parse-only"
        client = VisionClient(cfg)
        client._echo_sources = ()
        assert client._to_comment("场景：人物=选手｜事件=比赛") is None         # 只交场景行：白说一轮
        assert client._to_comment("[沉默]\n场景：人物=选手｜事件=比赛") is None  # 合规沉默：它自己的选择
        keep = client._to_comment("[无语] 这也能卡\n场景：人物=选手")
        assert keep is not None and keep.text == "这也能卡", keep

    def test_client_remote_paths():
        """真实 provider 的四条链路（describe / reply / nudge / absorb）：只把 `_post` 换成假响应。

        非 mock 的包装函数和 `_remote_*` 之间的参数必须对得上——曾经 `reply()` 多传了一个 frames，
        结果"打字互动"每次都抛 TypeError（mock 冒烟测试走的是 `_mock_reply`，看不到这个问题）。
        """
        cfg = Config()
        cfg.provider = "zhipu"
        cfg.api_key = "dummy-for-parse-only"      # 只过 ready 这一关，不发任何请求
        client = VisionClient(cfg)
        state = {"raw": "", "payload": None}

        def fake_post(payload, timeout=0.0):
            state["payload"] = payload
            client._echo_sources = VisionClient._payload_texts(payload)
            return state["raw"]

        client._post = fake_post

        state["raw"] = "这赛道修得也太糙了\n场景：人物=选手｜事件=比赛"
        comment = client.describe(frame, watch="(看片笔记)", frames=3)
        assert comment is not None and comment.text == "这赛道修得也太糙了", comment
        assert comment.scene is not None and comment.scene.who == "选手", comment.scene

        # 模型把提示词里的整行抄回来：动态挑一行真的在这轮提示词里的（比写死字符串更能发现问题）
        sent = "\n".join(str(text) for text in VisionClient._payload_texts(state["payload"]))
        echoed = next(line.strip() for line in sent.splitlines() if len(humanstyle.normalize(line)) >= 8)
        state["raw"] = echoed
        assert client.describe(frame) is None, echoed

        state["raw"] = "[好奇] 你这段卡了三回了，笑死"
        said = client.reply(
            "你在看啥", image=frame, frames=2, dialog_hint=dialog.hint(dialog.LEARN)
        )
        assert said is not None and said.text == "你这段卡了三回了，笑死", said
        assert said.mood == "curious" and said.kind == "chat", said
        # 私下分类换来的接话提醒真的进了这一轮的提示词（问句 → 先把答案给清楚）
        chat_sent = "\n".join(str(text) for text in VisionClient._payload_texts(state["payload"]))
        assert dialog.hint(dialog.LEARN) in chat_sent, "对话类型的提醒没进提示词"

        nudged = client.nudge(proactive.STILL_SCREEN, image=frame, note="画面定住了")
        assert nudged is not None and nudged.text, nudged
        assert nudged.kind == f"proactive:{proactive.STILL_SCREEN}", nudged.kind

        state["raw"] = (
            "主题：奔跑吧兄弟 泥地赛道特辑\n"
            "内容：嘉宾在沙盘赛道上比小车，泥坑一个接一个\n"
            "人物：浙江卫视的主持人和嘉宾\n"
            "看点：谁的车会卡在泥里"
        )
        note = client.absorb(frame, ocr_text="奔跑吧兄弟")
        assert note is not None and note.what, note
        assert "奔跑吧兄弟" in note.title, note

        # 两个"必须开腔"的场合：刷到新视频 / 他刚点赞收藏关注
        state["raw"] = "[好奇] 新的一支，这个我盯上了"
        opened = client.spotlight("new_video", note=note.line())
        assert opened is not None and opened.text == "新的一支，这个我盯上了", opened
        assert opened.kind == "spotlight:new_video", opened.kind
        state["raw"] = "[吐槽] 你还真点了赞"
        reacted = client.spotlight("action", action="点赞")
        assert reacted is not None and reacted.kind == "spotlight:action", reacted

    def test_taste():
        """口味档案：分类归一、互动解析、兴趣打分、画像生成、存盘读回。"""
        assert taste.normalize_genre("游戏直播") == "游戏"
        assert taste.normalize_genre("KPL 电竞比赛") == "游戏"
        assert taste.normalize_genre("浙江卫视综艺") == "影视综艺"
        assert taste.normalize_genre("完全看不懂的东西") == "其他"

        acts = taste.parse_actions("点赞=是｜收藏=否｜关注=是｜评论=是")
        assert acts == {"like": True, "fav": False, "follow": True, "comment": True}, acts
        assert taste.actions_from_text("已关注｜已收藏") == {"follow": True, "fav": True}
        assert taste.actions_from_text("什么线索都没有") == {}

        cfg = Config()
        cfg.taste.path = str(Path(tempfile.mkdtemp(prefix="taste-probe-")) / "taste.json")
        log = taste.TasteLog(cfg)
        assert log.records == [], (log.path, len(log.records))

        hot = log.start_video(watchlog.WatchNote(genre="游戏直播", title="王者荣耀集锦", keywords=["王者荣耀"]), at=1000.0)
        log.update(hot, actions={"like": True, "fav": True})
        log.finish(hot, 40.0, ended=True)
        assert hot.interest() >= 9.0, hot.interest()          # 点赞3 + 收藏4 + 看完2 + 看得久1

        mid = log.start_video(watchlog.WatchNote(genre="游戏", title="英雄联盟直播", keywords=["英雄联盟"]), at=1001.0)
        log.update(mid, actions={"follow": True})
        log.finish(mid, 30.0, ended=False)

        cold = log.start_video(watchlog.WatchNote(genre="搞笑", title="冷笑话合集"), at=1002.0)
        log.finish(cold, 2.0, ended=False)
        assert cold.interest() < 0, cold.interest()           # 秒划走扣分
        colder = log.start_video(watchlog.WatchNote(genre="搞笑", title="冷笑话合集2"), at=1003.0)
        log.finish(colder, 3.0, ended=False)

        tone, average, count = log.tone()
        assert tone == "warm" and count == 4, (tone, average, count)
        assert log.nudge_scale() < 1.0
        assert [row[0] for row in log.liked_genres()] == ["游戏"], log.liked_genres()
        assert [row[0] for row in log.cold_genres()] == ["搞笑"], log.cold_genres()
        assert "王者荣耀" in log.hot_topics()
        profile = log.profile_block()
        assert "游戏" in profile and "该用什么劲儿" in profile
        print(f"      {average:+.1f} 分 / 劲头系数 {log.nudge_scale()}")

        log.save(force=True)
        replayed = taste.TasteLog(cfg)
        assert len(replayed.records) == 4
        assert replayed.records[0].actions.get("like") is True
        assert replayed.records[0].watch_sec == 40.0

    def test_taste_wiring():
        """worker 把口味档案接上了：看到点赞就记进档案，并当场接一句（mock 不联网）。"""
        cfg = Config()
        cfg.taste.path = str(Path(tmpdir) / "taste-worker.json")
        cfg.memory.path = str(Path(tmpdir) / "mem-worker.json")
        worker = AnalysisWorker(cfg)
        note = watchlog.WatchNote(genre="游戏直播", title="王者荣耀直播", what="主播在打排位", point="连跪三把")
        worker._video_record = worker.taste.start_video(note, at=1000.0)
        worker._video_started_at = time.monotonic() - 30.0

        got = []
        worker.comment.connect(lambda c: got.append(c))
        worker._apply_actions({"like": True}, note=note.line())
        assert worker._actions.get("like") is True
        assert worker._video_record.actions.get("like") is True
        assert got and got[0].kind == "spotlight:action", got

        worker._finish_video()
        assert worker._video_record is None
        assert worker.taste.records[0].watch_sec >= 30.0, worker.taste.records[0]

    def test_taste_scale():
        """口味画像真的会影响搭话频率：劲头系数 >1 把冷却拉长，<1 缩短。"""
        cfg = Config()
        cfg.proactive.cooldown_sec = 100.0
        policy = ProactivePolicy(cfg)
        now = 1000.0
        policy._last_nudge = now          # 假装刚刚搭过一次话

        policy.scale = 10.0               # 他最近没什么兴致 → 冷却 ×10
        assert policy._local(proactive.LONG_QUIET, now + 200.0) is None
        policy.scale = 0.5                # 他看得起劲 → 冷却砍半
        assert policy._local(proactive.LONG_QUIET, now + 200.0) is not None

    def test_frame_safe_area():
        """形象得待在圆里：贴满画布的自定义帧会被自动裁边 + 缩到圆内安全区。

        现场翻车的样子就是这张图：人物画满画布 → 圆遮罩把鞋和身体两侧切成直边。
        """
        folder = Path(tmpdir) / "pet-fit"
        folder.mkdir(exist_ok=True)
        for index in range(2):
            pm = QPixmap(256, 256)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            # 故意画满整张画布、一点白边都不留；第二帧整体往下挪，模拟帧里的动作位移
            painter.fillRect(QRect(0, index * 8, 256, 248), QColor(255, 210, 60, 255))
            painter.end()
            pm.save(str(folder / f"idle_{index:02d}.png"))

        fitted = PetRenderer(folder)
        assert fitted.uses_frames, "临时帧没装上"
        canvas = fitted.frames_idle[0].width()
        boxes = [QRegion(pm.mask()).boundingRect() for pm in fitted.frames_idle]
        for box in boxes:
            corner = math.hypot(box.width() / 2.0, box.height() / 2.0)
            assert corner <= canvas / 2.0 + 0.5, f"内容的角跑到圆外了：{box}"
        span = max(boxes[0].width(), boxes[0].height()) / float(canvas)
        assert span <= 0.85, f"形象还是太满（占画布 {span:.2f}），圆一定会切到"
        assert boxes[0].top() != boxes[1].top(), "两帧被裁到了同一个位置，帧里的动作被归一化掉了"

        # 真按挂件尺寸画一遍：圆外的像素必须一个都没有
        side = 132
        out = QPixmap(side, side)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        fitted.draw(painter, QRectF(2, 2, side - 4, side - 4), 0.0)
        painter.end()
        image = out.toImage()
        radius = (side - 2) / 2.0
        outside = sum(
            1
            for x in range(side)
            for y in range(side)
            if image.pixelColor(x, y).alpha() > 8
            and math.hypot(x - side / 2.0 + 0.5, y - side / 2.0 + 0.5) > radius
        )
        assert outside == 0, f"圆遮罩外还有 {outside} 个像素：形象会被切出直边"
        print(f"      满画布的图被缩到 {span:.2f}，圆外像素 0 个")

    def test_act_frames_overflow():
        """动作帧不许把人缩小：它们不参与「统一内容框」，冒出去的允许被圆切掉。

        现场：动作 / 日常状态帧一多，蹦一下、伸个懒腰会把内容框撑大，
        于是"站着不动"的时候人也跟着变小。现在只让基础帧决定框。
        """
        def build(name, with_acts):
            folder = Path(tmpdir) / name
            folder.mkdir(exist_ok=True)
            for index in range(2):
                pm = QPixmap(256, 256)
                pm.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pm)
                painter.fillRect(QRect(88, 88, 80, 80), QColor(90, 180, 240, 255))
                painter.end()
                assert pm.save(str(folder / f"idle_{index:02d}.png"))
            if with_acts:
                for index in range(2):
                    pm = QPixmap(256, 256)
                    pm.fill(Qt.GlobalColor.transparent)
                    painter = QPainter(pm)
                    # 动作帧故意画满整张画布：蹦到天上 / 伸懒腰伸到边上
                    painter.fillRect(QRect(0, 0, 256, 256), QColor(240, 180, 90, 255))
                    painter.end()
                    assert pm.save(str(folder / f"hug_{index:02d}.png"))
            return PetRenderer(folder)

        def content(renderer, act=""):
            frames = renderer.frames_acts[act] if act else renderer.frames_idle
            return frames[0].width(), QRegion(frames[0].mask()).boundingRect()

        plain = build("fit-plain", False)
        acted = build("fit-act", True)
        canvas, plain_box = content(plain)
        same_canvas, acted_box = content(acted)
        assert canvas == same_canvas, (canvas, same_canvas)
        # 基础帧两套得一模一样：动作帧没资格把框撑大（旧做法下这块会缩到 1/3 都不到）
        assert abs(plain_box.width() - acted_box.width()) <= 2, (plain_box, acted_box)
        assert acted_box.width() < canvas * 0.8, acted_box
        # 动作帧：允许出框，所以它一直顶到画布边上；跟基础帧同一份缩放
        _, act_box = content(acted, "hug")
        assert act_box.width() >= canvas - 2 and act_box.height() >= canvas - 2, act_box
        print(f"      动作帧不参与内容框：基础帧 {acted_box.width()}px、动作帧顶到 {act_box.width()}px"
              f"（画布 {canvas}px）")

    def test_dedup_all_exits():
        """「别连着说同一句」要管住每个出口：吐槽、补说、本地搭话、兜底台词都得过闸。"""
        cfg = Config()
        cfg.provider = "zhipu"        # 非 mock 才认真防重复
        cfg.memory.path = str(Path(tmpdir) / "mem-dedup.json")
        cfg.taste.path = str(Path(tmpdir) / "taste-dedup.json")
        worker = AnalysisWorker(cfg)

        line = mood.Comment("这操作我真看不懂", "speechless")
        assert worker._dedup(line, "吐槽") is line
        worker._recent.append("这操作我真看不懂")
        assert worker._dedup(line, "吐槽") is None, "刚说过的台词又被放出去了"

        spoken = []
        worker.comment.connect(spoken.append)

        # 本地台词是写死的（"哟，回来了"），跨场次说话很容易撞上刚吐槽过的那句
        worker._recent.append("哟，回来了")
        worker._emit_nudge(
            proactive.Nudge(proactive.WELCOME_BACK, line="哟，回来了？", mood="curious"),
            time.monotonic(),
        )
        assert not spoken, [c.text for c in spoken]

        # 兜底台词从旁边溜过去过：模型不开口时它就会把同一句再说一遍
        worker._nudge_via_model = lambda nudge, avoid=(): None    # 模型这次没开口 → 走兜底
        worker._recent.append(proactive.FALLBACK_LINES[proactive.LONG_QUIET].text)
        worker._emit_nudge(proactive.Nudge(proactive.LONG_QUIET), time.monotonic())
        assert not spoken, [c.text for c in spoken]

        # 换一句没说过的话：照常说，别把出口堵死
        worker._emit_nudge(
            proactive.Nudge(proactive.WELCOME_BACK, line="回来啦，继续看", mood="happy"),
            time.monotonic(),
        )
        assert [c.text for c in spoken] == ["回来啦，继续看"], spoken

        # mock（离线演示）的台词池就那几句，不能被防重复卡成哑巴
        demo_cfg = Config()
        demo_cfg.memory.path = str(Path(tmpdir) / "mem-demo.json")
        demo = AnalysisWorker(demo_cfg)
        demo._recent.append("这操作我真看不懂")
        assert demo._dedup(mood.Comment("这操作我真看不懂", "speechless"), "吐槽") is not None

    def test_friends():
        """好友陪伴：让两个 VisitHub 互相当"对方那台电脑"，把串门整条路真走一遍。

        这一条是**真的开端口、真的发 HTTP**（都走 127.0.0.1，不碰局域网），
        所以它同时盯着几件最容易出事的地方：

        * 一个进程里开着两个门房时，口令必须各认各的（服务端不能把 peer 挂成全局）；
        * 对方一点头就招呼的那句话，比敲门人下一次来问（KNOCK_POLL）更早到，
          必须接得住，不能当成"不知道这话是谁说的"丢掉；
        * 两只宠物各在自己那台机器上生成台词，谁的模型就归谁调；
        * 客人窗口 / 好友面板真的能建、能画、能收。
        """
        def make(tag, nick, owner, auto=False):
            cfg = Config()
            cfg.provider = "mock"
            cfg.persona.name = nick
            cfg.friends.enabled = True
            cfg.friends.port = 0                 # 系统分个空端口：跑测试不跟本机那只见面
            cfg.friends.listen = "127.0.0.1"
            cfg.friends.owner = owner
            cfg.friends.auto_accept = auto
            cfg.friends.pet_chat_rounds = 2
            cfg.friends.pet_chat_gap_sec = 0.2
            cfg.friends.visit_seconds = 60.0
            cfg.friends.book = str(Path(tmpdir) / f"friend-{tag}.json")
            cfg.friends.guest_dir = str(Path(tmpdir) / f"guests-{tag}")
            cfg.friends.log = str(Path(tmpdir) / f"visits-{tag}.json")
            book = friends.open_book(cfg)
            return cfg, book, friends.VisitHub(cfg, book, ASSETS_DIR / "pet")

        def pump(until, timeout=20.0):
            """转事件循环直到条件成立：后台线程的结果要回到 GUI 线程才生效。"""
            end = time.time() + timeout
            while time.time() < end:
                app.processEvents()
                if until():
                    return True
                time.sleep(0.02)
            app.processEvents()
            return until()

        def watch(signal, bucket):
            signal.connect(lambda *args: bucket.append(args))

        cfg_a, book_a, hub_a = make("a", "黄豆", "小明")
        cfg_b, book_b, hub_b = make("b", "芝麻", "小红")
        seen = {name: [] for name in (
            "a_line", "a_said", "a_asked", "a_status", "a_away",
            "b_line", "b_said", "b_left", "b_ask_in", "b_arrived",
        )}
        watch(hub_a.my_line, seen["a_line"])
        watch(hub_a.guest_said, seen["a_said"])
        watch(hub_a.asked, seen["a_asked"])
        watch(hub_a.status, seen["a_status"])
        watch(hub_a.away, seen["a_away"])
        watch(hub_b.my_line, seen["b_line"])
        watch(hub_b.guest_said, seen["b_said"])
        watch(hub_b.guest_left, seen["b_left"])
        watch(hub_b.ask_in, seen["b_ask_in"])
        watch(hub_b.arrived, seen["b_arrived"])

        try:
            assert hub_a.start() and hub_b.start(), (hub_a.server.error, hub_b.server.error)
            assert hub_a.port() > 0 and hub_b.port() > 0
            assert book_a.token and book_a.token != book_b.token

            # ---- 名片：发一张、收一张 ----
            card = book_b.card()
            assert card.startswith(net.CARD_PREFIX) and net.parse_card(card)["pet_id"] == book_b.pet_id
            assert net.parse_card("今天天气不错") is None
            assert "已记下" in hub_a.remember_card(card)
            row_b = book_a.get(book_b.pet_id) or {}
            assert row_b.get("token") == book_b.token, row_b
            assert friends.FriendBook.label(row_b) == "小红家的 芝麻", row_b
            assert book_a.path.exists()
            assert "不像名片" in hub_a.remember_card("随便什么")

            # 两边都改走回环：测试不依赖局域网
            book_a.remember(book_b.profile(hub_b.port()), host="127.0.0.1", port=hub_b.port())
            book_b.remember(book_a.profile(hub_a.port()), host="127.0.0.1", port=hub_a.port())
            ok, data = net.post_json("127.0.0.1", hub_b.port(), net.PATH_HELLO,
                                     {"profile": book_a.profile(hub_a.port()), "token": book_b.token})
            assert ok and data.get("ok") is True, data
            bad, why = net.post_json("127.0.0.1", hub_b.port(), net.PATH_HELLO,
                                     {"profile": book_a.profile(hub_a.port()), "token": "NO"})
            assert bad is False and "口令" in str(why.get("error")), why
            # 两个门房各认各的口令（peer 挂成类属性时，后起的那个会把先起的顶掉）
            assert hub_a.server.token == book_a.token, (hub_a.server.token, book_a.token)
            wrong, why2 = net.post_json("127.0.0.1", hub_a.port(), net.PATH_HELLO,
                                        {"profile": book_b.profile(hub_b.port()), "token": book_b.token})
            assert wrong is False and "口令" in str(why2.get("error")), why2

            # ---- 出门做客：敲门 → 主人点头 → 进去 ----
            hub_a.visit(book_b.pet_id)
            assert seen["a_line"], "出门时该先在自己屏幕上说一句"
            assert pump(lambda: "等着" in " ".join(str(x) for x in seen["a_status"])), seen["a_status"]
            assert pump(lambda: len(hub_b.pending_knocks()) == 1), hub_b.pending_knocks()
            assert seen["b_ask_in"], "门口有人，B 该收到提醒"
            assert (hub_b.pending_knocks()[0].get("profile") or {}).get("pet_id") == book_a.pet_id
            assert not hub_b.guests and hub_a.away_home is None, "没点头之前谁都不许进门"

            hub_b.accept(book_a.pet_id)
            assert book_a.pet_id in hub_b.guests, list(hub_b.guests)
            assert seen["b_arrived"], "客人进门要发 arrived（屏幕上才开得了窗口）"
            guest = hub_b.guests[book_a.pet_id]
            assert isinstance(guest, Guest), guest
            assert guest.host == "127.0.0.1" and guest.port == hub_a.port(), (guest.host, guest.port)
            assert guest.label == "小明家的 黄豆", guest.label
            assert pump(lambda: hub_a.away_home is not None), hub_a.away_home
            assert seen["a_away"] and seen["a_away"][-1][0] is not None, seen["a_away"]
            assert hub_a._home_timer.isActive(), "串门到点要自己回家"
            # 对方一点头就招呼的那句，比敲门人下一次来问更早到——必须接住
            assert pump(lambda: hub_a._lines >= 2), "门里那句招呼被丢掉了"

            # ---- 两只宠物各在自己那台机器上说话 ----
            assert pump(lambda: seen["b_said"]), seen["b_said"]
            assert seen["b_said"][-1][0] == book_a.pet_id, seen["b_said"]
            assert pump(lambda: seen["b_line"]), seen["b_line"]

            # ---- 三个方向 ----
            before = len(seen["a_line"])
            hub_b.poke_guest(book_a.pet_id)
            assert pump(lambda: len(seen["a_line"]) > before), "戳客人，客人那边该回一句"
            hub_b.talk_to_guest(book_a.pet_id, "你家主人平时爱看什么呀")
            assert pump(lambda: seen["a_asked"]), seen["a_asked"]
            assert seen["a_asked"][-1][0] == book_b.nick, seen["a_asked"]
            assert hub_a.talk_to_guest(book_b.pet_id, "喂") is False, "自己没客人时不该揽这话"
            before_b = len(seen["b_line"])
            hub_a.poke_home()
            assert pump(lambda: len(seen["b_line"]) > before_b), "戳主人家那只，它该回一句"
            before_said = len(seen["b_said"])
            assert hub_a.tell_pet("帮我带句话给它们") is True
            assert pump(lambda: len(seen["b_said"]) > before_said), "带的话该出现在对方屏幕上"

            # ---- 主人请客人回家 / 小客人自己走 ----
            before_left = len(seen["b_left"])
            hub_b.drop_guest(book_a.pet_id, "这边要收摊了")
            assert pump(lambda: not hub_b.guests), list(hub_b.guests)
            assert len(seen["b_left"]) > before_left
            assert pump(lambda: hub_a.away_home is None), hub_a.away_home
            assert not hub_a._home_timer.isActive()
            assert hub_a.tell_pet("喂") is False, "到家了就不揽带话的活儿了"

            # ---- 主人不点头：进不去 ----
            hub_a.visit(book_b.pet_id)
            assert pump(lambda: len(hub_b.pending_knocks()) == 1), hub_b.pending_knocks()
            hub_b.refuse(book_a.pet_id)
            hub_a._ask_again()          # 不真等 KNOCK_POLL 那一轮
            assert pump(lambda: "没让我进" in " ".join(str(x) for x in seen["a_status"])), seen["a_status"]
            assert hub_a.away_home is None and not hub_b.guests

            # ---- 对方把门开着（auto_accept）：一步进去 ----
            hub_b.f.auto_accept = True
            hub_a.visit(book_b.pet_id)
            assert pump(lambda: hub_a.away_home is not None and book_a.pet_id in hub_b.guests), (
                hub_a.away_home, list(hub_b.guests))
            hub_a.come_home("测试结束，回家了")
            assert pump(lambda: hub_a.away_home is None)
            assert pump(lambda: not hub_b.guests), list(hub_b.guests)
            hub_b.f.auto_accept = False

            # ---- 对方设备不在工作中：连不上要明说，别糊成"对方不方便" ----
            book_a.remember(
                {"pet_id": "deadbeef", "name": "小灰", "owner": "小刚", "token": "X"},
                host="127.0.0.1", port=1,
            )
            seen["a_status"].clear()
            hub_a.visit("deadbeef")
            assert pump(lambda: any("不在工作" in str(x) for x in seen["a_status"])), seen["a_status"]

            # ---- 界面上那两件家伙：客人窗口 + 好友面板 ----
            pet = PetWindow(cfg_a, PetRenderer(ASSETS_DIR / "pet"))
            gw = GuestWindow(cfg_a, guest)
            gw.place_near(pet.pet_global_rect(), slot=0)
            gw.say("你好呀", "happy")
            assert gw.mood == "happy", gw.mood
            gw.show()
            pump(lambda: True, 0.3)
            pet.extra_occluders = [gw]
            assert pet.occlusion_rect().contains(gw.geometry()), (pet.occlusion_rect(), gw.geometry())
            gw.shutdown()
            assert not gw._timer.isActive()
            pet.shutdown()

            panel = FriendPanel(cfg_a, hub_a, book_a)
            panel.refresh()
            assert panel.card.text().startswith(net.CARD_PREFIX), panel.card.text()[:24]
            # 「出门」只有好友那一行的「去串门」：顶上那行「本机溜一圈」已经删掉
            assert not hasattr(panel, "stroll_btn"), "面板上还留着本机溜一圈那个按钮"
            assert not hasattr(panel, "strollRequested"), "那个死信号 strollRequested 该没了"
            labels = [button.text() for button in panel.findChildren(QPushButton)]
            assert "去串门" in labels, labels
            assert not any("去别的屏幕" in text for text in labels), labels
            panel.add_note("正在敲门……")
            assert "正在敲门" in panel.note.toPlainText()
            closed = []
            panel.closed.connect(lambda: closed.append(1))
            panel.close_panel()
            assert closed
            state = hub_a.state()
            assert state["running"] is True and state["me"].get("pet_id") == book_a.pet_id, state
            assert ":" in str(state["listen"]), state["listen"]

            print(f"      两个门房 {hub_a.port()}/{hub_b.port()}，串门流水落盘："
                  f"来 {len(seen['b_said'])} 句、去 {len(seen['a_line'])} 句")
        finally:
            hub_a.stop()
            hub_b.stop()


    def test_friend_doing_play():
        """串门时「好友那边在干嘛」+ 两只一起玩：能问到、能记下，尺度真的把得住。

        盯的是最容易翻车的几处：
        * 过网的只有 doing.py 拼好的**那一句话**——原始窗口标题一个字都不许出去
          （「干活」那件尤其危险：它的 label 本来就是 `main.py - pet - Visual Studio Code`）；
        * `share_doing = 0` 时对方问也问不出来（不是嘴上拒绝，是本机压根不拼）；
        * 动作由发起方定，两边各自播同一个动作（各发一次 acted），台词各说各的。
        """
        # ---- 「我这边主人在忙什么」这句怎么拼 ----
        board = doing.ActivityBoard(level=doing.SHARE_KIND, owner="小明")
        assert board.line() == "", "还没记过任何一件，就不该硬编一句出来"
        board.note("video", "《奔跑吧》第九季", at=1000.0)
        assert board.line() == "小明这会儿在看视频", board.line()
        board.note("work", "main.py - pet - Visual Studio Code", at=1200.0)
        line = board.line()
        assert "刚在看视频" in line and "这会儿在干活" in line, line
        assert "main.py" not in line, f"窗口标题漏出去了：{line}"
        # 开到 2 档：游戏名 / 片名可以说，「干活」那件照样不许带名字
        assert "《奔跑吧》第九季" in board.line(level=doing.SHARE_NAME)
        assert "Visual Studio Code" not in board.line(level=doing.SHARE_NAME)
        # 0 档 = 一个字都不说；同一件事重复记不算新的一件（"坐了 40 分钟"要算一件）
        assert board.line(level=doing.SHARE_OFF) == ""
        board.note("work", "main.py - pet - Visual Studio Code")
        assert len(board.entries) == 2, board.entries
        assert doing.ActivityBoard.heard("小红这会儿在打游戏", "小红家的 芝麻") == (
            "小红家的 芝麻那边说：小红这会儿在打游戏")

        # ---- 动作表：认名字、挑一个、那一包里该有的都在 ----
        move = play.get("highfive")
        assert move is not None and move.label == "击个掌" and move.seconds > 0, move
        assert play.get("不存在的动作") is None
        picked = play.pick([m.key for m in play.MOVES[:-1]])
        assert picked.key == play.MOVES[-1].key, picked
        info = play.info(move, by="x")
        assert info["move"] == "highfive" and info["anim"] == "highfive", info
        assert info["mood"] == move.mood and info["seconds"] == move.seconds, info
        # 还没有动作帧的时候，那一蹦也得有：act_t=0.5 时抬得最高（见 sprite.act_lift）
        from pet import sprite as sprite_mod
        box = QRectF(0, 0, 100, 100)
        assert sprite_mod.act_lift("", 0.5, box) == 0.0
        assert sprite_mod.act_lift("highfive", 0.0, box) == 0.0
        assert sprite_mod.act_lift("highfive", 0.5, box) < -1.0
        # 每个动作都给了生成帧用的抖法（tools/make_pet.py 拿它写 <动作名>_NN.png）
        made = play.motions()
        assert set(made) == {m.key for m in play.MOVES}, (sorted(made), len(play.MOVES))
        assert all("bob" in spec for spec in made.values()), made

        # ---- 动作帧：放一套 <动作名>_NN.png 进去，渲染器就得认（见 sprite.frames_acts）----
        act_dir = Path(tmpdir) / "act-frames"
        act_dir.mkdir(parents=True, exist_ok=True)
        for name in ("idle", "hug"):
            for index in range(3):
                pm = QPixmap(16, 16)
                pm.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pm)
                painter.fillRect(4, 4 + index * 2, 8, 6, QColor(90, 180, 240, 255))
                painter.end()
                assert pm.save(str(act_dir / f"{name}_{index:02d}.png"))
        framed = sprite_mod.PetRenderer(act_dir)
        assert framed.act_has_frames("hug") is True, framed.frames_acts
        assert len(framed.frames_acts["hug"]) == 3, framed.frames_acts
        assert framed.act_has_frames("pat") is False, "没放帧的动作不该说自己有帧"
        canvas = QPixmap(64, 64)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        framed.draw(painter, QRectF(0, 0, 64, 64), 0.0, False, False, False, "", (0.0, 0.0), "hug", 0.5)
        painter.end()
        assert not canvas.isNull()

        # ---- 日常状态 / 界面互动（见 pet/states.py）：18 条状态 + 三张映射表 ----
        from pet import states as states_mod
        assert len(states_mod.POSES) == 18, [p.key for p in states_mod.POSES]
        assert states_mod.for_reaction("click") is not None
        assert states_mod.for_reaction("click").key == "greet", states_mod.REACTIONS
        assert states_mod.for_reaction("double").key == "poke", states_mod.REACTIONS
        assert states_mod.for_reaction("这个互动不存在") is None
        assert states_mod.for_state("paused").key == "sleep", states_mod.STATES
        assert states_mod.for_state("thinking").key == "think"
        assert states_mod.for_state("") is None
        assert states_mod.get("不存在的状态") is None
        # 待机 / 说话是本来就有的帧，不重复生成；其余每一条都得给参数
        made_states = states_mod.motions()
        assert set(made_states) == {p.key for p in states_mod.POSES} - {"idle", "talk"}, sorted(made_states)
        # 两条路都算数：手写关键帧（keys，逐状态编排的动作）或正弦抖法（bob，呼吸那种）
        assert all(
            "bob" in spec or "keys" in spec for spec in made_states.values()
        ), made_states
        # 手写关键帧的那几条：得说清是"转一圈"还是"走一遍"（采样方式不同），参数得是数
        deco_names = {"spark", "?", "!", "anger", "sad"}   # 跟 tools/make_pet.py 的 DECO_COLORS 对齐
        for key, spec in made_states.items():
            if "keys" in spec:
                keys = spec["keys"]
                assert isinstance(spec.get("keys_loop"), bool), (key, spec)
                assert keys, key
                # 单拍可以写空字典 = "这一拍回到中立姿势"（每个量都取默认值）
                for frame in keys:  # type: ignore[union-attr]
                    assert all(isinstance(v, float) for v in frame.values()), (key, frame)
            if "deco" in spec:
                assert str(spec["deco"]) in deco_names, (key, spec)
        # 两张映射表里指到的名字都得真存在（写错名字 = 那一刻什么都不发生）
        for table in (states_mod.STATES, states_mod.REACTIONS, states_mod.MOOD_ACTIONS):
            for key, name in table.items():
                assert states_mod.get(name) is not None, (key, name)
        # 情绪 → 动作那张表：情绪得是真情绪（见 pet/mood.py），而且**每种情绪都得有动作**
        # （抓到的画面表达什么情绪，就带上什么动作）；空情绪一律不做
        assert states_mod.for_mood("") is None
        assert states_mod.for_mood("curious").key == "confused", states_mod.MOOD_ACTIONS
        for name in states_mod.MOOD_ACTIONS:
            assert name in mood.MOODS, name
        assert set(mood.mood_names()) <= set(states_mod.MOOD_ACTIONS), sorted(
            set(mood.mood_names()) - set(states_mod.MOOD_ACTIONS)
        )
        # 日常状态的帧跟动作帧共用一张表，只是"按时间转"而不是"按进度走一遍"
        for index in range(3):
            pm = QPixmap(16, 16)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            painter.fillRect(4, 4 + index * 2, 8, 6, QColor(240, 180, 90, 255))
            painter.end()
            assert pm.save(str(act_dir / f"walk_{index:02d}.png"))
        looped = sprite_mod.PetRenderer(act_dir)
        assert looped.act_has_frames("walk") is True, looped.frames_acts
        looping = QPixmap(64, 64)
        looping.fill(Qt.GlobalColor.transparent)
        painter = QPainter(looping)
        looped.draw(painter, QRectF(0, 0, 64, 64), 1.0, False, False, False, "", (0.0, 0.0),
                    "walk", 0.0, True, 1.6)
        painter.end()
        assert not looping.isNull()
        # 没配帧的日常状态不能一直"蹦一下"（那是"在做动作"的兜底），老实退回待机
        missing = QPixmap(64, 64)
        missing.fill(Qt.GlobalColor.transparent)
        painter = QPainter(missing)
        looped.draw(painter, QRectF(0, 0, 64, 64), 1.0, False, False, False, "", (0.0, 0.0),
                    "sleep", 0.5, True, 4.0)
        painter.end()
        assert not missing.isNull()
        print(f"      日常状态 {len(states_mod.POSES)} 条 ｜ 界面状态 {len(states_mod.STATES)} 种 ｜ "
              f"互动 {len(states_mod.REACTIONS)} 个")

        # ---- worker 认出"换了件事"就往小本子里记一笔（串门时问的就是它）----
        cfg_w = Config()
        cfg_w.memory.path = str(Path(tmpdir) / "mem-doing.json")
        cfg_w.taste.path = str(Path(tmpdir) / "taste-doing.json")
        worker = AnalysisWorker(cfg_w)
        board_w = doing.ActivityBoard(level=doing.SHARE_KIND, owner="小明")
        worker.activity_board = board_w
        worker._last_window = "main.py - pet - Visual Studio Code"
        worker._activity()             # 第一眼：只记"换成了什么"，还不报时长
        assert board_w.now() and board_w.now()["kind"] == keyinfo.ACTIVITY_WORK, board_w.now()
        assert "main.py" not in board_w.line(level=doing.SHARE_NAME)

        # ---- 真开两个端口，把「捎带 / 打听 / 一起玩」跑一整趟 ----
        def make(tag, nick, owner, activity):
            cfg = Config()
            cfg.provider = "mock"
            cfg.persona.name = nick
            cfg.friends.enabled = True
            cfg.friends.port = 0                 # 系统分个空端口：跑测试不跟本机那只见面
            cfg.friends.listen = "127.0.0.1"
            cfg.friends.owner = owner
            cfg.friends.auto_accept = True       # 这趟只盯"在干嘛 / 一起玩"，省掉点头那一步
            cfg.friends.pet_chat_rounds = 2
            cfg.friends.pet_chat_gap_sec = 0.2
            cfg.friends.visit_seconds = 60.0
            cfg.friends.share_doing = doing.SHARE_KIND
            cfg.friends.ask_doing = True
            cfg.friends.play_rounds = 1
            # 互动频率：现场默认 30 秒一次，测试里按 0.2 秒来（等 30 秒太久了）
            cfg.friends.play_gap_sec = 0.2
            cfg.friends.book = str(Path(tmpdir) / f"friend-{tag}.json")
            cfg.friends.guest_dir = str(Path(tmpdir) / f"guests-{tag}")
            cfg.friends.log = str(Path(tmpdir) / f"visits-{tag}.json")
            book = friends.open_book(cfg)
            return cfg, book, friends.VisitHub(cfg, book, ASSETS_DIR / "pet", activity)

        def pump(until, timeout=20.0):
            end = time.time() + timeout
            while time.time() < end:
                app.processEvents()
                if until():
                    return True
                time.sleep(0.02)
            app.processEvents()
            return until()

        def watch(signal, bucket):
            signal.connect(lambda *args: bucket.append(args))

        board_a = doing.ActivityBoard(level=doing.SHARE_KIND, owner="小明")
        board_a.note("game", "英雄联盟")
        cfg_a, book_a, hub_a = make("dpa", "黄豆", "小明", board_a)
        # 小红这边**没挂本子**：问她"你家主人这会儿在干嘛"就问不出来（这才是真边界）
        cfg_b, book_b, hub_b = make("dpb", "芝麻", "小红", None)
        heard_a, heard_b, acted_a, acted_b, played = [], [], [], [], []
        status_a, status_b = [], []
        watch(hub_a.heard_doing, heard_a)
        watch(hub_b.heard_doing, heard_b)
        watch(hub_a.acted, acted_a)
        watch(hub_b.acted, acted_b)
        watch(hub_a.played, played)
        watch(hub_a.status, status_a)
        watch(hub_b.status, status_b)
        try:
            assert hub_a.start() and hub_b.start(), (hub_a.server.error, hub_b.server.error)
            book_a.remember(book_b.profile(hub_b.port()), host="127.0.0.1", port=hub_b.port())
            book_b.remember(book_a.profile(hub_a.port()), host="127.0.0.1", port=hub_a.port())

            # ---- 出访：捎过去的那一句 + 进门那几步 ----
            hub_a.visit(book_b.pet_id)
            assert pump(lambda: book_a.pet_id in hub_b.guests), list(hub_b.guests)
            guest = hub_b.guests[book_a.pet_id]
            # ① 它带来的「我那边主人这会儿在忙什么」当场就到了 B（招呼那句能顺口提一句）
            assert "在打游戏" in guest.doing, guest.doing
            assert pump(lambda: heard_b and "打游戏" in str(heard_b[-1][1])), heard_b
            assert "打游戏" in str((book_b.get(book_a.pet_id) or {}).get("doing") or "")
            # ② 反过来打听 B 那边：B 没本子 → 问不出来（不是嘴上拒绝，是本机不拼）
            assert pump(lambda: any("没打听出来" in str(x) for x in status_a)), status_a
            assert not heard_a, heard_a

            # ③ 一起玩：B 招待客人玩一次，两边各自播**同一个**动作
            assert pump(lambda: acted_a and acted_b), (acted_a, acted_b)
            assert acted_a[-1][0] == book_a.pet_id and acted_b[-1][0] == book_b.pet_id, (acted_a[-1], acted_b[-1])
            assert acted_a[-1][1]["move"] == acted_b[-1][1]["move"], (acted_a[-1], acted_b[-1])
            assert acted_a[-1][1]["seconds"] > 0 and acted_a[-1][1]["label"], acted_a[-1]
            assert pump(lambda: played), played
            # ③b 互动频率：`play_rounds = 1` 时玩一次就收工；改成 0（不封顶）
            #     就该按 `play_gap_sec` 接着玩下去（现场默认 30 秒一次）
            count = len(played)
            hub_b.f.play_rounds = 0
            assert pump(lambda: len(played) > count), played
            hub_b._stop_playing()

            # ④ 手动点：面板上那两个按钮走的就是这两个入口（以后调动画时序也用它俩）
            before_b = len(acted_b)
            assert hub_a.play_home("hug") is True
            assert pump(lambda: len(acted_b) > before_b and acted_b[-1][1]["move"] == "hug"), acted_b
            before_a = len(acted_a)
            assert hub_b.play_guest(book_a.pet_id, "highfive") is True
            assert pump(lambda: len(acted_a) > before_a and acted_a[-1][1]["move"] == "highfive"), acted_a
            assert hub_b.play_guest("没这位") is False, "屏幕上没这位时不该揽这活儿"
            assert hub_a.play_guest("") is False, "自己这边没客人也不该揽这活儿"
            assert hub_a.tell_pet("带句话") is True     # 玩过之后带话照旧走得通

            # ⑤ 主人家问客人「你那边主人在忙啥」——A 有本子，这回问得出来
            before = len(heard_b)
            assert hub_b.ask_guest_doing(book_a.pet_id) is True
            assert pump(lambda: len(heard_b) > before and "打游戏" in str(heard_b[-1][1])), heard_b
            assert "打游戏" in str((book_b.get(book_a.pet_id) or {}).get("doing") or "")

            # ⑥ 尺度真的把得住：A 那本子关掉（share_doing = 0），再问就一个字都没有
            board_a.level = doing.SHARE_OFF
            before, seen_before = len(heard_b), len(status_b)
            hub_b.ask_guest_doing(book_a.pet_id)
            assert pump(lambda: any("没问出来" in str(x) for x in status_b[seen_before:])), status_b
            assert len(heard_b) == before, heard_b

            # ⑦ 认不出来的动作不瞎猜：回一句"不认识"，谁都不动
            ok, answer = net.post_json(
                "127.0.0.1", hub_b.port(), net.PATH_PLAY,
                {"pet_id": book_a.pet_id, "to_pet": book_b.pet_id, "move": "空翻", "token": book_b.token},
            )
            assert ok and answer.get("ok") is False, answer
            assert "不认识" in str(answer.get("why")), answer

            print(f"      在干嘛：捎过去 1 句、打听回来 {len(heard_b)} 句；"
                  f"一起玩 {len(played)} 次（两边各播一次 acted）")
        finally:
            hub_a.stop()
            hub_b.stop()

    def test_module_refs():
        """跨模块调用（`humanstyle.xxx` / `persona.xxx`）写的名字必须真的存在，别再让这种错跑进现场。

        用 token 扫，字符串/注释里提到的名字不算——就像文档里写 `proactive.model_nudge` 那样。
        """
        import importlib
        import io
        import re as _re
        import tokenize

        root = Path(__file__).resolve().parent.parent / "pet"
        # `from . import x` / `from . import y as z` 才算"把模块引进来了"（局部变量同名不算）
        imported = _re.compile(r"^\s*from\s+\.\s+import\s+(.+?)\s*(?:#.*)?$")
        checked = 0
        for path in sorted(root.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            bound = {}
            for line in source.splitlines():
                found = imported.match(line)
                if not found:
                    continue
                for part in found.group(1).split(","):
                    part = part.strip()
                    mod, _, alias = part.partition(" as ")
                    bound[(alias.strip() or mod)] = mod.strip()
            if not bound:
                continue
            tokens = [
                tok
                for tok in tokenize.generate_tokens(io.StringIO(source).readline)
                if tok.type in (tokenize.NAME, tokenize.OP)
            ]
            for i in range(len(tokens) - 2):
                head, dot, attr = tokens[i], tokens[i + 1], tokens[i + 2]
                if head.type != tokenize.NAME or dot.string != "." or attr.type != tokenize.NAME:
                    continue
                if i and tokens[i - 1].string == ".":      # a.b.c 里的 b.c 不算
                    continue
                if head.string not in bound:
                    continue
                target = importlib.import_module(f"pet.{bound[head.string]}")
                assert hasattr(target, attr.string), (
                    f"{path.name}:{head.start[0]} 里 {head.string}.{attr.string} 不存在"
                )
                checked += 1
        assert checked > 0
        print(f"      核对 {checked} 处跨模块引用")

        # 有帧、却没人触发 = 白做的动作（用户永远看不见）。assets/pet 里每个动作名都得有人用：
        # 它自己待着的样子（states.POSES）、两只凑一起玩的（play.MOVES），或者两张基础帧
        # （idle / talk）。这条专拦"动作做完了、入口忘了接"——这次就是踩着它发现的。
        # 帧还没生成（刚拉下来 / 换过形象）就跳过：这条是"别白做"，不是"必须有帧"。
        frames_dir = Path(__file__).resolve().parent.parent / "assets" / "pet"
        if frames_dir.is_dir():
            names = {p.stem.rsplit("_", 1)[0] for p in frames_dir.glob("*_[0-9][0-9].png")}
            used = (
                {pose.key for pose in states.POSES}      # 它自己待着的样子（走路 / 吃饭 / 摸鱼…）
                | {move.key for move in play.MOVES}      # 两只凑一起玩的（击掌 / 抱抱…）
                | set(mood.mood_names())                 # 表情帧（开心 / 无语 / 生气…）
                | {"idle", "talk"}                       # 两张基础帧
            )
            dead = sorted(name for name in names - used if name)
            assert not dead, f"这些动作有帧却没人触发（白做）：{dead}"
            print(f"      核对 {len(names)} 套动作帧：都有人触发，没有白做的")

    def test_progress_bar():
        """进度条：能认出"这支快看完了"，认不出来时什么都不做（绝不误判成看完）。"""

        def bar_image(ratio, width=640, height=360, row=330):
            """造一张带进度条的图：确定性的噪声底 + 底部一条「已播 + 未播」。"""
            image = Image.new("RGB", (width, height))
            pixels = image.load()
            for y in range(height):
                for x in range(width):
                    value = 90 + ((x * 37 + y * 17) % 21) - 10      # 每个像素都不一样
                    pixels[x, y] = (value, value, value)
            left, right = 20, width - 20
            filled = int(round((right - left) * ratio))
            for x in range(left, right):
                value = 235 if x < left + filled else 65            # 已播 / 未播
                for y in (row - 1, row, row + 1):
                    pixels[x, y] = (value, value, value)
            return image

        done = progress.detect(bar_image(0.93))
        assert done is not None and abs(done.ratio - 0.93) < 0.03, done
        early = progress.detect(bar_image(0.30))
        assert early is not None and abs(early.ratio - 0.30) < 0.03, early
        # 纯噪声画面（没有进度条）→ 认不出来
        assert progress.detect(bar_image(0.93).crop((0, 0, 640, 300))) is None
        # 进度条画在画面中间（不在底部那一截）→ 也不认
        assert progress.detect(bar_image(0.93, row=100)) is None
        assert progress.detect(None) is None

        # worker 接上了：这支视频见过的**最大**进度决定 ended
        cfg = Config()
        cfg.memory.path = str(Path(tmpdir) / "mem-progress.json")
        cfg.taste.path = str(Path(tmpdir) / "taste-progress.json")
        worker = AnalysisWorker(cfg)
        worker._video_record = worker.taste.start_video(
            watchlog.WatchNote(genre="游戏", title="看完了的那支"), at=1000.0
        )
        worker._video_started_at = time.monotonic() - 40.0
        worker._video_max_ratio = 0.0
        worker._note_progress(bar_image(0.93))
        assert worker._video_max_ratio > 0.9, worker._video_max_ratio
        worker._finish_video()
        finished = worker.taste.records[-1]
        assert finished.ended is True, finished
        assert finished.interest() >= 3.0, finished.interest()     # 看完 2 分 + 看得久 1 分

        # 只看到一半：还是老行为（不算看完，不加那 2 分）
        half = worker.taste.start_video(
            watchlog.WatchNote(genre="游戏", title="只看了一半"), at=1001.0
        )
        worker._video_record = half
        worker._video_started_at = time.monotonic() - 40.0
        worker._video_max_ratio = 0.0
        worker._note_progress(bar_image(0.30))
        worker._finish_video()
        assert half.ended is False and half.interest() < 3.0, half

        # 关掉这个能力之后，量都不去量
        worker.cfg.taste.progress_bar = False
        worker._video_record = worker.taste.start_video(
            watchlog.WatchNote(genre="游戏", title="关掉了"), at=1002.0
        )
        worker._video_max_ratio = 0.0
        worker._note_progress(bar_image(0.93))
        assert worker._video_max_ratio == 0.0, worker._video_max_ratio
        print(f"      量到 {done.line()} · 兴趣分 {finished.interest():+.1f}")

    def test_care_reminders():
        """上班时的健康提醒：只在工作时间说、到点才说、两句之间不连播、人不在就不说。"""
        cfg = Config()
        cfg.care.water_every_sec = 60.0
        cfg.care.stand_every_sec = 120.0
        cfg.care.eyes_every_sec = 240.0
        cfg.care.min_gap_sec = 30.0
        policy = care.CarePolicy(cfg)

        now = 10000.0
        policy.reset(now)
        assert policy.observe(now, 10, 1.0) is None              # 刚打开：先不提醒
        assert policy.observe(now + 59.0, 10, 1.0) is None       # 还没到点
        first = policy.observe(now + 61.0, 10, 1.0)              # 喝水到点
        assert first is not None and first.local and first.kind == care.WATER, first
        assert policy.observe(now + 70.0, 10, 1.0) is None       # 两条提醒之间要隔 min_gap
        assert policy.observe(now + 95.0, 10, 1.0) is None       # 站起来还没到点
        second = policy.observe(now + 130.0, 10, 1.0)            # 挑超期最久的那件
        assert second is not None and second.kind == care.STAND, second

        assert policy.observe(now + 900.0, 22, 1.0) is None      # 下班了不提醒
        assert policy.observe(now + 900.0, 3, 1.0) is None       # 凌晨也不提醒
        assert policy.observe(now + 900.0, 10, 9999.0) is None   # 人不在不提醒
        cfg.care.work_start_hour = 0                             # 两头相等 = 全天
        cfg.care.work_end_hour = 0
        assert policy.observe(now + 900.0, 3, 1.0) is not None
        cfg.care.enabled = False
        assert policy.observe(now + 3000.0, 10, 1.0) is None     # 关掉就彻底不提醒

        # worker 把提醒接到气泡上了：本地台词，不问模型
        cfg2 = Config()
        cfg2.memory.path = str(Path(tmpdir) / "mem-care.json")
        cfg2.taste.path = str(Path(tmpdir) / "taste-care.json")
        worker = AnalysisWorker(cfg2)
        worker.proactive.reset(time.monotonic())
        worker.care.reset(time.monotonic() - 99999.0)            # 三件事一起超期
        got = []
        worker.comment.connect(lambda c: got.append(c))
        worker._maybe_nudge(time.monotonic(), False, 1.0, 10)    # 上午 10 点、人刚动过
        assert got and got[0].kind == f"proactive:{care.WATER}", got
        print(f"      {got[0].kind} → {got[0].text}")

    def test_hug_local_pool():
        """抱抱现在只剩"他说难受时兜底那一句"：本地台词池还在，而且真的冒泡。

        用户点名要的那两条（夸夸我 / 抱抱我）已经并进主动搭话，热键和菜单都撤掉了；
        但"接口用不了也得把安慰给到"这条路还要靠本地那一池台词，所以这条测试留着。
        """
        cfg = Config()
        assert proactive.HUG in proactive.LOCAL_LINES
        assert proactive.HUG in proactive.REASONS
        policy = proactive.ProactivePolicy(cfg)
        hug = policy.local_line(proactive.HUG)
        assert hug is not None and hug.local and hug.line, hug
        assert policy.local_line("不存在的由头") is None
        # 连着要两次得换一句（不然会被防重复那道闸挡掉，用户就一句安慰都收不到）
        again = policy.local_line(proactive.HUG, avoid=[hug.line])
        assert again is not None and again.line != hug.line, again

        cfg2 = Config()
        cfg2.memory.path = str(Path(tmpdir) / "mem-hugpool.json")
        cfg2.taste.path = str(Path(tmpdir) / "taste-hugpool.json")
        worker = AnalysisWorker(cfg2)
        got = []
        worker.comment.connect(lambda c: got.append(c))
        worker._emit_nudge(worker.proactive.local_line(proactive.HUG), time.monotonic())
        assert got and got[0].kind == f"proactive:{proactive.HUG}", got
        print(f"      抱抱（本地兜底）→ {hug.line}")

    def test_activity_classify():
        """"他现在在干嘛"：游戏 / 干活 / 看视频 / 认不出来，四种都得认对。

        这块是"主动夸 / 主动关心"的眼睛：认错了就会夸错东西（拿半小时前那部剧去夸他
        现在写代码，或者反过来）。
        """
        assert keyinfo.activity_of("", ["王者荣耀"]) == (keyinfo.ACTIVITY_GAME, "王者荣耀")
        # 看片笔记还挂着，但人已经切去写代码了 → 得认成"干活"
        work = keyinfo.activity_of("main.py - pet - Visual Studio Code", [], video="《奔跑吧》第九季")
        assert work[0] == keyinfo.ACTIVITY_WORK and "Visual Studio Code" in work[1], work
        watching = keyinfo.activity_of("抖音 - Google Chrome", [], video="《奔跑吧》第九季")
        assert watching == (keyinfo.ACTIVITY_VIDEO, "《奔跑吧》第九季"), watching
        assert keyinfo.activity_of("", [])[0] == keyinfo.ACTIVITY_OTHER
        # 边看直播边打游戏：算在打游戏（游戏优先）
        both = keyinfo.activity_of("抖音", ["英雄联盟"], video="某场直播")
        assert both[0] == keyinfo.ACTIVITY_GAME, both
        # 非游戏的 App（微信 / 网易云）不该被当成游戏
        assert keyinfo.activity_of("", ["微信"])[0] != keyinfo.ACTIVITY_GAME
        print("      游戏=王者荣耀 ｜ 干活=Visual Studio Code ｜ 看片=《奔跑吧》 ｜ 认不出=other")

    def test_content_nudge():
        """把"夸夸 / 抱抱"并进主动搭话：看视频久了主动夸，打游戏太久了主动关心。"""
        cfg = Config()
        cfg.proactive.cooldown_sec = 0.0     # 这一条只测"该不该按内容开口"，冷却另有测试
        policy = ProactivePolicy(cfg)
        base = 10_000.0
        policy.reset(base)

        # 才 5 分钟（praise_after_min=20）：先别开口
        assert policy.observe(base + 1, True, 1.0, 14, activity=("video", "《奔跑吧》", 5)) is None
        # 看视频看了 25 分钟 → 夸一句，而且话头就是这支片
        praise = policy.observe(base + 2, True, 1.0, 14, activity=("video", "《奔跑吧》第九季", 25))
        assert praise is not None and praise.kind == proactive.PRAISE, praise
        assert "25 分钟" in praise.note and praise.topic == "《奔跑吧》第九季", praise
        # 同一件事刚夸过：不念第二遍
        assert policy.observe(base + 3, True, 1.0, 14, activity=("video", "《奔跑吧》第九季", 26)) is None
        # 打游戏打了一个钟头 → 该关心了（care_after_min=50）
        care_nudge = policy.observe(base + 4, True, 1.0, 21, activity=("game", "王者荣耀", 60))
        assert care_nudge is not None and care_nudge.kind == proactive.CARE_TOPIC, care_nudge
        assert care_nudge.topic == "王者荣耀", care_nudge
        # 认不出来（other）：什么都不说——夸空话比不夸还尴尬
        assert policy.observe(base + 5, True, 1.0, 21, activity=("other", "桌面", 90)) is None
        # 没传 activity（老调用方）：这一块整个不参与
        assert policy.observe(base + 6, True, 1.0, 21) is None

        # 提示词：夸要夸在**他正在做的这件事**上，关心要落在身体 + 具体建议上
        ask = persona.proactive_prompt(
            cfg, proactive.PRAISE, note="他已经看「《奔跑吧》」25 分钟了", topic="《奔跑吧》第九季", frames=1
        )
        assert "【现在轮到你去夸他】" in ask and "《奔跑吧》第九季" in ask, ask
        assert "看视频/剧" in ask and "打游戏" in ask and "忙活" in ask, ask
        assert "只回一句话" in ask and "[沉默]" in ask
        care_ask = persona.proactive_prompt(cfg, proactive.CARE_TOPIC, topic="王者荣耀", frames=1)
        assert "【现在轮到你去关心他】" in care_ask and "王者荣耀" in care_ask, care_ask
        assert "身体" in care_ask and "小建议" in care_ask, care_ask
        # 别的由头不该带上这两段
        plain = persona.proactive_prompt(cfg, proactive.LONG_QUIET, frames=1)
        assert "轮到你夸他" not in plain and "轮到你关心他" not in plain

        assert proactive.PRAISE in proactive.REASONS and proactive.CARE_TOPIC in proactive.REASONS
        assert proactive.FALLBACK_LINES[proactive.PRAISE].text
        assert proactive.FALLBACK_LINES[proactive.CARE_TOPIC].text
        assert proactive.LOCAL_LINES[proactive.PRAISE] and proactive.LOCAL_LINES[proactive.CARE_TOPIC]
        # 离线模式（mock）也得能走完这条路
        mock = VisionClient(cfg)
        assert mock._mock_nudge(proactive.PRAISE) is not None
        assert mock._mock_nudge(proactive.CARE_TOPIC) is not None
        print(f"      {praise.note} → 夸 ｜ {care_nudge.note} → 关心")

    def test_keyinfo_cache():
        """同一眼的画面关键信息只认一次：识别文字没变就不重算，也不再刷 [keyinfo] 日志。"""
        cfg = Config()
        cfg.memory.path = str(Path(tmpdir) / "mem-keycache.json")
        cfg.taste.path = str(Path(tmpdir) / "taste-keycache.json")
        worker = AnalysisWorker(cfg)

        calls = {"n": 0}
        original = keyinfo.extract

        def counting(lines, window=""):
            calls["n"] += 1
            return original(lines, window)

        keyinfo.extract = counting
        try:
            lines = ["抖音", "奔跑吧 第十四季", "第25集", "03:10 / 12:26"]
            window = "抖音 - Google Chrome"
            body = "同一段识别文字"
            first = worker._key_block(lines, window, body)
            assert first and "抖音" in first, first
            assert calls["n"] == 1, calls
            # 缓存里那份结构化结果也留着（"他在玩什么游戏"要从这儿取）
            assert worker._key_cache_info is not None
            assert "抖音" in worker._key_cache_info.platforms
            # 画面一直在播，但认出来的字一个没变：直接吃缓存
            assert worker._key_block(list(lines), window, body) == first
            assert calls["n"] == 1, calls
            # 换了内容：重新认一遍
            second = worker._key_block(["哔哩哔哩", "《歌手》第八期"], "哔哩哔哩 - Chrome", "换了文字")
            assert calls["n"] == 2, calls
            assert second and second != first
            assert "哔哩哔哩" in worker._key_cache_info.platforms
        finally:
            keyinfo.extract = original
        print(f"      同一段文字只认 1 次（一共认了 {calls['n']} 段）")

    def test_comfort_chat():
        """他诉苦那一轮：只安慰 + 抱抱，不讲道理（打字 / 语音走的都是这条路）。"""
        assert humanstyle.needs_comfort("今天加班到十点，好累啊")
        assert humanstyle.needs_comfort("被领导骂了一顿，难受")
        assert humanstyle.needs_comfort("唉")
        assert not humanstyle.needs_comfort("这条视频挺好看的")
        assert not humanstyle.needs_comfort("")

        cfg = Config()
        assert "诉苦" in persona.chat_system_prompt(cfg, comfort=True)
        assert "抱抱" in persona.chat_system_prompt(cfg, comfort=True)
        ask = persona.chat_user_prompt(cfg, "今天被领导骂了，好难受", comfort=True)
        assert "接住他的情绪" in ask and "抱抱" in ask and "不要讲道理" in ask, ask
        plain = persona.chat_user_prompt(cfg, "这视频好看吗")
        assert "接住他的情绪" not in plain and "回他这一句" in plain
        # 安慰那一段只挂在 comfort 那一轮
        assert "抱抱他" not in persona.chat_system_prompt(cfg)

        # worker：mock 模式下一说难受，回来的话一定带"抱"
        from collections import deque as _deque

        cfg2 = Config()
        cfg2.memory.path = str(Path(tmpdir) / "mem-comfort.json")
        cfg2.taste.path = str(Path(tmpdir) / "taste-comfort.json")
        worker = AnalysisWorker(cfg2)
        got = []
        worker.comment.connect(lambda c: got.append(c))
        worker._answer_user("今天加班到十点，真的好累", _deque())
        assert got and "抱" in got[0].text, got
        # 模型只顾着讲道理（没给抱抱）时补一句；已经抱了就别动它
        dry = mood.Comment("别难过了，明天会好的", "happy", "chat")
        assert "抱" in worker._with_hug(dry).text
        warm = mood.Comment("来，抱抱", "happy", "chat")
        assert worker._with_hug(warm).text == "来，抱抱"
        print(f"      诉苦 → {got[0].text}")

    def test_corpus_learn():
        """边看边学：噪声要挡住、招牌要认出来、问→答 攒够了才准进语料（全程不联网）。"""
        # ① 闸门：不像人话的一律不进（OCR 读到的多半是这些）
        assert corpus.looks_like_speech("这游戏真有这么难吗？")
        assert corpus.looks_like_speech("你试试不就知道了")
        for bad in (
            "已关注",
            "取消收藏",
            "抖音",
            "第25集",
            "03:10 / 12:26",
            "Enter your key",
            "哈哈哈哈哈",
            "10万播放",
            "范丞丞和郑恺站在两个显示时间为 21:15 的闹钟前，似乎是在参与某个环节",
            # 这三条是从现场 run.out 里抄下来的：原来那几条闸都拦不住
            "@ 暗区刂 TenZ 一 Valorant Agnes",
            "1 57 封耒读 〕 网易 “ 0 D 0418",
            "奔跑吧第六季第十一期",           # 集数标记在句子中间的标题
            # 评论区的界面文字：每支视频都不一样，比例式招牌闸抓不到，只能按长相认
            "@一只白色 QvQ。4 天前",
            "# 王者荣耀 # 对抗路 # 夏洛特游戏解说 # 娱乐",
            "小美 4 天前",
        ):
            assert not corpus.looks_like_speech(bad), bad
        # 中英混着说的人话不该被误伤（中文过半就收）
        assert corpus.looks_like_speech("这波 666 啊兄弟们")
        # 句子里只提一次「第 X 期」是真台词，别跟标题一起误杀
        assert corpus.looks_like_speech("这第六期太难看了")
        # 「N 天前」在句中是正常说法，只有落在行尾才是评论时间戳
        assert corpus.looks_like_speech("我早就说过 3 天前他还在装")
        # OCR 会在中文标点两边塞空格、碎片尾巴拖着孤零零的符号：进语料之前要收拾干净
        assert corpus._clip("男人从没学过刑侦 ， 竟波破格提拔为刑警队") == "男人从没学过刑侦，竟波破格提拔为刑警队"
        assert not corpus.looks_like_speech("叁我的 ·")
        # 现场日志里字幕被读成「幸福者退让 ．」：点被空格顶开了，尾巴还吊着个点，都要收干净
        assert corpus._clip("幸福者退让 ．") == "幸福者退让"
        assert corpus._clip("复活到死亡前一分钟 ． 40") == "复活到死亡前一分钟．40"
        assert corpus._key("幸福者退让 ．") == corpus._key("幸福者退让")
        # ② 问句：用来认「问 → 答」这种最值得学的接话对
        assert corpus.is_question("这游戏真有这么难吗？")
        assert corpus.is_question("你确定呢")
        assert not corpus.is_question("你试试不就知道了")

        cfg = Config()
        cfg.learn.path = str(Path(tmpdir) / "learn.json")
        cfg.learn.corpus = str(Path(tmpdir) / "chat_style.json")
        cfg.learn.promote_every = 9999        # 手动调 apply，别让 observe 自己偷着写
        cfg.learn.auto_promote = False        # 自动消化在这条用例里也关掉（它有自己的用例）
        learner = corpus.CorpusLearner(cfg)
        title = "奔跑吧兄弟第十四季精彩片段"     # 一直挂在屏幕上的"招牌"
        question, answer = "这游戏真有这么难吗？", "你试试不就知道了"
        base = time.time()

        learner.observe([title], at=base)
        learner.observe([title], at=base + 1)   # 画面没变（OCR 命中缓存）：不算一次新观察
        assert learner.observations == 1, learner.observations
        assert title in learner.pool

        learner.observe([title, question], at=base + 3)
        assert learner.observations == 2
        assert any(p.ask == title and p.reply == question for p in learner.pairs)

        # ③ 招牌认出来了：之前误收的台词和它配出来的对一起撤掉
        learner.observe([title, answer], at=base + 6)
        assert learner.observations == 3
        assert title not in learner.pool, "一直挂在屏幕上的招牌被当成台词记下来了"
        assert all(title not in (p.ask, p.reply) for p in learner.pairs), "招牌配出来的对没撤掉"
        first = next(p for p in learner.pairs if p.ask == question and p.reply == answer)
        assert first.qa, "上一句在问，这一对应该标成「问→答」"

        # ④ 够格线：问→答 见过两次才够格（不是问答的更严）
        assert learner.ready_pairs() == []
        learner.observe([title, question], at=base + 9)
        learner.observe([title, answer], at=base + 12)
        assert [p.row for p in learner.ready_pairs()] == [[question, answer]], learner.ready_pairs()

        # ⑤ 写回语料：先备份、别覆盖人家原有的内容、写完不用重启就生效
        path = Path(tmpdir) / "chat_style.json"
        path.write_text(
            json.dumps(
                {
                    "_怎么用": ["自己加的语料"],
                    "dialogue": [["我把你上次说的那家店去了", "咋样"]],
                    "banned": ["亲"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assert learner.apply() == 1
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["_怎么用"] == ["自己加的语料"], "写语料把人家原来写的东西抹了"
        assert saved["banned"] == ["亲"]
        assert ["我把你上次说的那家店去了", "咋样"] in saved["dialogue"], "原有的对话被顶掉了"
        assert [question, answer] in saved["dialogue"]
        assert path.with_name("chat_style.json.bak").exists(), "写之前没备份"

        learner.promoted.clear()               # 就算档案忘了，也不能把同一段塞两遍
        assert learner.apply() == 0
        assert len(json.loads(path.read_text(encoding="utf-8"))["dialogue"]) == 2

        original = humanstyle.USER_CORPUS
        try:
            humanstyle.USER_CORPUS = path
            humanstyle.reload()
            extra = humanstyle.user_corpus()
            assert set(extra) == {"dialogue", "patterns", "openers", "enders", "fillers", "banned"}
            assert any(row and row[0] == question for row in extra["dialogue"]), extra["dialogue"]
        finally:
            humanstyle.USER_CORPUS = original
            humanstyle.reload()

        # ⑥ 关掉就当它不存在；默认不许自己往语料里写
        assert Config().learn.enabled and not Config().learn.promote
        cfg.learn.enabled = False
        silent = corpus.CorpusLearner(cfg)
        before = silent.observations
        silent.observe(["那你别玩了真的"], at=base + 30)
        assert silent.observations == before and silent.apply() == 0
        print(f"      {learner.report().splitlines()[0]}")

        # ⑦ 同一句话被 OCR 读成两种标点写法，得算同一句——现场日志里就是这么漏的：
        #    计数被拆开，招牌闸永远攒不到 3 次（认不出招牌），同一行还会被当成"前后两句"
        #    自己跟自己配对，够格线也永远够不着 → 升格那条路等于白摆着。
        cfg3 = Config()
        cfg3.learn.path = str(Path(tmpdir) / "learn3.json")
        cfg3.learn.corpus = str(Path(tmpdir) / "chat_style3.json")
        cfg3.learn.promote_every = 9999
        cfg3.learn.auto_promote = False
        learner3 = corpus.CorpusLearner(cfg3)
        mark_a, mark_b = "作者声明：虚构演绎，仅供娱乐", "作者声明 ． 虚构演绎，仅供娱乐"
        assert corpus._key(mark_a) == corpus._key(mark_b)
        learner3.observe([mark_a], at=base)
        learner3.observe([mark_b], at=base + 1)     # 换个标点的同一句：既不算新观察，也不算新台词
        assert learner3.observations == 1, learner3.observations
        assert len(learner3.pool) == 1, learner3.pool

        learner3.observe([question], at=base + 3)
        learner3.observe([answer], at=base + 5)
        learner3.observe([question.replace("？", "?")], at=base + 7)   # 同一句换个标点再来一次
        learner3.observe([answer + "。"], at=base + 9)                  # 尾巴多一个句号
        ready3 = learner3.ready_pairs()
        assert [p.row for p in ready3] == [[question, answer]], ready3  # 够格线这才够得着
        assert all(
            corpus._key(p.ask) != corpus._key(p.reply) for p in learner3.pairs
        ), "同一句的两种标点写法被当成了一对接话"
        assert learner3.texts[corpus._key(question)] == question, "显示用的写法应该固定成第一次那句"

        # 升格过的那段：别重复写；就算档案忘了，语料里已经有的一段也不再塞
        assert learner3.apply() == 1
        assert learner3.apply() == 0
        learner3.promoted.clear()
        assert learner3.apply() == 0
        saved3 = json.loads((Path(tmpdir) / "chat_style3.json").read_text(encoding="utf-8"))
        assert saved3["dialogue"] == [[question, answer]], saved3["dialogue"]

    def test_corpus_digest():
        """边看边学要**一边消化**：够格的自己进语料，池子还得腾得出位置接新的。

        现场：观察 2117 次、台词池 400 句 / 接话对 200 对全满、**已进语料 0 段**——
        只采集不消化，攒满之后新读到的东西反而是被丢掉的（池满丢最低频 / 最旧的一批）。
        这里盯四件事：
        ① 到点了自己升格，不用人手点 `python -m pet.corpus apply`；
        ② 只收**问→答**那一批（非问答的照旧攒着，那批容易混进"念屏幕"的碎屑）；
        ③ 升格过的对从"还在攒的"里退休，池子才有空位接新的；
        ④ 语料满了先请出**自己学的**最旧一段，用户手写的一句都不许动。
        """
        q, a = "这游戏真有这么难吗？", "你试试不就知道了"
        noise, other = "这波真亏", "你别理他"

        def feed(learner, base):
            """攒出一个够格的问→答，外加一个够格的非问答（后者不该被自动收）。"""
            steps = ([q], [a], [q], [a]) + tuple([[noise], [other]]) * 3
            for step, lines in enumerate(steps):
                learner.observe(lines, at=base + step * 2)

        cfg = Config()
        cfg.learn.path = str(Path(tmpdir) / "digest-learn.json")
        cfg.learn.corpus = str(Path(tmpdir) / "digest-chat-style.json")
        cfg.learn.promote_every = 9999        # 这条用例走"按时间消化"，不走按次数那条
        cfg.learn.auto_promote = False        # 先只攒（别让 observe 顺手就消化掉）
        learner = corpus.CorpusLearner(cfg)
        feed(learner, time.time())

        ready = [p.row for p in learner.ready_pairs()]
        auto = [p.row for p in learner.auto_pairs()]
        assert [q, a] in ready and [q, a] in auto, ready
        assert [noise, other] in ready, ready
        assert [noise, other] not in auto, "非问答的对不该自己就往语料里收"

        # ① 打开自动消化、拨表到点：自己动手（不用谁去点 apply），而且只收问→答那一条
        learner.learn_cfg.auto_promote = True
        learner._last_digest = 0.0
        assert learner.maybe_digest() == 1, "到点了没自己消化"
        saved = json.loads((Path(tmpdir) / "digest-chat-style.json").read_text(encoding="utf-8"))
        assert [q, a] in saved["dialogue"], saved["dialogue"]
        assert [noise, other] not in saved["dialogue"], "把非问答的对也自动收了"
        # ② 刚消化过：间隔没到就不再动手（防着每帧都去写文件）
        assert learner.maybe_digest() == 0
        # ③ 退休：进语料的对不再占"还在攒的"名额，池子接得进新的
        assert all(not (p.ask == q and p.reply == a) for p in learner.pairs), "升格过的对没退休"

        # ④ 语料满了：只请出"自己学的"最旧一段，用户手写的原样不动
        cfg2 = Config()
        cfg2.learn.path = str(Path(tmpdir) / "digest-learn2.json")
        cfg2.learn.corpus = str(Path(tmpdir) / "digest-chat-style2.json")
        cfg2.learn.promote_every = 9999
        cfg2.learn.auto_promote = False
        cfg2.learn.max_dialogue = 2
        path2 = Path(tmpdir) / "digest-chat-style2.json"
        path2.write_text(
            json.dumps(
                {
                    "_怎么用": ["自己加的语料"],
                    "dialogue": [["我把你上次说的那家店去了", "咋样"], ["你看我新头像", "这谁啊"]],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        learner2 = corpus.CorpusLearner(cfg2)
        feed(learner2, time.time())
        # 假装配上一条（第二段）是我们上一轮收进去的，现在它是最旧的"自己人"
        learner2.promoted = [["你看我新头像", "这谁啊"]]
        assert learner2.apply() == 1
        saved2 = json.loads(path2.read_text(encoding="utf-8"))
        assert saved2["_怎么用"] == ["自己加的语料"]
        assert ["我把你上次说的那家店去了", "咋样"] in saved2["dialogue"], "把用户手写的那段挤掉了"
        assert ["你看我新头像", "这谁啊"] not in saved2["dialogue"], "满了没先请出自己学的旧段"
        assert [q, a] in saved2["dialogue"]
        print(f"      {learner.digest_line()}")

    def test_learn_debris_gate():
        """OCR 把界面读歪了的那种碎屑，一个字节都不许进语料（现场真混进来过一条）。

        现场那条：`杰可在 [ 设首 ] 里关闭或打开局内肯包中 3 囗色展示` ——
        它是游戏背包/设置面板被读歪的样子：半角方括号、错字「囗」、断句全乱。
        它够长、中文够多、还反复出现（画面不动就一直读得到），原来那几道闸都放它过去，
        于是"自动消化"一开，它就成了第一批进语料的"接话样本"。
        """
        for bad in (
            "杰可在 [ 设首 ] 里关闭或打开局内肯包中 3 囗色展示",
            "按 { ESC } 返回上一层",
            "< 装备栏 > 已满",
        ):
            assert not corpus.looks_like_speech(bad), bad
        # 真人台词里偶尔出现的书名号/引号不受影响（只认半角 [] {} <> 和错字 囗）
        assert corpus.looks_like_speech("他说「这也太难了吧」就走了")
        assert corpus.looks_like_speech("你倒是说说《暗区突围》好玩在哪")

    def test_note_digest_fields():
        """看片笔记的"玩法 / 画风 / 玩家群体"：读得出来，也进得了口味档案。

        这三项是"看懂他的喜好"那一层（游戏尤其管用）：类型只说"看哪一类"，
        玩法 / 画风 / 群体才说"具体喜欢什么样的"。它们搭在看片笔记**同一次**模型调用里，
        不额外烧接口钱——所以这里盯的是"解析得对、进得了档案、画像里带得出来"。
        """
        note = watchlog.parse_note(
            "类型：游戏\n"
            "主题：暗区突围游戏直播\n"
            "内容：主播在搜刮装备，准备撤离\n"
            "玩法：搜刮跑毒\n"
            "画风：写实军武\n"
            "玩家群体：硬核老玩家\n"
            "看点：装备交易\n"
            "关键词：暗区突围、装备"
        )
        assert note.genre == "游戏"
        assert note.playstyle == "搜刮跑毒" and note.art == "写实军武"
        assert note.audience == "硬核老玩家"
        assert "玩法=搜刮跑毒" in note.line(), note.line()
        assert "玩家群体：硬核老玩家" in note.block(), note.block()

        # 模型把提示词那几行说明抄回来时：值不像骨架，但也不是内容（见 watchlog._PROMPT_FRAGMENTS）
        echo = watchlog.parse_note(
            "玩法：**是游戏**才写（怎么玩的：搜刮跑毒 / 抽卡养成）\n"
            "画风：说不准就写「未知」\n"
            "玩家群体：看不出来写「未知」"
        )
        assert not (echo.playstyle or echo.art or echo.audience), (
            echo.playstyle, echo.art, echo.audience
        )

        cfg = Config()
        cfg.memory.path = str(Path(tmpdir) / "digest-memory.json")
        cfg.taste.path = str(Path(tmpdir) / "digest-taste.json")
        taste_log = taste.TasteLog(cfg)
        for _ in range(3):
            taste_log.start_video(note)
        styles = dict(taste_log.style_profile())
        assert styles.get("搜刮跑毒", 0) >= 2, taste_log.style_profile()
        assert styles.get("硬核老玩家", 0) >= 2, taste_log.style_profile()
        profile = taste_log.profile_block()          # 画像要满 MIN_SAMPLES 才出（3 支）
        assert "他好这口" in profile and "搜刮跑毒" in profile, profile

    def test_tray_tip_plain():
        """托盘悬浮提示说的是"我懂你什么"，不是"它采集到了什么"（现场那四行就是反例）。

        现场那四行：`隐身学习：开着（已学 0 轮 / 语料 +0 段 / 记忆 +0 条）`（本次运行的
        计数，重启归零 → 看着像没在学）、`记忆：200 条 / 常看 短视频、直播`（标签云）、
        `看片笔记：抖音《暗区突围》游戏直播（时间线 4 条）`（原始标题 + 后台计数）。
        它们是"看到的信息"，不是"想过之后给用户的话"。这里盯死：
        那几种形状不许再出现，"我懂你"必须在；后台口径另有 `digest_line()` 走日志。
        """
        from pet.app import ScreenPet

        cfg_path = Path(tmpdir) / "tip-cfg.json"
        base = Config()
        base.provider = "mock"
        base.ocr.backend = "off"
        base.memory.path = str(Path(tmpdir) / "tip-memory.json")
        base.taste.path = str(Path(tmpdir) / "tip-taste.json")
        base.learn.path = str(Path(tmpdir) / "tip-learn.json")
        base.learn.corpus = str(Path(tmpdir) / "tip-chat-style.json")
        base.save(cfg_path)
        pet = ScreenPet(Config.load(cfg_path), app)
        try:
            tip = pet._tray_tip()
            assert "我懂你：" in tip, tip
            for gone in ("已学", "语料 +", "记忆：", "时间线", "看片笔记："):
                assert gone not in tip, f"托盘里还摆着后台口径「{gone}」：{tip}"
            assert "现在：" in tip, tip
            assert tip.count("\n") <= 4, tip                      # 别又堆成一大坨
            # 后台口径不是删掉，是挪到日志/报告那边（还能查）
            line = pet.worker.learn.digest_line()
            assert "接话对" in line and "已进语料" in line, line
            print(f"      托盘：{tip.replace(chr(10), ' / ')}")
        finally:
            pet.quit()

    def test_paths():
        """打包之后"配置写哪儿"这件事：源码跑 = 项目根，打包版 = %APPDATA%，PET_HOME 说了算。

        这一条是最容易出事的地方——exe 待的目录（Program Files / 临时解包目录）不可写，
        写错了表现就是"改了配置不生效"或者"每次启动都忘光"。

        对不住的地方：整份冒烟测试为了不写脏项目，自己就设了 `PET_HOME`（见文件头），
        所以这里先把环境恢复成"没设 PET_HOME"的样子，验完再把它放回去。
        """
        saved_home = os.environ.pop("PET_HOME", None)
        try:
            assert not paths.is_frozen(), "源码里跑不该被判成打包版"
            assert paths.user_dir() == paths.code_dir(), "源码模式下配置应该还在项目根目录（老用户的位置）"
            assert paths.resource_dir() == paths.code_dir(), "源码模式下素材就在项目里"
            assert paths.asset_dir() == paths.resource_dir() / "assets"
            assert paths.config_path().parent == paths.user_dir()

            old = os.environ.get("PET_HOME")
            try:
                portable = Path(tmpdir) / "portable-home"
                os.environ["PET_HOME"] = str(portable)
                assert paths.user_dir() == portable, "PET_HOME 没生效（便携版就靠它）"
                assert paths.config_path() == portable / "config.json"

                # 第一次运行：照着 config.example.json 落一份配置（打包版首次启动走的就是这条路）
                target = portable / "config.json"
                assert paths.seed_config(target), "没能按模板生成 config.json"
                seeded = Config.load(target)
                assert seeded.ui.pet_size == Config().ui.pet_size, "模板生成的配置读出来不对"
                assert seeded.study.enabled is True, "config.example.json 里 study.enabled 该跟内置默认一致（开着）"
                assert seeded.source_path == str(target)
            finally:
                if old is None:
                    os.environ.pop("PET_HOME", None)
                else:
                    os.environ["PET_HOME"] = old

            assert paths.describe().startswith("源码版"), paths.describe()
        finally:
            if saved_home is not None:
                os.environ["PET_HOME"] = saved_home

    check("capture 截屏/缩放/指纹/编码", test_capture)
    check("config 读写与 provider 预设", test_config)
    check("pet_style 长相（预设 / 校验 / 坏值回退 / 真画到画面上）", test_pet_style)
    check("形象设计器（控件 → 长相 → 存进配置）", test_pet_designer)
    check("路径：源码跑 / 打包版 / PET_HOME（配置写到哪儿）", test_paths)
    check("命令行参数不写回配置文件", test_cli_override)
    check("mood 情绪解析与兜底", test_mood)
    check("dialog 对话类型（记录/分析/学习，私下分类不上界面）", test_dialog)
    check("ocr 文本清洗", test_ocr_lines)
    check("ocr Windows 引擎实测", test_ocr_engine)
    check("画面关键信息：台标/节目名/集数/话题人名（本地免费）", test_keyinfo)
    check("关键信息进提示词（摆在原始 OCR 前面）", test_keyinfo_prompt)
    check("memory 长期记忆读写/标签/画像 + 对话类型落盘", test_memory)
    check("memarchive 完整存档（裁剪/清空/重启/关记忆都不丢）", test_memory_archive)
    check("persona 提示词（人设/时间/话头）", test_persona_prompt)
    check("hotkey 解析与格式化", test_hotkey_parse)
    check("hotkey 真注册 + 合成按键", test_hotkey_live)
    check("proactive 键鼠空闲与系统时钟实测", test_proactive_idle)
    check("proactive 策略时序（假时钟）", test_proactive_policy)
    check("proactive 进阶：深夜劝睡 + 记忆话头", test_proactive_extras)
    check("proactive 端到端（不等冷却）", test_proactive_worker)
    check("vlm mock 吐槽", test_vlm_mock)
    check("sprite 渲染（含全部情绪）", test_renderer)
    check("window 挂件/气泡（不画情绪标签，高度与情绪无关）", test_window)
    check("overlay 划观看范围（拖一块 / 双击=整块屏 / F / Esc·右键取消）", test_picker)
    check("worker 完整循环", test_worker_cycle)
    check("app 整体组装 + 热键端到端", test_app_wiring)
    check("scene 5W1H 场景解析（容错）", test_scene_parse)
    check("config chat/asr 两节 + 新热键", test_chat_config)
    check("chatpanel 打字面板（回复不再带情绪标签）", test_chat_panel)
    check("asr 语音输入实测", test_asr_engine)
    check("worker 对话端到端（打字→回答 + 私下分类）", test_worker_chat)
    check("humanstyle 语料与相似度（防车轱辘）", test_humanstyle)
    check("corpus 边看边学（挡噪声 / 认招牌 / 攒接话对 / 写回语料）", test_corpus_learn)
    check("corpus 边看边消化（够格自己进语料 / 池子退休 / 语料满了先退自己人）", test_corpus_digest)
    check("corpus 碎屑闸（半角括号 / 囗 这种读歪的界面文字不进语料）", test_learn_debris_gate)
    check("看片笔记：玩法 / 画风 / 玩家群体 进口味档案（搭车不加钱）", test_note_digest_fields)
    check("托盘悬浮提示说人话（我懂你 / 现在在看什么，不摆后台口径）", test_tray_tip_plain)
    check("persona 真人腔提示词 + 去书面腔", test_persona_human)
    check("worker 防重复两道闸", test_repeat_guard)
    check("winfind 按进程找窗口", test_winfind)
    check("锁定进程：跟随窗口 / 找不到就不抓屏", test_target_process)
    check("目标最小化时监视不停（先试抓、再回放最后一眼，不拍屏幕）", test_minimized_target_keeps_watching)
    check("窗口直抓 + 连拍分镜图", test_window_capture)
    check("提示词块头 / 单字段场景不当台词（噪音闸）", test_noise_guard)
    check("vlm 文本→台词解析链（含抄提示词丢弃）", test_client_parse)
    check("解说词闸门（谁和谁站在…似乎是在参与环节）", test_narration_guard)
    check("解说词补要一句（当场要，不许翻倍）", test_narration_retry)
    check("机器识图描述不进气泡（电脑屏幕截图 / 包含…界面）", test_screen_caption_guard)
    check("vlm 真实四链路（describe/reply/nudge/absorb）", test_client_remote_paths)
    check("taste 分类/互动/兴趣画像（自我训练）", test_taste)
    check("worker 接上口味档案 + 互动即时应声", test_taste_wiring)
    check("口味影响搭话频率（劲头系数）", test_taste_scale)
    check("输出格式的沉默契约（不许只交场景行）", test_silence_contract)
    check("上一眼场景只当背景（禁止照抄）", test_scene_block_is_background)
    check("补说提示词只在补说那轮出现", test_insist_prompt)
    check("补说链路（describe insist → 真实 payload）", test_insist_chain)
    check("补说的闸门（安静够了才补，且不翻倍）", test_insist_guard)
    check("换视频清掉上一眼的场景", test_scene_reset_on_new_video)
    check("只交场景行 / 合规沉默都不当台词", test_scene_only_and_quiet)
    check("形象待在圆内安全区（贴满画布的帧也不会被切）", test_frame_safe_area)
    check("动作帧不参与内容框（蹦一下 / 伸懒腰不许把人缩小）", test_act_frames_overflow)
    check("防重复管住每个出口（本地/兜底台词也过闸）", test_dedup_all_exits)
    check("进度条：判断这支看完没有（本地免费）", test_progress_bar)
    def test_webstudy():
        """隐身学习：收进托盘之后自己上网学（网页 → 口语语料 + 知识点）。

        这一套**不联网**：搜/抓那半段用假 HTML 验解析，模型那半段走 mock（离线也有
        假材料），真正要守住的是"哪些字能进语料 / 哪些必须丢掉"和"写完立刻生效"。
        """
        # ① 网页读成纯文字：script / 样式 / 注释都不许进去，块级标签换行
        page = (
            "<html><head><style>b{color:red}</style></head><body>"
            "<!-- 注释 --><script>var x=1;</script><p>第一行</p><div>第二行 &amp; 三</div>"
            "</body></html>"
        )
        assert webstudy.strip_html(page) == "第一行\n第二行 & 三", webstudy.strip_html(page)
        assert len(webstudy.strip_html("<p>" + "字" * 500 + "</p>", 100)) == 100

        # ② 搜索结果：只挑真网址（去掉搜索站自己 / 把跳转壳还原成真地址）
        bing = (
            '<li class="b_algo"><h2><a href="https://zh.wikipedia.org/wiki/AAA">AAA</a></h2></li>'
            '<h2><a href="https://www.bing.com/ck/a">搜索结果自己</a></h2>'
        )
        assert webstudy.result_links("bing", bing) == ["https://zh.wikipedia.org/wiki/AAA"]
        ddg = '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fp">x</a>'
        assert webstudy.result_links("duckduckgo", ddg) == ["https://example.org/p"]
        assert webstudy.real_url("javascript:void(0)") == ""

        # ③ 模型回话：只认行首那几种标记，解释 / markdown / 书面腔一律丢
        reply = (
            "好的，我帮你整理：\n"
            "- 接话：这游戏真有这么难吗？ ｜ 你试试不就知道了\n"
            "接话：这也太离谱了｜离谱的点在哪\n"
            "接话：只有一句没有下一句（丢掉）\n"
            "1. 知识：王者荣耀 KPL 春季赛一共 18 支队伍。\n"
            "知识：值得注意的是，这体现了团队协作的重要性。\n"
            "以上是我整理的内容。\n"
        )
        rows, notes = webstudy.parse_reply(reply, banned=("值得注意",))
        assert rows == [
            ["这游戏真有这么难吗？", "你试试不就知道了"],
            ["这也太离谱了", "离谱的点在哪"],
        ], rows
        assert notes == ["王者荣耀 KPL 春季赛一共 18 支队伍。"], notes
        assert webstudy.parse_reply("") == ([], [])
        # 家长里短之外再挡一道：模型把**提示词骨架**照抄回来（现场真发生过一次：
        # `["上句", "下句"]` 被当成一段接话写进了语料）
        assert webstudy.parse_reply("接话：上句 ｜ 下句\n知识：一句话\n") == ([], [])
        assert webstudy.parse_reply("接话：某句 ｜ 某句") == ([], [])
        assert not webstudy.usable_note("太短")
        assert not webstudy.usable_note("1234567890")
        assert not webstudy.usable_note("值得注意，这一点很有意义。", banned=("值得注意",))

        # ④ 一轮走完：话头 → 语料 + 记忆；写完 humanstyle 立刻能读到，还要先备份
        cfg = Config()
        cfg.provider = "mock"            # mock 也有假材料：不配 Key 也能整条跑通
        cfg.study.enabled = True
        cfg.study.engine = "none"        # 这一段不联网，只验"拿到模型回话之后"那半段
        cfg.study.topics = ["王者荣耀"]
        cfg.study.interval_sec = 9999.0  # 一轮之后就别再学了（这里要看它守规矩）
        cfg.learn.path = str(Path(tmpdir) / "study-learn.json")
        cfg.learn.corpus = str(Path(tmpdir) / "study-corpus.json")
        cfg.memory.path = str(Path(tmpdir) / "study-memory.json")
        # 话题账本也指到临时目录：测试绝不能往项目里的 data/ 写东西
        cfg.study.ledger = str(Path(tmpdir) / "study-ledger.json")
        corpus_path = Path(tmpdir) / "study-corpus.json"
        corpus_path.write_text(
            json.dumps(
                {
                    "_怎么用": ["自己加的语料"],
                    "dialogue": [["我把你上次说的那家店去了", "咋样"]],
                    "banned": ["亲"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        memory = Memory(cfg)
        study = webstudy.WebStudy(cfg, memory=memory, client=VisionClient(cfg))
        assert study.due() and study.next_wait() == 0.0
        lesson = study.run_round()
        assert lesson is not None and lesson.added_rows >= 1, lesson
        assert lesson.added_notes == 1, lesson
        assert lesson.via == "model" and not lesson.sources, lesson   # 没联网 → 只问了模型
        print(f"      {lesson.line}")

        saved = json.loads(corpus_path.read_text(encoding="utf-8"))
        assert saved["_怎么用"] == ["自己加的语料"], "写语料把人家原来写的东西抹了"
        assert saved["banned"] == ["亲"]
        assert ["我把你上次说的那家店去了", "咋样"] in saved["dialogue"], "原有的对话被顶掉了"
        assert len(saved["dialogue"]) == 1 + lesson.added_rows, saved["dialogue"]
        assert corpus_path.with_name("study-corpus.json.bak").exists(), "写语料之前没备份"
        assert memory.stats()["entries"] == 1
        assert [kind for kind, _ in memory.stats()["dialog"]] == [dialog.LEARN], memory.stats()["dialog"]

        original_corpus = humanstyle.USER_CORPUS
        try:
            humanstyle.USER_CORPUS = corpus_path
            humanstyle.reload()
            assert humanstyle.user_corpus()["dialogue"], "写进语料的东西读不回来"
        finally:
            humanstyle.USER_CORPUS = original_corpus
            humanstyle.reload()

        # 刚学完：间隔没到、一小时上限也满了，都不许再学（别猛刷接口）
        assert not study.due() and study.next_wait() > 60, study.next_wait()
        assert "开着" in study.status() and "1 轮" in study.status(), study.status()
        greeting = study.greeting()
        assert greeting and "王者荣耀" in greeting, greeting

        # ⑤ 同一段不许再塞一遍；关掉之后一步都不动
        assert study.save_rows(lesson.rows) == 0
        cfg.study.enabled = False
        off = webstudy.WebStudy(cfg, memory=memory, client=VisionClient(cfg))
        assert not off.due() and off.run_round() is None
        assert "关着" in off.status()

        # ⑥ 材料跟话头对不上时它该交白卷（现场真搜到过同名杂志 / 公司官网）
        class _SkipClient:
            ready = True

            def summarize(self, prompt, max_tokens=400):
                return "跳过"

        cfg.provider = "zhipu"           # 走假客户端这条：mock 会自己编台词，测不到"跳过"
        cfg.study.enabled = True
        skipper = webstudy.WebStudy(cfg, memory=memory, client=_SkipClient())
        before = memory.stats()["entries"]
        blank = skipper.run_round()
        assert blank is not None and blank.added_rows == 0 and blank.added_notes == 0, blank
        assert memory.stats()["entries"] == before, "交白卷还是往记忆里写了东西"

        # ⑦ 官网 FAQ 那种"有什么 X？→ 有 A、B 等。"不算闲聊；一轮也收不了十几条
        assert webstudy.looks_like_faq("王者峡谷有什么英雄？", "有李白、孙悟空等。")
        assert not webstudy.looks_like_faq("你会玩这个？", "带带我呗")
        assert webstudy.parse_reply("接话：王者峡谷有什么装备？ ｜ 有防御装、输出装等。\n") == ([], [])

        class _FloodClient:
            """一口气写 6 组接话 + 5 条知识（现场抓到过 15 组 + 30 条）。"""

            ready = True

            def summarize(self, prompt, max_tokens=400):
                rows = [
                    "接话：这波团开早了 ｜ 怪我怪我",
                    "接话：打野怎么不来 ｜ 他在刷野呢",
                    "接话：你手速真快 ｜ 练了好几年",
                    "接话：别冲那么前 ｜ 忍不住啊",
                    "接话：先出这个装 ｜ 听你的",
                    "接话：下把带带我 ｜ 行啊",
                ]
                notes = [
                    "知识：发育路先清红buff",
                    "知识：上路记得看兵线",
                    "知识：打野两级来抓中",
                    "知识：辅助先帮打野",
                    "知识：坦克得站前面",
                ]
                return "\n".join(rows + notes)

        flood = webstudy.WebStudy(cfg, memory=memory, client=_FloodClient())
        lesson3 = flood.run_round()
        assert lesson3 is not None
        assert lesson3.added_rows == 3, lesson3.added_rows        # 限量：一轮最多 3 段
        assert lesson3.added_notes == 2, lesson3.added_notes      # 限量：一轮最多 2 条
        print(f"      一口气 6 组 + 5 条 → 只收 {lesson3.added_rows} 段 + {lesson3.added_notes} 条")
        # 同一句不写第二遍：再来一轮，重复的接话 / 知识点都该被挡掉
        assert flood.run_round().added_notes == 0, "同一句知识写了第二遍"

        # ⑧ 学得更准那几道闸：话头精修 / 页面过筛 / 校验回话 / 话题账本 / 补课专用模型
        #    a. 话头精修：标题当话头最准，但得先剥掉"第 X 期""完整版""- 哔哩哔哩"这些
        #       噪声——原样拿去搜，搜回来的是同一支视频的播放页，不是这个话题本身。
        assert webstudy.refine_topic("奔跑吧第十四季第11期（完整版）- 哔哩哔哩") == "奔跑吧", \
            webstudy.refine_topic("奔跑吧第十四季第11期（完整版）- 哔哩哔哩")
        assert webstudy.refine_topic("【4K】王者荣耀 KPL 2025 春季赛总决赛 直播回放_哔哩哔哩") == \
            "王者荣耀 KPL 2025 春季赛总决赛"
        assert webstudy.refine_topic("。。。") == ""
        assert webstudy.is_vague("游戏") and not webstudy.is_vague("王者荣耀")

        #    b. 页面过筛：够长 + 有中文 + 跟话头沾边，三条缺一不可
        good = "王者荣耀里最出名的梗就是这个" * 30
        assert webstudy.page_ok(good, "王者荣耀")
        assert not webstudy.page_ok(good, "原神"), "跟话头不沾边的页面也放行了"
        assert not webstudy.page_ok("字" * 60, "王者荣耀"), "太短的页面也放行了"
        assert not webstudy.page_ok("the quick brown fox " * 40, "王者荣耀"), "整页英文也放行了"
        assert webstudy.cjk_ratio("abcdefg王") < webstudy.PAGE_MIN_CJK + 0.01

        #    c. 校验回话：只认编号；「没有」= 一条不留；没照格式回 = 原样收下（不能清零）
        assert webstudy.parse_verify("1\n3", 3) == [0, 2]
        assert webstudy.parse_verify("没有", 3) == []
        assert webstudy.parse_verify("我看了一下，前两条都有", 3) is None
        assert webstudy.parse_verify("", 3) is None

        #    d. 话题账本：刚学过的往后退、老白卷的排最后（都不丢，只是排队）
        ledger_path = Path(tmpdir) / "study-ledger-test.json"
        ledger = webstudy.TopicLedger(ledger_path, cooldown=3600.0, fail_penalty=2)
        ledger.record("学过的", True)
        for _ in range(2):
            ledger.record("老白卷", False)
        ranked = ledger.rank(["老白卷", "学过的", "新的"])
        assert ranked[0] == "新的" and ranked[-1] == "老白卷", ranked
        assert ledger.cooldown_left("学过的") > 0
        assert webstudy.TopicLedger(ledger_path, cooldown=3600.0).fails("老白卷") == 2, "账本读不回来"

        #    e. 校验那一道真跑一遍：材料里没依据的丢掉
        #       （假客户端按"问题里有没有候选清单"分两问：第一问答好、第二问只留 1 号）
        class _VerifyClient:
            ready = True

            def __init__(self):
                self.prompts = []

            def summarize(self, prompt, max_tokens=400):
                self.prompts.append(prompt)
                if "候选清单" in prompt:
                    return "1"
                return (
                    "接话：这英雄是不是有点超模 ｜ 早该削了\n"
                    "接话：你怎么老盯着打野 ｜ 他节奏带得好\n"
                    "知识：这体现了团队协作的重要性。"
                )

        checker = webstudy.WebStudy(cfg, memory=memory, client=_VerifyClient())
        fake = checker._client
        rows_in = [["这英雄是不是有点超模", "早该削了"], ["你怎么老盯着打野", "他节奏带得好"]]
        notes_in = ["这体现了团队协作的重要性。"]
        keep_rows, keep_notes = checker.verify(
            "王者荣耀", "材料：王者荣耀里最出名的梗就是这个……", rows_in, notes_in
        )
        assert keep_rows == [rows_in[0]] and keep_notes == [], (keep_rows, keep_notes)
        assert len(fake.prompts) == 1 and "最出名的梗" in fake.prompts[0], "校验没把材料带上"
        assert "1. 接话" in fake.prompts[0] and "3. 知识" in fake.prompts[0], "候选没编号"
        # 读不懂就原样收下（白核一轮也比把这一轮清零强）
        class _MuteClient:
            ready = True

            def summarize(self, prompt, max_tokens=400):
                return "我觉得写得挺好"

        quiet = webstudy.WebStudy(cfg, memory=memory, client=_MuteClient())
        kept_rows, kept_notes = quiet.verify("王者荣耀", "材料：……", rows_in, notes_in)
        assert kept_rows == rows_in and kept_notes == notes_in, "校验读不懂就把东西全扔了"

        #    f. 补课专用模型：填了 study.model 就单开一份配置，主配置一个字都不动
        picked = Config()
        picked.provider = "zhipu"
        picked.study.model = "glm-4-flash"
        picked.study.ledger = str(Path(tmpdir) / "study-ledger-pick.json")
        picked.memory.path = str(Path(tmpdir) / "study-memory-pick.json")
        before_model = picked.model
        picky = webstudy.WebStudy(picked, client=_SkipClient())
        assert picky.study_cfg() is not picked, "填了 study.model 还是用主配置"
        assert picky.study_cfg().model == "glm-4-flash", picky.study_cfg().model
        assert picked.model == before_model, "改补课模型把主配置也改了"
        plain_cfg = Config()
        plain_cfg.memory.path = str(Path(tmpdir) / "study-memory-plain.json")
        untouched = webstudy.WebStudy(plain_cfg, client=_SkipClient())
        assert untouched.study_cfg() is untouched.cfg, "没填 study.model 却单开了配置"

    def test_hidden_no_capture():
        """端到端：收进托盘之后**真的不抓屏**（人都走了，拍下来也没人看）。

        「隐身」不只是把窗口藏起来：worker 那一段连截图都不做（见 worker._study_while_hidden）。
        这里让 worker 真跑一秒半，断言它一次都没抓过画面。
        """
        quiet = Config()
        quiet.provider = "mock"
        quiet.capture.interval_sec = 0.3
        quiet.ocr.backend = "off"
        quiet.learn.path = str(Path(tmpdir) / "hidden-learn.json")
        quiet.learn.corpus = str(Path(tmpdir) / "hidden-corpus.json")
        quiet.memory.path = str(Path(tmpdir) / "hidden-memory.json")
        quiet.taste.path = str(Path(tmpdir) / "hidden-taste.json")
        quiet.study.enabled = False          # 不学，只看它有没有在背后偷拍
        hidden_worker = AnalysisWorker(quiet)
        hidden_worker.handshake_ready = True
        hidden_worker.set_hidden(True)
        hidden_worker.start()
        loop = QEventLoop()
        QTimer.singleShot(1500, loop.quit)
        loop.exec()
        hidden_worker.stop()
        hidden_worker.wait(3000)
        assert hidden_worker._last_hash is None, "收进托盘了还在分析画面"
        assert hidden_worker._last_small is None, "收进托盘了还在截图"

    check("上班时的健康提醒（喝水 / 站起来 / 闭眼）", test_care_reminders)
    check("隐身学习：收进托盘后自己上网学（页面→语料+记忆，噪声全挡）", test_webstudy)
    check("收进托盘后真的不抓屏（隐身 = 不看屏幕）", test_hidden_no_capture)
    def test_episode_brief():
        """"扒原片"的落地版：认出是哪一集 → 做一次功课 → 资料卡进提示词。"""
        info = keyinfo.KeyInfo(shows=["奔跑吧"], episode=["第九季", "第28集"])
        assert episode.identify(info) == ("奔跑吧", "第九季", "第28集")
        # 认不出是哪一集：什么都不问（宁可不做，也不瞎编）
        assert episode.identify(keyinfo.KeyInfo(shows=["奔跑吧"])) == ("奔跑吧", "", "")
        assert episode.identify(keyinfo.KeyInfo(episode=["第九季"])) == ("", "第九季", "")

        # 解析模型交回来的那份功课
        parsed = episode.parse(
            "【这一集】\n"
            "梗概：这一期在户外分组做任务，中间有人笑场。\n"
            "人物：宋雨琦、孟子义\n"
            "看点：倒计时器把人整不会了\n看点：有人当场拆台\n"
        )
        assert "户外分组做任务" in parsed.summary, parsed
        assert "宋雨琦" in parsed.cast, parsed
        assert len(parsed.points) == 2, parsed.points
        # 模型说不知道：什么都不存
        assert episode.parse("梗概：不知道这一集的具体内容").is_empty()
        assert episode.parse("这一集我查不到资料").is_empty()
        # 但真梗概里出现「不知道」是正常的，别误伤
        assert "不知道下一秒" in episode.parse("梗概：他们不知道下一秒会发生什么，结果全场笑疯").summary

        cfg = Config()
        cfg.episode.enabled = True                 # 资料卡现在默认关着（主线是"跟着进度看"）
        cfg.episode.path = str(Path(tmpdir) / "episodes.json")
        log = episode.EpisodeLog(cfg)
        assert log.note(info) == "奔跑吧|第九季|第28集"
        assert log.needs_brief() is True
        log.mark_asked()
        assert log.needs_brief() is False          # 刚问过，缓一缓
        log.apply(log.current_key, "梗概：这一期在户外做任务\n看点：倒计时器")
        assert log.block() and "户外做任务" in log.block()
        assert log.needs_brief() is False          # 做过功课就别再问了
        again = episode.EpisodeLog(cfg)            # 存盘读回
        assert again.note(info) == "奔跑吧|第九季|第28集"
        assert "户外做任务" in again.block()

        # 资料卡真的进提示词；没有资料就不加空壳
        ask = persona.user_prompt(cfg, episode=log.block())
        assert "这一集讲的是什么" in ask and "户外做任务" in ask
        assert "这一集讲的是什么" not in persona.user_prompt(cfg)
        # 做功课那次调用的提示词：写死"不知道就说不知道"
        assert "不许编" in persona.episode_system_prompt()
        ep_ask = persona.episode_prompt("奔跑吧", "第九季", "第28集", extra="抖音、第九季")
        assert "梗概：" in ep_ask and "不知道这一集的具体内容" in ep_ask and "抖音、第九季" in ep_ask
        # 离线模式也能走完这条路
        assert "梗概" in VisionClient(cfg)._mock_episode("奔跑吧", "第九季", "第28集")

        # worker 端到端：认出来 → 做功课 → 资料卡进提示词
        cfg2 = Config()
        cfg2.episode.enabled = True
        cfg2.episode.path = str(Path(tmpdir) / "episodes-worker.json")
        cfg2.memory.path = str(Path(tmpdir) / "mem-episode.json")
        cfg2.taste.path = str(Path(tmpdir) / "taste-episode.json")
        worker = AnalysisWorker(cfg2)
        worker._key_cache_info = info
        worker._learn_episode()
        assert worker.episode.block(), "做完功课应该有资料卡"
        assert worker.episode.line()
        print(f"      功课：{worker.episode.line()}")

        # 默认关着：用户改主意了（"没法扒原片就跟着进度看"），别偷偷多花那一次调用
        assert Config().episode.enabled is False
        off = Config()
        off.episode.path = str(Path(tmpdir) / "episodes-off.json")
        assert episode.EpisodeLog(off).note(info) is None

    def test_viewing_progress():
        """跟着他的进度看：进度每往前走 10% 就重读一遍，并把"读到哪了"交给提示词。"""
        cfg = Config()
        cfg.capture.absorb_progress_step = 0.10
        cfg.capture.absorb_progress_min_gap_sec = 0.0
        cfg.capture.absorb_refresh_sec = 0.0        # 这一轮只验"按进度"，不掺时间那条
        cfg.memory.path = str(Path(tmpdir) / "mem-view.json")
        cfg.taste.path = str(Path(tmpdir) / "taste-view.json")
        worker = AnalysisWorker(cfg)
        assert worker._client.ready, "离线 mock 也该是 ready"

        now = time.monotonic()
        worker._last_absorb_at = now
        worker._remember_viewing(watchlog.WatchNote(title="第28集", what="他们在分组做任务"))
        assert worker._last_absorb_ratio == 0.0
        assert not worker._should_refresh_absorb(now), "刚读完，别马上又读"

        # 进度才走到 5%：没到步长，先不读
        worker._video_max_ratio = 0.05
        assert not worker._should_refresh_absorb(now)
        # 走到 12%：够一个步长了 → 重读（把这一段也看完）
        worker._video_max_ratio = 0.12
        assert worker._should_refresh_absorb(now)
        worker._remember_viewing(watchlog.WatchNote(title="第28集", what="任务做到一半", point="笑场"))
        assert abs(worker._last_absorb_ratio - 0.12) < 1e-9
        assert not worker._should_refresh_absorb(now), "读过了就别重读"

        # 「看到哪了」交给提示词：进度 + 一路读到的 + 别剧透
        block = worker._viewing_block()
        assert "12%" in block, block
        assert "分组做任务" in block and "笑场" in block, block
        assert "剧透" in block, block
        assert "看到哪了" in worker.watch_context(), worker.watch_context()
        cfg.watch.enabled = False
        assert "看到哪了" in worker.watch_context(), "关了看片笔记也要知道看到哪了"
        cfg.watch.enabled = True                   # 下面的限速判定还要求它开着

        # 拖着进度条乱跳：最小间隔没到就不读（防连读）
        cfg.capture.absorb_progress_min_gap_sec = 20.0
        worker._video_max_ratio = 0.9
        assert not worker._should_refresh_absorb(now)
        assert worker._should_refresh_absorb(now + 21.0)

        # 还没看过任何东西：不生成空壳
        fresh_cfg = Config()
        fresh_cfg.memory.path = str(Path(tmpdir) / "mem-fresh.json")
        fresh = AnalysisWorker(fresh_cfg)
        assert fresh._seen_notes == [] and fresh._last_absorb_ratio == 0.0
        assert fresh._viewing_block() == ""
        # 攒太多要截断（只留最近几条）
        for i in range(worker.VIEWING_KEEP + 5):
            worker._video_max_ratio = 0.01 * i
            worker._remember_viewing(watchlog.WatchNote(title=f"第{i}段", what=f"内容{i}"))
        assert len(worker._seen_notes) == worker.VIEWING_KEEP, len(worker._seen_notes)
        print(f"      进度触发重读 + 攒了 {len(worker._seen_notes)} 条「读到哪了」")

    def test_internal_leak_guard():
        """内部信息不许进气泡（现场抓到过：「画面关键信息：画面关键信息：/ 哇，这倒计时器，节」）。"""
        # 开头的块头反复剥掉，后面那句真话留着
        assert humanstyle.strip_internal_prefix("画面关键信息：画面关键信息：/ 哇，这倒计时器") == "哇，这倒计时器"
        assert humanstyle.strip_internal_prefix("【你记得的事】他最近老在看《奔跑吧》") == "他最近老在看《奔跑吧》"
        assert humanstyle.strip_internal_prefix("【现在】现在时间：凌晨 1:20") == "现在时间：凌晨 1:20"
        # 夹在中间、剥不掉的：整句不要
        assert humanstyle.looks_like_internal("我把画面关键信息念了一遍")
        assert humanstyle.looks_like_internal("我翻了看片记录")
        assert humanstyle.looks_like_internal("【你摸清的他的口味】说他爱看游戏")
        # 「跟着进度看」那一块的头也算内部信息（worker._viewing_block）
        assert humanstyle.strip_internal_prefix("【他这支片子看到哪了】他这支已经看到 62% 了") == "他这支已经看到 62% 了"
        assert humanstyle.looks_like_internal("【他这支片子看到哪了】他这支已经看到 62% 了")
        assert humanstyle.looks_like_internal("一路看过来读到的：第28集｜分组做任务")
        # 正常台词不许误伤
        assert not humanstyle.looks_like_internal("这倒计时器是真折磨人")
        assert not humanstyle.looks_like_internal("哈哈这也能行")
        assert humanstyle.strip_internal_prefix("哈哈这也能行") == "哈哈这也能行"

        # 整条解析链：剥完还留得住的，就当正常台词放行
        client = VisionClient(Config())
        kept = client._to_comment("画面关键信息：画面关键信息：/ 哇，这倒计时器", "test")
        assert kept is not None and kept.text == "哇，这倒计时器", kept
        # 现场那句长的（逗号分隔）也要留住后面那半句——人说话是用逗号的
        kept2 = client._to_comment(
            "画面关键信息：画面关键信息：画面关键信息：/ 哇，这倒计时器，节日快乐", "test"
        )
        assert kept2 is not None and kept2.text.startswith("哇"), kept2
        # 剥不掉的：不进气泡，并且标记成 internal（worker 会当场再要一句）
        dropped = client._to_comment("画面关键信息：抖音、第九季、第28集", "test")
        assert dropped is None and client.last_drop == "internal", (dropped, client.last_drop)
        print("      块头剥掉、念信息的整句丢弃、正常台词放行")

        # 现场那句**碎片拼的**（图一）：`画面关键信息：第孬期 . / > 0 收/A《抖音 ×/ 第集：`
        # ——没有一个顿号，老判据抓不住；新判据是"你说的每个字，屏幕上都有"。
        screen = "抖音 奔跑吧 第九季 第28期 合集 点赞 收藏 关注 分享 12:06 / 45:00 ×"
        assert humanstyle.looks_like_screen_echo("第孬期 0 收 抖音 第集", (screen,))
        assert humanstyle.looks_like_screen_echo("第28期 抖音", (screen,))
        # 正常台词不许误伤：大部分字屏幕上没有，就不算念屏幕
        assert not humanstyle.looks_like_screen_echo("这倒计时器是真折磨人", (screen,))
        assert not humanstyle.looks_like_screen_echo("哈哈这也能行", (screen,))
        assert not humanstyle.looks_like_screen_echo("第28期", (screen,))      # 太短，不算
        assert not humanstyle.looks_like_screen_echo("抖音抖音抖音", ("",))    # 没屏幕文字
        # 整条链：认出这是念屏幕 → 不进气泡
        client._screen_text = screen
        dump = client._to_comment("画面关键信息：第孬期 . / > 0 收/A《抖音 ×/ 第集：", "test")
        assert dump is None and client.last_drop == "internal", (dump, client.last_drop)
        # 同一句屏幕文字，带上自己的反应就能过（它是"跟着说"，不是"照着念"）
        ok = client._to_comment("画面关键信息：第28集 抖音", "test")
        assert ok is None or "第28集" not in ok.text, ok
        keep = client._to_comment("[吐槽] 都第28集了这个倒计时还没走完，离谱", "test")
        assert keep is not None and "离谱" in keep.text, keep
        print("      念屏幕的（碎片拼接也算）不再进气泡")

    def test_title_guard():
        """页面上抓来的标题不许进气泡（现场那条：`《复仇者联盟3》剧情设定细节探案幕后解读`）。

        为什么单列一道闸：标题可以一个「内部字眼」都不带，块头那道闸（looks_like_internal）
        认不出它，「念屏幕」那道闸又要**先剥掉块头**才生效——整条标题就这么漏进了气泡。
        """
        # 现场那条 + 同形状的（书名号 / 话题标签 / 标题噪声词 / 尾巴上的括号补充）
        for live in (
            "《复仇者联盟3》剧情设定细节探案幕后解读",
            "万字深拆《复仇者联盟3》剧情设定细节彩蛋幕后解读（再次重温内容更饱满）#复联 #复联3",
            "奔跑吧第十四季第11期",
            "《歌手》第八期 完整版",
            "庆余年第二季 中字 4K",
            "灭霸的手下被击败的幕后花絮",
        ):
            assert humanstyle.looks_like_title(live), live
        # 正常台词不许误伤：口气词 / 问号 / 句读一出现就是在说话，不是在念标题
        for chat in (
            "这解说讲得真啰嗦",
            "他这表情是要发火了吧",
            "《流浪地球》我看过两遍",
            "这集确实有点东西",
            "就这？",
            "哈哈这也行",
            "咱们接着看",
        ):
            assert not humanstyle.looks_like_title(chat), chat

        # 整条链：标题不进气泡，并且标记成 title（worker 会当场再要一句）
        client = VisionClient(Config())
        client._echo_sources = ()
        client._screen_text = ""
        assert client._to_comment("[吐槽] 《复仇者联盟3》剧情设定细节探案幕后解读", "test") is None
        assert client.last_drop == "title", client.last_drop
        # 同一支视频，带了自己的反应：照说
        keep = client._to_comment("[吐槽] 这剧情设定真够绕的，看得我头大", "test")
        assert keep is not None and "头大" in keep.text, keep

        # 隐身学习那条路也别把标题当成「接话对 / 知识点」收进去
        assert not webstudy._usable_line("《复仇者联盟3》剧情设定细节探案幕后解读")
        assert not webstudy.usable_note("《复仇者联盟3》剧情设定细节探案幕后解读")
        assert webstudy._usable_line("带带我呗")
        print("      抓来的标题不进气泡、也不进语料/记忆")

    def test_meta_guard():
        """它那套机器（学习 / 记忆 / 分类 / 自问自答）不许从气泡里露出来。

        用户的原话是「不许露出『我在学 / 我在记 / 我分过类』的痕迹（包括念语料、念刚学的那句、
        念资料卡）」。判据见 `humanstyle.looks_like_meta_talk`——三层：机器词 / 进行体的自我汇报 /
        笔记字段的形状（一行两个以上字段标签，或整句以字段名开头）。
        """
        # ①② 机器词 + 进行体自我汇报（"我在学 / 我在记 / 我在补课"）
        for meta in (
            "我刚学到一个知识点：长按就能保存",
            "我在学习怎么防这个技能，等下教你",
            "我在记这个操作，回头告诉你",
            "我把这句存进语料了",
            "我的记忆里好像有这事",
            "这集我做过功课了",
            "资料卡里写着这段是即兴的",
            "我在补课，别吵我",
            "关键词都归档了",
        ):
            assert humanstyle.looks_like_meta_talk(meta), meta
        # ③ 笔记字段：一行两个以上字段标签、或者整句以字段名开头、或者自问自答的形状
        for dump in (
            "键名：类型 / 答案：影视综艺 / 键名：主题",
            "类型=游戏｜主题=抖音《MIMIC PARTY｜内容=四个卡通角色｜看点=角色互动",
            "看点：任务失败那一下",
            "【看点】有人当场拆台",
            "别人问：这个怎么玩？ 我接：先点这里",
        ):
            assert humanstyle.looks_like_meta_talk(dump), dump
        # 正常台词不许误伤：完成体（"我记住了"）就是熟人聊天里的"我知道了"，
        # 带冒号的字段名只出现一个也不算念资料卡
        for chat in (
            "这解说讲得真啰嗦",
            "我记住了，明天叫你",
            "我记性不好，老是忘",
            "我学着点，看你怎么整",
            "这类型片我看不下去",
            "带带我呗",
            "他这波操作可以写进教材了",
            "那分类垃圾桶真该换了",
            "哈哈这也行",
        ):
            assert not humanstyle.looks_like_meta_talk(chat), chat

        # 整条链：进气泡之前就拦掉，并且标成 meta（worker 会当场再要一句）
        client = VisionClient(Config())
        client._echo_sources = ()
        client._screen_text = ""
        assert client._to_comment("[好奇] 我刚学到一个知识点：长按就能保存", "test") is None
        assert client.last_drop == "meta", client.last_drop
        assert client._to_comment("[吐槽] 键名：类型 / 答案：影视综艺 / 键名：主题", "test") is None
        assert client.last_drop == "meta", client.last_drop
        # 同一件事，说的是自己的反应：照说
        keep = client._to_comment("[好奇] 长按就能保存？我还真不知道", "test")
        assert keep is not None and "不知道" in keep.text, keep

        # 两条重问路径都得认得 "meta"（吐槽那条 + 用户对话那条）：少一处，这一轮就静默哑掉
        src = (Path(__file__).resolve().parent.parent / "pet" / "worker.py").read_text(encoding="utf-8")
        assert src.count('"meta"') >= 2, src.count('"meta"')
        print("      它那套机器的话（我在学 / 念语料 / 念资料卡）不再进气泡")

    def test_note_skeleton_gate():
        """看片笔记别把**提示词的字段骨架**当内容。

        现场：模型把提示词骨架原样抄了回来，看片笔记的「主题」变成了
        「键名：类型 / 答案：影视综艺 / 键名：主题」；它又被当成"他刚在看的那一支"的话头
        送去学（`worker._study_topics`），学回来的东西再进气泡——等于把提示词端上桌。
        闸在 `watchlog.parse_note`（`_is_content`，判据是
        `humanstyle.looks_like_note_skeleton`：只认字段形状，不认"知识点 / 语料"这种词）。
        """
        # ① 骨架不当字段内容，也不留进 raw（raw 是"解析不出来"时的兜底，最后会被当标题）
        note = watchlog.parse_note(
            "主题：键名：类型 / 答案：影视综艺 / 键名：主题\n"
            "类型：影视综艺\n"
            "内容：四个卡通角色在台上抢麦\n"
            "看点：有人当场拆台"
        )
        assert note.title == "", note.title
        assert "键名" not in note.raw, note.raw
        assert note.what == "四个卡通角色在台上抢麦", note.what
        assert note.point == "有人当场拆台", note.point
        assert note.genre == "影视综艺", note.genre     # 类型照样认，别的字段没被连累

        # ② 整条笔记只有骨架时：算"没读出东西"（不会变成一句空话头）
        only = watchlog.parse_note("键名：类型 / 答案：影视综艺 / 键名：主题")
        assert only.is_empty() and not only.raw and not only.title, (only.raw, only.title)

        # ③ 内容里带"知识点 / 语料"这种词不算骨架，一个字都不许丢
        keep = watchlog.parse_note("内容：这集讲的是航天知识点\n看点：语料库该怎么建")
        assert keep.what == "这集讲的是航天知识点", keep.what
        assert keep.point == "语料库该怎么建", keep.point

        # ④ 判据只管字段形状（"我在学"那类机器词仍归 meta 闸，见上一个测试）
        assert humanstyle.looks_like_note_skeleton("键名：类型 / 答案：影视综艺 / 键名：主题")
        assert humanstyle.looks_like_note_skeleton("【看点】有人当场拆台")
        assert humanstyle.looks_like_note_skeleton("别人问：这个怎么玩？ 我接：先点这里")
        assert not humanstyle.looks_like_note_skeleton("这集讲的是航天知识点")
        assert not humanstyle.looks_like_note_skeleton("航天知识讲得挺细的")   # 内容里出现「知识」也不算骨架
        assert humanstyle.looks_like_meta_talk("键名：类型 / 答案：影视综艺 / 键名：主题")
        assert not humanstyle.looks_like_meta_talk("航天知识讲得挺细的")
        print("      看片笔记的字段骨架（键名：类型 / 答案：…）不当内容，也不进 raw / 话头")

    def test_bubble_marks():
        """气泡里的标点：只留 ，。？！… 和引号、书名号——「/」那种必须断掉。

        现场那条（图三）是模型把**三句备选**并列着交上来：
        `这波稳了 / 这画面，高启强这表情，感觉他要发火了！ / 你得服从指挥吧你得接受`
        ——屏幕上就是一串「A / B / C」，那不是说话，是草稿。
        """
        said = persona.clean_reply(
            "[吐槽] 这波稳了 / 这画面，高启强这表情，感觉他要发火了！ / 你得服从指挥吧你得接受"
        )
        assert said == "[吐槽] 这波稳了", said          # 只念第一句
        # 句首的情绪标签得留着（mood.split_mood 还要拿它切表情），别的符号照清
        assert persona.clean_reply("[好奇] 你这段卡了三回了，笑死~") == "[好奇] 你这段卡了三回了，笑死"
        for raw in ("[开心] 这也太#离谱#了吧", "[开心] 绝了*无敌*", "[开心] 好家伙—真行", "[无语] a|b"):
            cleaned = persona.clean_reply(raw)
            assert not any(ch in cleaned for ch in "#*—~|｜/／"), (raw, cleaned)
        # 引号、书名号、动作括号都留着（那是表达意思要用的）；省略号统一成「…」
        kept = persona.clean_reply("[激动] 他说“成了”，就是《奔跑吧》那个")
        assert "“成了”" in kept and "《奔跑吧》" in kept, kept
        assert persona.clean_reply("[吐槽] 啊这。。。真行") == "[吐槽] 啊这…真行"
        assert persona.clean_reply("[开心] （一把抱住）今天辛苦了") == "[开心] （一把抱住）今天辛苦了"
        # 太长就在句读处断开，不从中间劈开（气泡里读着才不别扭）
        assert persona.fit("诶这波稳了", 20) == "诶这波稳了"
        cut = persona.fit("这波稳了，他这表情是真要发火，后面还有一长串根本说不完的话", 20)
        assert cut == "这波稳了，他这表情是真要发火…", cut
        # 整条链：进气泡的那句话里不许再有「/」
        client = VisionClient(Config())
        client._echo_sources = ()
        chain = client._to_comment("[吐槽] 稳了 / 他这表情 / 你得接受", "test")
        assert chain is not None and "/" not in chain.text, chain
        print(f"      气泡标点：{chain.text}｜按句读截断：{cut}")

    def test_simplified_and_label_only():
        """屏幕上不该出现的两样东西：**繁体字**、**只剩标签的空话**。

        都是现场气泡截图里抓到的：

        * 图一：`[吐槽] 因為求折磨這個犧牲太大了我覺得`——整句繁体；
        * 图二：`[无语] 场景：`——模型把情绪标签和一个**空的场景行**写在了同一行。
          摘标签之前场景判断在行首失配，这一整行被当成台词念了出去，
          标点一清，气泡里就只剩「场景」俩字。

        所以这里两遍：程序侧统一繁→简（见 `pet/zh.py`），
        再加一道「只有个标签就不是话」的闸（见 `scene.is_label_only`）。
        """
        # 繁体字级对照表先自检：写坏的条目必须是 0（写错一条会连带整张表串位）
        assert zh.bad_entries() == 0, f"繁简对照表有 {zh.bad_entries()} 条写坏了"
        assert zh.table_size() > 500, zh.table_size()
        assert zh.to_simplified("因為求折磨這個犧牲太大了我覺得") == "因为求折磨这个牺牲太大了我觉得"
        assert zh.to_simplified("已经就是简体了") == "已经就是简体了"      # 简体原样返回
        assert persona.clean_reply("[吐槽] 因為求折磨這個犧牲太大了我覺得") == "[吐槽] 因为求折磨这个牺牲太大了我觉得"

        client = VisionClient(Config())
        client._echo_sources = ()
        # 图二那种：只有个标签，不是话 → 不进气泡
        for raw in ("[无语] 场景：", "场景：", "[无语] 场景", "【场景】", "（场景）", "未知", "[好奇] 未知"):
            assert client._to_comment(raw, "test") is None, raw
        # 走完整条链（含繁体）出来的是简体
        kept = client._to_comment("[吐槽] 因為求折磨這個犧牲太大了我覺得", "test")
        assert kept is not None and kept.text == "因为求折磨这个牺牲太大了我觉得", kept
        # 正常台词不许被误伤（繁体版同理）
        for raw, want in (
            ("[无语] 这操作我真看不懂", "这操作我真看不懂"),
            ("[好奇] 时间过得真快", "时间过得真快"),
            ("[开心] 哈哈这也能行", "哈哈这也能行"),
            ("[吐槽] 這操作我真看不懂", "这操作我真看不懂"),
            ("[无语] 场景：人物=主播｜事件=连抽十次没出金", None),   # 场景行不当台词念
        ):
            got = client._to_comment(raw, "test")
            assert (None if got is None else got.text) == want, (raw, got)
        # 句首的情绪标签不该被当成台词留下来（留了会把后面真正的台词挤掉）
        speech, parsed = scene.split("[无语] 场景：人物=主播｜事件=抽卡")
        assert speech == "" and "主播" in parsed.brief(), (speech, parsed)
        # 判定口径：「不知道」是人话（照说），「[场景]」「未知」是标签（丢掉）
        assert not scene.is_label_only("不知道")
        assert scene.is_label_only("[场景]") and scene.is_label_only("未知")
        print(f"      繁简表 {zh.table_size()} 字；「场景：」「未知」这种只剩标签的不再进气泡")

    def test_tray_minimal():
        """托盘菜单只留「显示挂件」，其余命令全在挂件右键菜单里（原来托盘独有的项一项没丢）。

        那把菜单以前 20 项，跟右键菜单重了一大半；要的是"托盘只管放出来 / 收回去"。
        这里静态盯一遍：托盘那边只许有「显示挂件」这一个动作；让出去的
        「打开记忆文件 / 清除长期记忆」得在右键菜单里找得到，信号也真的接上了。
        （「马上吐槽一句」后来又去掉了：只剩 `Ctrl+Alt+S` 热键；打字 / 语音合成"点一下挂件"。）
        """
        root = Path(__file__).resolve().parent.parent / "pet"
        app_src = (root / "app.py").read_text(encoding="utf-8")
        window_src = (root / "window.py").read_text(encoding="utf-8")
        tray = app_src.split("def _setup_tray", 1)[1].split("def ", 1)[0]
        assert tray.count("menu.addAction") == 1, tray
        assert '"显示挂件"' in tray, tray
        for gone in ("划一下观看范围", "只盯着一个程序", "马上吐槽一句", "打开配置文件", "清除长期记忆"):
            assert gone not in tray, f"托盘菜单里还留着「{gone}」"
        # 让出去的两项：右键菜单里有，接线也得在（少一处，那项就是死的）
        for moved in ("打开记忆文件", "清除长期记忆…", "设计我的形象…"):
            assert moved in window_src, f"右键菜单里少了「{moved}」"
        for sig in ("memoryRequested", "clearMemoryRequested", "designRequested"):
            assert f"{sig} = Signal(" in window_src, sig
            assert f"w.{sig}.connect(" in app_src, sig
        # 「马上吐槽一句」的入口只剩热键那一条路：菜单项和信号都清掉了
        assert "马上吐槽" not in window_src, "右键菜单里还留着「马上吐槽一句」"
        assert "analyzeRequested" not in window_src and "analyzeRequested" not in app_src
        assert '"say": self.cfg.hotkey.say' in app_src, "Ctrl+Alt+S 那条热键接线也没了"
        print("      托盘只剩「显示挂件」；打开记忆 / 清除记忆 / 设计我的形象在右键菜单里，马上吐槽只剩热键")


    check("抱抱本地兜底台词（给诉苦那条路用）", test_hug_local_pool)
    check("他在干嘛：游戏 / 干活 / 看视频（本地免费）", test_activity_classify)
    check("主动夸 / 主动关心（并进主动搭话）", test_content_nudge)
    check("画面关键信息只认一次（缓存 + 不再刷屏）", test_keyinfo_cache)
    check("内部信息不许进气泡（块头剥掉 / 念屏幕就丢）", test_internal_leak_guard)
    check("抓来的标题不许进气泡 / 语料（书名号 · 话题标签 · 标题噪声词）", test_title_guard)
    check("它那套机器不许进气泡（我在学 / 念语料 / 念资料卡）", test_meta_guard)
    check("看片笔记的字段骨架不当内容（键名：类型 / 答案：… 也不进 raw）", test_note_skeleton_gate)
    check("跟着他的进度看（进度触发重读 + 看到哪了进提示词）", test_viewing_progress)
    check("整集资料卡（认出是哪一集 → 做功课 → 进提示词）", test_episode_brief)
    check("他诉苦时只安慰 + 抱抱（打字 / 语音那条路）", test_comfort_chat)
    check("气泡标点（只留 ，。？！…，斜杠一律断掉）+ 按句读截断", test_bubble_marks)
    check("一律简体（繁→简零依赖）+ 「场景：」这种只剩标签的不进气泡", test_simplified_and_label_only)
    check("好友系统：串门整条路（名片/敲门/点头/唠嗑/告别/界面/设备离线）", test_friends)
    check("好友在干嘛 + 两只一起玩（尺度 / 打听 / 同步动作）", test_friend_doing_play)
    check("跨模块引用名核对（静态）", test_module_refs)
    check("托盘只留「显示挂件」（其余命令都在右键菜单里）", test_tray_minimal)

    if window is not None:
        window.shutdown()

    print()
    shutil.rmtree(SMOKE_HOME, ignore_errors=True)   # 临时"家"（见文件头）用完就删
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
