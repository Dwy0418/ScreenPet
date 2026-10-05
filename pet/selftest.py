"""无界面自检：不弹窗，把「截屏 → 缩放 → 指纹 → OCR → 窗口标题 → 记忆 → 问模型」走一遍。

用法：
    python main.py --selftest
    python main.py --selftest --image .\\demo.png   # 用图片代替实时截屏
"""
from __future__ import annotations

import time
from typing import List, Optional

from PIL import Image

from . import capture, dialog, foreground, make_console_safe, mood, proactive
from .asr import SpeechReader
from .config import ASSETS_DIR, Config
from .memory import Memory
from .ocr import TextReader
from .proactive import ProactivePolicy, user_idle_seconds
from .style import PetStyle
from .vlm import VlmError, VisionClient
from .watchlog import WatchLog


def run(cfg: Config, image_path: Optional[str] = None) -> int:
    make_console_safe()
    print("=== screen-pet 自检 ===")
    print(f"配置文件 : {cfg.config_path()}")
    print(f"provider : {cfg.provider} / model={cfg.model or '(未设置)'}")
    print(f"接口地址 : {cfg.endpoint if cfg.provider != 'mock' else '(mock: 不联网)'}")
    print(f"API Key  : {'已配置' if cfg.api_key else '未配置'}")
    print(f"观看范围 : {cfg.capture.region or '整块屏幕'}")
    print(f"OCR      : {cfg.ocr.backend if cfg.ocr.enabled else 'off'}（本地识别，图片不上传）")
    print(f"长期记忆 : {'开' if cfg.memory.enabled else '关'} → {cfg.memory.path}")
    print(
        f"看片笔记 : {'开' if cfg.watch.enabled else '关'}"
        f"（画面跳变 {cfg.capture.scene_cut_threshold:.2f} 以上就当换了支视频，"
        f"先读一遍再聊；时间线 {int(cfg.watch.timeline_size)} 条"
        f"{'，读笔记也关掉了' if not cfg.capture.absorb_enabled else ''}）"
    )
    print("操作方式 : 全部用鼠标（右键我 → 菜单里有全部入口；没有全局热键）")
    print(
        f"打字聊天 : {'开' if cfg.chat.enabled else '关'}"
        f"（点一下挂件，或右键「打字跟我唠…」，单次最多回 {int(cfg.chat.max_chars)} 字，"
        f"记住最近 {int(cfg.chat.history_size)} 轮）"
    )
    reader = SpeechReader(cfg)
    if not reader.available():
        print(f"语音输入 : 用不了（{'配置里关了' if not cfg.asr.enabled else '这台机器上没找到可用引擎'}，"
              "右键「说一句（语音）」这个入口还在）")
    else:
        engines = reader.recognizers()
        shown = "、".join(engines) if engines else "系统默认语音包"
        print(
            f"语音输入 : 开（{shown}，离线识别、不上传音频，"
            f"一次听 {cfg.asr.seconds:.0f} 秒）"
        )
    pro = cfg.proactive
    print(
        f"主动搭话 : {'开' if pro.enabled else '关'}（画面静止 {int(pro.still_after_sec)}s / "
        f"沉默 {int(pro.quiet_after_sec)}s 就自己找话，人走开 {int(pro.away_after_sec)}s 后招呼一声，"
        f"每小时最多 {int(pro.max_per_hour)} 次，问模型={'开' if pro.model_nudge else '关'}）"
    )
    print(
        f"          进阶：{'翻长期记忆找话头' if pro.topic_from_memory else '不翻记忆'} / "
        f"{f'{int(pro.night_start_hour)} 点后劝睡' if pro.time_aware else '不分时段'}"
        f"，现在 {proactive.clock_text()}（{proactive.time_phase(time.localtime().tm_hour, pro)}）"
    )
    idle = user_idle_seconds()
    print(f"键鼠空闲 : {'拿不到（非 Windows，就不判断走开/回来）' if idle is None else f'{idle:.1f} 秒（实测）'}")
    # 长什么样：一份 `ui.pet_style`（见 pet/style.py）。没配过的人看到的就是出厂那套，
    # 写坏的那几项会被退回预设并在这儿说一句——不然用户会以为自己改的那行没生效。
    style = PetStyle.from_config(cfg.ui)
    frame_files = sorted((ASSETS_DIR / "pet").glob("idle_*.png"))
    if bool(getattr(cfg.ui, "prefer_vector", False)):
        source = "装了图片帧也先画这套矢量长相（ui.prefer_vector）"
    elif frame_files:
        source = f"assets/pet 里有 {len(frame_files)} 张待机帧，默认先播帧"
    else:
        source = "assets/pet 里没装帧，就画这套矢量长相"
    print(f"形象     : {style.describe()}（{source}；右键菜单「设计我的形象…」可现调现换）")
    for problem in style.problems:
        print(f"          注意：{problem}")

    for index, monitor in enumerate(capture.list_monitors()):
        tag = "全部屏幕" if index == 0 else f"屏幕{index}"
        print(f"  {tag}: {monitor.get('width')}x{monitor.get('height')} @ ({monitor.get('left')},{monitor.get('top')})")

    print("[1/6] 取一帧画面 ...")
    try:
        if image_path:
            image = Image.open(image_path).convert("RGB")
            print(f"      使用图片：{image_path}")
        else:
            image = capture.grab(cfg.capture.region)
    except Exception as exc:
        print(f"      失败：{exc}")
        return 1
    print(f"      尺寸 {image.width}x{image.height}")

    print("[2/6] 缩放 + 指纹 ...")
    small = capture.shrink(image, int(cfg.capture.max_width))
    digest = capture.dhash(small)
    payload = capture.to_jpeg_base64(small, int(cfg.capture.jpeg_quality))
    print(f"      送给模型：{small.width}x{small.height}，base64 约 {len(payload) // 1024} KB")
    print(f"      dHash    : {digest:016x}（画面没变化时就不会再调用模型）")
    print(f"      亮度     : {capture.brightness(small):.2f}")
    print(f"      主色调   : {capture.dominant_hue(small)}")

    print("[3/6] 读画面文字（弹幕/字幕）...")
    ocr_text = ""
    if not cfg.ocr.enabled:
        print("      已在配置里关掉（ocr.enabled = false），跳过")
    else:
        reader = TextReader(cfg)
        if not reader.enabled:
            print("      后端被设成 off，跳过")
        elif not reader.available():
            print("      没有可用的 OCR 后端（Windows 语言包缺失？装有 rapidocr 也行）")
        else:
            started = time.monotonic()
            result = reader.read(image, digest)
            if not result.ok:
                print(f"      失败：{result.error}")
            else:
                ocr_text = result.as_text(cfg.ocr.max_lines)
                print(f"      后端 {reader.engine_name}，耗时 {time.monotonic() - started:.2f}s，"
                      f"识别到 {len(result.lines)} 行")
                for line in result.lines[:6]:
                    print(f"        · {line}")

    print("[4/6] 前台窗口 + 长期记忆 ...")
    window = foreground.describe() if cfg.ocr.window_title else ""
    print(f"      当前窗口：{window or '(拿不到)'}")
    memory = Memory(cfg)
    stats = memory.stats()
    print(f"      记忆文件：{stats['path']}")
    print(f"      已记录  ：{stats['entries']} 条吐槽，画像：{stats['profile'] or '(还没有)'}")
    if stats["top_tags"]:
        print("      常看标签：" + "、".join(f"{tag}×{count}" for tag, count in stats["top_tags"]))
    if stats.get("dialog"):
        # 私下统计：它自己说过的话偏哪一类（记录/分析/学习）——不上界面
        print("      对话类型：" + dialog.tally(dict(stats["dialog"])))
    context = memory.context()
    if context:
        print("      会塞给模型的背景：")
        for line in context.splitlines():
            print(f"        · {line}")
    topics = memory.topics()
    if topics:
        print("      能拿去搭话的话头：" + "、".join(f"{t.label}×{t.count}" for t in topics))
    else:
        print("      能拿去搭话的话头：(还不够，多陪你看几段就有了)")

    print("[5/6] 调用视觉模型 ...")
    client = VisionClient(cfg)
    if not client.ready:
        print("      未配置 API Key，跳过。想先看效果可以把 provider 设为 mock。")
        _dry_run_section(cfg, memory)
        print("=== 自检结束（部分跳过）===")
        return 0
    started = time.monotonic()
    try:
        comment = client.describe(
            small,
            ocr_text=ocr_text,
            window=window,
            memory=memory.context(),
            clock=proactive.clock_text(),
        )
    except VlmError as exc:
        print(f"      失败：{exc}")
        return 1
    print(f"      耗时 {time.monotonic() - started:.1f}s")
    if comment is None:
        print("      这次它选择沉默（[沉默]）")
    else:
        print(f"      情绪：{mood.label(comment.mood)}（{comment.mood}）")
        print(f"      对话类型（私下分类，不上界面）：{dialog.label(dialog.classify(comment.text, mood=comment.mood))}")
        print(f"      吐槽：{comment.text}")
        parsed = getattr(comment, "scene", None)
        if parsed is not None and not parsed.is_empty():
            print(f"      场景 5W1H：{parsed.line()}")
        else:
            print("      场景 5W1H：（这轮没给场景行——检查模型有没有按提示词的两行格式回）")

    _watch_section(cfg, client, small, ocr_text, window, comment)

    _dry_run_section(cfg, memory)

    print("=== 自检结束，一切正常 ===")
    return 0


def _watch_section(cfg, client, small, ocr_text: str, window: str, comment) -> None:
    """看片笔记这一节：验证"视频一刷出来就先读完、之后聊到它答得上话"这条链路。"""
    print("[5.5/6] 看片笔记（换视频时它先把这一眼读完）...")
    if not bool(getattr(cfg.watch, "enabled", True)):
        print("      配置里关掉了（watch.enabled = false），跳过")
        return
    started = time.monotonic()
    try:
        note = client.absorb(small, ocr_text=ocr_text, window=window, clock=proactive.clock_text())
    except VlmError as exc:
        print(f"      失败：{exc}")
        return
    except Exception as exc:
        print(f"      异常：{exc}")
        return
    if note is None:
        print("      没读出来（模型这轮没按六行格式回，检查它是不是被别的话带跑了）")
        return

    log = WatchLog(
        timeline_size=int(getattr(cfg.watch, "timeline_size", 12)),
        history_size=int(getattr(cfg.watch, "note_history", 5)),
    )
    log.start_video(note)
    parsed = getattr(comment, "scene", None) if comment is not None else None
    scene_line = parsed.line() if parsed is not None and not parsed.is_empty() else ""
    log.remember(
        scene=scene_line,
        ocr=ocr_text,
        said=getattr(comment, "text", "") if comment is not None else "",
    )

    print(f"      耗时 {time.monotonic() - started:.1f}s，它记下的是：")
    for row in note.block().splitlines():
        print(f"        · {row}")

    demo_question = (note.keywords[0] + "是怎么回事") if note.keywords else "这视频讲的啥"
    context = log.context_for(demo_question, limit=int(getattr(cfg.watch, "context_items", 6)))
    print(f"      举例：你问「{demo_question}」时，喂给模型的看片记录（{len(context)} 字）：")
    for row in context.splitlines()[:4]:
        print(f"        {row[:80]}")
    print("      （回答问题时就是靠这一段，它才答得上「里面到底讲了啥」）")


def _dry_run_section(cfg, memory=None) -> None:
    print("[6/6] 主动搭话时序预演（假时钟，几秒跑完几分钟的等待）...")
    for line in proactive_timeline(cfg, memory):
        print(f"      {line}")


def proactive_timeline(cfg, memory=None) -> List[str]:
    """用假时钟把"画面静止 → 自己找话 / 人走开 → 打盹 / 人回来 → 招呼 / 深夜劝睡"跑一遍。

    真等下去要好几分钟，这里只把传给策略的时间往前推，判定逻辑和线上完全一样。
    顺手把长期记忆里的真实话头喂给它，看"你最近老在看的东西"会不会被用上。
    """
    pro = cfg.proactive
    lines: List[str] = []

    topics = []
    if memory is not None and pro.topic_from_memory:
        try:
            topics = memory.topics()
        except Exception as exc:  # 自检不因为这个挂掉
            lines.append(f"（话题读取失败：{exc}）")
    policy = ProactivePolicy(cfg, topic_source=(lambda: topics) if topics else None)

    clock = [1000.0]
    if not (pro.model_nudge and cfg.ready):
        lines.append('注：没配 API Key（或关了 proactive.model_nudge），"自己找话"那两种会被跳过')
    if topics:
        lines.append("手里的话头：" + "、".join(f"{t.label}×{t.count}" for t in topics))
    else:
        lines.append("手里的话头：暂时没有（记忆还没攒够，这次就不会出现「聊你最近老看的」）")

    def step(seconds: float, changed: bool, idle: float, label: str, hour: int = 15) -> None:
        clock[0] += seconds
        nudge = policy.observe(clock[0], changed, idle, hour)
        if nudge is None:
            lines.append(f"{label} → 没开口")
            return
        what = nudge.line if nudge.local else f"(要问模型) {nudge.note}"
        if nudge.topic:
            what += f" 〔话头：{nudge.topic}〕"
        lines.append(f"{label} → [{nudge.kind}] {what}")

    still = int(pro.still_after_sec)
    quiet = int(pro.quiet_after_sec)
    cooldown = int(pro.cooldown_sec)
    away = int(pro.away_after_sec)
    gap = cooldown + 30
    step(0.0, True, 1.0, "下午三点，画面在动、人也在")
    step(still + 10, False, 1.0, f"画面静止 {still + 10} 秒")
    step(20.0, False, 1.0, "刚说完一句，继续安静地看")
    step(gap, True, 1.0, f"画面一直在动，它已经 {20 + gap} 秒没说话（超过 {quiet} 秒就该自己找话）")
    step(3700.0, True, 1.0, "跳到凌晨两点还在看（顺带过掉了一小时上限）", hour=2)
    step(700.0, False, away + 300, f"凌晨两点，键鼠 {away + 300} 秒没动 + 画面定住", hour=3)
    step(120.0, True, 0.5, "人回来了（凌晨三点）", hour=3)
    info = policy.stats()
    lines.append(
        f"（收尾：状态={info['state']}，本次预演开口 {info['count']} 次，"
        f"最近一小时 {info['recent']} 次 / 上限 {int(pro.max_per_hour)} 次，"
        f"聊过的话头={'、'.join(info['topics_used']) or '无'}）"
    )
    return lines
