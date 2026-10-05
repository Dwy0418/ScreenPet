"""重启挂件：先停再起，改完代码让它生效的最短一步。

    python tools/restart_pet.py            # 先停掉在跑的挂件，再用新代码起一个
    python tools/restart_pet.py --stop     # 只关（跑冒烟测试前用它，先把在跑的退掉）
    python tools/restart_pet.py --start    # 只起
    python tools/restart_pet.py --status   # 只看看现在跑没跑

双击项目根目录的 restart.cmd 等于跑第一条。

为什么改代码前非得先停：

1. **源码是启动时读的**——挂件在跑，你改的 `.py` 一行都不生效；
2. **它退出时会把内存里的配置和记忆写回文件**，所以"开着的时候改配置"会被盖掉；
3. 冒烟测试跑的是同一份源码和素材，两边一起动项目目录容易打架。

于是流程固定成：`--stop` → 改 → 跑测试 → `--start`。
停和起都复用 `tools/set_key.py` 里的同一套实现（`find_running_pet` / `stop_pets` / `start_pet`），
免得两边各写一份、行为还不一样。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

# tools/ 自己要在 sys.path 上，才能 import 同目录的 set_key
sys.path.insert(0, str(Path(__file__).resolve().parent))

from set_key import find_running_pet, start_pet, stop_pets  # noqa: E402

from pet import make_console_safe  # noqa: E402


def _pids(ids: Sequence[int]) -> str:
    return "、".join(str(pid) for pid in ids)


def stop(wait: float = 1.5) -> List[int]:
    """停掉在跑的挂件，返回停完还剩的几个（正常是空的）。"""
    running = find_running_pet()
    if not running:
        print("没发现正在跑的挂件，跳过停止这一步。")
        return []
    print(f"先停掉正在跑的挂件（PID {_pids(running)}）...")
    stop_pets(running)
    time.sleep(wait)     # 等它把配置和记忆写完、窗口放干净
    left = find_running_pet()
    print("已停。" if not left else f"没停干净的还有：PID {_pids(left)}")
    return left


def start() -> Optional[int]:
    """用当前代码把挂件起起来，返回新 PID（失败给 None）。"""
    pid = start_pet()
    if pid:
        print(f"已用当前代码启动挂件（PID {pid}）。日志追加写进 run.out。")
        return pid
    print("启动失败：双击 run.cmd 手动起一下，或者 `python main.py` 看报错。")
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="重启挂件（先停再起）")
    pick = parser.add_mutually_exclusive_group()
    pick.add_argument("--stop", action="store_true", help="只关掉在跑的挂件")
    pick.add_argument("--start", action="store_true", help="只把挂件起起来")
    pick.add_argument("--status", action="store_true", help="只看看现在跑没跑")
    args = parser.parse_args(argv)

    if args.status:
        running = find_running_pet()
        print(f"挂件在跑：PID {_pids(running)}" if running else "挂件没在跑。")
        return 0

    if not args.start:
        stop()
        if args.stop:
            return 0

    return 0 if start() else 1


if __name__ == "__main__":
    make_console_safe()
    raise SystemExit(main())
