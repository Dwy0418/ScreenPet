"""开局自带的那点常识：把随包发的「干净知识」撒给第一次打开挂件的人。

为什么要它：挂件上网补课（`pet/webstudy.py`）学到的东西**只落在它自己这台机器上**——
换个用户下载，宠物又是白纸一张，同一个话题白学一遍，接口钱花两遍。所以打包时带一份
`knowledge.example.json`（作者挑过的通用知识，怎么挑见 `tools/make_knowledge_seed.py`），
第一次运行时撒进**用户自己的目录**，从此跟用户自己学的东西长在一起。

三条规矩：

  · **只撒一次**：撒完落一个 `.knowledge-seed.json` 标记。用户后来自己删过的记忆不会
    又被塞回来（「右键我 → 重新认识一下」之后不该再冒出来）。
  · **已经有数据就不动**：`memory.json` 里已经有条目、账本里已经有话头——那是人家自己的
    东西，一个字都不碰（标记照落，免得几百轮之后又来撒一次）。
  · **源码里跑不撒**：`python main.py` 时 `user_dir()` 就是**项目根目录**，撒进去等于往
    仓库里写运行期数据。测试想验它传 `force=True`（见 tools/smoke_test.py）。

种子里**不该有别人自己的东西**（用户画像、爱好标签、屏幕上看来的台词/接话语料）——
那道线由那个抽种子的工具守着，也写在种子文件的 `_怎么用` 里。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

from . import paths

#: 种子文件（打包时跟 config.example.json 一起塞进 exe，从 resource_dir() 拿）
SEED_NAME = "knowledge.example.json"
#: 撒过的标记：有这个文件就不再撒（删掉它就等于「下次启动重来一遍」）
MARKER_NAME = ".knowledge-seed.json"
VERSION = 1


def seed_path() -> Path:
    """种子在哪儿：打包后是 exe 里那一份，源码里跑就是项目根目录。"""
    packaged = paths.resource_dir() / SEED_NAME
    if packaged.exists():
        return packaged
    return paths.code_dir() / SEED_NAME


def marker_path() -> Path:
    """撒过的标记放哪儿（用户自己的目录，跟 config.json 一个地方）。"""
    return paths.user_dir() / MARKER_NAME


def load_seed() -> Dict[str, Any]:
    """读种子；没有 / 读坏了就当没有这份常识——**第一次启动这条路不能被一个坏 json 堵死**。"""
    path = seed_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[knowledge] 读 {path.name} 失败（就当没有这份常识）：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def seed_user_data(cfg, *, force: bool = False) -> bool:
    """第一次运行撒一次常识，返回这次到底撒没撒（调用方不用管结果）。

    `force=True` 只给测试用：源码里跑也照撒。真实调用一律不带它——源码版的
    `user_dir()` 是项目根目录，撒进去就是把运行期数据写进仓库。
    """
    if not force and not paths.is_frozen():
        return False
    seed = load_seed()
    if not seed:
        return False
    marker = marker_path()
    if marker.exists():
        return False

    lessons = [
        row for row in (seed.get("lessons") or [])
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    ]
    topics = [
        row for row in (seed.get("topics") or [])
        if isinstance(row, dict) and str(row.get("topic") or "").strip()
    ]
    written_lessons = _seed_lessons(cfg, lessons)
    written_topics = _seed_topics(cfg, topics)
    _write_marker(marker, seed, written_lessons, written_topics)
    if written_lessons or written_topics:
        print(
            f"[knowledge] 开局常识撒好了：知识点 {written_lessons} 条、话头 {written_topics} 个"
            f"（来自 {seed_path().name}；不想再撒就删掉 {marker.name}）"
        )
    else:
        print(f"[knowledge] 已经有自己的记忆/账本了，这份常识不撒（标记落在 {marker.name}）")
    return True


def _seed_lessons(cfg, lessons: List[dict]) -> int:
    """把知识点写进 `memory.json`。

    借 `Memory` 自己的落盘格式（`memory.Memory.save`），**不手写 json**——格式一变
    它自己就跟着变，这里不用再抄一遍字段。
    """
    if not lessons:
        return 0
    from . import memory as memory_mod   # 放进函数里：memory 那头也要 import 一圈模块

    mem = memory_mod.Memory(cfg)
    if mem.entries or mem.profile:
        return 0      # 人家已经有自己的记忆了：一个字都不动
    now = time.time()
    mem.entries = [
        memory_mod.Episode(
            ts=now,
            text=str(row.get("text") or "").strip(),
            mood="",
            tags=[str(tag) for tag in (row.get("tags") or []) if str(tag).strip()],
            scene="",
            dialog="learn",
        )
        for row in lessons
    ]
    # 只动「学习」这一个计数：画像、爱好标签、陪看次数**全是用户自己的**，一个都不碰。
    mem.dialog["learn"] = len(mem.entries)
    if not mem.save(force=True):
        return 0
    return len(mem.entries)


def _seed_topics(cfg, topics: List[dict]) -> int:
    """把话头写进话题账本（读的是 `study.ledger`，默认 `data/study.json`）。

    `ts` 一律写成**当下**：等于「这个话题刚学过」，冷却期内它不会为同一个话头再花一次
    接口钱（账本的作用就是这个，见 `webstudy.TopicLedger`）。
    """
    if not topics:
        return 0
    from . import webstudy as webstudy_mod

    study = getattr(cfg, "study", None)
    ledger = webstudy_mod.TopicLedger(
        webstudy_mod.ledger_path_for(cfg),
        cooldown=float(getattr(study, "topic_cooldown_sec", 21600.0) or 0.0),
        fail_penalty=int(getattr(study, "fail_penalty", 3) or 3),
    )
    if ledger.topics:
        return 0      # 人家已经有自己的账本了
    now = time.time()
    for row in topics:
        name = str(row.get("topic") or "").strip()
        if not name:
            continue
        ledger.topics[name] = {
            "ts": now,
            "ok": int(row.get("ok") or 0),
            "blank": int(row.get("blank") or 0),
        }
    if not ledger.topics:
        return 0
    ledger.save()
    return len(ledger.topics)


def _write_marker(marker: Path, seed: Dict[str, Any], lessons: int, topics: int) -> None:
    """落标记。写不动也认了：下次顶多再撒一遍，**不能因为一个标记把启动搞崩**。"""
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "version": int(seed.get("version") or VERSION),
                    "at": time.time(),
                    "lessons": lessons,
                    "topics": topics,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"[knowledge] 写不下标记 {marker.name}：{exc}")

