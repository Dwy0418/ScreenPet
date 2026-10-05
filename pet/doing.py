"""好友在干嘛：把「我这边主人此刻在忙什么」压成一句**能过网的话**。

串门有两个方向，这里管的是"谁知道谁在忙什么"这一半（聊天那一半在 friends.py）：

* **出访**：我的宠物把「我主人这会儿在忙啥」捎过去（`/visit` 的 `doing` 字段），
  到了人家那边，主人家的问候就能顺口提一句；
* **做客 / 接待**：问一句「你家主人刚在干嘛」（`net.PATH_DOING`），
  对方**在它自己那台机器上**拼一句回去，我这边的宠物再用自己的话说给自己主人听。

隐私边界（跟 friends.py 那条一样，只是方向相反）：

* 原始窗口标题、截图、OCR、看片笔记**一个字都不过网**；
* 过网的只有这一层拼出来的**那一句话**。拼它不调模型（零成本、零延迟、也不会
  被模型自由发挥），而且按 `friends.share_doing` 分三档：
      0  一个字都不说
      1  只说大类（默认）：在打游戏 / 在看视频 / 在干活 / 在忙别的
      2  连具体名字也说——**只有游戏名和片名**；
         「干活」那件永远不带名字，因为它手里那点就是**窗口标题原文**
         （`main.py - pet - Visual Studio Code` 这种），那正是不能说出去的东西。

「刚刚」和「现在」都留着：本子只记**换过的事**（同一件事坐 40 分钟算一件），
所以能说出「刚在看视频，这会儿在打游戏」——对方刚好切个窗口，也不至于报错。
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from . import keyinfo

# 分享档位（friends.share_doing）
SHARE_OFF = 0      # 不说
SHARE_KIND = 1     # 只说大类
SHARE_NAME = 2     # 连游戏名 / 片名也说

# 大类 -> 人话。就这四个，别再加：再细就是细节，细节就是隐私。
_WORD: Dict[str, str] = {
    keyinfo.ACTIVITY_GAME: "在打游戏",
    keyinfo.ACTIVITY_VIDEO: "在看视频",
    keyinfo.ACTIVITY_WORK: "在干活",
    keyinfo.ACTIVITY_OTHER: "在忙别的",
}

MAX_NAME = 20       # 名字（游戏名 / 片名）最多留几个字
DEFAULT_KEEP = 6    # 本子最多记几件（够说"刚刚""刚才那件"就行）


def _name(text: str, limit: int = MAX_NAME) -> str:
    """名字只留干干净净的一截（**不动书名号**：《奔跑吧》第九季 比 奔跑吧》第九季 好看）。"""
    value = " ".join(str(text or "").split())
    value = value.strip(" \t\"'“”「」『』")
    return value[:limit].strip()


def _do(entry: Dict[str, object], level: int) -> str:
    """这一件事怎么说（level 决定带不带名字）。"""
    kind = str(entry.get("kind") or "")
    label = _name(str(entry.get("label") or ""))
    if level >= SHARE_NAME and label:
        if kind == keyinfo.ACTIVITY_VIDEO:
            return f"在看{label}"
        if kind == keyinfo.ACTIVITY_GAME:
            return f"在打{label}"
    return _WORD.get(kind) or _WORD[keyinfo.ACTIVITY_OTHER]


class ActivityBoard:
    """「我这边主人在干嘛」的小本子（纯本地：不上网、不落盘）。

    谁往里写：worker 认出"这件事换了"的时候（见 `worker.AnalysisWorker._activity`）。
    谁往里读：串门的时候——`friends.VisitHub` 通过 `line()` 拿一句话就发走。
    """

    def __init__(self, level: int = SHARE_KIND, owner: str = "", keep: int = DEFAULT_KEEP):
        self.level = int(level)
        self.owner = str(owner or "")
        self.keep = max(1, int(keep))
        self.entries: List[Dict[str, object]] = []

    # ---------- 记 ----------

    def note(self, kind: str, label: str = "", at: Optional[float] = None) -> None:
        """记一件事；跟上一件一模一样就什么都不做（同一件事坐多久都算一件）。"""
        key = str(kind or "")
        if key not in _WORD:
            key = keyinfo.ACTIVITY_OTHER
        label = str(label or "").strip()
        if self.entries:
            last = self.entries[-1]
            if str(last.get("kind")) == key and str(last.get("label")) == label:
                return
        self.entries.append({"kind": key, "label": label, "at": time.time() if at is None else float(at)})
        del self.entries[: max(0, len(self.entries) - self.keep)]

    def clear(self) -> None:
        self.entries = []

    # ---------- 读 ----------

    def now(self) -> Optional[Dict[str, object]]:
        """现在在干嘛（还没记过任何一件就返回 None）。"""
        return dict(self.entries[-1]) if self.entries else None

    def ago(self) -> Optional[Dict[str, object]]:
        """刚刚在干嘛：上一件**跟现在不一样**的事（同一件连着记的不算）。"""
        if len(self.entries) < 2:
            return None
        back = self.entries[-2]
        current = self.entries[-1]
        if str(back.get("kind")) == str(current.get("kind")) and str(back.get("label")) == str(current.get("label")):
            return None
        return dict(back)

    def line(self, owner: str = "", level: Optional[int] = None) -> str:
        """拼成一句能过网的话（空串 = 没什么好说的，别硬凑）。"""
        level = self.level if level is None else int(level)
        if level <= SHARE_OFF:
            return ""
        now = self.now()
        if now is None:
            return ""
        who = (owner or self.owner or "").strip() or "我家主人"
        back = self.ago()
        if back is not None:
            return f"{who}刚{_do(back, level)}，这会儿{_do(now, level)}"
        return f"{who}这会儿{_do(now, level)}"

    # ---------- 收 ----------

    @staticmethod
    def heard(line: str, friend_label: str = "") -> str:
        """把对方回的那句话拼成「给自己主人看的一行」（面板 / 状态栏用）。"""
        text = str(line or "").strip()
        if not text:
            return ""
        who = (friend_label or "").strip()
        return f"{who}那边说：{text}" if who else text
