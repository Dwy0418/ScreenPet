"""把新功能跑一遍给你看：模拟一个下午，把它"什么时候开口、说什么"打成一张表。

    python tools/demo_nudges.py

不联网、不弹窗（mock 模式），也不用等几个小时——用假时钟把时序在毫秒里跑完。
想看的几件事：
    ① 主动夸 / 主动关心（看视频、打游戏、干活，各是什么时候夸、什么时候关心）
    ② 你说难受的时候它怎么回（安慰 + 抱抱）
    ③ 整集资料卡（认出是哪一集 → 做功课 → 卡片长什么样；**默认关着**）
    ④ 内部信息闸（现场那句漏进气泡的话，现在怎么处理；含"念屏幕"那条）
    ⑤ 画面关键信息只认一次（省算力那条）
    ⑥ 跟着你的进度看（进度走一段就读一遍，攒出"看到哪了"）
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pet import episode, humanstyle, keyinfo, make_console_safe, proactive, watchlog  # noqa: E402
from pet.config import Config  # noqa: E402
from pet.mood import Comment  # noqa: E402
from pet.vlm import VisionClient  # noqa: E402


def demo_nudges(cfg: Config) -> None:
    """① 主动夸 / 主动关心：模拟一个下午的时序表。"""
    cfg.proactive.cooldown_sec = 0.0       # 演示用：别让冷却把话压掉
    cfg.proactive.max_per_hour = 99
    # 也是演示用：把"画面定住了 / 它憋太久了"两条摁掉——它们跟"按内容夸"抢戏，
    # 而且这里的时间是跳着走的，很容易在开头就撞上"憋太久"。
    cfg.proactive.quiet_after_sec = 10.0 ** 9
    cfg.proactive.still_after_sec = 10.0 ** 9
    policy = proactive.ProactivePolicy(cfg)
    client = VisionClient(cfg)
    base = 10_000.0
    policy.reset(base)

    def step(minutes: float, hour: int, label: str, activity=None) -> None:
        nudge = policy.observe(base + minutes * 60.0, True, 1.0, hour, activity=activity)
        if nudge is None:
            print(f"  {label:<28} —（这一轮不开口）")
            return
        if nudge.local:
            kind, line = nudge.kind, nudge.line
        else:
            picked = client._mock_nudge(nudge.kind) or Comment("（模型没接上）", "curious")
            kind, line = nudge.kind, picked.text
        print(f"  {label:<28} [{kind}] {line}")

    print('① 主动夸 / 主动关心（假时钟；括号里是"他已经做了多久"）')
    step(3, 14, "看《奔跑吧》第28集 3 分钟", ("video", "《奔跑吧》第九季 第28集", 3))
    step(25, 14, "→ 25 分钟", ("video", "《奔跑吧》第九季 第28集", 25))
    step(56, 14, "→ 56 分钟", ("video", "《奔跑吧》第九季 第28集", 56))
    step(26, 20, "打王者荣耀 26 分钟", ("game", "王者荣耀", 26))
    step(61, 20, "→ 61 分钟", ("game", "王者荣耀", 61))
    step(35, 22, "写代码 35 分钟", ("work", "main.py - pet - Visual Studio Code", 35))
    step(90, 22, "桌面发呆 90 分钟", ("other", "桌面", 90))
    print()


def demo_comfort(cfg: Config) -> None:
    """② 你说难受的时候。"""
    print("② 你说难受的时候（只安慰 + 抱抱，不讲道理）")
    client = VisionClient(cfg)
    for text in ("今天加班到十点，真的好累", "被领导骂了一顿，难受", "这条视频好看吗"):
        comfort = humanstyle.needs_comfort(text)
        reply = client._mock_reply(text, comfort=comfort)
        tag = "诉苦→安慰+抱抱" if comfort else "正常接话"
        print(f"  你：{text:<20} [{tag}] {reply.text if reply else '—'}")
    print()


def demo_episode(cfg: Config) -> None:
    """③ 整集资料卡：认出是哪一集 → 做功课 → 卡片（默认关着，演示里手动打开）。"""
    print('③ 整集资料卡（认出"这是哪一集"就先弄清楚它讲的是什么）')
    cfg.episode.enabled = True     # 默认关：现在主线是"跟着他的进度看"，这张卡要手动开
    cfg.episode.path = str(Path(tempfile.gettempdir()) / "screen-pet-demo-episodes.json")
    info = keyinfo.KeyInfo(shows=["奔跑吧"], episode=["第九季", "第28集"])
    print(f"  画面上的字认出来：{episode.identify(info)}")
    log = episode.EpisodeLog(cfg)
    if log.note(info):
        print(f"  该做功课吗：{log.needs_brief()}")
        log.mark_asked()
        text = VisionClient(cfg).episode_brief("奔跑吧", "第九季", "第28集", extra="抖音、第九季")
        brief = log.apply(log.current_key, text)
        print(f"  资料卡：{brief.line()}")
        print("  进提示词的那一段：")
        for row in brief.block().splitlines():
            print(f"    {row}")
    print()


def demo_internal_guard(cfg: Config) -> None:
    """④ 内部信息闸：现场那句漏进气泡的话。"""
    print("④ 内部信息闸（内部字眼不许进气泡）")
    client = VisionClient(cfg)
    leaked = "画面关键信息：画面关键信息：画面关键信息：/ 哇，这倒计时器，节"
    kept = client._to_comment(leaked, "demo")
    print(f"  模型原话：{leaked}")
    print(f"  进气泡的：{kept.text if kept else '（整句丢掉，当场再要一句）'}")
    dropped = client._to_comment("我把画面关键信息念了一遍", "demo")
    print(f"  夹在中间的（丢掉）：{dropped} / last_drop={client.last_drop}")
    # 现场那句"碎片拼的"：剥掉块头后，剩下的字**全都在画面上写着** → 也是在念屏幕
    screen = "抖音 奔跑吧 第九季 第28期 合集 点赞 收藏 关注 12:06 / 45:00 ×"
    client._screen_text = screen
    junk = "画面关键信息：第孬期 . / > 0 收/A《抖音 ×/ 第集："
    print(f"  念屏幕的（丢掉）：{junk}")
    print(f"    → {client._to_comment(junk, 'demo')} / last_drop={client.last_drop}")
    good = client._to_comment("[吐槽] 都第28集了这倒计时还没走完，离谱", "demo")
    print(f"  同一眼，说人话就放行：{good.text if good else '（被误伤了）'}")
    print(f"  正常台词不受影响：{humanstyle.strip_internal_prefix('现在几点了')!r} "
          f"{humanstyle.looks_like_internal('现在几点了')}")
    print()


def demo_keyinfo_cache(cfg: Config) -> None:
    """⑤ 同一眼的关键信息只认一次。"""
    print("⑤ 画面关键信息只认一次（省算力 / 不刷屏）")
    try:
        from pet.worker import AnalysisWorker
    except Exception as exc:                       # 没装 PySide6 就跳过这一节
        print(f"  （跳过：{exc}）")
        return
    cfg.memory.path = str(Path(tempfile.gettempdir()) / "screen-pet-demo-memory.json")
    cfg.taste.path = str(Path(tempfile.gettempdir()) / "screen-pet-demo-taste.json")
    worker = AnalysisWorker(cfg)
    calls = {"n": 0}
    real = keyinfo.extract

    def counting(lines, window=""):
        calls["n"] += 1
        return real(lines, window)

    keyinfo.extract = counting
    try:
        lines = ["抖音", "奔跑吧 第九季", "第28集", "03:10 / 12:26"]
        for index in range(3):
            worker._key_block(list(lines), "抖音 - Google Chrome", "同一段识别文字")
            print(f"  第 {index + 1} 眼看同一段字：累计认出 {calls['n']} 次")
        worker._key_block(["哔哩哔哩", "《歌手》第八期"], "哔哩哔哩 - Chrome", "换了内容")
        print(f"  换了一支片子之后：累计认出 {calls['n']} 次")
    finally:
        keyinfo.extract = real
    print()


def demo_viewing_progress(cfg: Config) -> None:
    """⑥ 跟着你的进度看：进度每走一段就重读一遍，攒出"他这支看到哪了"。"""
    print("⑥ 跟着你的进度看（一路读过来，不是只知道开头那一眼）")
    try:
        from pet.worker import AnalysisWorker
    except Exception as exc:                       # 没装 PySide6 就跳过这一节
        print(f"  （跳过：{exc}）")
        return
    cfg.memory.path = str(Path(tempfile.gettempdir()) / "screen-pet-demo-memory.json")
    cfg.taste.path = str(Path(tempfile.gettempdir()) / "screen-pet-demo-taste.json")
    cfg.capture.absorb_progress_min_gap_sec = 0.0   # 演示用：不要"刚读过就别读"
    worker = AnalysisWorker(cfg)
    now = time.monotonic()
    worker._last_absorb_at = now
    step = float(worker.cfg.capture.absorb_progress_step)
    print(f"  规矩：他这支片子每往前走 {step:.0%} 就重读一遍（只记不说话）")

    rows = (
        (0.00, watchlog.WatchNote(title="第28集", what="几个人在户外分组做任务")),
        (0.08, None),
        (0.21, watchlog.WatchNote(title="第28集", what="第一轮结束，输的那队在受罚", point="有人笑场")),
        (0.40, None),
        (0.62, watchlog.WatchNote(title="第28集", what="换到第二轮，倒计时器把人整不会了", point="倒计时器")),
    )
    for ratio, note in rows:
        worker._video_max_ratio = ratio
        if note is None:
            due = worker._should_refresh_absorb(now)
            print(f"  到 {ratio:.0%}：{'该重读了' if due else '离上一次读得还不够远，先不读'}")
            continue
        worker._remember_viewing(note)
        print(f"  到 {ratio:.0%}：读完这一段 → {worker._seen_notes[-1]}")

    print("  下一条提示词里会带上这些：")
    for row in worker._viewing_block().splitlines():
        print(f"    {row}")
    print()


def main() -> int:
    make_console_safe()          # 台词里有 ❤ 这种字符，GBK 控制台会炸
    cfg = Config()
    print(f"配置：provider={cfg.provider}（mock 不联网）｜"
          f"夸={cfg.proactive.praise_after_min:.0f} 分钟 关心={cfg.proactive.care_after_min:.0f} 分钟\n")
    demo_nudges(cfg)
    demo_comfort(cfg)
    demo_episode(cfg)
    demo_internal_guard(cfg)
    demo_keyinfo_cache(cfg)
    demo_viewing_progress(cfg)
    print("想做真实效果：先配 API Key（provider 换成 zhipu / dashscope 等），再跑 python main.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
