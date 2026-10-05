"""进度条识别：量一眼画面底部那条播放进度，判断"这支视频到底看完没有"。

为什么要它
    taste.py 的档案里「看完」值 +2 分，但 `ended` 一直是 False——程序从来不知道
    视频播到哪了，看片笔记那一轮模型也看不出进度。于是那些 "+2" 一次都没算进去过。

为什么不上 OCR
    进度条上没有字。"已播 92%" 是画出来的，不是写出来的，OCR 读不到。

怎么认（纯本地、免费、不联网）
    一条真的进度条长得很有特征：一行里有**两段又长又平**的颜色（已播 + 未播），
    中间只隔一道边，整条横跨播放器大半宽度；而且它**浮在画面上**——把这一段往上
    挪几像素，颜色就明显不一样了（底下是视频内容）。
    于是判定就是：找"够长、两段够平、分界够明显、而且正上方确实不是它自己"的那一对。
    "正上方不是它自己"这条最要紧：它是把背景（黑边、纯色底、播放器外壳）和进度条
    区分开的唯一依据，一定要比同一段 x 范围的平均色，而不是比一个单像素。

保守的几处（宁可认不出来，也别算错）
    · 两段都要够长（各占整条的一小半以上）——0% 和 100% 的条认不出来，无所谓，
      我们只关心"它是不是快到头了"；
    · 认不出来就返回 None，worker 那边只是不加那 2 分，跟以前的版本一模一样；
    · 半透明的进度条（盖在花花的视频上）满足不了"够平"，也会被放过。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# 只看画面底部这一截——播放器的进度条都在这一带
BOTTOM_RATIO = 0.25
# 相邻像素亮度差多少算"不平"（进度条内部是均匀的，视频内容是花的）
FLAT_TOL = 8
# 一段之内最亮-最暗超过这个数就不算平（防着慢慢漂移的渐变底）
FLAT_SPREAD = 12
# 两段各至少要占整条的比例（0.05 = 5%，正好把紧挨着进度条的碎噪声挡在外面）
MIN_SHARE = 0.05
# 整条至少要占画面宽度这么多：短条一定是别的东西（图标、文字下划线之类）
MIN_SPAN_RATIO = 0.35
# 两段亮度差多少才算"看得出一道分界"
MIN_DELTA = 20.0
# 段中央往上挪几像素去取参考色（躲开进度条自己的描边/阴影）
ABOVE_OFFSET = 4
# 那一段正上方（同一段 x 范围）的平均色跟它自己差多少，才算"它浮在画面上"
MIN_BG_DELTA = 16.0


@dataclass(frozen=True)
class ProgressBar:
    """量到的一条进度条。"""

    ratio: float    # 0~1：已播部分占整条的比例
    row: int        # 在第几行量到的（图片坐标，看日志时用得上）
    start: int      # 整条左端（图片坐标）
    end: int        # 整条右端（图片坐标）
    score: float    # 有多像进度条（长 × 对比度），只用来在候选里挑最好的

    def line(self) -> str:
        return f"第 {self.row} 行 · {self.ratio:.0%}（{self.start}~{self.end}）"


def _runs(lum: Sequence[int]) -> List[Tuple[int, int]]:
    """把一行切成一段段"平"的区间（闭区间）。"""
    out: List[Tuple[int, int]] = []
    if not lum:
        return out
    start = 0
    low = high = int(lum[0])
    for index in range(1, len(lum)):
        value = int(lum[index])
        if abs(value - int(lum[index - 1])) > FLAT_TOL:
            if index - 1 > start and (high - low) <= FLAT_SPREAD:
                out.append((start, index - 1))
            start, low, high = index, value, value
        else:
            low = min(low, value)
            high = max(high, value)
    if len(lum) - 1 > start and (high - low) <= FLAT_SPREAD:
        out.append((start, len(lum) - 1))
    return out


def _mean(values: Sequence[int]) -> float:
    return sum(values) / float(len(values)) if values else 0.0


def _scan_row(
    row: Sequence[int],
    above: Optional[Sequence[int]],
    min_run: int,
    min_span: int,
) -> Optional[ProgressBar]:
    """在一行里找进度条：相邻两段"平"的区间凑成的一对。"""
    runs = _runs(row)
    best: Optional[ProgressBar] = None
    for left, right in zip(runs, runs[1:]):
        a1, b1 = left
        a2, b2 = right
        gap = a2 - b1 - 1
        if gap < 0 or gap > 4:
            continue
        len1, len2 = b1 - a1 + 1, b2 - a2 + 1
        span = b2 - a1 + 1
        if span < min_span or min(len1, len2) < min_run:
            continue
        mean1 = _mean(row[a1 : b1 + 1])
        mean2 = _mean(row[a2 : b2 + 1])
        delta = abs(mean1 - mean2)
        if delta < MIN_DELTA:
            continue
        if above is not None:
            # 它得浮在画面上：同一段 x 范围，往上几像素的平均色必须不一样。
            # 黑边、纯色底、播放器外壳都是"上下一样"，这一步就把它们排除了。
            if abs(_mean(above[a1 : b1 + 1]) - mean1) < MIN_BG_DELTA:
                continue
            if abs(_mean(above[a2 : b2 + 1]) - mean2) < MIN_BG_DELTA:
                continue
        ratio = len1 / float(len1 + len2)
        score = span * delta
        if best is None or score > best.score:
            best = ProgressBar(ratio=ratio, row=0, start=a1, end=b2, score=score)
    return best


def detect(image) -> Optional[ProgressBar]:
    """在画面底部找播放进度条。找不到返回 None（= 这一次量不出来，什么都不做）。"""
    if image is None:
        return None
    try:
        gray = image.convert("L")
    except Exception:
        return None
    width, height = gray.size
    if width < 160 or height < 80:
        return None

    depth = max(24, int(height * BOTTOM_RATIO))
    top = height - depth
    band = gray.crop((0, top, width, height))
    data = band.tobytes()          # 一个像素一个字节，按行排
    if len(data) != width * depth:  # pragma: no cover - 只是防着 PIL 行对齐出意外
        return None

    min_run = max(4, int(width * MIN_SHARE))
    min_span = max(24, int(width * MIN_SPAN_RATIO))
    best: Optional[ProgressBar] = None
    for offset in range(depth):
        start = offset * width
        row = data[start : start + width]
        above = None
        if offset >= ABOVE_OFFSET:
            base = (offset - ABOVE_OFFSET) * width
            above = data[base : base + width]
        found = _scan_row(row, above, min_run, min_span)
        # 同一节进度条会在好几行上都被量出来（值一样），所以后来者相等就让它覆盖：
        # 最终留下的是最靠下、最像的那一条。
        if found is not None:
            found = ProgressBar(
                ratio=found.ratio, row=top + offset, start=found.start,
                end=found.end, score=found.score,
            )
            if best is None or found.score >= best.score:
                best = found
    return best
