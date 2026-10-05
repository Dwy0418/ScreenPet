"""探测用：截一张屏，直接调 Windows 自带 OCR，把识别到的文字打出来。

    python tools/probe_winocr.py

用来确认这台机器到底有没有可用的 OCR 语言包（有中文包才会识别中文）。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pet import capture  # noqa: E402
from pet import make_console_safe  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _decode(raw: bytes) -> str:
    """PowerShell 报错信息在中文系统上是 GBK，这里两种都试一下。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _safe(text: str) -> str:
    """让输出在当前控制台编码下也能打印（中文系统的 PowerShell 是 GBK）。"""
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, "replace").decode(encoding, "replace")


def main() -> int:
    make_console_safe()
    script = ROOT / "pet" / "winocr.ps1"
    if not script.exists():
        print(f"找不到 {script}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        image_path = Path(tmp) / "shot.png"
        out_path = Path(tmp) / "ocr.txt"
        image = capture.grab(None)
        capture.shrink(image, 1600).save(image_path)

        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                str(image_path),
                str(out_path),
            ],
            capture_output=True,
            timeout=30,
        )
        print(f"返回码 {proc.returncode}")
        if proc.stdout:
            print("stdout:", _safe(_decode(proc.stdout))[:400])
        if proc.stderr:
            print("stderr:", _safe(_decode(proc.stderr))[:1200])
        if out_path.exists():
            lines = out_path.read_text(encoding="utf-8").splitlines()
            print(f"识别到 {len(lines)} 行：")
            for line in lines[:25]:
                print("   ", _safe(line))
        else:
            print("没有输出文件，OCR 不可用")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
