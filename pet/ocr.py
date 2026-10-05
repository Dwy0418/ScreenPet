"""弹幕 / 字幕 OCR：把画面上的文字读出来，让模型知道"大家在聊什么"。

两个后端，都是**本地**的，图片不会离开这台机器：
  * winocr   —— Windows 自带的 Windows.Media.Ocr（默认，零依赖、离线）
  * rapidocr —— 装了 rapidocr-onnxruntime 就优先用它（更快，不用起进程）

按 dHash 指纹缓存：同一帧不会重复识别；失败两次后自动关闭自身，不刷屏。
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from PIL import Image

CREATE_NO_WINDOW = 0x08000000
SCRIPT_PATH = Path(__file__).resolve().parent / "winocr.ps1"

# Windows OCR 引擎对超大图会自行缩放，超过这个宽度就自己先缩一下
MAX_ENGINE_WIDTH = 2560

_CJK_SPACE = re.compile(r"(?<=[\u3400-\u9fff\u3040-\u30ff])\s+(?=[\u3400-\u9fff\u3040-\u30ff])")
_MEANINGFUL = re.compile(r"[\w\u3400-\u9fff\u3040-\u30ff]")


def clean_lines(raw: Sequence[str], min_chars: int = 2) -> List[str]:
    """去掉碎字符、纯符号行，合并中文之间被 OCR 拆出来的空格。"""
    out: List[str] = []
    seen = set()
    for item in raw:
        text = _CJK_SPACE.sub("", str(item or "")).strip()
        text = re.sub(r"\s{2,}", " ", text)
        if len(_MEANINGFUL.findall(text)) < min_chars:
            continue  # 一个字的碎片、纯标点、纯图标，都没用
        if len(set(text)) <= 1:
            continue  # "oooo"、"...." 之类
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


@dataclass
class OcrResult:
    lines: List[str] = field(default_factory=list)
    engine: str = ""
    elapsed: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_text(self, max_lines: int = 12) -> str:
        """拼成给模型看的短文本；行数从下往上取（弹幕一般堆在底部）。"""
        if not self.lines:
            return ""
        picked = self.lines[-max_lines:] if len(self.lines) > max_lines else self.lines
        return " / ".join(picked)


class _Engine:
    name = "base"

    def available(self) -> bool:  # pragma: no cover - 由子类实现
        return False

    def read(self, image: Image.Image, scratch: Path) -> List[str]:  # pragma: no cover
        raise NotImplementedError


class WinOcrEngine(_Engine):
    """Windows 自带 OCR。要起一次 PowerShell（约 0.4s），换来零依赖 + 完全离线。"""

    name = "winocr"

    def __init__(self, language: str = "zh-Hans", timeout: float = 10.0):
        self.language = language
        self.timeout = timeout

    def available(self) -> bool:
        return SCRIPT_PATH.exists()

    def _prepare(self, image: Image.Image, scratch: Path) -> Path:
        path = scratch / "ocr-input.png"
        if image.width > MAX_ENGINE_WIDTH:
            ratio = MAX_ENGINE_WIDTH / float(image.width)
            image = image.resize(
                (MAX_ENGINE_WIDTH, max(1, int(image.height * ratio))), Image.LANCZOS
            )
        image.convert("RGB").save(path, format="PNG")
        return path

    def read(self, image: Image.Image, scratch: Path) -> List[str]:
        source = self._prepare(image, scratch)
        target = scratch / "ocr-output.txt"
        if target.exists():
            target.unlink()

        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT_PATH),
            str(source),
            str(target),
        ]
        if self.language:
            command.append(self.language)

        proc = subprocess.run(
            command,
            capture_output=True,
            timeout=self.timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"OCR 进程退出码 {proc.returncode}")
        if not target.exists():
            raise RuntimeError("系统 OCR 没有产出结果（可能缺少中文语言包）")
        return [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


class RapidOcrEngine(_Engine):
    """可选后端：pip install rapidocr-onnxruntime。不启进程、更快，多装约 100MB。"""

    name = "rapidocr"

    def __init__(self, min_score: float = 0.5):
        self.min_score = min_score
        self._engine = None
        self._failed = False

    def available(self) -> bool:
        if self._failed:
            return False
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except Exception:
            return False
        return True

    def _ensure(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine

    def read(self, image: Image.Image, scratch: Path) -> List[str]:
        path = scratch / "ocr-input.png"
        image.convert("RGB").save(path, format="PNG")
        engine = self._ensure()
        result, _elapse = engine(str(path))
        if not result:
            return []
        lines: List[str] = []
        for item in result:
            if len(item) >= 3:
                text, score = item[1], float(item[2])
                if score < self.min_score:
                    continue
                lines.append(str(text))
            elif len(item) == 2:
                lines.append(str(item[1]))
        return lines


class TextReader:
    """对外的唯一入口：挑后端、按指纹缓存、失败自动降级。"""

    MAX_FAILURES = 2

    def __init__(self, cfg, scratch_dir: Optional[Path] = None):
        self.cfg = cfg
        self.ocr = cfg.ocr
        self._scratch = (
            Path(scratch_dir)
            if scratch_dir
            else Path(tempfile.mkdtemp(prefix="screen-pet-ocr-"))
        )
        self._scratch.mkdir(parents=True, exist_ok=True)
        self._cache = {}
        self._engine: Optional[_Engine] = None
        self._failures = 0
        self.disabled_reason = ""
        self.last_engine = ""

    # ---------- 后端选择 ----------

    def _pick(self) -> Optional[_Engine]:
        if self._engine is not None:
            return self._engine
        wanted = str(self.ocr.backend or "auto").lower()
        candidates: List[_Engine] = []
        if wanted in ("auto", "rapidocr"):
            candidates.append(RapidOcrEngine())
        if wanted in ("auto", "winocr"):
            candidates.append(WinOcrEngine(language=self.ocr.language, timeout=self.ocr.timeout))
        for engine in candidates:
            if engine.available():
                self._engine = engine
                return engine
        return None

    @property
    def enabled(self) -> bool:
        return bool(self.ocr.enabled) and str(self.ocr.backend).lower() != "off"

    def available(self) -> bool:
        return self.enabled and self._pick() is not None

    @property
    def engine_name(self) -> str:
        engine = self._pick() if self.enabled else None
        return engine.name if engine else "off"

    # ---------- 识别 ----------

    def read(self, image: Image.Image, digest: int) -> OcrResult:
        """识别一帧；同一个指纹（画面没变）直接吃缓存。"""
        if not self.enabled:
            return OcrResult(error="off")
        if digest in self._cache:
            return OcrResult(lines=list(self._cache[digest]), engine=self.last_engine)

        engine = self._pick()
        if engine is None:
            return OcrResult(error="没有可用的 OCR 后端")

        started = time.monotonic()
        try:
            lines = clean_lines(engine.read(image, self._scratch), self.ocr.min_chars)
        except Exception as exc:
            self._failures += 1
            if self._failures >= self.MAX_FAILURES:
                self.disabled_reason = str(exc)
                self._engine = None
            return OcrResult(engine=engine.name, error=str(exc))

        self._failures = 0
        self.last_engine = engine.name
        self._remember(digest, lines)
        return OcrResult(lines=lines, engine=engine.name, elapsed=time.monotonic() - started)

    def _remember(self, digest: int, lines: Sequence[str]) -> None:
        self._cache[digest] = list(lines)
        limit = max(1, int(self.ocr.cache_size))
        while len(self._cache) > limit:
            self._cache.pop(next(iter(self._cache)))

    def read_text(self, image: Image.Image, digest: int) -> str:
        """识别并直接拼成给模型的文本（出错或没文字就是空串）。"""
        result = self.read(image, digest)
        if not result.ok:
            return ""
        return result.as_text(self.ocr.max_lines)


