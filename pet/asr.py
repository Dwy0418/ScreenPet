"""语音输入：让你直接说话，走 Windows 自带的语音识别（完全离线、零安装）。

和 ocr.py 一个路子：起一次 PowerShell（pet/asr.ps1），识别结果写到临时文件再读回来。
识别引擎是系统里的 System.Speech（"设置 → 时间和语言 → 语音"里那个），
不需要 API Key、不需要联网，音频也不会上传。

    reader = SpeechReader(cfg)
    if reader.available():
        result = reader.listen()      # 阻塞几秒，回来就是文字（可能为空 = 没听清）
    reader.recognizers()              # 这台机器装了哪些语音包

界面里一般不用自己开线程，直接用 ListenTask（QThread + 信号）。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

CREATE_NO_WINDOW = 0x08000000
SCRIPT_PATH = Path(__file__).resolve().parent / "asr.ps1"


class AsrError(RuntimeError):
    """语音识别用不了（没有语音包 / 没有麦克风 / 被隐私设置挡住……）。

    `environment=True` 表示"这台机器本身就不具备条件"（没有录音设备、没装语音包），
    不是代码出了问题。界面上一视同仁（都当"用不了"提示），但**测试**要靠它区分：
    云端 CI 的机器没有麦克风，那种失败不该算回归（见 tools/smoke_test.py 的 test_asr_engine）。
    """

    def __init__(self, message: str, environment: bool = False):
        super().__init__(message)
        self.environment = bool(environment)


@dataclass
class AsrResult:
    text: str = ""
    error: str = ""
    engine: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error


class SpeechReader:
    """用系统自带引擎听一句话。"""

    def __init__(self, cfg, scratch_dir: Optional[Path] = None):
        self.cfg = cfg
        self.asr = cfg.asr
        self._scratch = (
            Path(scratch_dir)
            if scratch_dir
            else Path(tempfile.mkdtemp(prefix="screen-pet-asr-"))
        )
        self._scratch.mkdir(parents=True, exist_ok=True)
        self._engines: Optional[List[str]] = None

    # ---------- 可用性 ----------

    @property
    def enabled(self) -> bool:
        backend = str(getattr(self.asr, "backend", "sapi") or "sapi").lower()
        return bool(getattr(self.asr, "enabled", True)) and backend != "off"

    def script_path(self) -> Path:
        raw = str(getattr(self.asr, "script", "") or "").strip()
        return Path(raw) if raw else SCRIPT_PATH

    def available(self) -> bool:
        return self.enabled and os.name == "nt" and self.script_path().exists()

    @property
    def engine_name(self) -> str:
        return "winasr" if self.available() else "off"

    def recognizers(self) -> List[str]:
        """这台机器上有哪些语音包（列不出来就返回空列表，不抛异常）。"""
        if self._engines is not None:
            return self._engines
        if not self.available():
            self._engines = []
            return self._engines
        out = self._scratch / "asr-engines.txt"
        status = self._scratch / "asr-engines-status.txt"
        rows: List[str] = []
        try:
            self._run("list", out, status, seconds=1.0)
            rows = [
                line.strip()
                for line in out.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except Exception as exc:
            print(f"[asr] 列语音包失败：{exc}")
        self._engines = rows
        return rows

    # ---------- 听一句 ----------

    def listen(self, seconds: Optional[float] = None) -> AsrResult:
        """听一句话（阻塞）。返回的 text 可能是空串（没听清）。"""
        if not self.available():
            raise AsrError("这台机器上用不了语音输入（要 Windows 自带的语音识别）")
        span = float(seconds or getattr(self.asr, "seconds", 6.0) or 6.0)
        out = self._scratch / "asr-heard.txt"
        status = self._scratch / "asr-status.txt"
        started = time.monotonic()
        self._run("listen", out, status, seconds=span)
        elapsed = time.monotonic() - started

        text = out.read_text(encoding="utf-8").strip() if out.exists() else ""
        flag = status.read_text(encoding="utf-8").strip() if status.exists() else ""
        if flag.startswith("error"):
            kind = self._classify(flag)
            raise AsrError(self._explain(flag), environment=kind in ("recognizer", "microphone"))
        engine = flag.split("|", 1)[1] if "|" in flag else ""
        return AsrResult(text, "", engine, elapsed)

    @staticmethod
    def _classify(flag: str) -> str:
        """把 PowerShell 的报错归个类：recognizer / microphone / 其它（空串）。

        归到前两类的意思是"**这台机器**没这个条件"——云端 CI 的机器没麦克风就走这里，
        测试据此跳过而不是判失败；其它报错（超时、进程起不来）一律当真的出错。
        """
        raw = flag[len("error:"):].strip() if flag.startswith("error:") else flag
        low = raw.lower()
        if "recognizer" in low or "no token" in low or "installed" in low:
            return "recognizer"
        if "microphone" in low or "audio" in low or "0x8007" in low:
            return "microphone"
        return ""

    @staticmethod
    def _explain(flag: str) -> str:
        """把 PowerShell 的报错翻译成人话。"""
        raw = flag[len("error:"):].strip() if flag.startswith("error:") else flag
        kind = SpeechReader._classify(flag)
        if kind == "recognizer":
            return f"系统里没有可用的语音识别包（{raw}）"
        if kind == "microphone":
            return (
                f"麦克风用不了（{raw}）：看看有没有麦克风，"
                "以及 Windows 隐私设置里「允许桌面应用访问麦克风」是不是开着"
            )
        return raw or "语音识别失败"

    def _run(self, mode: str, out: Path, status: Path, seconds: float) -> None:
        for path in (out, status):
            if path.exists():
                path.unlink()
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script_path()),
            "-Mode",
            mode,
            "-Seconds",
            f"{max(1.0, float(seconds)):.1f}",
            "-Culture",
            str(getattr(self.asr, "culture", "zh-CN") or "zh-CN"),
            "-OutPath",
            str(out),
            "-StatusPath",
            str(status),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=float(getattr(self.asr, "timeout", 40.0) or 40.0),
                creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise AsrError("语音识别超时了（可能麦克风没声音）") from exc
        except OSError as exc:
            raise AsrError(f"起不了 PowerShell：{exc}") from exc
        if completed.returncode != 0 and not status.exists():
            detail = (completed.stderr or b"").decode("utf-8", "replace").strip()[:200]
            raise AsrError(f"语音识别进程出错：{detail or completed.returncode}")


class ListenTask(QThread):
    """在后台听一句，别把界面卡住；结果通过信号回主线程。"""

    heard = Signal(str)   # 识别到的文字（空串 = 没听清）
    failed = Signal(str)  # 用不了 / 出错

    def __init__(self, reader: SpeechReader, seconds: Optional[float] = None, parent=None):
        super().__init__(parent)
        self.reader = reader
        self.seconds = seconds

    def run(self) -> None:
        try:
            result = self.reader.listen(self.seconds)
        except AsrError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # 兜住一切，别把线程带走
            self.failed.emit(f"语音输入出错：{exc}")
            return
        self.heard.emit(result.text)
