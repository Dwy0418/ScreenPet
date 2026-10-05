"""完整记忆存档：memory.json 会被裁剪，这里一条都不丢。

为什么要有它
    `memory.json` 是**给模型看的那一份**：它只留最近的 `memory.max_entries` 条
    （默认 200），这样提示词、读写都不会越拖越慢。副作用是——**看得久了，早先记的
    东西就真没了**（现场就是这样：翻记忆文件只剩最近几天的，更早的连痕迹都没有）。

    这个模块给记忆配一条**只增不减**的流水：每记一条吐槽、每更新一次观众画像，就往
    `data/memory/archive.jsonl` 追加一行（JSON Lines，一行一条）。`memory.json` 怎么
    裁都不影响它；进程被杀最多丢"正在写的那一行"，比 `memory.json` 每 20 秒才落一次
    盘安全得多。

    写入失败**只打印一行日志、绝不抛异常**：存档是"顺带留一份"，不能因为它自己挂了
    就连吐槽都不记了。

存档长什么样（utf-8，一行一条）
    {"v": 1, "kind": "episode", "ts": 1790731834.5, "text": "这操作我真看不懂",
     "mood": "speechless", "tags": ["搞笑", "名场面"], "scene": "…", "dialog": "analyze"}
    {"v": 1, "kind": "profile", "ts": 1790731800.0, "profile": "爱看游戏集锦…",
     "tags": ["游戏", "集锦"]}

    读得顺（每行一个 JSON 对象）、也经得起手翻（坏行跳过，不影响别的行）。

想看一眼
    python -m pet.memarchive            条数 / 时间范围 / 常看标签
    python -m pet.memarchive tail 20    最近 20 条（给人看的那种）
    python -m pet.memarchive path       只告诉我文件在哪，好自己打开

    （右键挂件 →「打开完整存档」是同一个文件。）
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

EPISODE = "episode"
PROFILE = "profile"


class MemoryArchive:
    """只增不减的一本记忆流水：一行一条 JSON，追加式写入。"""

    VERSION = 1

    def __init__(self, path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self._lock = threading.Lock()      # 采集在后台线程、补课也可能在别的线程
        self._warned = False               # 只提醒一次，别把控制台刷满
        self._count: Optional[int] = None  # 懒加载的行数（追加时 +1，不用每次重数）

    @property
    def on(self) -> bool:
        return self.enabled

    def _warn(self, message: str) -> None:
        if not self._warned:
            self._warned = True
            print(message)

    # ---------- 写 ----------

    def _append(self, row: Dict[str, object]) -> bool:
        if not self.enabled:
            return False
        line = json.dumps({"v": self.VERSION, **row}, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception as exc:
            self._warn(f"[memory] 写存档 {self.path} 失败（记忆仍然照记）：{exc}")
            return False
        if self._count is not None:
            self._count += 1
        return True

    def add_episode(self, episode) -> bool:
        """记一条吐槽/它对应的那一眼（`episode` 是 memory.Episode 那种对象）。"""
        text = str(getattr(episode, "text", "") or "").strip()
        if not text:
            return False
        return self._append(_episode_row(episode))

    def add_profile(self, profile: str, tags: Sequence[str] = ()) -> bool:
        """记一次观众画像（模型总结出来的那 2~3 句 + 标签）。"""
        text = str(profile or "").strip()
        labels = [str(tag).strip() for tag in (tags or ()) if str(tag).strip()]
        if not text and not labels:
            return False
        return self._append(
            {"kind": PROFILE, "ts": time.time(), "profile": text, "tags": labels}
        )

    def backfill(self, episodes) -> int:
        """第一次见这个存档时，把 `memory.json` 里现有的条目先倒进去。

        为什么要有：老用户已经攒了几百条记忆，光加个存档等于"从今天才开始记"，
        之前那些照样会随着裁剪消失。**只有存档文件不存在时才回填**——已经有的
        存档一个字都不动，免得每次启动都倒一遍。
        """
        rows = [e for e in (episodes or ()) if str(getattr(e, "text", "") or "")]
        if not self.enabled or not rows:
            return 0
        with self._lock:
            if self.path.exists():
                return 0
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    for episode in rows:
                        handle.write(
                            json.dumps(_episode_row(episode), ensure_ascii=False) + "\n"
                        )
            except Exception as exc:
                self._warn(f"[memory] 回填存档 {self.path} 失败：{exc}")
                return 0
        self._count = len(rows)
        print(f"[memory] 完整存档建好了：{self.path}（先把现有 {len(rows)} 条倒进去）")
        return len(rows)

    def clear(self) -> bool:
        """删掉整本流水（只有用户明确要"连存档一起清"时才该调）。"""
        with self._lock:
            try:
                if self.path.exists():
                    self.path.unlink()
            except Exception as exc:
                self._warn(f"[memory] 清空存档 {self.path} 失败：{exc}")
                return False
        self._count = 0
        return True

    # ---------- 读 ----------

    def records(self, kind: str = "") -> Iterator[Dict[str, object]]:
        """一行一条地读回来；坏行直接跳过（手改过 / 写了一半断电都不至于读不动）。"""
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if kind and str(row.get("kind") or "") != kind:
                        continue
                    yield row
        except Exception as exc:
            self._warn(f"[memory] 读存档 {self.path} 失败：{exc}")

    def count(self) -> int:
        if self._count is None:
            if not self.path.exists():
                self._count = 0
            else:
                try:
                    with self.path.open("r", encoding="utf-8") as handle:
                        self._count = sum(1 for _ in handle)
                except Exception as exc:
                    self._warn(f"[memory] 数存档 {self.path} 失败：{exc}")
                    self._count = 0
        return self._count

    def stats(self) -> Dict[str, object]:
        """一眼看明白这本流水里有什么（只扫 ts / kind / tags，不把全文读进内存）。"""
        episodes = profiles = 0
        first_ts = last_ts = 0.0
        tags: Dict[str, int] = {}
        for row in self.records():
            ts = float(row.get("ts") or 0.0)
            if ts:
                first_ts = ts if not first_ts else min(first_ts, ts)
                last_ts = max(last_ts, ts)
            if str(row.get("kind") or "") == PROFILE:
                profiles += 1
            else:
                episodes += 1
            for tag in row.get("tags") or []:
                text = str(tag or "").strip()
                if text:
                    tags[text] = tags.get(text, 0) + 1
        top = sorted(tags.items(), key=lambda item: (-item[1], item[0]))[:8]
        return {
            "path": str(self.path),
            "enabled": self.enabled,
            "lines": self.count(),
            "episodes": episodes,
            "profiles": profiles,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "top_tags": top,
        }

    def report(self) -> str:
        info = self.stats()

        def stamp(ts: float) -> str:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"

        tags = "、".join(f"{tag}×{count}" for tag, count in info["top_tags"]) or "（暂无）"
        return "\n".join(
            [
                f"完整记忆存档：{info['path']}",
                f"状态：{'开着' if info['enabled'] else '关着'}｜共 {info['lines']} 行"
                f"（吐槽 {info['episodes']} 条 / 画像 {info['profiles']} 份）",
                f"时间范围：{stamp(info['first_ts'])} ~ {stamp(info['last_ts'])}",
                f"常看标签：{tags}",
            ]
        )

    def tail(self, limit: int = 20) -> str:
        """最近几条（给人翻的写法，跟 memory.json 里看到的长得一样）。"""
        rows = list(self.records())
        if not rows:
            return "（存档还是空的——挂件还在攒）"
        out: List[str] = []
        for row in rows[-max(1, int(limit)):]:
            when = time.strftime("%m-%d %H:%M", time.localtime(float(row.get("ts") or 0.0)))
            if str(row.get("kind") or "") == PROFILE:
                out.append(f"{when}  [画像] {row.get('profile') or ''}")
                continue
            tags = row.get("tags") or []
            tag_text = ("[" + ",".join(str(t) for t in tags) + "] ") if tags else ""
            out.append(f"{when}  {tag_text}{row.get('text') or ''}")
        return "\n".join(out)


def _episode_row(episode) -> Dict[str, object]:
    """把一条记忆摊平成存档里的一行（不 import memory，免得转着圈互相依赖）。"""
    return {
        "kind": EPISODE,
        "ts": float(getattr(episode, "ts", 0.0) or 0.0),
        "text": str(getattr(episode, "text", "") or "").strip(),
        "mood": str(getattr(episode, "mood", "") or ""),
        "tags": [str(tag) for tag in (getattr(episode, "tags", None) or ())],
        "scene": str(getattr(episode, "scene", "") or ""),
        "dialog": str(getattr(episode, "dialog", "") or ""),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - 手动跑的小工具
    """不启动挂件，单独看这本完整存档：

        python -m pet.memarchive            汇总（默认）
        python -m pet.memarchive tail 20    最近 20 条
        python -m pet.memarchive path       文件路径

    挂件在跑的时候也能看：读和写是分开的（写是追加），看到的就是最新那份。
    """
    from .config import Config
    from .memory import archive_path_for

    args = [str(item) for item in (sys.argv[1:] if argv is None else argv) if str(item).strip()]
    action = (args[0] if args else "report").lower()
    cfg = Config.load()
    archive = MemoryArchive(
        archive_path_for(cfg),
        enabled=bool(getattr(cfg.memory, "archive_enabled", True)),
    )

    if action == "path":
        print(archive.path)
        return 0
    if action == "tail":
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        print(archive.tail(limit))
        return 0
    if action in ("report", "r", "show"):
        print(archive.report())
        return 0
    print("用法：python -m pet.memarchive [report|tail N|path]")
    print("  report  汇总：条数 / 时间范围 / 常看标签（默认）")
    print("  tail N  最近 N 条（默认 20）")
    print("  path    只打印存档文件路径")
    return 2


if __name__ == "__main__":        # pragma: no cover - 手动跑的小工具
    raise SystemExit(main())
