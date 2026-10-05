"""把 assets/pet 里那套桌宠帧打成「能直接拿去替换的素材包」。

    python tools/pack_assets.py                   # 全流程：下载 → 抠透明 → 归类 → 压缩
    python tools/pack_assets.py --url <图片地址>   # 第一步先下载源图，其余照旧
    python tools/pack_assets.py --no-cutout       # 跳过抠图：只归类 + 压缩现有的帧
    python tools/pack_assets.py --list            # 只归类核对，不出包

双击项目根目录的 pack-assets.cmd 等于跑第一条。

四步各干什么：

1. **下载**：给了 `--url` 就把源图抓下来存进 `assets/source/`；没给就跳过，用本地那张。
2. **抠透明**：源图交给 `tools/make_pet.py`（背景抠成透明底 → 裁边 → 缩放），
   生成整套 `<状态名>_NN.png` 写进 `assets/pet/`。这一步**不自己实现**——
   抠图和抖法的口径只有一份，就在 make_pet 里，这里只负责喊它。
3. **归类**：按三张**真源表**分组核对，一套都不漏：
   `pet/states.py` 的 `POSES`（基础帧 + 日常状态帧）、`pet/play.py` 的 `MOVES`（串门动作帧）、
   `pet/mood.py` 的 `MOODS`（情绪帧）。每组写清「几张帧 + 建议时长」，
   缺哪套只提示不报错（缺了不崩：循环状态退回待机 / 说话，一次性动作退回「蹦一下」）。
4. **压缩**：打成一个 zip（`assets/pet/*` + `README.txt` + `清单.txt`），
   解压后把 `assets` 覆盖到项目根目录、重启挂件即可生效。

命名沿用现成那套（`idle_00.png` / `walk_00.png` / `highfive_00.png`…）：
名字就是 `states.Pose.key` / `play.Move.key`，**不用改任何代码**，覆盖同名文件就认。

要不要先关挂件：**不用**。形象帧是启动时一次性读进去的，这里只是往磁盘写文件，
跟正在跑的那个进程不抢东西；重新生成了帧的话，脚本最后会用它自己那套重启逻辑
（`tools/restart_pet.py`）把挂件换一遍，不想重启就加 `--no-restart`。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
ASSETS_PET = ROOT / "assets" / "pet"
ASSETS_SRC = ROOT / "assets" / "source"
DIST = ROOT / "dist"
DL_NAME = "pet_dl.png"      # --url 抓下来的图存这个名（后缀不猜，PIL 认内容）

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

# 帧数、默认源图这些口径从 make_pet 那儿取，免得两处各写一份、改了一处忘了另一处。
import make_pet  # noqa: E402

from pet import __version__, make_console_safe, mood as mood_mod  # noqa: E402
from pet import play as play_mod, states as states_mod  # noqa: E402

DEFAULT_SOURCE = make_pet.DEFAULT_SOURCE

#: 打包时默认拿哪张源图：跟 `regen.cmd` 里那行 SRC 对齐（现在这套帧就是从它生成的）。
#: 它不在就退回 make_pet 的默认源图，再没有就只能报「找不到源图」。
PREFERRED_SOURCE = ASSETS_SRC / "pet_new.png"


@dataclass(frozen=True)
class Row:
    """素材包里的一套帧。"""

    key: str            #: 帧名前缀 = states.Pose.key / play.Move.key / 情绪名
    label: str          #: 中文名
    seconds: float      #: 建议时长（循环 = 转一圈；一次性 = 播一遍；0 = 由配置定）
    note: str           #: 什么时候用它
    files: int = 0      #: 磁盘上实际找到几张（0 = 这套缺）


@dataclass
class Group:
    """清单里的一个分组。"""

    title: str
    note: str
    rows: List[Row] = field(default_factory=list)


def _rel(path: object) -> str:
    """相对项目根显示；在外面就原样显示。"""
    text = Path(str(path))
    try:
        return str(text.relative_to(ROOT))
    except ValueError:
        return str(text)


def _flush() -> None:
    """把已经 print 的几行立刻刷出去。

    要起子进程之前必须刷一次：子进程是**直接写控制台**的，而父进程的 stdout 在
    重定向到文件时是块缓冲——不刷的话，make_pet 那一整段输出会跑到脚本最开头去
    （真踩过，看着像"步骤顺序乱了"）。
    """
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _frame_name(path: Path) -> Tuple[str, int, str]:
    """拆出「前缀 + 末尾数字 + 后缀」，按数字排序（跟 sprite._frame_order 一个口径）。"""
    match = re.search(r"(\d+)$", path.stem)
    if not match:
        return (path.stem, 0, path.suffix)
    return (path.stem[: match.start()], int(match.group(1)), path.suffix)


def frames_of(key: str) -> List[Path]:
    """这套帧在磁盘上的全部文件，按帧号排好（png / webp 都认，跟渲染器一样）。"""
    if not ASSETS_PET.is_dir():
        return []
    return sorted(
        list(ASSETS_PET.glob(f"{key}_*.png")) + list(ASSETS_PET.glob(f"{key}_*.webp")),
        key=_frame_name,
    )


def anim_period() -> float:
    """待机那套多久转一圈：读 config.json 的 ui.anim_period（没配就拿默认配置的）。"""
    for path in (ROOT / "config.json", ROOT / "config.example.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        value = (data.get("ui") or {}).get("anim_period")
        try:
            if float(value) > 0:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 30.0


def emotion_tempo() -> Dict[str, float]:
    """情绪 → tempo（一个周期相对待机的倍率），真源在 `sprite.MOOD_STYLE`。

    要 import 一个带 Qt 的模块，值不值得？值得——建议时长就是拿它算的；
    万一 import 不进来（没装 PySide6）也不算错，退回「时长待定」。
    """
    try:
        from pet.sprite import MOOD_STYLE
    except Exception:
        return {}
    return {name: float(style.get("tempo", 1.0)) for name, style in MOOD_STYLE.items()}


def _row(key: str, label: str, seconds: float, note: str) -> Row:
    """造一行，顺手把磁盘上这套帧的实况带上。"""
    return Row(key, label, float(seconds), note, len(frames_of(key)))


def build_groups() -> List[Group]:
    """按三张真源表把整套素材归好类（顺序就是给人看的顺序）。"""
    period = anim_period()
    tempo = emotion_tempo()

    base = Group("基础帧", "必须。这两套一有形象就活了，其余都能省。")
    for pose in states_mod.POSES:
        if pose.key in ("idle", "talk"):
            base.rows.append(_row(pose.key, pose.label, pose.seconds, "一直循环"))

    moods = Group("情绪帧", "模型给每句话打一个情绪（见 pet/mood.py），有帧就切过去。")
    for name, info in mood_mod.MOODS.items():
        label = str(info.get("label") or name)
        seconds = period * tempo[name] if name in tempo else 0.0
        moods.rows.append(_row(name, label, seconds, "情绪一来就换这套"))

    acts = Group("串门动作帧", "两只桌宠凑一起才做的动作（见 pet/play.py），动作名要过网。")
    for move in play_mod.MOVES:
        acts.rows.append(_row(move.key, move.label, move.seconds, "两只一起玩时按进度播"))

    daily = Group("日常状态帧", "它自己待着的样子 + 你点它一下的反应（见 pet/states.py）。")
    for pose in states_mod.POSES:
        if pose.key in ("idle", "talk"):
            continue
        daily.rows.append(
            _row(pose.key, pose.label, pose.seconds, "一直循环" if pose.loop else "播一遍就停")
        )
    return [base, moods, acts, daily]


def _frames_text(row: Row) -> str:
    return "缺" if not row.files else f"{row.files} 张"


def _seconds_text(row: Row) -> str:
    return "—" if not row.seconds else f"{row.seconds:.1f}s"


def report_lines(groups: Sequence[Group]) -> List[str]:
    """归类报告（控制台和 zip 里那份清单共用同一份文字）。"""
    lines: List[str] = []
    for group in groups:
        sets = sum(1 for row in group.rows if row.files)
        frames = sum(row.files for row in group.rows)
        lines.append(f"【{group.title}】{sets}/{len(group.rows)} 套 · {frames} 帧 —— {group.note}")
        for row in group.rows:
            mark = "  " if row.files else "⚠ "
            lines.append(
                f"{mark}{row.key}（{row.label}） {_frames_text(row)} · "
                f"建议 {_seconds_text(row)} · {row.note}"
            )
        lines.append("")
    return lines


def totals(groups: Sequence[Group]) -> Tuple[int, int, List[str]]:
    """（有帧的套数、总帧数、缺的那几套）。"""
    sets = sum(1 for group in groups for row in group.rows if row.files)
    frames = sum(row.files for group in groups for row in group.rows)
    missing = [row.key for group in groups for row in group.rows if not row.files]
    return sets, frames, missing


def build_manifest(
    groups: Sequence[Group],
    sets: int,
    frames: int,
    source: Optional[Path],
) -> str:
    """zip 里那份「清单.txt」：一眼能数清有哪几套、各几张、建议多久。"""
    lines = [
        "screen-pet 桌宠素材包 · 清单",
        f"版本：v{__version__}",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}",
        f"源图：{_rel(source) if source else '（这次没有重生成，用的是 assets/pet 里现有的帧）'}",
        f"帧目录：{_rel(ASSETS_PET)}",
        f"合计：{sets} 套 / {frames} 帧",
        "",
    ]
    lines += report_lines(groups)
    lines += [
        "怎么用：解压，把 assets 覆盖到项目根目录（同名文件直接覆盖），重启挂件即可生效。",
        "为什么不用改代码：帧名就是 pet/states.py 里 Pose.key、pet/play.py 里 Move.key，",
        "渲染器按 <名字>_*.png 找帧（见 pet/sprite.py 的 frames_acts），名字对得上就认。",
        "帧要求：透明底 PNG，正方形最好，边长 256px 以上；想换成手画的，覆盖同名文件即可。",
    ]
    return "\n".join(lines) + "\n"


def download(url: str, dest: Path) -> Optional[Path]:
    """第一步里的「下载」：把源图抓下来。抓不下来返回 None（退回本地那张）。"""
    print(f"[1/4] 下载源图 → {_rel(dest)}")
    try:
        import requests
    except ImportError:
        print("      没装 requests（pip install -r requirements.txt），这一步跳过。")
        return None
    try:
        response = requests.get(url, timeout=30.0)
        response.raise_for_status()
    except Exception as exc:
        print(f"      下载失败：{exc}")
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    # 存下来的东西未必真是图（地址给错时可能是一页 HTML），先让 PIL 认一眼再往下走
    try:
        from PIL import Image

        with Image.open(dest) as probe:
            probe.verify()
    except Exception as exc:
        print(f"      下下来的不是能打开的图片（{exc}）；把 --url 后面的地址要成图片直链再试。")
        dest.unlink(missing_ok=True)
        return None
    print(f"      拿到 {len(response.content) / 1024:.0f} KB 的图片。")
    return dest


def pick_default_source() -> Path:
    """没给 --source 时用哪张：先看 regen.cmd 那张，再退回 make_pet 的默认源图。"""
    if PREFERRED_SOURCE.exists():
        return PREFERRED_SOURCE
    return DEFAULT_SOURCE


def local_source(source: Optional[str]) -> Optional[Path]:
    """本地那张源图（没给 --url 时用它）。"""
    path = Path(source) if source else pick_default_source()
    if not path.exists():
        print(f"      找不到源图：{_rel(path)}")
        print(f"      存一张到 {_rel(pick_default_source())}，或者用 --source 指个路径。")
        return None
    print(f"      用本地这张：{_rel(path)}")
    return path


def run_make_pet(source: Path, size: Optional[int], frames: Optional[int]) -> bool:
    """第二步：把源图交给 make_pet（抠图 + 生成整套帧）。

    不 import 它再直接调函数，而是**再起一个进程**：它自己那套控制台保护、
    参数解析、逐行进度输出原样都在，出问题也能单独跑同一条命令复现。
    """
    cmd = [sys.executable, str(TOOLS / "make_pet.py"), str(source)]
    if size:
        cmd += ["--size", str(size)]
    if frames:
        cmd += ["--frames", str(frames)]
    print(f"[2/4] 抠透明 + 生成帧（{_rel(source)} → {_rel(ASSETS_PET)}）")
    _flush()
    if subprocess.call(cmd, cwd=str(ROOT)) != 0:
        _flush()
        print("      生成失败——上面那几行就是原因，下面按现有帧继续。")
        return False
    _flush()
    return True


def pack(zip_path: Path, manifest: str) -> Tuple[int, float]:
    """第四步：把 assets/pet 整个打成一个 zip（帧 + README.txt + 清单.txt）。"""
    items = sorted(path for path in ASSETS_PET.iterdir() if path.is_file())
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in items:
            bundle.write(path, f"assets/pet/{path.name}")
        bundle.writestr("清单.txt", manifest)
    return len(items), zip_path.stat().st_size / 1024 / 1024


def default_zip() -> Path:
    """默认落点：dist/桌宠素材包-<版本>.zip（跟 tools/build.py 出的东西放一起）。"""
    return DIST / f"桌宠素材包-v{__version__}.zip"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack-assets",
        description="把桌宠帧打成能直接替换的素材包（下载 → 抠透明 → 归类 → 压缩）。",
    )
    parser.add_argument("--url", help="源图地址：先下载再抠图（不给就用下面那张本地源图）")
    parser.add_argument(
        "--source",
        help=f"本地源图；默认 {_rel(pick_default_source())}（给了 --url 且下成了就听 --url）",
    )
    parser.add_argument(
        "--no-cutout",
        action="store_true",
        help="跳过抠图：只归类 + 压缩 assets/pet 里现有的帧（手画的帧别重生成）",
    )
    parser.add_argument("--list", action="store_true", help="只归类核对，不出包")
    parser.add_argument("--out", help=f"zip 落点；默认 {_rel(default_zip())}")
    parser.add_argument(
        "--size", type=int, help="每帧边长，原样转给 tools/make_pet.py（默认 256）"
    )
    parser.add_argument(
        "--frames", type=int, help=f"每套几帧，原样转给 tools/make_pet.py（默认 {make_pet.FRAME_COUNT}）"
    )
    parser.add_argument(
        "--no-restart", action="store_true", help="重新生成了帧也不重启挂件（默认会重启一遍）"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    make_console_safe()
    args = build_parser().parse_args(argv)

    print("screen-pet 桌宠素材包：下载 → 抠透明 → 归类 → 压缩\n")

    source: Optional[Path] = None
    if args.no_cutout:
        print("[1/4] 跳过下载（--no-cutout：不重生成，就用现有的帧）。")
    elif args.url:
        source = download(args.url, ASSETS_SRC / DL_NAME)
        if source is None:
            print("      退回本地那张。")
            source = local_source(args.source)
    else:
        print("[1/4] 没有 --url，跳过下载。")
        source = local_source(args.source)

    regenerated = False
    if args.no_cutout or source is None:
        print("[2/4] 跳过抠图，直接用 assets/pet 里现有的帧。")
    else:
        regenerated = run_make_pet(source, args.size, args.frames)

    groups = build_groups()
    sets, frames, missing = totals(groups)
    print(f"[3/4] 归类核对 → {_rel(ASSETS_PET)}")
    for line in report_lines(groups):
        print(line)
    print(f"      合计 {sets} 套 / {frames} 帧")
    if missing:
        print(f"      ⚠ 缺 {len(missing)} 套：{'、'.join(missing)}")
        print("        缺了不崩：一直循环的退回待机 / 说话，一次性动作退回「蹦一下」。")

    if args.list:
        print("\n--list：只核对，不出包。")
        return 0

    if not frames:
        print(f"\n⚠ {_rel(ASSETS_PET)} 里一张帧都没有，没什么可打包的。")
        return 1

    print("[4/4] 压缩")
    zip_path = Path(args.out) if args.out else default_zip()
    count, megabytes = pack(zip_path, build_manifest(groups, sets, frames, source))
    print(f"      写好 {_rel(zip_path)}（{count} 个文件 + 清单.txt，{megabytes:.1f} MB）")
    print("      解压后把 assets 覆盖到项目根目录，重启挂件即可生效。")

    if regenerated and not args.no_restart:
        print("\n帧是新生成的，顺手用当前这套重启逻辑把挂件换一遍（--no-restart 可以不重启）。")
        _flush()
        subprocess.call([sys.executable, str(TOOLS / "restart_pet.py")], cwd=str(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
