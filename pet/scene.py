"""5W1H 场景解析：让模型先把"谁 / 做了什么 / 什么时候 / 在哪 / 为什么"看准，再开口。

模型每轮回答两行（格式要求见 persona.py）：

    [无语] 这操作我真看不懂
    场景：人物=黑衣男主播｜事件=连抽十次没出金色｜时间=深夜｜地点=抽卡直播间｜原因=想抽到当期角色

这里负责把第二行拆成 Scene：

* 缺项留空、写着「未知 / 不确定」的也算空——宁可空着，也不要编；
* 第一行仍然是「[情绪] 一句话」，原来的解析链路（mood.split_mood）不受影响；
* 模型偶尔把两行挤成一行、把场景写在前面、或写成 `人物: xxx`，都按同一套规则兜住；
  解析不出来只是拿到一个空 Scene，不会抛异常。

Scene 有两个用途：
    1. 进长期记忆（memory.add 的 scene 参数）——"他看的是什么内容"更准，打标签也更容易命中；
    2. 下一帧的提示词里回喂给模型（worker 的 _last_scene）——同一个场景别车轱辘话说三遍。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

# 五个维度 + 模型可能用的各种写法（长的排前面，先匹配"人物"再匹配"人"）
FIELDS: Tuple[str, ...] = ("who", "what", "when", "where", "why")

# 单字写法（人=/事=/地=）也认，但必须紧跟分隔符才算，且至少要凑够两个字段才当场景行，
# 所以"主持人：来来来"这种正常台词不会被误判成场景。
FIELD_LABELS: Dict[str, Tuple[str, ...]] = {
    "who": ("人物", "何人", "主角", "谁", "who", "人"),
    "what": ("事件", "何事", "发生了什么", "干什么", "事情", "动作", "what", "事"),
    "when": ("时间", "何时", "when", "时"),
    "where": ("地点", "何地", "场所", "位置", "where", "地"),
    "why": ("原因", "何因", "动机", "为什么", "why", "因"),
}

# 中文名字，给日志和记忆用
FIELD_NAMES: Dict[str, str] = {
    "who": "人物",
    "what": "事件",
    "when": "时间",
    "where": "地点",
    "why": "原因",
}

# 「人物」「事件」这几个词本身：模型偶尔把**格式骨架**照抄回来当内容
# （现场气泡：`这画面还挺酷的 / 场景：人物｜事件｜时间｜地点｜原因`）。
# 抄回来的那一刻它就是"没看出来"，绝不能当成一看就懂的 facts，更不能进气泡。
SKELETON_WORDS = frozenset(
    label.lower() for labels in FIELD_LABELS.values() for label in labels
) | frozenset(name.lower() for name in FIELD_NAMES.values())

# 五个字段名按顺序被「｜」串起来——模型抄骨架最典型的样子。
# 出现在一行中间，就说明**后半截是场景、不是台词**（这是它漏进气泡的那条路）。
_SKELETON_SEQ = re.compile("[｜|]\\s*".join(FIELD_NAMES[name] for name in FIELDS))

# 整行只是一个骨架（「人物｜事件｜时间｜地点｜原因」）：没有内容，但也不能被当台词念出来。
_SKELETON_ONLY = re.compile(
    r"^\s*(?:(?:场景|画面场景|5W1H|5W|环境|scene)\s*[:：]?\s*)?"
    + "(?:" + "|".join(FIELD_NAMES[name] for name in FIELDS) + ")"
    + r"\s*(?:[｜|]\s*(?:" + "|".join(FIELD_NAMES[name] for name in FIELDS) + r")\s*)+$",
    re.IGNORECASE,
)

# 台词和场景挤在一行时，中间那个分隔符要从台词尾巴上剥掉（`这画面还挺酷的 / 场景：…`）
_HEAD_TRIM = " \t/／\\｜|;；,，、。.·-—"

# 场景项里不该出现句读：一出现就说明这不是"短句"，而是台词的后半截
_SENTENCE_MARK = re.compile(r"[，,。！？!?；;…]")

FIELD_RULES: Dict[str, str] = {
    "who": "画面里是谁：人物 / 角色 / 主播 / 宠物，认不出就写「未知」",
    "what": "画面里正在发生什么：具体动作或事件，别写「在播视频」这种废话",
    "when": "什么时候：白天/深夜/第几局/画面上写着的日期，看不出来就写「未知」",
    "where": "在哪：直播间 / 游戏地图 / 教室 / 厨房 这类场景，看不出来就写「未知」",
    "why": "为什么：他这么做图什么，只能猜就写「未知」",
}

# 「场景：」「[场景]」「5W1H:」「环境:」「scene:」这些都算场景行的开头
_SCENE_MARK = re.compile(
    r"[\[【(（]?\s*(?:场景|画面场景|5W1H|5W|环境|scene)\s*[\]】)）]?\s*[:：=]?",
    re.IGNORECASE,
)

# 「【你刚才说过】」「【现在】」「【你现在】」这种是提示词里的块头——
# 模型偶尔会把它当台词一起抄回来。判定用通用规则：整行只是一个短括号短语就算块头
# （注意不含「」『』这种引号，那是模型给台词加的引号，要照常剥掉而不是丢掉整句）。
_HEADER_ONLY = re.compile(r"^\s*[【\[（(]\s*[^】\]）)]{1,16}\s*[】\]）)]\s*[:：]?[。.]?\s*$")

# 句首那个情绪标签（`[无语] …`）：它是留给 mood.split_mood 剥的，不是台词的一部分。
# 判断「这一行到底是不是场景行」时要先把它摘掉看一眼——现场那次漏进气泡的正是
# `[无语] 场景：`（情绪标签 + 一个空的场景行）：摘标签之前 `_SCENE_MARK` 在行首失配，
# 整行被当成台词念了出去，标点一清屏幕上就只剩「场景」俩字。
_LEAD_TAG = re.compile(r"^\s*[\[【(（]\s*[^\]】)）]{1,8}\s*[\]】)）]\s*")

# 单字段的场景片段（比如「时间=白天」）也得当成场景行，别当台词念出来。
# 只用两字以上的标签判——免得「人：来来来」这种正常台词被吞掉。
_STRONG_LABELS: Tuple[str, ...] = (
    "人物", "事件", "时间", "地点", "原因", "何人", "何事", "何时", "何地", "何因",
    "主角", "角色", "场所", "动机", "动作", "事情", "位置", "干什么", "发生了什么",
    "画面场景",
)
_STRONG_PAIR = re.compile(
    r"^\s*[【\[]?\s*(?:" + "|".join(_STRONG_LABELS) + r")\s*[】\]]?\s*[=:：]\s*\S"
)

# 分隔符：全角竖线、竖线、分号、逗号、顿号
_SEPARATORS = "｜|;；,，、"

# 每一项最多留这么多字，免得模型写小作文把提示词撑爆
MAX_VALUE_CHARS = 28
# 整个场景行最多这么长
MAX_LINE_CHARS = 200

_UNKNOWN_WORDS = frozenset(
    {
        "未知", "不详", "不明", "不清楚", "不确定", "看不出", "看不出来", "无法确定",
        "未知/不确定", "不确定/未知", "无", "没有", "暂无", "none", "n/a", "na", "null",
        "-", "--", "—", "?", "？", "未知。",
    }
)

# 去掉首尾的引号、书名号、括号、空白
_TRIM_CHARS = " \t\u3000\"'“”‘’「」『』《》()（）[]【】*`。.,，、:："


@dataclass(frozen=True)
class Scene:
    """一帧画面的 5W1H。五个字段都可能是空串（那就是"没看出来"）。"""

    who: str = ""
    what: str = ""
    when: str = ""
    where: str = ""
    why: str = ""

    @property
    def filled(self) -> int:
        return sum(1 for name in FIELDS if getattr(self, name))

    def is_empty(self) -> bool:
        return self.filled == 0

    def get(self, name: str) -> str:
        return getattr(self, name, "") if name in FIELDS else ""

    def line(self, sep: str = "｜") -> str:
        """拼成「人物=…｜事件=…」；空项不写。"""
        return sep.join(
            f"{FIELD_NAMES[name]}={getattr(self, name)}"
            for name in FIELDS
            if getattr(self, name)
        )

    def brief(self, limit: int = 60) -> str:
        """给日志/提示词用的一句话版本，超长截断。"""
        text = self.line(sep="；")
        return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"

    def what_or(self, fallback: str = "") -> str:
        return self.what or fallback


def _is_unknown(value: str) -> bool:
    text = (value or "").strip().strip(_TRIM_CHARS)
    if not text:
        return True
    if text.lower() in _UNKNOWN_WORDS:
        return True
    # 「人物」「事件」这种字段名：是模型抄回来的骨架，不是它看出来的内容
    return text.lower() in SKELETON_WORDS


def clean_value(value: str, limit: int = MAX_VALUE_CHARS) -> str:
    """洗掉引号/换行/尾部标点，太长就截断；「未知」这类统一变成空串。"""
    text = re.sub(r"\s+", " ", (value or "").replace("\r", " ").replace("\n", " ")).strip()
    text = text.strip(_TRIM_CHARS).strip()
    text = text.strip(_TRIM_CHARS)  # 去两遍：先去引号再去句号
    if _is_unknown(text):
        return ""
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _pair_pattern(label: str) -> re.Pattern:
    """匹配「人物=」「人物:」「人物：」这类键值。"""
    return re.compile(rf"{re.escape(label)}\s*[=:：]\s*", re.IGNORECASE)


def _label_hits(text: str) -> List[Tuple[int, int, str, str]]:
    """找出所有「标签+分隔符」的位置，返回 [(起点, 值起点, 字段, 标签)]，按位置排序。"""
    hits: List[Tuple[int, int, str, str]] = []
    used: List[Tuple[int, int]] = []
    # 先长后短，避免"人物"被"人"抢走一半
    pairs = sorted(
        ((label, name) for name, labels in FIELD_LABELS.items() for label in labels),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for label, name in pairs:
        for match in _pair_pattern(label).finditer(text):
            start, value_start = match.start(), match.end()
            if any(s <= start < e for s, e in used):
                continue
            used.append((start, value_start))
            hits.append((start, value_start, name, label))
    hits.sort(key=lambda item: item[0])
    return hits


def _split_items(body: str) -> List[str]:
    """把场景内容切成一项一项——只认「｜」（提示词要求的就是它）。

    特意**不**认逗号顿号：人说话是用逗号的，拿逗号切会把台词的后半句切进来。
    """
    return [part.strip() for part in re.split(r"[｜|]", body) if part.strip()]


def _as_items(body: str) -> List[str]:
    """一串短句能不能算「场景项」：两段以上、每段都是短句、且不带句读。"""
    parts = _split_items(body)
    if len(parts) < 2:
        return []
    if not all(len(part) <= MAX_VALUE_CHARS and not _SENTENCE_MARK.search(part) for part in parts):
        return []
    return parts


def looks_like_scene_body(text: str) -> bool:
    """「场景：」后面那一串，是不是真的场景内容（而不是台词的后半句）。

    两种写法都认：

    * 带标签的键值对——`人物=主播｜事件=抽卡`；
    * 按顺序摆的短句——`主播｜抽卡｜深夜`（提示词现在要求的就是这个写法）。

    为什么非要有这个东西：模型经常把两行**挤成一行**，写成
    `这画面还挺酷的 / 场景：人物｜事件｜时间｜地点｜原因`。
    老代码只认「人物=」这种写法，于是这一整串被当台词念进了气泡——
    屏幕上就出现了"五要素"本身，那是格式，不是它看懂了什么。
    """
    body = (text or "").strip()
    if not body or len(body) > MAX_LINE_CHARS:
        return False
    if _label_hits(body):
        return True
    return bool(_as_items(body))


def _parse_positional(body: str) -> Scene:
    """没有「人物=」这类标签时，按顺序把短句贴到五个字段上。

    提示词要的就是这个写法（用「｜」把 人物 / 事件 / 时间 / 地点 / 原因 隔开），
    所以解析必须跟它对齐：`主播｜抽卡｜深夜` → 人物=主播、事件=抽卡、时间=深夜。

    只有"两段以上、每段都是短句、且不超过五项"才按顺序拆；否则还是整句塞进 what
    （宁可少记一件事，也不要把台词后半截当成"他看懂了"）。
    """
    parts = _as_items(body)
    if not (2 <= len(parts) <= len(FIELDS)):
        return Scene(what=clean_value(body))
    values: Dict[str, str] = {name: "" for name in FIELDS}
    for name, part in zip(FIELDS, parts):
        values[name] = clean_value(part)
    return Scene(**values)


def parse_fields(text: str) -> Scene:
    """把一行场景文本拆成 Scene；拆不出字段就把整句塞进 what。"""
    body = _SCENE_MARK.sub("", (text or "").strip(), count=1).strip()[:MAX_LINE_CHARS]
    if not body:
        return Scene()

    hits = _label_hits(body)
    if not hits:
        return _parse_positional(body)

    values: Dict[str, str] = {name: "" for name in FIELDS}
    for index, (_, value_start, name, _) in enumerate(hits):
        # 这一项的值 = 从这里到下一个标签之前，再切掉尾巴上的分隔符
        end = hits[index + 1][0] if index + 1 < len(hits) else len(body)
        chunk = body[value_start:end].strip().strip(_SEPARATORS + " ").strip()
        if chunk and not values[name]:  # 同一个字段写了两遍，只认第一遍
            values[name] = clean_value(chunk)
    return Scene(**values)


def looks_like_scene(line: str) -> bool:
    """判断一行是不是"场景行"。

    * 以「场景：」这类标记开头的，一律算（哪怕格式没写全，也不能当台词念出来）；
    * 「时间=白天」这种单字段片段也算（模型偷懒时只填了一项）；
    * 整行只是「人物｜事件｜时间｜地点｜原因」这种骨架的，也算
      （它没有内容，但更不该被念出来——现场就是这个形状漏进了气泡）；
    * 否则至少出现两个 5W 字段才算——单个字段更像台词里的冒号，不当场景。
    """
    text = (line or "").strip()
    if not text:
        return False
    if _SCENE_MARK.match(text):
        return True
    if _STRONG_PAIR.match(text):
        return True
    if _SKELETON_ONLY.match(text):
        return True
    return len(_label_hits(text)) >= 2


# 「只有个标签、没有一句人话」的那些字样：整句就是它，那这一轮等于什么都没说。
# 现场气泡里出现过 `场景`（就是上面那个漏点留下的）和 `未知`（模型只填了个占位词）。
# 它们既不是吐槽也不是回答，摆在脸上最像是"它坏了"——宁可这一轮不说。
_LABEL_ONLY_WORDS = frozenset(
    {
        "场景", "画面场景", "5w1h", "5w", "scene",
        "人物", "事件", "时间", "地点", "原因",
        "who", "what", "when", "where", "why",
        "未知", "不详", "不明", "不确定",
    }
)
# 判定前先把标点、括号、空白摘掉：「（场景）」「场景：」「[场景]」都是同一个东西
_LABEL_TRIM = re.compile(r"[\s，,。.！!？?：:=｜|、；;（）()\[\]【】《》“”\"'…—\-]+")


def is_label_only(text: str) -> bool:
    """这一句是不是只有个标签 / 占位词（「场景」「未知」这种）——没有一句人话。

    两遍都认：**原文**（`[场景]` 这种整个就是标签）和**摘掉句首情绪标签之后**的
    （`[无语] 未知` 这种，标签只是碰巧也在）。
    """
    raw = (text or "").strip()
    if not raw:
        return False
    for line in (raw, _LEAD_TAG.sub("", raw, count=1)):
        key = _LABEL_TRIM.sub("", line).lower()
        if key and key in _LABEL_ONLY_WORDS:
            return True
    return False


def split(raw: str) -> Tuple[str, Scene]:
    """把模型输出拆成「台词, 场景」。

    * 台词只保留第一行有效内容（和原来 clean_reply 的口径一致）；
    * 场景行可以是第 1 行也可以是第 2 行，甚至和台词挤在同一行；
    * 一行里既有台词又带「场景：」时，从标记处切开（切完把台词尾巴上的
      「 / 」这类分隔符剥掉，别让气泡里留个孤零零的斜杠）；
    * 骨架（「人物｜事件｜时间｜地点｜原因」）直接丢掉：那是格式说明，不是内容。
    """
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    speech = ""
    scene_lines: List[str] = []

    for line in text.split("\n"):
        item = line.strip()
        if not item:
            continue
        if _HEADER_ONLY.match(item):   # 提示词块头被抄回来了：跳过，别当台词
            continue
        mark = _SCENE_MARK.search(item)
        if mark and mark.start() > 0 and looks_like_scene_body(item[mark.start():]):
            head = _LEAD_TAG.sub("", item[: mark.start()].strip().strip(_HEAD_TRIM), count=1).strip()
            if head and not speech:
                speech = head
            scene_lines.append(item[mark.start():])
            continue
        # 台词后面直接粘着一串场景项、连「场景：」都没写：
        # `这画面还挺酷的 / 人物｜事件｜时间｜地点｜原因`——从骨架开头处切开。
        seq = _SKELETON_SEQ.search(item)
        if seq and seq.start() > 0:
            head = _LEAD_TAG.sub("", item[: seq.start()].strip().strip(_HEAD_TRIM), count=1).strip()
            if head and not speech:
                speech = head
            continue
        if looks_like_scene(item):
            scene_lines.append(item)
            continue
        # 句首的情绪标签会把场景标记挡住：`[无语] 场景：`——摘掉标签再认一次。
        # 剩下从「场景：」开头的，就是模型把情绪标签和（空的）场景行写在了同一行，
        # 这行是场景行不是台词（不这么认的话，标点一清气泡里就只剩「场景」俩字）。
        tag = _LEAD_TAG.match(item)
        if tag:
            probe = item[tag.end():].strip()
            if probe and _SCENE_MARK.match(probe) and looks_like_scene(probe):
                scene_lines.append(probe)
                continue
        if not speech:
            speech = item

    scene = parse_fields(" ".join(scene_lines)) if scene_lines else Scene()
    return speech, scene


def describe(value: str) -> str:
    """给日志用：空场景也写清楚，方便一眼看出"这轮到底看懂了什么"。"""
    return (value or "").strip() or "（没看出来）"


def all_rules() -> Sequence[str]:
    """按 FIELDS 顺序给出五项要求，拼提示词用。"""
    return [FIELD_RULES[name] for name in FIELDS]
