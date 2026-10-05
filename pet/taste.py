"""学你的口味：每刷一支视频记一条档案，攒出「他到底爱看啥」。

仿抖音群聊小助手那套逻辑：**先分类，再看人怎么反应，最后按反应调整说话的劲儿**。

三件事：
    1. 分类       —— 看片笔记那一轮的模型顺手给出「类型」（GENRES 里的固定大类，方便统计）
    2. 看反应     —— 点赞 / 收藏 / 关注 / 评论（画面按钮状态 + 弹幕字幕关键词，模型和本地各判一次）
    3. 算兴趣     —— 固定规则打分（收藏 > 关注 > 点赞 = 评论，看完/看得久加分，秒划走扣分）

攒够之后对外只有两个口子：
    profile_block()  给提示词的「你摸清的他的口味」——喜欢的内容多接话、多起哄，无感的少开口
    nudge_scale()    主动搭话频率系数：越对他胃口的内容，越愿意多陪一句

全都存在本地 taste.json，不联网、不花一分钱（类型是搭车看片笔记那一轮的模型调用得来的）。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# 固定大类（模型给别的说法会被 normalize_genre 归一到这里，统计才有意义）
GENRES: Tuple[str, ...] = (
    "游戏",
    "影视综艺",
    "剧情解说",
    "搞笑",
    "音乐舞蹈",
    "知识科普",
    "生活日常",
    "美食",
    "体育",
    "宠物",
    "汽车房产",
    "新闻财经",
    "带货直播",
    "其他",
)

# 细词 -> 大类（模型爱自由发挥，这里兜一下）
_GENRE_HINTS: Tuple[Tuple[str, str], ...] = (
    ("王者", "游戏"), ("英雄联盟", "游戏"), ("lol", "游戏"), ("吃鸡", "游戏"), ("原神", "游戏"),
    ("游戏", "游戏"), ("手游", "游戏"), ("端游", "游戏"), ("电竞", "游戏"), ("单机", "游戏"),
    ("综艺", "影视综艺"), ("电视剧", "影视综艺"), ("电影", "影视综艺"), ("剧集", "影视综艺"),
    ("影视", "影视综艺"), ("番剧", "影视综艺"), ("动画", "影视综艺"), ("短剧", "影视综艺"),
    ("解说", "剧情解说"), ("剧情", "剧情解说"), ("讲", "剧情解说"),
    ("搞笑", "搞笑"), ("沙雕", "搞笑"), ("段子", "搞笑"), ("整活", "搞笑"),
    ("音乐", "音乐舞蹈"), ("歌", "音乐舞蹈"), ("舞蹈", "音乐舞蹈"), ("跳舞", "音乐舞蹈"),
    ("知识", "知识科普"), ("科普", "知识科普"), ("教程", "知识科普"), ("编程", "知识科普"),
    ("代码", "知识科普"), ("学习", "知识科普"), ("新闻", "新闻财经"), ("财经", "新闻财经"),
    ("生活", "生活日常"), ("vlog", "生活日常"), ("日常", "生活日常"), ("旅行", "生活日常"),
    ("美食", "美食"), ("吃播", "美食"), ("做饭", "美食"), ("探店", "美食"),
    ("体育", "体育"), ("足球", "体育"), ("篮球", "体育"), ("健身", "体育"),
    ("宠物", "宠物"), ("猫", "宠物"), ("狗", "宠物"),
    ("汽车", "汽车房产"), ("试驾", "汽车房产"), ("房产", "汽车房产"), ("楼市", "汽车房产"),
    ("看房", "汽车房产"), ("房价", "汽车房产"), ("装修", "汽车房产"),
    ("带货", "带货直播"), ("直播", "带货直播"), ("购物", "带货直播"),
)

# 互动信号的中文写法（模型和 OCR 都往这里靠）
_ACTION_WORDS: Tuple[Tuple[str, str], ...] = (
    ("点赞", "like"), ("喜欢", "like"), ("like", "like"), ("赞", "like"),
    ("收藏", "fav"), ("fav", "fav"), ("save", "fav"),
    ("关注", "follow"), ("follow", "follow"),
    ("评论", "comment"), ("comment", "comment"), ("回复", "comment"),
)

ACTION_LABELS: Dict[str, str] = {
    "like": "点赞",
    "fav": "收藏",
    "follow": "关注",
    "comment": "评论",
}

# 一个动作值多少兴趣分（收藏/关注比点赞更"贵"）
_ACTION_SCORE: Dict[str, float] = {"like": 3.0, "fav": 4.0, "follow": 5.0, "comment": 3.0}


def normalize_genre(text: str) -> str:
    """把模型给的类型说法归一到大类；认不出来归「其他」。"""
    raw = (text or "").strip().lower()
    if not raw:
        return "其他"
    for genre in GENRES:
        if genre.lower() == raw:
            return genre
    for hint, genre in _GENRE_HINTS:
        if hint in raw:
            return genre
    return "其他"


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"是", "有", "yes", "y", "true", "1", "✓", "√", "已"}


def parse_actions(text: str) -> Dict[str, bool]:
    """把「点赞=是｜收藏=否｜关注=是｜评论=否」这类文本解析成动作表（给看片笔记用）。"""
    out: Dict[str, bool] = {}
    for chunk in (text or "").replace("｜", "|").replace(";", "|").split("|"):
        item = chunk.strip()
        if not item:
            continue
        head, _, tail = item.partition("=")
        if not tail:
            head, _, tail = item.partition(":")
        head, tail = head.strip(), tail.strip()
        if not head:
            continue
        for word, name in _ACTION_WORDS:
            if word in head.lower():
                out[name] = _truthy(tail)
                break
    return out


# 画面文字里的互动线索：免费、稳（「已关注」「已收藏」这种字比猜红心靠谱）
_OCR_ACTION_HINTS: Tuple[Tuple[str, str], ...] = (
    ("已关注", "follow"), ("+关注", "follow"), ("取消关注", "follow"),
    ("已收藏", "fav"), ("取消收藏", "fav"), ("我的收藏", "fav"),
    ("已点赞", "like"), ("取消点赞", "like"),
)


def actions_from_text(text: str) -> Dict[str, bool]:
    """从 OCR / 字幕里扫互动线索（免费兜底；模型那条看按钮的结论为主）。"""
    blob = text or ""
    out: Dict[str, bool] = {}
    for word, name in _OCR_ACTION_HINTS:
        if word in blob:
            out[name] = "取消" not in word
    return out


@dataclass
class VideoRecord:
    """一支视频的档案：看了啥 + 你什么反应 + 算出来的兴趣分。"""

    at: float = 0.0
    genre: str = "其他"
    title: str = ""
    what: str = ""
    point: str = ""
    playstyle: str = ""          # 玩法（游戏才有）：搜刮跑毒 / 抽卡养成…
    art: str = ""                # 画风（游戏才有）：写实军武 / 像素 / 二次元…
    audience: str = ""           # 玩家群体（游戏才有）：硬核老玩家 / 学生党…
    keywords: List[str] = field(default_factory=list)
    actions: Dict[str, bool] = field(default_factory=dict)
    watch_sec: float = 0.0
    ended: bool = False          # 是不是看完了（看到尾声/自己划走算没看完）
    clock: str = ""

    def taste_words(self) -> List[str]:
        """这条档案里"能看出喜好"的那几个词（玩法 / 画风 / 玩家群体）。

        单独拎出来是给 `TasteLog.style_profile` 统计用的：类型只说明"看哪一类"，
        这几个词才说明"具体喜欢什么样的"——同样是游戏，他反复看的是搜刮跑毒还是抽卡养成，
        接话的方向完全不一样。
        """
        words: List[str] = []
        for raw in (self.playstyle, self.art, self.audience):
            for part in re.split(r"[、,，/|｜;；\s]+", str(raw or "")):
                word = part.strip("「」『』\"'“”()（）[]【】。.，,、 ")
                if 2 <= len(word) <= 12 and word not in ("未知", "不清楚", "其他"):
                    if word not in words:
                        words.append(word)
        return words

    def interest(self) -> float:
        """兴趣分：动作给主要权重，观看时长微调（秒划走是负分）。"""
        score = 0.0
        for name, hit in (self.actions or {}).items():
            if hit:
                score += _ACTION_SCORE.get(name, 0.0)
        if self.watch_sec >= 25.0:
            score += 1.0
        if self.ended:
            score += 2.0
        if 0.0 < self.watch_sec < 5.0:
            score -= 2.0
        return round(score, 2)

    def hint(self) -> str:
        """给提示词看的一行（不是要它照念的台词）。"""
        marks = [ACTION_LABELS[name] for name, hit in (self.actions or {}).items() if hit]
        tail = f"（你{'、'.join(marks)}了）" if marks else ""
        return f"{self.clock} {self.genre}｜{self.title}{tail}"


class TasteLog:
    """所有视频档案 + 从里面学出来的口味画像（本地 taste.json）。"""

    VERSION = 1
    SAVE_INTERVAL = 20.0
    MIN_SAMPLES = 3          # 至少这么多支视频，才敢下"他现在什么状态"的结论
    GENRE_MIN_SAMPLES = 2    # 单个类型至少看够这么多支，才敢说"他喜欢/不喜欢这类"
    TONE_WINDOW = 20         # 看最近多少支来定"现在该用什么劲儿"

    def __init__(self, cfg):
        self.cfg = cfg
        self.taste_cfg = getattr(cfg, "taste", None)
        self.path = self._resolve_path(cfg)
        self.records: List[VideoRecord] = []
        self._last_save = 0.0
        self._dirty = False
        self.load()

    def _resolve_path(self, cfg) -> Path:
        raw = str(getattr(self.taste_cfg, "path", "") or "taste.json")
        path = Path(raw)
        if not path.is_absolute():
            path = Path(cfg.config_path()).parent / path
        return path

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.taste_cfg, "enabled", True))

    # ---------- 读写 ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[taste] 读取 {self.path} 失败：{exc}")
            return
        for item in data.get("records") or []:
            if not isinstance(item, dict):
                continue
            try:
                self.records.append(
                    VideoRecord(
                        at=float(item.get("at") or 0.0),
                        genre=str(item.get("genre") or "其他"),
                        title=str(item.get("title") or ""),
                        what=str(item.get("what") or ""),
                        point=str(item.get("point") or ""),
                        playstyle=str(item.get("playstyle") or ""),
                        art=str(item.get("art") or ""),
                        audience=str(item.get("audience") or ""),
                        keywords=[str(k) for k in (item.get("keywords") or [])],
                        actions={str(k): _truthy(v) for k, v in (item.get("actions") or {}).items()},
                        watch_sec=float(item.get("watch_sec") or 0.0),
                        ended=bool(item.get("ended")),
                        clock=str(item.get("clock") or ""),
                    )
                )
            except Exception:
                continue

    def save(self, force: bool = False) -> bool:
        if not self.enabled:
            return False
        now = time.monotonic()
        if not force and (not self._dirty or now - self._last_save < self.SAVE_INTERVAL):
            return False
        limit = max(1, int(getattr(self.taste_cfg, "max_records", 200)))
        payload = {
            "version": self.VERSION,
            "records": [asdict(item) for item in self.records[-limit:]],
        }
        try:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            print(f"[taste] 写入 {self.path} 失败：{exc}")
            return False
        self._dirty = False
        self._last_save = now
        return True

    # ---------- 记录 ----------

    def start_video(
        self,
        note,
        ocr_text: str = "",
        window: str = "",
        at: Optional[float] = None,
    ) -> Optional[VideoRecord]:
        """新视频：从看片笔记开一条档案。"""
        if not self.enabled or note is None:
            return None
        stamp = float(at if at is not None else time.time())
        record = VideoRecord(
            at=stamp,
            genre=normalize_genre(getattr(note, "genre", "")),
            title=str(getattr(note, "title", "") or ""),
            what=str(getattr(note, "what", "") or ""),
            point=str(getattr(note, "point", "") or ""),
            playstyle=str(getattr(note, "playstyle", "") or ""),
            art=str(getattr(note, "art", "") or ""),
            audience=str(getattr(note, "audience", "") or ""),
            keywords=[str(k) for k in (getattr(note, "keywords", None) or [])],
            actions={},
            clock=time.strftime("%m-%d %H:%M", time.localtime(stamp)),
        )
        self.records.append(record)
        limit = max(1, int(getattr(self.taste_cfg, "max_records", 200)))
        if len(self.records) > limit:
            del self.records[:-limit]
        self._dirty = True
        self.save()
        print(f"[taste] 记下第 {len(self.records)} 支：{record.genre}｜{record.title[:24]}")
        return record

    def update(self, record: Optional[VideoRecord], note=None, actions=None) -> None:
        """同一支视频重读 / 又看出互动：就地更新，不新增记录。"""
        if not self.enabled or record is None:
            return
        if note is not None:
            genre = normalize_genre(getattr(note, "genre", ""))
            if genre != "其他" or record.genre == "其他":
                record.genre = genre
            for name in ("title", "what", "point", "playstyle", "art", "audience"):
                value = str(getattr(note, name, "") or "")
                if value:
                    setattr(record, name, value)
            words = list(record.keywords)
            for word in getattr(note, "keywords", None) or ():
                if str(word) not in words:
                    words.append(str(word))
            record.keywords = words[:8]
        if actions:
            for name, hit in actions.items():
                if hit and not record.actions.get(name):
                    print(f"[taste] 看到你{ACTION_LABELS.get(name, name)}了：{record.title[:20]}")
                record.actions[name] = bool(hit)
        self._dirty = True
        self.save()

    def finish(self, record: Optional[VideoRecord], watch_sec: float, ended: bool) -> None:
        """这支看完了 / 被划走了：结算观看时长，兴趣分这时候才算得准。"""
        if not self.enabled or record is None:
            return
        record.watch_sec = max(float(record.watch_sec), float(watch_sec or 0.0))
        record.ended = bool(ended or record.ended)
        self._dirty = True
        self.save()
        print(
            f"[taste] 结算：{record.genre}｜看了 {int(record.watch_sec)}s｜"
            f"兴趣 {record.interest():+.1f}｜{record.hint()}"
        )

    # ---------- 学出来的结论 ----------

    def stats(self, window: int = 0) -> Dict[str, Tuple[int, float]]:
        """大类 -> (支数, 平均兴趣分)。window>0 时只看最近这么多支。"""
        rows = self.records[-window:] if window else list(self.records)
        buckets: Dict[str, List[float]] = {}
        for item in rows:
            buckets.setdefault(item.genre or "其他", []).append(item.interest())
        return {
            genre: (len(scores), round(sum(scores) / len(scores), 2))
            for genre, scores in buckets.items()
        }

    def tone(self) -> Tuple[str, float, int]:
        """现在该用什么劲儿：warm（他看得起劲）/ neutral / cool（他没什么兴致）。"""
        rows = self.records[-self.TONE_WINDOW:]
        if not rows:
            return "neutral", 0.0, 0
        avg = sum(item.interest() for item in rows) / len(rows)
        if len(rows) >= self.MIN_SAMPLES and avg >= 1.5:
            return "warm", round(avg, 2), len(rows)
        if len(rows) >= self.MIN_SAMPLES and avg <= -0.5:
            return "cool", round(avg, 2), len(rows)
        return "neutral", round(avg, 2), len(rows)

    def nudge_scale(self) -> float:
        """主动搭话的频率系数：他爱看的类型多陪几句，没兴致的少开口。"""
        return {"warm": 0.6, "neutral": 1.0, "cool": 1.6}.get(self.tone()[0], 1.0)

    def liked_genres(self, limit: int = 3) -> List[Tuple[str, int, float]]:
        """他真喜欢的类型：样本够 + 平均兴趣为正，按兴趣分排。"""
        rows = [
            (genre, count, avg)
            for genre, (count, avg) in self.stats().items()
            if count >= self.GENRE_MIN_SAMPLES and avg > 0.5 and genre != "其他"
        ]
        rows.sort(key=lambda row: (row[2], row[1]), reverse=True)
        return rows[:limit]

    def cold_genres(self, limit: int = 2) -> List[Tuple[str, int, float]]:
        """他基本划走的类型。"""
        rows = [
            (genre, count, avg)
            for genre, (count, avg) in self.stats().items()
            if count >= self.GENRE_MIN_SAMPLES and avg <= 0.0 and genre != "其他"
        ]
        rows.sort(key=lambda row: row[2])
        return rows[:limit]

    def hot_topics(self, limit: int = 4) -> List[str]:
        """他点过赞/收藏的那些视频里反复出现的词——他真正在意的东西。"""
        counts: Dict[str, int] = {}
        for item in self.records:
            if item.interest() <= 0:
                continue
            for word in item.keywords:
                text = str(word).strip()
                if 2 <= len(text) <= 12:
                    counts[text] = counts.get(text, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        return [word for word, _ in ranked[:limit]]

    def recent_titles(self, limit: int = 3) -> List[str]:
        return [item.title for item in self.records[-limit:] if item.title]

    def style_profile(self, limit: int = 4) -> List[Tuple[str, int]]:
        """他看的内容里反复出现的"玩法 / 画风 / 玩家群体"词（本地统计，不花钱）。

        为什么要单算这个：类型（游戏 / 影视综艺）只说明"看哪一类"，说不出
        "具体喜欢什么样的"。同样是游戏，他反复看的是**搜刮跑毒的射击**还是**抽卡养成**，
        玩法/画风/群体这几个词一带上，接话的方向才应景（也才知道该跟谁聊什么）。
        只数出现 2 次以上的：一次可能是模型随口一写，反复出现才算"他就吃这一口"。
        """
        counts: Dict[str, int] = {}
        for item in self.records:
            for word in item.taste_words():
                counts[word] = counts.get(word, 0) + 1
        ranked = sorted(
            ((word, count) for word, count in counts.items() if count >= 2),
            key=lambda kv: (-kv[1], kv[0]),
        )
        return ranked[:limit]

    def profile_block(self) -> str:
        """给提示词的一段「你摸清的他的口味」；数据太少就返回空串（没有就别硬编）。"""
        if not self.enabled or len(self.records) < self.MIN_SAMPLES:
            return ""
        tone, avg, count = self.tone()
        stats = sorted(self.stats().items(), key=lambda kv: kv[1][0], reverse=True)
        lines: List[str] = []
        top = "、".join(f"{genre}（{need[0]} 支）" for genre, need in stats[:4] if need[0])
        if top:
            lines.append("· 看得最多：" + top)
        liked = self.liked_genres()
        if liked:
            lines.append(
                "· 会主动点赞/收藏的："
                + "、".join(f"{genre}（{n} 支，平均 {a:+.1f}）" for genre, n, a in liked)
            )
        cold = self.cold_genres()
        if cold:
            lines.append(
                "· 基本划走的：" + "、".join(f"{genre}（平均 {a:+.1f}）" for genre, _, a in cold)
            )
        topics = self.hot_topics()
        if topics:
            lines.append("· 他反复看的：" + "、".join(f"「{word}」" for word in topics))
        styles = self.style_profile()
        if styles:
            lines.append(
                "· 他好这口（玩法 / 画风 / 玩家群体）："
                + "、".join(f"{word}（{count} 次）" for word, count in styles)
            )
        recent = self.recent_titles()
        if recent:
            lines.append("· 最近这几支：" + " / ".join(recent))
        mood = {
            "warm": "他最近看得挺来劲，你可以跟着一起起哄、多聊两句。",
            "cool": "他最近看得没什么兴致，别硬找话说，一句带过就行。",
            "neutral": "他看的东西杂，跟着内容走就行，别端着。",
        }.get(tone, "")
        lines.append(f"· 现在该用什么劲儿：{mood}（最近 {count} 支平均兴趣 {avg:+.1f}）")
        return (
            "【你摸清的他的口味（你自己攒下来的，别念出来，只用来决定说什么、说到什么程度）】\n"
            + "\n".join(lines)
        )
