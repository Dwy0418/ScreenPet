"""隐身学习：你不需要它的时候，它收进托盘，自己上网补课。

为什么要有它
    收进托盘 = "这会儿不用你"，屏幕上就没它了。可它原来还在后台空转：那套循环照旧
    拍你的屏幕——人都走了，拍下来也没人看，白花算力、白花接口钱。所以这里给它换一份
    活干：**不看你屏幕，改成自己上网**。拿长期记忆里"你最近老在看什么"
    （`memory.topics()` / `top_tags()`）当话头，搜几页、读一遍，再让模型把读到的
    整成两样东西：

      · **口语语料** → `data/chat_style.json` 的 dialogue（跟边看边学同一个出口，
        写完 `humanstyle.reload()`，回屏幕之后说话就用上了，不用重启）
      · **知识点**   → `memory.json`（`memory.add(..., dialog="learn")`，
        就是 dialog.py 里「学习」那一类）

四道闸（网上抄来的东西大半是垃圾，跟 corpus.py 一个思路）
    ① 网页先剥标签、只留正文，太长就截断（模型读不完，也省 token）；**太短 /
       中文太少 / 跟话头不沾边**的页面直接不算料（`page_ok()`）——现场抓过：
       搜索结果里一半是导航页和加载页，塞给模型只会让它硬编。
    ② 模型写回来的每一句都过 `corpus.looks_like_speech()`：不像人话、书面腔、
       解说腔、界面词，一律不要
    ③ 有网页材料时再核一遍（`study.verify`）：把候选连同材料交给模型问
       "哪几句材料里真有依据"，材料里找不到的一律不要（`verify()`）——小模型
       爱把常识当"从网上学的"，这一道专治那个。核不出来时**保留原样**，宁可白核。
    ④ 语料里已有的段落用 `humanstyle.similarity` 比一遍（≥0.7 当同一段，不再塞）

学得更准的另外两件事（都不额外花接口钱）
    · **补课单独指定模型**：`study.model / base_url / provider` 填了就单开一份客户端
      （补课是纯文本活儿，用 glm-4-flash 这类文本模型比主配置那个视觉模型准得多、
      也便宜）；留空则跟主配置同一个。
    · **话头先精修再出门**：标题里的「第 X 期」「完整版」「- 哔哩哔哩」这类噪声剥掉，
      "游戏""短视频"这种查了也是白查的宽泛词直接不要（`refine_topic()`）；
      学过的话头记进账本（`study.ledger`，默认 `data/study.json`），
      `topic_cooldown_sec` 之内不再学，一直交白卷的排到最后（省得每轮都啃同一个梗）。

联网不通怎么办（国内搜索引擎经常打不开）
    `study.model_fallback=true`（默认）时就直接拿话头问模型本人——一样是"上网"、
    一样花接口钱，只是没有网页做依据；关掉它就这一轮什么都不写。**两种情况都不会
    自己编内容往语料里塞**：模型没按格式回 → 这一轮交白卷。

默认开着（收进托盘就自己补课，不用谁点头；右键菜单里那个开关已经撤了）。它要联网、要花钱，
不想让它花就在 config.json 里把 `study.enabled` 改成 false。
"""
from __future__ import annotations

import copy
import html as html_mod
import json
import re
import shutil
import sys
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

import requests

from . import corpus, humanstyle
from .config import StudyConfig
from .vlm import VlmError, VisionClient

#: 搜索引擎：名字 -> 查询地址模板（{q} 会被 URL 编码后填进去）
ENGINES: Dict[str, str] = {
    "bing": "https://www.bing.com/search?q={q}&setlang=zh-CN",
    "duckduckgo": "https://html.duckduckgo.com/html/?q={q}&kl=cn-zh",
}
AUTO_ORDER: Tuple[str, ...] = ("bing", "duckduckgo")

#: 不带浏览器 UA 的话，这两家都会甩一个"你像机器人"的空白页回来
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

#: 记忆还空着（第一次跑 / 刚清过）时学什么：挑"搜得动"的词——
#: 现场试过：写成「大家最近都在聊什么」这种句子，搜回来的是同名杂志和公司官网，
#: 材料跟话头对不上，学到的全是垃圾。
DEFAULT_TOPICS: Tuple[str, ...] = ("网络热梗", "网友常用的口头禅")

#: 搜索页自己不算"料"（免得抓回来一堆搜索结果列表）
_SKIP_HOSTS: Tuple[str, ...] = ("bing.com", "duckduckgo.com", "baidu.com", "google.com")

#: 话头最长这么多字：再长搜索引擎也当整句处理，搜回来的东西更散
TOPIC_MAX_CHARS = 24

#: 标题当话头时要剥掉的噪声：`奔跑吧（第11期）`、`XX完整版-哔哩哔哩`、`【4K】…`。
#: 这些词拿去搜，搜回来的是同一支视频的切片和播放页，不是"这个话题本身"。
_TOPIC_BRACKET_RE = re.compile(r"[（(【\[][^）)】\]]{0,24}[）)】\]]")
_TOPIC_NOISE_RE = re.compile(
    r"第\s*[0-9一二三四五六七八九十百]+\s*[集期季话部篇]|完整版|合集|花絮|预告|"
    r"高清|超清|\d{3,4}\s*[pP]|4K|双语|中字|字幕|重制|直播回放|全场|精选",
    re.I,
)
#: 尾巴上的站名：`… - 哔哩哔哩` / `…_腾讯视频`（同一条视频在哪个站都一样，留着只会干扰搜索）
_TOPIC_SITE_RE = re.compile(
    r"[\s\-—_|｜·]*(?:哔哩哔哩|bilibili|腾讯视频|爱奇艺|优酷|西瓜视频|抖音|快手|"
    r"小红书|微博|好看视频|芒果TV|YouTube|百度|搜狗|知乎|贴吧)[\s\-—_|｜·]*$",
    re.I,
)
#: 太宽泛的话头：拿它们去搜，回来的是同名杂志 / 公司官网 / 一堆不相干的页面（现场踩过）。
#: 注意这里**不是**禁用，只是排到最后（记忆还空着时只有 DEFAULT_TOPICS 可用，
#: 硬禁会让第一次跑起来的那只宠物没得学）。
VAGUE_TOPICS: Tuple[str, ...] = (
    "游戏", "视频", "短视频", "直播", "推荐页", "首页", "热搜", "热点", "弹幕",
    "动画", "电影", "电视剧", "综艺", "音乐", "图片", "表情包", "热门内容",
    "内容", "东西", "这个", "那个", "不明", "看不懂", "网络热梗",
)

#: 正文里中文占比低于这个数（整页英文 / 全是符号）就不算料：中文宠物学这个没用
PAGE_MIN_CJK = 0.15
_CJK_ALL_RE = re.compile(r"[\u3400-\u9fff]")
_WORD_RE = re.compile(r"[0-9A-Za-z\u3400-\u9fff]+")

#: 「这些话在材料里有依据吗」那段提示词。要点跟 PROMPT 一样：只给骨架、不给成品，
#: 并且明确"别拿你自己知道的补"——不然它会把常识当"从网上学的"。
VERIFY_PROMPT = """材料：
{material}

下面是一份候选清单（有些条目在材料里根本没有依据）：
{items}

只做一件事：挑出**材料里确实能指出对应句子**的那些，回它们前面的编号。
一行一个编号，别的什么都别写。一条都没有依据，就只回两个字：{empty}
不要用材料以外的知识补，材料里找不到就算没有。
"""
#: 校验结果里"一条都没有"的说法（模型爱写这两种）
EMPTY_MARKS: Tuple[str, ...] = ("没有", "无", "none", "None", "NONE")

_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"', re.I)
_DDG_LINK = re.compile(r'class="result__a"[^>]*href="([^"]+)"', re.I)

_SCRIPT_RE = re.compile(r"<(script|style|noscript|head)[^>]*>.*?</\1>", re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BLOCK_RE = re.compile(r"</?(?:p|div|br|li|tr|h[1-6]|section|article)[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")

#: 模型回话的行首标记（只认这几个词，其它行一律当噪声——宁可少学一句，
#: 也不能让"以下是解释"这种话混进语料）
ROW_HEADS: Tuple[str, ...] = ("接话", "对话", "一问一答")
NOTE_HEADS: Tuple[str, ...] = ("知识", "知识点", "记住")
#: 行首是"接话 / 知识"这一类词，后面跟可选的序号、装饰和冒号——
#: 模型爱写 `**接话**：…`、`接话1：…`、`` `知识`：… `` 这些花样，
#: 卡太死就等于它白说（现场真发生过：一整轮 0 产出）。
_LINE_RE = re.compile(r"^(?P<head>[^\s:：]{1,10})\s*(?:\d+)?\s*[:：]\s*(?P<body>.+)$")
_SPLIT_RE = re.compile(r"\s*(?:\||｜|→|->|=>)\s*")
#: 两头要剥掉的装饰：markdown 的 ** / # / `、列表的短横线 / 空白
_DECOR_RE = re.compile(r"^[\s*`_#>·•\-–—]+|[\s*`_#>·•]+$")
#: 模型自创的标签：现场抓到过 glm-4v-flash 写成「别人问：… / 我接：…」两行一组。
#: 只认"像在问 / 像在答"这两种，别的（"特点：""介绍："）一律当解释丢掉。
_SELF_ASK_RE = re.compile(r"问|提问")
_SELF_REPLY_RE = re.compile(r"接|答|回")

#: "一问一答的官网 FAQ"长什么样：问句在问"有什么 / 是什么 / 怎么玩"，
#: 答句是清单式（"有 A、B 等。"）。现场抓到过一次：模型把王者荣耀官网 FAQ
#: 整段搬了进来（15 组全是一个模子）。这种东西写进 dialogue 会把它的说话方式
#: 往"客服 / 百科"上带——README 里那套反书面腔就白设了。判据故意保守：
#: 两边都像才拦，正常的"你会玩这个？/带带我呗"这种闲聊不受影响。
_FAQ_ASK_RE = re.compile(r"有什么|有哪些|是什么|怎么玩|怎么用|怎么回事|怎么下载|怎么获得")
_FAQ_REPLY_RE = re.compile(r"^(?:有|包括|例如|比如|分为|主要是)|等[。\s]*$")

#: **骨架占位词**：提示词里写的是「接话：上句 ｜ 下句」「知识：一句话」，
#: 小模型十有八九会把这两行原样抄回来（现场抓到过一次：`["上句", "下句"]` 真被写进
#: 语料）。所以这几个词一律当"它没真回答"，直接丢。
PLACEHOLDERS: Tuple[str, ...] = (
    "上句", "下句", "上一句", "下一句", "一句话", "例句", "示例", "某句",
    "问题", "回答", "提问", "回复", "接话", "知识", "省略", "……", "...",
)

#: 材料跟话头对不上时，让它交白卷（搜"大家最近都在聊什么"会搜到同名的杂志、
#: 公司官网——那种页面里抄出来的"知识"还不如不学）
SKIP_MARK = "跳过"

#: 让模型把材料整成"能用的东西"的那段话。格式写得死板的理由跟 persona.py 一样：
#: 只要给它一句能直接抄的成品，它就会照抄那一句；所以这里只给骨架。
PROMPT = """你是「{name}」——一个陪人看视频的桌面宠物。你主人这会儿不在，你在后台上网补课。
话头：{topic}
材料：
{material}

照着下面这个样子写，**只写 4 行，多一行都不要**（内容必须写在冒号后面的同一行里，
分成两行写的我不要）：
接话：（一个人随口问的一句话）｜（另一个人随口接的一句话）
接话：（一个人随口问的一句话）｜（另一个人随口接的一句话）
知识：（一句具体的知识点）
知识：（一句具体的知识点）

要求：
· 接话要像两个人一起玩 / 一起看的时候随口说的（「这波真亏」「你会玩这个？」「带带我呗」），
  不是问答百科：**不要**写「X 有什么 Y？」「X 是什么？」「X 怎么玩？」这种问句，
  也不要「有 A、B 等。」这种清单式回答。
· 知识要短、要具体，一句话不超过 25 个字；**不要罗列**（不要一条接一条写十几行）。
· 别复述材料原文，别写标题，别写解释。

材料要是跟「{topic}」没关系（比如搜到同名的杂志、公司、无关新闻），就只回两个字：跳过
"""


@dataclass
class Lesson:
    """一轮学习的产出（给日志和托盘状态看）。"""

    topic: str = ""
    rows: List[List[str]] = field(default_factory=list)     # 接话对（会进语料）
    notes: List[str] = field(default_factory=list)          # 知识点（会进记忆）
    sources: List[str] = field(default_factory=list)        # 真读了哪几页
    via: str = ""                                           # web = 读过网页 / model = 只问了模型
    added_rows: int = 0
    added_notes: int = 0
    dropped: int = 0                                        # 校验拦掉了几条"材料里没依据的"

    @property
    def line(self) -> str:
        """给日志和托盘看的一句话。"""
        how = f"读了 {len(self.sources)} 页" if self.sources else "只问了模型"
        extra = f"，核掉 {self.dropped} 条没依据的" if self.dropped else ""
        return (
            f"{self.topic}（{how}）：语料 +{self.added_rows} 段"
            f" / 记忆 +{self.added_notes} 条{extra}"
        )


# ---------------------------------------------------------------- 纯函数（好测）

def strip_html(raw: str, limit: int = 2400) -> str:
    """把一页 HTML 读成纯文字。

    只用正则，不引 BeautifulSoup：这里要的不是"解析得准"，而是"别把 script、
    导航、样式表当正文喂给模型"。块级标签换成换行（正文是按行读的，糊成一坨更糟），
    最后按 limit 截断。
    """
    text = _COMMENT_RE.sub(" ", raw or "")
    text = _SCRIPT_RE.sub(" ", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    body = "\n".join(line for line in lines if line)
    return body[: max(0, int(limit))]


def real_url(raw: str) -> str:
    """把搜索结果里那个"跳转壳"还原成真地址（DuckDuckGo 把真地址藏在 uddg 里）。"""
    url = (raw or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if "uddg=" in url:
        query = urllib.parse.urlparse(url).query
        target = urllib.parse.parse_qs(query).get("uddg") or []
        if target:
            url = urllib.parse.unquote(target[0])
    return url if url.startswith("http") else ""


def result_links(engine: str, html_text: str) -> List[str]:
    """从搜索结果页里挑出几条真网址（去掉搜索站自己 + 去重）。"""
    pattern = _DDG_LINK if engine == "duckduckgo" else _BING_LINK
    out: List[str] = []
    for raw in pattern.findall(html_text or ""):
        url = real_url(raw)
        if not url or any(host in url for host in _SKIP_HOSTS):
            continue
        if url not in out:
            out.append(url)
    return out


def refine_topic(topic: str) -> str:
    """把话头搓成一个"搜得动"的查询词。

    标题当话头最准（worker 给的就是"他刚在看的那一支"），可标题本身带着一堆跟内容
    无关的噪声：`奔跑吧第十四季第11期（完整版）- 哔哩哔哩`。原样拿去搜，搜回来的是
    同一支视频的播放页和切片，不是"这个话题本身"。剥法：先去掉方括号里的短语
    （`【4K】` `（第11期）`），再删噪声词，最后去掉尾巴上的站名；
    实在剥没了（或者只剩标点）返回空串，调用方换下一个话头。
    """
    text = _TOPIC_BRACKET_RE.sub(" ", str(topic or ""))
    text = _TOPIC_NOISE_RE.sub(" ", text)
    text = _TOPIC_SITE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -—_|｜·、,，")
    if len(text) > TOPIC_MAX_CHARS:
        text = text[:TOPIC_MAX_CHARS].strip()
    words = _WORD_RE.findall(text)
    if not words or sum(len(word) for word in words) < 2:
        return ""
    return text


def is_vague(topic: str) -> bool:
    """这个话头是不是太宽泛（拿来搜只会搜到同名杂志 / 公司官网那种，见 VAGUE_TOPICS）。

    只用来**排序**：宽泛的排到最后，别的都用完了才轮到它——所以第一次跑起来
    （记忆还空着、只有 DEFAULT_TOPICS）也照样有得学。
    """
    text = str(topic or "").strip()
    return (not text) or len(text) < 2 or text in VAGUE_TOPICS


def topic_grams(topic: str) -> List[str]:
    """话头的"两字词"：判断一页正文跟这个话题沾不沾边（中文没空格，只能这么切）。"""
    text = "".join(_WORD_RE.findall(str(topic or "")))
    return [text[i : i + 2] for i in range(len(text) - 1)]


def cjk_ratio(text: str) -> float:
    """汉字占多少：整页英文 / 全是符号的页面直接不算料。"""
    body = str(text or "")
    if not body:
        return 0.0
    return len(_CJK_ALL_RE.findall(body)) / len(body)


def page_ok(text: str, topic: str, min_chars: int = 300, min_match: int = 1) -> bool:
    """这一页算不算"料"：够长、有中文、跟话头沾边（三条缺一不可）。

    现场踩出来的：搜索结果里相当一部分是导航页 / 加载页 / 404 / 同名别的站，
    塞给模型它就照着这些编。宁可这一轮少读一页，也别让它拿废话当材料。
    """
    body = str(text or "").strip()
    if len(body) < max(0, int(min_chars)):
        return False
    if cjk_ratio(body) < PAGE_MIN_CJK:
        return False
    want = max(0, int(min_match))
    if want <= 0:
        return True
    grams = topic_grams(topic)
    if not grams:
        return True
    return sum(1 for gram in grams if gram in body) >= want


def parse_verify(reply: str, count: int) -> Optional[List[int]]:
    """读校验回话：返回"材料里真有依据"的编号（0 基）；读不懂返回 None（= 保持原样）。

    只认编号。回「没有」= 一条都不留；回了一堆别的字、一个数字都没有 = 它没照格式
    来——这时候**不能**把学到的全丢了（白核一轮总比把这一轮清零强），返回 None。
    """
    text = str(reply or "")
    if not text.strip():
        return None
    numbers = {int(n) for n in re.findall(r"\d+", text)}
    keep = sorted(n - 1 for n in numbers if 1 <= n <= max(0, int(count)))
    if keep:
        return keep
    if any(mark in text for mark in EMPTY_MARKS):
        return []
    return None


def usable_note(text: str, banned: Sequence[str] = ()) -> bool:
    """这条"知识点"能不能进记忆：够短、是中文、不是解说腔、不含书面腔、不是占位词。"""
    body = corpus._clip(text, 60)
    if len(body) < 6 or len(body) > 60:
        return False
    if body in PLACEHOLDERS:
        return False
    if not _CJK_RE.search(body):
        return False
    if humanstyle.looks_like_narration(body):
        return False
    if humanstyle.looks_like_title(body):
        # 抄来的标题（`《复仇者联盟3》剧情设定细节探案幕后解读`）不是知识点：
        # 写进记忆之后它迟早会把这条念给用户听，等于把"抓来的信息"又端上桌一次。
        return False
    return not any(word and word in body for word in banned)


def tidy(text: str) -> str:
    """剥掉模型爱加的装饰（`**` `#` 反引号 短横线 空白），只留字。"""
    return _DECOR_RE.sub("", (text or "").strip())


def looks_like_faq(ask: str, reply: str) -> bool:
    """这一对是不是"官网 FAQ 问答"而不是朋友闲聊（见 _FAQ_ASK_RE 那段注释）。"""
    return bool(_FAQ_ASK_RE.search(ask or "")) and bool(_FAQ_REPLY_RE.search(reply or ""))


def parse_reply(
    text: str, banned: Sequence[str] = ()
) -> Tuple[List[List[str]], List[str]]:
    """把模型按格式写回来的东西拆成「接话对」和「知识点」。

    模型不守规矩的花样（这些都是现场抓到的原话）：

    * 把内容写到**下一行**（`接话：` 后面空着，下一行才是话）；
    * 自己发明标签（`别人问：…` / `我接：…` 两行一组）；
    * 把 `接话` 加粗、后面带序号（`**接话**：`、`接话1：`）；
    * 一口气罗列十几条百科长句当"知识"。

    所以这里只认行首那几个词（接话 / 对话 / 知识）+ 上面那种自创标签，其余行一律丢掉
    ——"宁可这一轮少学两句，也不把解释说明塞进语料"；每一句还要过 corpus 那道
    "像不像人话"的闸，骨架占位词（上句 / 下句 / 一句话）一律不算回答（见 PLACEHOLDERS）。
    """
    rows: List[List[str]] = []
    notes: List[str] = []
    seen = set()
    pending = ""                      # 上一句"只有一问"的接话，等下一句配上

    def keep(ask: str, reply: str) -> None:
        """一句一问一答，过了所有闸才留下。"""
        ask, reply = corpus._clip(ask, 24), corpus._clip(reply, 24)
        if not ask or not reply or ask == reply:
            return
        if ask in PLACEHOLDERS or reply in PLACEHOLDERS:
            # 把骨架抄回来了（"接话：上句 ｜ 下句"）：它其实没回答，一句都不算
            return
        key = (corpus._key(ask), corpus._key(reply))
        if key in seen:
            return
        if looks_like_faq(ask, reply):
            # 官网 FAQ 那种"有什么 X？→ 有 A、B 等。"：不是闲聊，不进语料
            return
        if not _usable_line(ask, banned) or not _usable_line(reply, banned):
            return
        seen.add(key)
        rows.append([ask, reply])

    for raw_line in (text or "").replace("\r", "\n").split("\n"):
        line = tidy(raw_line)
        line = re.sub(r"^\d+[.、)]\s*", "", line).strip()   # 有序列表的 `1. `
        match = _LINE_RE.match(line)
        if not match:
            continue
        head = re.sub(r"\d+$", "", tidy(match.group("head")))
        body = tidy(match.group("body"))
        if not body:
            continue
        if head in NOTE_HEADS:
            note = corpus._clip(body, 60)
            if usable_note(note, banned):
                notes.append(note)
            continue
        if head in ROW_HEADS:
            parts = [part.strip() for part in _SPLIT_RE.split(body) if part.strip()]
            if len(parts) >= 2:
                keep(parts[0], parts[1])
            elif pending:              # 上一行是一问、这一行就是一答：配一对
                keep(pending, body)
                pending = ""
            else:                      # 只有这一句：先拿着，等下一行
                pending = body
            continue
        if _SELF_ASK_RE.search(head) and not _SELF_REPLY_RE.search(head):
            pending = body
        elif _SELF_REPLY_RE.search(head) and pending:
            keep(pending, body)
            pending = ""
    return rows, notes


def _usable_line(text: str, banned: Sequence[str] = ()) -> bool:
    """一句接话能不能进语料：跟屏幕上读来的台词用同一把尺子（corpus 那道闸）。

    外面再补一条自己的：**标题不是接话**——搜回来的页面标题（`《复仇者联盟3》剧情设定
    细节探案幕后解读`）偶尔会被它当成一问一答递上来，收进语料就等于学会了"念标题"。
    """
    if humanstyle.looks_like_title(text):
        return False
    return corpus.looks_like_speech(text, min_chars=2, max_chars=24, banned=banned)


class TopicLedger:
    """话题账本：谁学过、学成没学成（`study.ledger`，默认 `data/study.json`）。

    为什么要记这两样：

      · **学过的**：记忆里"老在看"的就那么几个，不给冷却它每轮都挑同一个话头，
        等于把接口钱花在重复劳动上（现场观察到连着几轮都在学同一支视频）；
      · **白卷的**：有的词搜回来全是同名杂志 / 公司官网，模型只能交白卷——这种话头
        该往后排，别一直啃。

    读坏了就当空账本：学习这条路不能被一个坏 json 堵死。
    """

    VERSION = 1
    MAX_TOPICS = 200            # 账本自己也会长胖：只留最近管过的这么多个

    def __init__(self, path, cooldown: float = 21600.0, fail_penalty: int = 3):
        self.path = Path(path)
        self.cooldown = max(0.0, float(cooldown))
        self.fail_penalty = max(1, int(fail_penalty))
        self.topics: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[study] 读话题账本 {self.path.name} 失败（当空账本）：{exc}")
            return
        rows = data.get("topics") if isinstance(data, dict) else None
        if not isinstance(rows, dict):
            return
        for name, item in rows.items():
            if isinstance(item, dict):
                row = {
                    "ts": float(item.get("ts") or 0.0),
                    "ok": int(item.get("ok") or 0),
                    "blank": int(item.get("blank") or 0),
                }
                if isinstance(item.get("src"), list):
                    row["src"] = [str(url) for url in item["src"][:3]]
                self.topics[str(name)] = row

    def save(self) -> None:
        if len(self.topics) > self.MAX_TOPICS:
            recent = sorted(
                self.topics.items(), key=lambda kv: float(kv[1].get("ts") or 0.0), reverse=True
            )
            self.topics = dict(recent[: self.MAX_TOPICS])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {"version": self.VERSION, "topics": self.topics},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[study] 写话题账本失败：{exc}")

    def cooldown_left(self, topic: str, now: Optional[float] = None) -> float:
        """这个话头还要冷多久才值得再学（0 = 现在就能学）。"""
        item = self.topics.get(str(topic))
        if not item or not self.cooldown:
            return 0.0
        now = time.time() if now is None else float(now)
        return max(0.0, self.cooldown - (now - float(item.get("ts") or 0.0)))

    def fails(self, topic: str) -> int:
        """这个话头白卷过几次。"""
        return int((self.topics.get(str(topic)) or {}).get("blank") or 0)

    def rank(self, topics: Sequence[str]) -> List[str]:
        """挑话头的顺序：刚学过的靠后、宽泛的靠后、老交白卷的排最后。

        稳定排序，同档的保持原顺序（"他正在看的那一支"永远优先于记忆里的老话题）。
        """
        return sorted(
            [str(topic) for topic in topics],
            key=lambda topic: (
                self.cooldown_left(topic) > 0.0,
                is_vague(topic),
                self.fails(topic) >= self.fail_penalty,
            ),
        )

    def record(self, topic: str, ok: bool, sources: Sequence[str] = ()) -> None:
        """一轮学完记一笔：学到东西标 ok，交白卷标 blank（顺手记下读过的页）。"""
        name = str(topic or "").strip()
        if not name:
            return
        item = self.topics.setdefault(name, {"ts": 0.0, "ok": 0, "blank": 0})
        key = "ok" if ok else "blank"
        item["ts"] = time.time()
        item[key] = int(item.get(key) or 0) + 1
        if sources:
            item["src"] = [str(url) for url in list(sources)[:3]]
        self.save()


# ---------------------------------------------------------------- 干活的那只手

class WebStudy:
    """隐身时的"上网补课"：挑话头 → 搜 → 读 → 让模型整 → 写进语料和记忆。

    这一层不碰 Qt、不碰窗口，全是网络 + 文件，所以既能被 worker 丢在后台线程里跑，
    也能在冒烟测试里单独叫起来（见 tools/smoke_test.py 的 test_webstudy）。
    """

    def __init__(self, cfg, memory=None, client=None, topics_from=None):
        self.cfg = cfg
        self.mem = getattr(cfg, "study", None) or StudyConfig()
        self._memory = memory
        # client 既可以直接给一个 VisionClient，也可以给一个"取客户端"的函数——
        # worker 换 Key 的时候会整个换掉客户端（见 reload_client），给函数才不会拿旧的
        self._client = client
        # 补课专用客户端（study.model / base_url / provider 填了才有）：懒建，见 _study_client
        self._own_client = None
        # 额外的话头来源（worker 会给"他最近看的这支视频 / 这个游戏"）：
        # 比"游戏""短视频"这种类别词搜出来的东西准得多（见 _study_topics）
        self._topics_from = topics_from
        self.learner = corpus.CorpusLearner(cfg)   # 借它的语料路径 / 去重 / 读坏保护
        self.ledger = TopicLedger(
            self._ledger_path(cfg),
            cooldown=float(getattr(self.mem, "topic_cooldown_sec", 21600.0) or 0.0),
            fail_penalty=int(getattr(self.mem, "fail_penalty", 3) or 3),
        )
        self._session = requests.Session()
        self._last_round = 0.0
        self._rounds: Deque[float] = deque(maxlen=64)
        self.rounds = 0
        self.learned_rows = 0
        self.learned_notes = 0
        self.studied: List[str] = []      # 学过的话头（近的在前）：下一轮换一个
        self.last: Optional[Lesson] = None

    def _ledger_path(self, cfg) -> Path:
        """账本放哪儿：跟 memory.json / taste.json 一个规矩（相对 config.json 所在目录）。"""
        raw = str(getattr(self.mem, "ledger", "") or "data/study.json")
        path = Path(raw)
        if not path.is_absolute():
            path = Path(cfg.config_path()).parent / path
        return path

    # ---------- 该不该学 ----------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.mem, "enabled", False))

    @property
    def provider(self) -> str:
        """补课实际用的 provider：study.provider 填了就听它的，没填跟主配置。"""
        return str(getattr(self.mem, "provider", "") or getattr(self.cfg, "provider", "")).strip()

    def study_cfg(self):
        """补课用的那份配置。

        study.model / base_url / provider 一个都没填 = 原配置（老行为）；填了就浅拷贝一份、
        改几个顶层字段——**只动副本**，屏幕那边照旧用视觉模型，两边互不影响。
        补课是纯文本活儿，用 glm-4-flash 这类文本模型比视觉模型准得多、也便宜。
        """
        model = str(getattr(self.mem, "model", "") or "").strip()
        base_url = str(getattr(self.mem, "base_url", "") or "").strip()
        provider = str(getattr(self.mem, "provider", "") or "").strip()
        if not (model or base_url or provider):
            return self.cfg
        clone = copy.copy(self.cfg)
        if provider:
            clone.provider = provider
        if model:
            clone.model = model
        if base_url:
            clone.base_url = base_url
        elif provider:
            clone.base_url = ""      # 换了 provider 就别再拿上一家的地址
        clone.apply_preset()         # 只写了 provider 的（地址 / 模型没写）按预设补齐
        return clone

    def _study_client(self, log: bool = False):
        """这次实际要用的客户端：配了补课专用模型就单开一份，否则用注入的那个。"""
        if self.study_cfg() is self.cfg:
            return self._client() if callable(self._client) else self._client
        if self._own_client is None:
            self._own_client = VisionClient(self.study_cfg())
            if log:
                print(
                    f"[study] 这一轮走补课专用模型 {self._own_client.cfg.model}"
                    f"（provider={self._own_client.cfg.provider}）"
                )
        return self._own_client

    @property
    def client(self):
        """现在能用的模型客户端（没配 Key / 是 mock 也照样返回，由调用方看 ready）。"""
        client = self._study_client()
        return client if getattr(client, "ready", False) else None

    def next_wait(self, now: Optional[float] = None) -> float:
        """还要等多久才该学下一轮（worker 拿它当睡觉时长，最长也就几十秒）。"""
        now = now if now is not None else time.monotonic()
        if not self._last_round:
            return 0.0
        gap = max(
            float(getattr(self.mem, "interval_sec", 1800.0)),
            float(getattr(self.mem, "min_gap_sec", 30.0)),
            1.0,
        )
        remain = gap - (now - self._last_round)
        per_hour = max(0, int(getattr(self.mem, "max_per_hour", 2)))
        window = [t for t in self._rounds if now - t < 3600.0]
        if per_hour and len(window) >= per_hour:
            # 这一小时学够了：等最早那一轮滑出窗口
            remain = max(remain, 3600.0 - (now - min(window)))
        return max(0.0, remain)

    def due(self, now: Optional[float] = None) -> bool:
        """现在该不该学一轮：开关、有客户端、间隔、每小时上限，全过了才学。"""
        if not self.enabled:
            return False
        if int(getattr(self.mem, "max_per_hour", 2)) <= 0:
            return False
        if self.client is None:
            return False
        return self.next_wait(now) <= 0.0

    # ---------- 学什么（话头从长期记忆里来） ----------

    def topics(self) -> List[str]:
        """这一轮学什么：固定清单 > 他最近在看的那一支 / 那个游戏 > 长期记忆里的常看话题。

        挑之前先过两道（都为了"学得准"）：

          · `refine_topic()`：标题里的「第 X 期」「完整版」「- 哔哩哔哩」这些噪声先剥掉
            ——原样拿去搜，搜回来的是同一支视频的播放页和切片，不是"这个话题本身"；
          · `ledger.rank()`：刚学过的往后放（别重复花接口钱）、宽泛的往后放、
            老交白卷的排最后。

        一条都挑不出来才用 DEFAULT_TOPICS（第一次跑起来时就是这种情况）。
        """
        limit = max(1, int(getattr(self.mem, "topic_limit", 2)))
        fixed = [
            str(topic).strip()
            for topic in (getattr(self.mem, "topics", None) or [])
            if str(topic).strip()
        ]
        if fixed:
            return self._order(fixed)[:limit]
        ordered = self._order(self._picks()) or self._order(list(DEFAULT_TOPICS))
        fresh = [topic for topic in ordered if topic not in self.studied]
        chosen = fresh or ordered
        return chosen[:limit]

    def _picks(self) -> List[str]:
        """原始候选话头（没精修、没排序）：他刚在看的那一支 > 长期记忆里的常看。"""
        limit = max(1, int(getattr(self.mem, "topic_limit", 2)))
        picks: List[str] = []
        if callable(self._topics_from):
            try:
                picks = [str(item).strip() for item in (self._topics_from() or []) if str(item).strip()]
            except Exception as exc:
                print(f"[study] 取「他最近在看什么」失败：{exc}")
        if self._memory is not None:
            try:
                picks += [topic.label for topic in self._memory.topics(limit=limit)]
                if not picks:
                    # 还没攒到"老在看"的程度：退一步，用常看的类型当话头
                    picks += [tag for tag, _ in self._memory.top_tags(limit)]
            except Exception as exc:
                print(f"[study] 翻记忆挑话头失败：{exc}")
        return list(dict.fromkeys(picks))

    def candidates(self) -> List[Tuple[str, str]]:
        """给人看的候选清单：`(原话头, 会被怎么处理)`——`python -m pet.webstudy --topics` 用。

        把"为什么挑中它 / 为什么绕开它"写清楚，不然"它怎么老学同一件事""怎么老不学"
        根本查不出来。
        """
        picks = self._picks() or list(DEFAULT_TOPICS)
        out: List[Tuple[str, str]] = []
        for raw in picks:
            clean = refine_topic(raw)
            if not clean:
                out.append((raw, "剥完没剩下东西 → 跳过"))
                continue
            notes: List[str] = []
            if clean != raw:
                notes.append(f"精修成「{clean}」")
            if is_vague(clean):
                notes.append("太宽泛，只有没得挑时才用")
            left = self.ledger.cooldown_left(clean)
            if left > 0:
                notes.append(f"刚学过，{left / 60:.0f} 分钟后再轮到")
            fails = self.ledger.fails(clean)
            if fails >= self.ledger.fail_penalty:
                notes.append(f"白卷过 {fails} 次，排最后")
            out.append((raw, "；".join(notes) or "直接用"))
        return out

    def _order(self, topics: Sequence[str]) -> List[str]:
        """精修 + 去重 + 按账本排序。

        精修之后全被否掉（话头全是"游戏"这种宽泛词）就退回原话头——排到最后也得有得学，
        不然第一次跑起来（记忆还空着）的宠物会一直闲着。
        """
        cleaned = [refine_topic(topic) for topic in topics]
        cleaned = [topic for topic in cleaned if topic and not is_vague(topic)]
        if not cleaned:
            cleaned = [str(topic).strip() for topic in topics if str(topic).strip()]
        return self.ledger.rank(list(dict.fromkeys(cleaned)))

    # ---------- 上网：搜 + 读 ----------

    def search(self, topic: str) -> List[str]:
        """搜一下这个话题，返回几页的网址（一家搜不到就换一家；都搜不到返回空表）。"""
        engine = str(getattr(self.mem, "engine", "auto") or "auto").strip().lower()
        if engine == "none":
            return []
        order = list(AUTO_ORDER) if engine == "auto" else [engine]
        for name in order:
            if name not in ENGINES:
                print(f"[study] 不认识的搜索引擎：{name}")
                continue
            url = ENGINES[name].format(q=urllib.parse.quote(topic))
            try:
                resp = self._get(url)
            except Exception as exc:
                print(f"[study] {name} 搜不动（{exc}），换一家")
                continue
            links = result_links(name, resp.text)
            if links:
                return links
            print(f"[study] {name} 没搜出结果（可能被挡了），换一家")
        return []

    def fetch(self, url: str, topic: str = "") -> str:
        """把一页读成纯文字；打不开 / 不像料就返回空串（少读一页不影响这一轮）。

        读回来还要过 `page_ok()`：太短、没中文、跟话头不沾边的页面不算料（见那里）。
        """
        limit = max(200, int(getattr(self.mem, "page_chars", 2400)))
        if self._blocked(url):
            print(f"[study] {url} 在 block_hosts 里，跳过")
            return ""
        try:
            resp = self._get(url)
        except Exception as exc:
            print(f"[study] 打不开 {url}：{exc}")
            return ""
        if resp.status_code != 200:
            print(f"[study] {url} 回了 {resp.status_code}，跳过")
            return ""
        text = strip_html(resp.text, limit)
        min_chars = int(getattr(self.mem, "min_page_chars", 300) or 0)
        min_match = int(getattr(self.mem, "min_page_match", 1) or 0)
        if not page_ok(text, topic, min_chars=min_chars, min_match=min_match):
            print(f"[study] {url} 不像料（正文 {len(text)} 字），不读它")
            return ""
        return text

    def _blocked(self, url: str) -> bool:
        """这一页在不在"别读"名单里（`study.block_hosts`，子串匹配）。"""
        extra = [str(host).strip().lower() for host in (getattr(self.mem, "block_hosts", None) or [])]
        low = str(url or "").lower()
        return any(host and host in low for host in extra)

    def _get(self, url: str):
        timeout = max(2.0, float(getattr(self.mem, "timeout", 12.0)))
        return self._session.get(
            url,
            timeout=timeout,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )

    # ---------- 让模型整成能用的东西 ----------

    def _ask(self, prompt: str, max_tokens: int = 600) -> str:
        """把一段提示词交给补课用的客户端：纯文本、不发图（vlm.summarize 那条路）。"""
        client = self._study_client()
        if client is None or not getattr(client, "ready", False):
            raise VlmError("还没配上能用的模型（没填 API Key，或者 provider 还是 mock）")
        return client.summarize(prompt, max_tokens=max_tokens)

    def ask_model(self, topic: str, material: str = "") -> str:
        """把材料（可能为空）和话头交给模型，让它按固定格式回话。"""
        if self.provider == "mock":
            return self._mock_reply(topic)
        self._study_client(log=True)      # 用了补课专用模型就在日志里说一声
        name = str(getattr(getattr(self.cfg, "persona", None), "name", "") or "桌宠")
        prompt = PROMPT.format(
            name=name,
            topic=topic,
            material=material or "（这次没搜到网页，就凭你知道的说）",
        )
        return self._ask(prompt, max_tokens=600)

    def verify(
        self,
        topic: str,
        material: str,
        rows: Sequence[Sequence[str]],
        notes: Sequence[str],
    ) -> Tuple[List[List[str]], List[str]]:
        """核一遍：这些候选里，哪些在材料里真有依据（`study.verify`）。

        小模型很爱"顺手"补一句自己知道的（"这体现了团队协作的重要性"），材料里根本
        没有——写进语料和记忆就是污染。这一道把候选连编号交给它，材料里找不到的丢掉。

        读不懂回话（`parse_verify` 返回 None）就**原样收下**：白核一轮，也强过把这一轮
        学到的全清零。
        """
        items: List[str] = []
        for ask, reply in rows:
            items.append(f"{len(items) + 1}. 接话：{ask} ｜ {reply}")
        for note in notes:
            items.append(f"{len(items) + 1}. 知识：{note}")
        if not items:
            return list(rows), list(notes)
        prompt = VERIFY_PROMPT.format(
            material=material, items="\n".join(items), empty=EMPTY_MARKS[0]
        )
        try:
            reply = self._ask(prompt, max_tokens=120)
        except Exception as exc:
            print(f"[study] 校验问不出去（照原样收下）：{exc}")
            return list(rows), list(notes)
        keep = parse_verify(reply, len(items))
        if keep is None:
            one_line = " ".join(str(reply or "").split())[:80]
            print(f"[study] 校验没照格式回（照原样收下）：{one_line!r}")
            return list(rows), list(notes)
        if not keep:
            print(f"[study] 校验说「{topic}」这些候选在材料里都没依据，一条都不收")
            return [], []
        row_count = len(rows)
        kept_rows = [list(rows[i]) for i in keep if i < row_count]
        kept_notes = [str(notes[i - row_count]) for i in keep if i >= row_count]
        dropped = len(rows) + len(notes) - len(kept_rows) - len(kept_notes)
        if dropped:
            print(f"[study] 校验挡掉 {dropped} 条材料里没依据的（省得它自己编）")
        return kept_rows, kept_notes

    # ---------- 学一轮 ----------

    def run_round(self) -> Optional[Lesson]:
        """学一轮：挑话头 → 搜 + 读 → 让模型整 → 写进语料和记忆。

        什么都没学到（搜不到、模型不守格式）就返回 None——它**不会为了"有产出"
        去编内容**：写进语料和记忆的每一句都过了三道闸。
        """
        if not self.enabled:
            return None
        topic = self.topics()[0]
        material, sources = "", []
        if str(getattr(self.mem, "engine", "auto") or "auto").strip().lower() != "none":
            want = max(1, int(getattr(self.mem, "results", 3)))
            for url in self.search(topic):
                text = self.fetch(url, topic)     # 这一页够长、有中文、跟话头沾边才读
                if not text:
                    continue
                sources.append(url)
                material += f"\n\n【{url}】\n{text}"
                if len(sources) >= want:
                    break
        if not material and not bool(getattr(self.mem, "model_fallback", True)):
            print(f"[study] 「{topic}」既搜不到也读不到，这一轮什么都不写")
            self.ledger.record(topic, False)
            return None

        try:
            reply = self.ask_model(topic, material)
        except VlmError as exc:
            print(f"[study] 这一轮问不出去：{exc}")
            self.ledger.record(topic, False)
            return None
        except Exception as exc:      # 网络 / 解析出问题不能把后台线程带走
            print(f"[study] 问模型时出了问题：{exc}")
            self.ledger.record(topic, False)
            return None

        rows, notes = parse_reply(reply, banned=humanstyle.banned())
        # 限量入账：模型经常一口气写十几组（现场抓到过 15 组 + 30 条），一轮学那么多
        # 只会把语料和记忆冲淡，宁缺毋滥——想多学就多学几轮（间隔由 study.interval_sec 管）
        rows = rows[: max(0, int(getattr(self.mem, "max_rows", 3)))]
        notes = notes[: max(0, int(getattr(self.mem, "max_notes", 2)))]
        # 有材料才核得动（没材料就是"凭它自己知道"，那没必要再问一遍）
        before = len(rows) + len(notes)
        if material and bool(getattr(self.mem, "verify", False)) and self.provider != "mock":
            rows, notes = self.verify(topic, material, rows, notes)
        lesson = Lesson(
            topic=topic,
            rows=rows,
            notes=notes,
            sources=sources,
            via="web" if material else "model",
            dropped=before - (len(rows) + len(notes)),
        )
        lesson.added_rows = self.save_rows(rows)
        lesson.added_notes = self.remember(topic, notes)
        self._last_round = time.monotonic()
        self._rounds.append(self._last_round)
        self.rounds += 1
        self.ledger.record(topic, bool(lesson.added_rows or lesson.added_notes), sources)
        self.learned_rows += lesson.added_rows
        self.learned_notes += lesson.added_notes
        self.studied = ([topic] + [t for t in self.studied if t != topic])[:12]
        self.last = lesson
        if lesson.added_rows or lesson.added_notes:
            print(f"[study] 隐身补课 {lesson.line}")
        elif SKIP_MARK in (reply or ""):
            print(f"[study] 「{topic}」这一轮材料对不上，它说跳过（一条都不写）")
        else:
            # 交白卷时留一行原话在日志里——不然"它到底回了什么"永远查不出来
            one_line = " ".join((reply or "").split())[:160]
            print(f"[study] 「{topic}」这一轮没解析出东西，白卷。它的原话：{one_line!r}")
        return lesson

    def save_rows(self, rows: Sequence[Sequence[str]]) -> int:
        """接话对写进 `data/chat_style.json` 的 dialogue——跟边看边学**同一个出口**。

        只动 dialogue 这一个键（patterns / openers / banned 那是人写的），写前先备份
        `.bak`，写完 `humanstyle.reload()`：回屏幕之后下一次说话就用上了，不用重启。
        去重和"语料读坏了就什么都不写"这两条，直接用 corpus.CorpusLearner 现成的尺子。
        """
        todo = [list(row) for row in rows if len(row) >= 2]
        if not todo:
            return 0
        data = self.learner._read_corpus()
        if data is None:
            return 0
        existing = [row for row in (data.get("dialogue") or []) if isinstance(row, list) and row]
        limit = max(1, int(getattr(self.mem, "max_dialogue", 60)))
        added = 0
        for row in todo:
            if len(existing) >= limit:
                break
            if self.learner._already_in(existing, row):
                continue
            existing.append(row)
            added += 1
        if not added:
            return 0
        data["dialogue"] = existing
        path = self.learner.corpus_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:
            print(f"[study] 写 {path.name} 失败：{exc}")
            return 0
        humanstyle.reload()
        print(f"[study] 语料多了 {added} 段（现在一共 {len(existing)} 段），不用重启就生效")
        return added

    def remember(self, topic: str, notes: Sequence[str]) -> int:
        """知识点写进长期记忆：`memory.add(..., dialog="learn")`。

        dialog="learn" 就是「学习」那一类（见 pet/dialog.py）：之后总结画像、翻话头、
        主动搭话都带着它。话头当 context 一起进去，顺手就能打上标签。

        同一句话不写第二遍（模型很爱把同一句抄十几遍，跨轮也会重复）：跟记忆里已有的
        条目比一遍，像到 0.8 就当"这条学过了"。
        """
        if self._memory is None or not notes:
            return 0
        known = [str(getattr(entry, "text", "")) for entry in getattr(self._memory, "entries", [])]
        added = 0
        for note in notes:
            if any(humanstyle.similarity(other, note) >= 0.8 for other in known if other):
                continue
            try:
                self._memory.add(note, context=topic, dialog="learn")
            except Exception as exc:
                print(f"[study] 写记忆失败：{exc}")
                continue
            known.append(note)
            added += 1
        if added:
            try:
                self._memory.save(force=True)
            except Exception as exc:
                print(f"[study] 存记忆失败：{exc}")
        return added

    # ---------- 给人看的 ----------

    def studied_topics(self) -> int:
        """账本里一共学过多话题（**跨会话**，重启不归零）。

        为什么要问账本而不是 `self.rounds`：`rounds` 是这次运行的计数，进程一重启就归零
        ——托盘上显示"已学 0 轮"，看着就像它什么都没干（现场就是这么被误会的）。
        """
        return len(getattr(self.ledger, "topics", {}) or {})

    def status(self) -> str:
        """托盘里那一行：开没开、补过几个话题的课。

        口径换成账本（跨会话）之后才说实话：数字只会往前走，不会因为重启归零。
        本次运行学了几轮 / 花了多少，属于后台账，见 app.study_detail_text()。
        """
        if not self.enabled:
            return "补课：关着（收进托盘就只歇着）"
        known = self.studied_topics()
        if not known and not self.rounds:
            return "补课：开着（还没挑到合适的题）"
        line = f"补课：开着（自己补过 {known} 个话题"
        if self.rounds:
            line += f"，这次 {self.rounds} 轮"
        line += "）"
        cooling = sum(1 for topic in self.ledger.topics if self.ledger.cooldown_left(topic) > 0)
        if cooling:
            line += f"｜{cooling} 个话题刚学过，过阵子再啃"
        return line

    def greeting(self) -> str:
        """回到屏幕上时它想说的那句；没学到东西就没话说（返回空串）。"""
        lesson = self.last
        if lesson is None or not (lesson.added_rows or lesson.added_notes):
            return ""
        if lesson.added_rows and lesson.added_notes:
            return f"我刚在后台查了查「{lesson.topic}」，学了几句，也记下两条"
        if lesson.added_rows:
            return f"你不在的时候我上网学了点「{lesson.topic}」"
        return f"我刚查了查「{lesson.topic}」，记下来一点"

    def _mock_reply(self, topic: str) -> str:
        """离线模式的假材料：不配 Key 也能把"隐身学习"这条链路整条跑通。

        从内置的真人对话样本里挑两段（就是 humanstyle 里那些），再补一条说明性的
        知识点——mock 本来就"不联网"，这里只是让链路有东西可走、能被测试。
        """
        pool = list(humanstyle.SAMPLES) or [("这你也信", "我还真信了")]
        lines = [f"接话：{row[0]} ｜ {row[1]}" for row in pool[:2] if len(row) >= 2]
        lines.append(f"知识：{topic}这个话头在离线模式下不会真的联网，配了 Key 它才会去查。")
        return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """不启动挂件，单独试一次"隐身学习"：

        python -m pet.webstudy            学一轮（尊重 config.json 里的开关和间隔）
        python -m pet.webstudy --force    不管开关 / 间隔 / 冷却，立刻学一轮
        python -m pet.webstudy --topics   只看看它打算学什么、为什么挑它（不联网、不花钱）

    跟 `python -m pet.corpus` 一个路子：这一层的东西要能单独看见，才能放心让它自己
    往语料和记忆里写。
    """
    from .config import Config
    from .memory import Memory
    from .vlm import VisionClient

    args = [str(arg) for arg in (sys.argv[1:] if argv is None else argv)]
    cfg = Config.load()
    memory = Memory(cfg)
    study = WebStudy(cfg, memory=memory, client=VisionClient(cfg))

    if "--topics" in args:
        for raw, note in study.candidates():
            print(f"话头：{raw}　→　{note}")
        print("这一轮会用：" + "、".join(study.topics()))
        return 0

    if "--force" in args:
        study.mem.enabled = True
        study._last_round = 0.0
        study._rounds.clear()
        study.ledger.cooldown = 0.0      # 手动试的时候别被"刚学过"挡住
        cfg.study.min_gap_sec = 0.0
    if not study.enabled:
        print("隐身学习在配置里关着（study.enabled = false）；想试一次就加 --force。")
        return 1

    print(f"话头：{study.topics()[0]}")
    lesson = study.run_round()
    print(study.status())
    if lesson is None:
        print("这一轮什么都没学到（搜不到 / 模型没按格式回）——没有内容能过那四道闸")
        return 1
    print(lesson.line)
    for row in lesson.rows:
        print(f"  接话：{row[0]}  →  {row[1]}")
    for note in lesson.notes:
        print(f"  知识：{note}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
