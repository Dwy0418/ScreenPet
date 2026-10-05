"""长期记忆：本地 memory.json，让「黄豆」对你有熟人感。

省钱的部分：关键词自动打标签（本地词典，命中就计数，不花一分钱）。
花一点点钱的部分：每 N 句 / 每半小时，让模型把最近的记录总结成 2~3 句"观众画像"。
画像 + 常看标签会作为背景塞进提示词，模型才说得出"你最近怎么老在看这个"这种话。

每条记录还带一个**私下的对话类型**（记录 / 分析 / 学习，见 dialog.py）——
它不上界面，只用来统计它自己说过的话偏哪一类，并在上下文里给模型一句提醒。

所有数据只存在本机 memory.json 里，随时可以删掉。

**一条都不会丢**：`memory.json` 只留最近 `max_entries` 条（给模型看的那一份），
同一时刻还在往 `memarchive`（`data/memory/archive.jsonl`）追加一本只增不减的流水——
裁剪、崩溃、重启都不影响它。想翻全账就 `python -m pet.memarchive`。
"""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import dialog as dialog_mod
from . import memarchive

# 类别 -> 关键词。命中的类别和具体词都会记一笔。
TAG_KEYWORDS: Dict[str, Sequence[str]] = {
    "游戏": ("王者荣耀", "英雄联盟", "lol", "吃鸡", "和平精英", "原神", "崩坏", "steam", "单机", "游戏", "排位", "上分", "打野", "五杀", "团战"),
    "抽卡": ("抽卡", "十连", "保底", "欧皇", "非酋", "氪金", "开箱"),
    "电竞比赛": ("kpl", "s赛", "季后赛", "总决赛", "赛点", "解说"),
    "短视频": ("抖音", "快手", "视频号", "推荐页", "刷到", "下一条"),
    "直播": ("直播间", "弹幕", "主播", "开播", "打赏", "灯牌"),
    "剧情": ("剧情", "剧集", "第几集", "大结局", "狗血", "反转"),
    "搞笑": ("搞笑", "笑死", "沙雕", "整活", "名场面", "离谱"),
    "音乐": ("music", "歌曲", "翻唱", "副歌", "伴奏", "钢琴", "吉他", "演唱会"),
    "舞蹈": ("跳舞", "舞", "编舞", "手势舞"),
    "美食": ("美食", "做饭", "探店", "下厨", "厨艺", "夜宵"),
    "体育": ("足球", "篮球", "nba", "世界杯", "进球", "扣篮"),
    "科技": ("代码", "编程", "python", "显卡", "装机", "测评", "发布会", "ai "),
    "学习": ("教程", "教学", "讲解", "课程", "考研", "高考", "刷题"),
    "影视": ("电影", "预告", "影评", "番剧", "动画", "国漫"),
    "汽车": ("汽车", "试驾", "电车", "油耗", "提车"),
    "宠物": ("猫", "狗", "宠物", "萌宠"),
    "户外": ("旅游", "露营", "钓鱼", "徒步", "自驾"),
    "生活": ("工作", "加班", "租房", "理财", "装修"),
}

SUMMARY_HEAD = "以下是这个观众最近看的画面里出现的文字，以及你（一个陪看 AI）当时的吐槽记录："
SUMMARY_ASK = (
    "请用 2~3 句中文，像一个熟人那样总结这个观众的偏好和性格：爱看什么、看什么会兴奋、对什么无感。\n"
    "不要复述单条内容，不要说“该用户”这种口吻，要具体到内容类型。\n"
    "然后另起一行，给出 6~10 个话题标签，用逗号分隔。\n"
    "输出格式（不要多余的解释）：\n画像：……\n标签：标签1, 标签2, 标签3"
)


@dataclass
class Episode:
    ts: float
    text: str
    mood: str = ""
    tags: List[str] = field(default_factory=list)
    scene: str = ""   # 这一眼的 5W1H（人物=…｜事件=…），没有就是空串
    dialog: str = ""  # 私下的对话类型（记录/分析/学习，见 dialog.py），不上界面


# 太笼统、拿去搭话会很空的标签
TOPIC_STOPWORDS = frozenset({
    "内容", "画面", "视频", "东西", "这个", "那个", "一般", "其他",
    "短视频", "推荐页", "看不懂", "不明",
})

TOPIC_WINDOW = 60        # 从最近的多少条记录里找话题（"最近"要是真的最近）
TOPIC_MIN_LEN = 2        # 太短不成词（"猫"可以，"啊"不行）
TOPIC_MAX_LEN = 10       # 太长像句子，不像话题


@dataclass(frozen=True)
class Topic:
    """一个可以拿去搭话的话头，比如「他最近老在看『王者荣耀』」。"""

    label: str
    count: int = 1
    last_seen: float = 0.0
    specific: bool = False   # 是具体作品名（「王者荣耀」）还是一个笼统类别（「游戏」）

    @property
    def hint(self) -> str:
        """给模型看的一句提示（不是要它照念的台词）。"""
        times = f"，你记录到 {self.count} 次" if self.count else ""
        return f"他最近老在看「{self.label}」{times}"


def _usable_topic(label: str) -> bool:
    text = (label or "").strip()
    if not (TOPIC_MIN_LEN <= len(text) <= TOPIC_MAX_LEN):
        return False
    return text not in TOPIC_STOPWORDS


def topic_candidates(
    entries: Sequence[Episode],
    *,
    min_count: int = 3,
    limit: int = 4,
    window: int = TOPIC_WINDOW,
) -> List[Topic]:
    """从最近的记录里挑几个"他最近老在看"的话头（纯本地，不花一分钱）。

    只看最近 window 条记录，这样"最近"才名副其实；一个词至少出现 min_count 次
    才算"老在看"。具体的作品名（「王者荣耀」）排在笼统类别（「游戏」）前面——
    前者才聊得起来。
    """
    counts: Counter = Counter()
    last: Dict[str, float] = {}
    recent = list(entries)[-max(1, int(window)):]
    for episode in recent:
        for tag in getattr(episode, "tags", None) or ():
            if not _usable_topic(tag):
                continue
            counts[tag] += 1
            seen = float(getattr(episode, "ts", 0.0) or 0.0)
            if seen > last.get(tag, 0.0):
                last[tag] = seen

    floor = max(2, int(min_count))
    picked = [
        Topic(label, count, last.get(label, 0.0), len(label) >= 3)
        for label, count in counts.items()
        if count >= floor
    ]
    picked.sort(key=lambda t: (not t.specific, -t.count, -t.last_seen, t.label))
    return picked[: max(1, int(limit))]


def extract_tags(*texts: str, limit: int = 6) -> List[str]:
    """从若干段文字里抽标签（类别 + 具体作品名），命中顺序去重。"""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return []
    hits: List[str] = []
    for category, keywords in TAG_KEYWORDS.items():
        for keyword in keywords:
            if keyword in blob:
                hits.append(category)
                if len(keyword) >= 3 and keyword not in hits:
                    hits.append(keyword)
                break
    return hits[:limit]


#: 完整存档的默认位置（相对配置文件所在目录）；想挪地方就改 `memory.archive_path`。
DEFAULT_ARCHIVE_PATH = "data/memory/archive.jsonl"


def _resolve(raw: str, cfg) -> Path:
    """相对路径按**配置文件所在目录**算（跟 taste.path / learn.path 一个规矩）。"""
    path = Path(str(raw or ""))
    if not path.is_absolute():
        path = Path(cfg.config_path()).parent / path
    return path


def archive_path_for(cfg) -> Path:
    """完整存档该在哪儿（**纯算路径**，不建文件也不读文件）。

    配了 `memory.archive_path` 就按那个走（相对路径仍按配置文件所在目录算）；
    留空（默认）就放在 **memory.json 旁边**的 `data/memory/archive.jsonl`——
    这样"记忆挪到哪儿，存档跟着走"（测试 / 便携版把 memory.json 指到别处时，
    存档也落在同一个地方，不会写脏程序目录）。正常用的时候两种算法是同一个位置。
    """
    raw = str(getattr(getattr(cfg, "memory", None), "archive_path", "") or "").strip()
    if raw:
        return _resolve(raw, cfg)
    memory_path = _resolve(
        getattr(getattr(cfg, "memory", None), "path", "") or "memory.json", cfg
    )
    return memory_path.parent / DEFAULT_ARCHIVE_PATH


class Memory:
    """负责 memory.json 的读写、打标签、生成"给模型看"的背景。"""

    SAVE_INTERVAL = 20.0
    VERSION = 1

    def __init__(self, cfg):
        self.cfg = cfg
        self.mem = cfg.memory
        self.path = self._resolve_path(cfg)
        # 完整存档（只增不减）：memory.json 按 max_entries 裁剪，这里一条都不丢。
        # 记忆整个关掉时它也跟着关——不然"关掉记忆"就成了一纸空文。
        self.archive = memarchive.MemoryArchive(
            self._archive_path(cfg),
            enabled=bool(getattr(self.mem, "archive_enabled", True))
            and bool(self.cfg.memory.enabled),
        )
        self.entries: List[Episode] = []
        self.tags: Counter = Counter()
        self.dialog: Counter = Counter()   # 它自己说过的话各是哪一类（私下统计，不上界面）
        self.profile: str = ""
        self.profile_updated_at: float = 0.0
        self.comment_count: int = 0
        self._last_save = 0.0
        self._dirty = False
        self.load()

    def _resolve_path(self, cfg) -> Path:
        return _resolve(getattr(self.mem, "path", "") or "memory.json", cfg)

    def _archive_path(self, cfg) -> Path:
        """完整存档放哪儿（规则见模块里的 `archive_path_for`）。"""
        return archive_path_for(cfg)

    # ---------- 读写 ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[memory] 读取 {self.path} 失败：{exc}")
            return
        self.profile = str(data.get("profile") or "")
        self.profile_updated_at = float(data.get("profile_updated_at") or 0.0)
        self.comment_count = int(data.get("comment_count") or 0)
        self.tags = Counter({str(k): int(v) for k, v in (data.get("tags") or {}).items()})
        self.dialog = Counter(
            {str(k): int(v) for k, v in (data.get("dialog") or {}).items() if str(k) in dialog_mod.KINDS}
        )
        for item in data.get("entries") or []:
            if isinstance(item, dict) and item.get("text"):
                self.entries.append(
                    Episode(
                        ts=float(item.get("ts") or 0.0),
                        text=str(item["text"]),
                        mood=str(item.get("mood") or ""),
                        tags=[str(t) for t in (item.get("tags") or [])],
                        scene=str(item.get("scene") or ""),
                        dialog=str(item.get("dialog") or ""),
                    )
                )
        # 头一回见这本存档（老的 memory.json 里已经攒了东西）：先把现有的倒进去，
        # 免得"从今天才开始记"。已经有存档的话一个字都不动。
        self.archive.backfill(self.entries)

    def save(self, force: bool = False) -> bool:
        if not self.cfg.memory.enabled:
            return False
        now = time.monotonic()
        if not force and (not self._dirty or now - self._last_save < self.SAVE_INTERVAL):
            return False
        payload = {
            "version": self.VERSION,
            "profile": self.profile,
            "profile_updated_at": self.profile_updated_at,
            "comment_count": self.comment_count,
            "tags": dict(self.tags.most_common(40)),
            "dialog": {k: int(self.dialog.get(k, 0)) for k in dialog_mod.KINDS if self.dialog.get(k)},
            "entries": [
                {
                    "ts": e.ts,
                    "text": e.text,
                    "mood": e.mood,
                    "tags": e.tags,
                    "scene": e.scene,
                    "dialog": e.dialog,
                }
                for e in self.entries[-int(self.mem.max_entries):]
            ],
        }
        try:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            print(f"[memory] 写入 {self.path} 失败：{exc}")
            return False
        self._dirty = False
        self._last_save = now
        return True

    # ---------- 记录 ----------

    def add(
        self,
        text: str,
        mood: str = "",
        context: str = "",
        scene: str = "",
        dialog: str = "",
    ) -> List[str]:
        """记一条吐槽，并按台词/画面文字/这一眼的场景打标签。

        scene 是 5W1H 那一行（scene.Scene.line()）：一起丢给关键词词典，
        "直播间""打boss""厨房"这类词就能进记忆，画像和搭话话头都更准。

        dialog 是这句话的类型（记录/分析/学习，见 dialog.py）：不给就当场判一个，
        只用来做私下统计，不上界面。

        记下的这一条同时会追加进**完整存档**（`memarchive`）：memory.json 那份会被
        裁剪，存档那份不会。
        """
        if not self.cfg.memory.enabled:
            return []
        kind = dialog or dialog_mod.classify(text, mood=mood)
        tags = extract_tags(text, context, scene)
        self.entries.append(
            Episode(ts=time.time(), text=text, mood=mood, tags=tags, scene=scene, dialog=kind)
        )
        limit = max(1, int(self.mem.max_entries))
        if len(self.entries) > limit:
            del self.entries[:-limit]
        for tag in tags:
            self.tags[tag] += 1
        if kind in dialog_mod.KINDS:
            self.dialog[kind] += 1
        self.comment_count += 1
        # 完整存档：这一条**立刻**落一行（memory.json 那 20 秒的存盘节奏、后面的裁剪
        # 都跟它没关系）。写失败只打一行日志，绝不影响上面的记账。
        if self.entries:
            self.archive.add_episode(self.entries[-1])
        self._dirty = True
        return tags

    # ---------- 背景信息 ----------

    def needs_summary(self, now: Optional[float] = None) -> bool:
        if not self.cfg.memory.enabled:
            return False
        now = now if now is not None else time.time()
        if self.comment_count < max(1, int(self.mem.summary_every_n)):
            return False
        if self.profile and (now - self.profile_updated_at) < max(60.0, float(self.mem.summary_every_sec)):
            return False
        return True

    def summary_prompt(self, limit: int = 30) -> str:
        lines = []
        for entry in self.entries[-limit:]:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(entry.ts))
            tag_text = ("[" + ",".join(entry.tags) + "] ") if entry.tags else ""
            scene = getattr(entry, "scene", "")
            scene_text = f"（当时看到：{scene}）" if scene else ""
            lines.append(f"{stamp} {tag_text}{entry.text}{scene_text}")
        top = "、".join(tag for tag, _ in self.top_tags(8))
        parts = [
            SUMMARY_HEAD,
            "\n".join(lines) or "(还没有记录)",
            f"目前统计出的高频话题：{top or '(暂无)'}",
        ]
        if self.profile:
            parts.append(f"上一次的画像（可以修正它）：{self.profile}")
        parts.append(SUMMARY_ASK)
        return "\n".join(parts)

    def apply_summary(self, reply: str) -> bool:
        """解析模型返回的「画像：… / 标签：…」。"""
        text = (reply or "").strip()
        if not text:
            return False
        profile = ""
        labels: List[str] = []
        for raw_line in text.replace("\r", "\n").split("\n"):
            line = raw_line.strip().lstrip("-•* ").strip()
            if not line:
                continue
            if line.startswith(("画像", "总结", "Profile")):
                profile = _after_colon(line)
            elif line.startswith(("标签", "Tags")):
                labels = [p.strip() for p in _after_colon(line).replace("，", ",").split(",") if p.strip()]
        if not profile and not labels:
            profile = text[: int(self.mem.profile_max_chars)]
        if profile:
            self.profile = profile[: int(self.mem.profile_max_chars)]
        # 不管这次有没有解析出画像，都把时间戳推后，避免每轮都重新总结
        self.profile_updated_at = time.time()
        for label in labels[:10]:
            self.tags[label] += 2  # 模型给的标签权重高一点
        # 画像也进完整存档：以后翻流水能看到"它当时是这么理解我的"
        self.archive.add_profile(self.profile, labels[:10])
        self._dirty = True
        return bool(profile or labels)

    def top_tags(self, limit: int = 6) -> List[tuple]:
        """高频标签。顺手把两头的空白和空标签滤掉——`"ai "` 这种带尾空格的标签
        现场真出现过（模型回「标签：ai , 剪辑」时带的），显示成「常看 ai 」「」很难看。
        """
        rows: List[tuple] = []
        for tag, count in self.tags.most_common(max(1, int(limit)) * 2):
            text = str(tag or "").strip()
            if not text:
                continue
            rows.append((text, count))
            if len(rows) >= limit:
                break
        return rows

    def topics(self, limit: int = 4) -> List[Topic]:
        """能拿去主动搭话的话头（"他最近老在看…"）。没记忆、没攒够就返回空列表。

        给主动搭话用，**只读不写**：搭话本身不会污染记忆。
        """
        if not self.cfg.memory.enabled:
            return []
        return topic_candidates(
            self.entries,
            min_count=int(getattr(self.cfg.proactive, "topic_min_count", 3)),
            limit=limit,
        )

    def dialog_mix(self, limit: int = 3) -> List[tuple]:
        """它自己说过的话偏哪一类（私下统计，控制台/自检看的）。"""
        return [(kind, int(self.dialog.get(kind, 0))) for kind, _ in self.dialog.most_common(limit)]

    def context(self) -> str:
        """拼成塞进提示词的背景；没有内容就返回空串。"""
        if not self.cfg.memory.enabled:
            return ""
        parts: List[str] = []
        if self.profile:
            parts.append(f"你记得关于这个观众的事：{self.profile}")
        tags = self.top_tags(6)
        if tags:
            parts.append("他最近常看的内容类型：" + "、".join(tag for tag, _ in tags))
        learned = dialog_mod.mix_hint(self.dialog)
        if learned:
            parts.append(learned)
        if self.comment_count:
            parts.append(f"你已经陪他看过 {self.comment_count} 段画面了。")
        if not parts:
            return ""
        parts.append("这些只是背景，不要每句话都提以前的事，也别生硬复述。")
        return "\n".join(parts)

    def stats(self) -> Dict[str, object]:
        return {
            "path": str(self.path),
            "entries": len(self.entries),
            "comments": self.comment_count,
            "profile": self.profile,
            "top_tags": self.top_tags(8),
            "dialog": self.dialog_mix(),
            "archive_path": str(self.archive.path),
            "archive": self.archive.count(),
        }

    def clear(self) -> None:
        """清空它脑子里那一份（memory.json + 标签 + 画像）。

        **完整存档不动**：这是"我们重新认识一下"（模型从此想不起你来过），
        不是"把我陪你看过的全烧了"。真想连流水一起删，自己删那个 .jsonl 就行
        （路径见 `stats()["archive_path"]`，或者右键我 →「打开完整存档」）。
        """
        self.entries.clear()
        self.tags.clear()
        self.dialog.clear()
        self.profile = ""
        self.profile_updated_at = 0.0
        self.comment_count = 0
        self._dirty = True
        self.save(force=True)


def _after_colon(line: str) -> str:
    for sep in ("：", ":"):
        if sep in line:
            return line.split(sep, 1)[1].strip()
    return line.strip()


