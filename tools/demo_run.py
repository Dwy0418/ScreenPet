"""视觉自检：启动挂件 → 等几秒 → 截一张屏幕图 → 关掉。

    python tools/demo_run.py 9 snap.png

用来人工确认形象位置、气泡、透明度是不是对（截屏走的是 mss，跟挂件自己一样的视角）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import mss
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pet import make_console_safe  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    make_console_safe()
    wait = float(sys.argv[1]) if len(sys.argv) > 1 else 9.0
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "snap.png"
    if not out.is_absolute():
        out = ROOT / out

    log_path = out.with_name(out.stem + ".log")
    log = open(log_path, "w", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--interval", "1.5"],
        cwd=str(ROOT),
        stdout=log,
        stderr=log,
    )
    try:
        time.sleep(wait)
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            shot = sct.grab(sct.monitors[0])
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        image.save(out)
        print(f"saved {out} ({image.width}x{image.height})")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            proc.kill()
        log.close()
        size = log_path.stat().st_size if log_path.exists() else 0
        print(f"挂件日志：{log_path}（{size} 字节，非空一般说明有报错）")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
