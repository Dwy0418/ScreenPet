"""上班时的健康提醒：喝水 / 站起来走两步 / 闭眼歇一会儿。

为什么单独做一块
    它本来只会"看着画面说话"。你连着写三小时代码，屏幕一点变化都没有，
    它就真的一句话不说——它擅长的是陪看视频，不是照顾坐太久的人。
    这三件事跟画面没关系，是**按时间**提醒的，塞进 proactive 那套
    "画面静止 / 它自己憋久了"的由头里只会互相打架。

三道闸门（缺一道就变成话痨）
    1. 工作时间：只在 work_start_hour ~ work_end_hour 之间提醒（两头相等 = 全天）；
    2. 每件事各自的最小间隔（喝水 45 分钟、站起来 1 小时、闭眼 1.5 小时）；
    3. 全局最小间隔：两条提醒之间至少隔这么久，免得三件事凑一块儿连播；
       另外人不在（键鼠空闲太久）就先不提醒——提醒给谁看呢。

一条都不花接口钱：文案全是本地写好的（跟 proactive 的 DOZING / LATE_NIGHT 一样）。
时间跟 proactive 一样由调用方通过 observe(now, hour, idle) 传进来，所以能用假时钟测。
"""
from __future__ import annotations

import random
import time
from typing import Dict, Optional, Tuple

from .mood import Comment
from .proactive import Nudge
from .proactive import in_night as _in_window

# 三件事（同时也是日志里的由头名）
WATER = "care:water"
STAND = "care:stand"
EYES = "care:eyes"

# 本地台词：想换嘴直接改这里
LINES: Dict[str, Tuple[Comment, ...]] = {
    WATER: (
        Comment("这个点了……喝口水吧", "curious"),
        Comment("嘴都干了吧，去接杯水", "curious"),
        Comment("你水杯空多久了，我替你记着呢", "smirk"),
    ),
    STAND: (
        Comment("坐挺久了，站起来走两步", "speechless"),
        Comment("腰不要了？起来抻一下", "speechless"),
        Comment("别钉在椅子上，晃两圈再回来", "smirk"),
    ),
    EYES: (
        Comment("眼睛该歇会儿了，闭十秒", "speechless"),
        Comment("看这么久，你眼睛不酸我都酸", "speechless"),
        Comment("抬头看看远处，别老盯这一块屏", "curious"),
    ),
}


class CarePolicy:
    """什么时候该提醒他照顾自己。"""

    def __init__(self, cfg, rng: Optional[random.Random] = None):
        self.cfg = cfg
        self.care = cfg.care
        self.rng = rng or random.Random()
        self._last: Dict[str, float] = {}
        self._last_any = 0.0
        self._last_line: Dict[str, str] = {}
        self._started = False
        self.count = 0

    # ---------- 对外的口子 ----------

    @property
    def items(self) -> Tuple[Tuple[str, float], ...]:
        """(由头, 间隔秒)。顺序就是同时到点时的优先顺序。"""
        return (
            (WATER, float(getattr(self.care, "water_every_sec", 2700.0))),
            (STAND, float(getattr(self.care, "stand_every_sec", 3600.0))),
            (EYES, float(getattr(self.care, "eyes_every_sec", 5400.0))),
        )

    def reset(self, now: Optional[float] = None) -> None:
        """把三个计时器都拨到"现在"——刚打开挂件不该立刻被提醒喝水。"""
        stamp = time.monotonic() if now is None else float(now)
        self._last = {kind: stamp for kind, _ in self.items}
        self._last_any = stamp
        self._started = True

    def observe(self, now: float, hour: Optional[int] = None, idle=None) -> Optional[Nudge]:
        """看一眼该不该提醒。要提醒就返回一句本地台词（不花钱），否则 None。"""
        if not bool(getattr(self.care, "enabled", True)):
            return None
        if not self._started:
            self.reset(now)
            return None
        if not self._in_work_hours(hour):
            return None
        if bool(getattr(self.care, "only_when_active", True)) and idle is not None:
            if float(idle) > float(getattr(self.care, "active_idle_sec", 300.0)):
                return None                      # 人不在，先别提醒
        gap = float(getattr(self.care, "min_gap_sec", 600.0))
        if gap > 0 and (now - self._last_any) < gap:
            return None
        kind = self._most_overdue(now)
        if kind is None:
            return None
        comment = self._pick(kind)
        self._last[kind] = now
        self._last_any = now
        self.count += 1
        return Nudge(kind=kind, line=comment.text, mood=comment.mood)

    def stats(self) -> Dict[str, object]:
        now = time.monotonic()
        return {
            "enabled": bool(getattr(self.care, "enabled", True)),
            "count": self.count,
            "work": f"{int(self.care.work_start_hour)}-{int(self.care.work_end_hour)}",
            "due_in": {
                kind: None if kind not in self._last else round(wait - (now - self._last[kind]), 1)
                for kind, wait in self.items
            },
        }

    # ---------- 内部 ----------

    def _in_work_hours(self, hour: Optional[int]) -> bool:
        if hour is None:
            return True
        start = int(self.care.work_start_hour) % 24
        end = int(self.care.work_end_hour) % 24
        if start == end:
            return True                          # 两头相等 = 全天都提醒
        return _in_window(int(hour), start, end)

    def _most_overdue(self, now: float) -> Optional[str]:
        """挑一件"超期最久"的；一件都没到点就返回 None。"""
        best: Optional[str] = None
        best_overdue = 0.0
        for kind, wait in self.items:
            if wait <= 0:
                continue
            overdue = now - self._last.get(kind, now) - wait
            if overdue >= 0 and (best is None or overdue > best_overdue):
                best, best_overdue = kind, overdue
        return best

    def _pick(self, kind: str) -> Comment:
        pool = LINES.get(kind) or (Comment("……", "speechless"),)
        last = self._last_line.get(kind)
        choices = [c for c in pool if c.text != last] or list(pool)
        picked = self.rng.choice(choices)
        self._last_line[kind] = picked.text
        return picked
