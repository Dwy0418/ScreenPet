"""抽「开局常识」种子：把作者本机学到的**干净知识**抽成 `knowledge.example.json`。

    python tools/make_knowledge_seed.py                      # 只看会抽到什么，不写文件
    python tools/make_knowledge_seed.py --write              # 真的写 knowledge.example.json
    python tools/make_knowledge_seed.py --home D:\\some\\dir  # 到哪个「家」去读（默认 user_dir()）

为什么要有这个工具：挂件上网补课（`pet/webstudy.py`）学到的东西只落在**它自己这台机器**
上，换个用户下载，宠物又变回白纸一张——同一个话题白学一遍，接口钱花两遍。所以打包时
带一份"作者挑过的通用知识"，第一次运行撒给用户（见 `pet/knowledge.py`）。

挑的时候有一条硬界限，这个工具就是替你把这条线守住：

    进种子的：**谁都能听的通用知识**（"液氮遇热会迅速蒸发"这类陈述句）
              + 「这个话题学过了」「这个话头学不出东西」的账本
    不进的：  它照着作者屏幕说的那些话（"这集都第28集了"）、作者的用户画像、
              爱好标签、台词/接话语料——**那是一个人自己的东西**，不该发给所有人

怎么分辨"知识"和"照着屏幕说的话"：见下面的 `SCREEN_TALK`。挂件的吐槽有个很明显的形状
（"就这？""这画面…""这集都第N集了"），而知识点是**陈述句**、不冲着谁说话、不带屏幕上的
具体数字。这条规则故意**保守**：宁可少收几条，也别把作者的屏幕内容发出去。

话头账本那份**只留白名单里的通用词**（`KEEP_TOPICS`）：账本的作用只是"别重复学 / 别啃
白卷"，留通用词就够了；`抖音《奔跑吧》` 这种话题名本身就等于把"作者爱看什么"写给所有人看。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pet import make_console_safe, paths   # noqa: E402

SEED_NAME = "knowledge.example.json"

#: 话头账本里**只有这些**才配进种子。这一条是**故意收紧**的：账本的作用只是"别重复学 /
#: 别啃白卷"，留通用词就够了，而话题名本身就会泄露"作者爱看什么"——
#: `抖音《奔跑吧》`、`暗区突围`、`王者荣耀` 这类一律不要（作者点名要的）。
KEEP_TOPICS: Tuple[str, ...] = (
    "ai",
    "网页浏览器截图",
)

#: 平台 / 站名也不进种子：`抖音算法推荐个性化内容` 这种句子，一半是平台自己的说明书、
#: 一半等于告诉所有人"他用哪个 App"。知识该是通用的，不该带着平台的口音。
PLATFORM_WORDS: Tuple[str, ...] = (
    "抖音", "快手", "哔哩哔哩", "B站", "b站", "微信", "QQ", "qq", "小红书", "微博", "知乎", "贴吧",
)

#: "照着屏幕说的话"的形状：吐槽、念画面、报集数、冲着画面里的人喊。
SCREEN_TALK = re.compile(
    r"就这|这集都第|画面(中|里|上|展示|显示)|这谁啊|这啥情况|这也能|这能行|这广告|这是什么|这画面|"
    r"啊？|这球|这女的|这孩子|这货|这男|这两个|这三个人|这雨|这地铁|这对话|这表情|这波操作|"
    r"这遥控器|这浴室|这穿|竟然|卧槽|龙不穿|这你biu|还在打|还活着|还在排队|还在厨房|还在说|"
    r"还在聊|还在搞|这游戏怎么|这画质|这节目|这视频|这红发|这竖起|这古装|这数字"
)

#: 句尾是这几个字的，是"对着人说话"，不是知识（"…是不是破空吗""…为了折磨我吧"）。
TALK_TAILS = ("吗", "吧", "呢", "啊", "哦", "呀")

#: 知识点的标签（写进 memory 条目的 tags，**不是**爱好档案——那个只看用户自己怎么说）。
TAG_RULES: Tuple[Tuple[str, Sequence[str]], ...] = (
    ("科学", ("液氮", "磁悬浮", "蒸发", "原理", "物理")),
    ("短视频平台", ("抖音", "算法", "推荐", "剪辑")),
    ("动漫", ("二次元", "动漫", "番剧")),
    ("常识", ()),   # 兜底
)


def lesson_tags(text: str) -> List[str]:
    """给一条知识点配个干净的类别标签（认不出就是"常识"）。"""
    for name, words in TAG_RULES:
        if any(word in text for word in words):
            return [name]
    return ["常识"]


def read_rows(home: Path) -> List[dict]:
    """把「完整存档」（`data/memory/archive.jsonl`）读成一行一条。

    读存档而不是只读 `memory.json`：后者只留最近 200 条，补课学的那几条早被挤掉了
    （现场就是这样：memory.json 里一条真知识都不剩，全在存档里）。
    """
    path = home / "data" / "memory" / "archive.jsonl"
    if not path.exists():
        print(f"[seed] 没找到完整存档：{path}")
        return []
    rows: List[dict] = []
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    print(f"[seed] 完整存档 {len(rows)} 行：{path}")
    return rows


def read_ledger(home: Path) -> Dict[str, dict]:
    """读话题账本（`data/study.json`）里的 topics。"""
    path = home / "data" / "study.json"
    if not path.exists():
        print(f"[seed] 没找到话题账本：{path}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[seed] 话题账本读不动（当空账本）：{exc}")
        return {}
    rows = data.get("topics") if isinstance(data, dict) else None
    print(f"[seed] 话题账本 {len(rows or {})} 个话头：{path}")
    return rows if isinstance(rows, dict) else {}


def extract_lessons(rows: Iterable[dict]) -> Tuple[List[dict], List[Tuple[str, str]]]:
    """从补课记录里挑出**通用知识**，顺手把不要的东西连理由一起交出去（好让人复核）。"""
    keep: List[dict] = []
    drop: List[Tuple[str, str]] = []
    seen = set()
    for row in rows:
        if str(row.get("dialog") or "") != "learn":
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        if text in seen:
            drop.append((text, "重复"))
        elif SCREEN_TALK.search(text):
            drop.append((text, "照着屏幕说的（吐槽/念画面/报集数）"))
        elif any(word in text for word in PLATFORM_WORDS):
            drop.append((text, "带着平台名（等于说他用哪个 App）"))
        elif "《" in text or "》" in text:
            drop.append((text, "页面标题的残片"))
        elif text.endswith(("？", "?")):
            drop.append((text, "它是提问，不是知识"))
        elif text.endswith(TALK_TAILS):
            drop.append((text, "冲着人说的口气，不是陈述句"))
        elif len(text) < 6:
            drop.append((text, "太短，不成信息"))
        else:
            keep.append({"text": text, "tags": lesson_tags(text)})
        seen.add(text)
    return keep, drop


def extract_topics(ledger: Dict[str, dict]) -> Tuple[List[dict], List[Tuple[str, str]]]:
    """话头账本只留白名单里的通用词（理由见模块开头）。"""
    keep: List[dict] = []
    drop: List[Tuple[str, str]] = []
    for name, item in ledger.items():
        text = str(name or "").strip()
        if text not in KEEP_TOPICS:
            drop.append((text, "不在白名单（作者看过什么不该写给大家）"))
            continue
        row = item if isinstance(item, dict) else {}
        keep.append(
            {
                "topic": text,
                "ok": int(row.get("ok") or 0),
                "blank": int(row.get("blank") or 0),
            }
        )
    return keep, drop


def build_seed(lessons: List[dict], topics: List[dict]) -> dict:
    """拼成种子文件（字段含义写在 `_怎么用` 里，用户打开就能看懂）。"""
    return {
        "_怎么用": [
            "这是「开局自带的一点常识」：第一次运行挂件时 pet/knowledge.py 会把它撒进你自己的目录"
            "（打包版是 %APPDATA%\\ScreenPet），**只撒一次**。",
            "lessons 是上网补课学到的通用知识点，会当成几条长期记忆写进 memory.json（dialog 标 learn）。",
            "topics 是「这个话题学过了 / 这个话头学不出东西」的账本：学过的先放着、老交白卷的排最后，"
            "省的是你自己的接口钱。",
            "**这里不该有别人自己的东西**：用户画像、爱好标签、屏幕上看来的台词/接话语料——"
            "那些是每个用户各长各的，谁都不该继承（这道线由 tools/make_knowledge_seed.py 负责守住）。",
            "想加内容就改这个文件（或者重跑那个工具），改完重启挂件；已经撒过的老用户不会重撒——"
            "想重撒就删掉用户目录里的 .knowledge-seed.json。",
        ],
        "version": 1,
        "lessons": lessons,
        "topics": topics,
    }


def _report(title: str, kept: List, dropped: List[Tuple[str, str]], show: int = 12) -> None:
    print(f"\n=== {title}：收下 {len(kept)} 条，丢掉 {len(dropped)} 条 ===")
    for item in kept:
        print("  + " + (item.get("text") or item.get("topic") or ""))
    reasons: Dict[str, List[str]] = {}
    for text, why in dropped:
        reasons.setdefault(why, []).append(text or "(空)")
    for why, items in reasons.items():
        print(f"  - [{why}] {len(items)} 条：{'；'.join(items[:show])}"
              + ("…" if len(items) > show else ""))


def main(argv=None) -> int:
    make_console_safe()
    parser = argparse.ArgumentParser(description="抽「开局常识」种子")
    parser.add_argument("--home", default="", help="到哪个「家」去读（默认 user_dir()）")
    parser.add_argument("--write", action="store_true", help="真的写 knowledge.example.json")
    args = parser.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else paths.user_dir()
    print(f"[seed] 读：{home}")
    print(f"[seed] 写：{ROOT / SEED_NAME}" + ("" if args.write else "（本次只看不写）"))

    lessons, lesson_drop = extract_lessons(read_rows(home))
    topics, topic_drop = extract_topics(read_ledger(home))
    seed = build_seed(lessons, topics)
    _report("知识点", lessons, lesson_drop)
    _report("话头账本", topics, topic_drop)

    if not args.write:
        print("\n[seed] 没加 --write，什么都没写。先看看上面收下的东西对不对——"
              "有不该进去的就改这个工具里的规则，或者直接改写出来的那个文件。")
        return 0
    target = ROOT / SEED_NAME
    target.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n[seed] 写好了：{target}（知识点 {len(lessons)} 条 / 话头 {len(topics)} 个）")
    print("[seed] 记得打开看一眼有没有不该发出去的东西，再跑 tools/build.py 打包。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


