"""整集资料卡：认出"这是哪一集"，先把这一集的底细弄到手，再去跟用户互动。

为什么要有它
    `keyinfo` 能从画面上的字里认出「《奔跑吧》第九季 第28集」，但它只知道**这是哪一集**，
    不知道**这一集讲了什么**。用户的原话是：「根据这个去扒他的原片，整个看完再回来」——
    也就是先弄清楚这一集到底是什么内容，吐槽、夸人、答话才接得住。

做到了什么、没做什么（这点要写清楚，免得误会）
    · **没做**：真去下载并播放一整集视频。那需要视频源、登录态、几十上百 MB 流量，
      还有版权问题——不是这个挂件该干的事，也不该让用户的机器偷偷去干。
    · **做了什么**：拿「节目名 + 季 + 集」去问一次模型「这一集讲了什么」（**纯文本**、
      不走画面、不额外截图），把答案存成一张资料卡，之后一直拿它当背景。
      这等于把"看完这一集"换成了"把这一集的底细弄到手"——对"能不能接住用户的话"
      这件事来说，效果是一样的。
    · **认不出来就不写**：模型说不知道就当没问过，不硬编（跟 keyinfo 一个态度）。
    · **想接自己的数据源**：`episode.source` 里填一个命令或 URL 模板
      （`{show}` / `{season}` / `{episode}` 三个占位符），它拿到的文字会直接当资料卡。

存哪儿
    `episodes.json`（跟 memory.json / taste.json 一样，纯本地、可随时删；
    `episode.enabled = false` 就整个关掉，一次模型都不问）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

# 「第九季」「第28集」「第三期」——季和集都可能出现在 info.episode 里，靠这两个正则分开
_SEASON = re.compile(r"^第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*季$")
_EPISODE = re.compile(r"^第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*[集期部话回]$")

MAX_SHOW_CHARS = 16
MAX_FIELD_CHARS = 90
MAX_POINTS = 4
MAX_RECORDS = 120

# 模型交回来的键名 → 我们的字段
_KEY_FIELDS: Dict[str, str] = {
    "梗概": "summary", "剧情": "summary", "讲的什么": "summary", "内容": "summary",
    "这一集": "summary", "介绍": "summary",
    "人物": "cast", "嘉宾": "cast", "主角": "cast", "阵容": "cast",
    "看点": "point", "亮点": "point", "名场面": "point", "精彩": "point",
}
_KV = re.compile(r"^\s*[-*·•\d.、]*\s*[【\[]?\s*([^：:\[\]【】\s]{1,8})\s*[】\]]?\s*[:：]\s*(.*)$")
# 「我不知道」「查不到」这种：说明它没这份资料，那就什么都别存
_UNKNOWN = re.compile(r"不知道|不清楚|不了解|查不到|没有这|无法确定|没找到|不确定|无法提供")
# 明确的"我不知道这一集"说法（长这样就直接判为没有）
_UNKNOWN_STRONG = re.compile(
    r"不知道这一集|不清楚这一集|不了解这一集|不确定这一集|没有这一集|查不到|"
    r"没找到|无法确定|无法提供|不知道具体"
)
# 整段就是一个光杆儿"不知道 / 不确定"
_UNKNOWN_BARE = re.compile(r"^(?:我)?(?:也|真|确实|暂时|目前)?(?:不知道|不清楚|不了解|不确定|查不到)$")
# 模型爱写的前缀（「当然」「好的」），剥掉
_STRIP_HEAD = re.compile(r"^\s*(?:好的|当然|没问题|这一集|本集|关于这一集)[，,、:：\s]*")


def _clip(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip()).strip("「」『』\"'“”《》[]【】()（）。.,，、 ")
    if limit and len(value) > limit:
        value = value[: max(1, limit - 1)].rstrip() + "…"
    return value


@dataclass
class Brief:
    """一集的资料卡（认不出来就是空的，什么都不写）。"""

    key: str = ""
    show: str = ""
    season: str = ""
    episode: str = ""
    at: float = 0.0
    summary: str = ""
    cast: str = ""
    points: List[str] = field(default_factory=list)
    raw: str = ""

    @property
    def title(self) -> str:
        return " ".join(part for part in (self.show, self.season, self.episode) if part).strip()

    def is_empty(self) -> bool:
        return not (self.summary or self.cast or self.points)

    def line(self) -> str:
        parts: List[str] = []
        if self.title:
            parts.append(self.title)
        if self.summary:
            parts.append(self.summary)
        if self.points:
            parts.append("看点：" + " / ".join(self.points))
        return _clip("｜".join(parts), 140)

    def block(self) -> str:
        """给提示词的那一段；没有资料就返回空串（不加空壳）。"""
        if self.is_empty():
            return ""
        rows: List[str] = []
        if self.title:
            rows.append("· 这一集：" + self.title)
        if self.summary:
            rows.append("· 讲的什么：" + self.summary)
        if self.cast:
            rows.append("· 人物/嘉宾：" + self.cast)
        if self.points:
            rows.append("· 名场面/看点：" + " / ".join(self.points))
        return (
            "【这一集讲的是什么（你事先做过功课，不是只看这一眼）】\n"
            + "\n".join(rows)
            + "\n这一份是你**开播前了解过的整集内容**：聊到这一集时可以直接用里面的细节，"
            "省得只盯着眼前这一帧瞎猜。但它是你的印象，说起来留点余地"
            "（「我印象里」「听说这集」），别当成铁板钉钉的事实，也别整段念给用户听。"
        )


def identify(info) -> Tuple[str, str, str]:
    """从 keyinfo 认出来的东西里挑出 (节目名, 季, 集)。挑不出来就返回空字符串。"""
    shows = [str(x) for x in (getattr(info, "shows", None) or ())]
    episodes = [str(x) for x in (getattr(info, "episode", None) or ())]
    show = ""
    for name in shows:
        if not name or len(name) > MAX_SHOW_CHARS:
            continue
        if _SEASON.match(name) or _EPISODE.match(name):
            continue                      # 「第九季」这种是季数，不是节目名
        show = name
        break
    season = next((x for x in episodes if _SEASON.match(x)), "")
    episode = next((x for x in episodes if _EPISODE.match(x)), "")
    return show, season, episode


def _clean_key(word: str) -> str:
    return (word or "").strip().strip("「」『』\"'“”《》[]【】")


def _is_unknown(value: str) -> bool:
    """这一段是不是"我也不知道"（提示词里给的写法是「不知道这一集的具体内容」）。

    判得**保守**一点：真梗概里出现「不知道」很正常
    （「他们不知道下一秒会发生什么，结果全场笑疯」），不能因为这三个字就把好料扔掉。
    所以要么是"我不知道这一集"这种明确说法，要么整段就是个光杆儿「不知道」。
    """
    text = (value or "").strip()
    if not text:
        return False
    return bool(_UNKNOWN_STRONG.search(text) or _UNKNOWN_BARE.match(text))


def parse(text: str) -> Brief:
    """把模型交回来的那份功课解析成 Brief（认不出键值就整段当梗概）。"""
    brief = Brief(raw=(text or "").strip())
    body = _STRIP_HEAD.sub("", brief.raw)
    for line in [row.strip() for row in body.splitlines() if row.strip()]:
        found = _KV.match(line)
        if not found:
            continue
        word, value = _clean_key(found.group(1)), found.group(2).strip()
        target = _KEY_FIELDS.get(word)
        if not target or not value or _is_unknown(value):
            continue
        value = _clip(value)
        if target == "summary" and not brief.summary:
            brief.summary = value
        elif target == "cast" and not brief.cast:
            brief.cast = value
        elif target == "point" and value not in brief.points and len(brief.points) < MAX_POINTS:
            brief.points.append(value)
    if not (brief.summary or brief.cast or brief.points):
        # 没写成键值对：整段当梗概——但「我不知道」那种不算（宁可不写，也不瞎编）
        if body and not _UNKNOWN.search(body):
            brief.summary = _clip(body)
    if brief.is_empty():
        return Brief(raw=brief.raw)
    return brief


class EpisodeLog:
    """整集资料卡：按「节目|季|集」缓存，同一集只做一次功课。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.ep = getattr(cfg, "episode", None)
        path = str(getattr(self.ep, "path", "") or "episodes.json")
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = ROOT / self.path
        self._briefs: Dict[str, Brief] = {}
        self._asked = 0.0
        self._current: Tuple[str, str, str] = ("", "", "")
        self._current_key = ""
        self.count = 0
        self.load()

    # ---------- 开关 / 身份 ----------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.ep, "enabled", True))

    @property
    def current_key(self) -> str:
        return self._current_key

    @property
    def current_parts(self) -> Tuple[str, str, str]:
        return self._current

    def note(self, info) -> Optional[str]:
        """看一眼这一眼认出来的是哪一集；换了集就换身份。

        返回非空表示"认出来了"（不管有没有做过功课）。
        """
        if not self.enabled or info is None:
            return None
        show, season, episode = identify(info)
        if not show or not (season or episode):
            return None                      # 认不出是哪一集，就什么都别问
        key = "|".join((show, season, episode))
        if key != self._current_key:
            self._current_key = key
            self._current = (show, season, episode)
        return key

    # ---------- 什么时候该做功课 ----------

    def needs_brief(self) -> bool:
        """这一集还没做过功课（或者太久没更新）时返回 True。"""
        if not self.enabled or not self._current_key:
            return False
        gap = max(0.0, float(getattr(self.ep, "min_gap_sec", 20.0) or 0.0))
        if gap and (time.monotonic() - self._asked) < gap:
            return False                     # 刚问过，缓一缓（别在片头连问好几次）
        brief = self._briefs.get(self._current_key)
        if brief is None:
            return True
        if brief.is_empty():
            return False                     # 问过了、模型也不知道：别再问了
        refresh = max(0.0, float(getattr(self.ep, "refresh_days", 30.0) or 0.0)) * 86400.0
        return bool(refresh) and (time.time() - brief.at) > refresh

    def mark_asked(self) -> None:
        self._asked = time.monotonic()

    def apply(self, key: str, text: str) -> Optional[Brief]:
        """把做好的功课存下来（模型不知道就存一张空卡，免得同一集反复问）。"""
        brief = parse(text)
        brief.key = key
        parts = list(key.split("|")) + ["", "", ""]
        brief.show, brief.season, brief.episode = parts[0], parts[1], parts[2]
        brief.at = time.time()
        self._briefs[key] = brief
        self._trim()
        self.save()
        return brief

    # ---------- 给提示词 ----------

    def block(self) -> str:
        """当前这一集的资料卡（没有就是空串）。"""
        if not self.enabled or not self._current_key:
            return ""
        brief = self._briefs.get(self._current_key)
        return brief.block() if brief is not None else ""

    def line(self) -> str:
        brief = self._briefs.get(self._current_key) if self._current_key else None
        return brief.line() if brief is not None else ""

    def stats(self) -> Dict[str, object]:
        return {
            "enabled": self.enabled,
            "count": self.count,
            "current": self._current_key,
            "cached": len(self._briefs),
        }

    # ---------- 存盘 ----------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[episode] 资料卡读坏了，当空的：{exc}")
            return
        for item in data.get("briefs") or []:
            key = str(item.get("key") or "")
            if not key:
                continue
            self._briefs[key] = Brief(
                key=key,
                show=str(item.get("show") or ""),
                season=str(item.get("season") or ""),
                episode=str(item.get("episode") or ""),
                at=float(item.get("at") or 0.0),
                summary=str(item.get("summary") or ""),
                cast=str(item.get("cast") or ""),
                points=[str(x) for x in (item.get("points") or []) if str(x).strip()][:MAX_POINTS],
                raw=str(item.get("raw") or ""),
            )
        self.count = len(self._briefs)

    def save(self) -> None:
        if not self.enabled:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "briefs": [
                    {
                        "key": brief.key,
                        "show": brief.show,
                        "season": brief.season,
                        "episode": brief.episode,
                        "at": round(brief.at, 3),
                        "summary": brief.summary,
                        "cast": brief.cast,
                        "points": list(brief.points),
                        "raw": brief.raw if len(brief.raw) <= 600 else brief.raw[:600],
                    }
                    for brief in self._briefs.values()
                ]
            }
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            self.count = len(self._briefs)
        except Exception as exc:      # 存不下来不能把后台线程带走
            print(f"[episode] 资料卡存不下来：{exc}")

    def _trim(self) -> None:
        limit = max(1, int(getattr(self.ep, "max_records", MAX_RECORDS) or MAX_RECORDS))
        if len(self._briefs) <= limit:
            return
        old = sorted(self._briefs.items(), key=lambda kv: kv[1].at)
        for key, _ in old[: max(1, len(self._briefs) - limit)]:
            self._briefs.pop(key, None)


def fetch_source(template: str, show: str, season: str, episode: str, timeout: float = 20.0) -> str:
    """`episode.source`：自己接一个数据源（命令或 URL 模板）。

    模板里可以写 `{show}` / `{season}` / `{episode}`；拿到什么就原样当资料卡的文字。
    这是个口子——有靠谱的片单 / 接口就往里填，没有就不填（默认走"问模型"那条路）。
    """
    import urllib.request

    command = (template or "").strip()
    if not command:
        return ""
    text = (
        command.replace("{show}", show).replace("{season}", season).replace("{episode}", episode)
    )
    if text.lower().startswith(("http://", "https://")):
        with urllib.request.urlopen(text, timeout=timeout) as resp:   # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        done = subprocess.run(                     # noqa: S603
            text, shell=True, capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=flags,
        )
    except Exception as exc:
        print(f"[episode] 自定义数据源跑失败：{exc}")
        return ""
    return (done.stdout or "").strip()

