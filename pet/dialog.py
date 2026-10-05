"""对话类型：把每一句话**私下**分成「记录 / 分析 / 学习」，决定怎么记、怎么分析、怎么学。

这个分类**不上界面**——气泡上不画情绪小标签，聊天面板里也不写 [xx]，
它只在程序这一侧干活，三个类别各对应一件事：

    record  记录  他在陈述/交代事情（"我在看…"、"今天加班了"）
                  → 原样进长期记忆和看片笔记，回话时别复述他的原话
    analyze 分析  他在评价/吐槽/带情绪（"太离谱了"、"笑死"）
                  → 记情绪、进口味统计，回话时先跟着他的情绪走
    learn   学习  他在提问/碰到没见过的东西（"这是什么"、"为什么啊"）
                  → 先把知道的答清楚（不知道就直说），这句算"要补的功课"

判断只用本地关键词 + 情绪兜底（不联网、不花钱，和 mood.py 一个路子）；
拿不准就判成「记录」——记下来最安全，不会让它的说话方式跑偏。

运行时的用法：
    kind = dialog.classify(text, mood=comment.mood)     # 这句话是哪一类
    memory.add(..., dialog=kind)                        # 记录/分析落进记忆
    persona.chat_user_prompt(..., dialog_hint=dialog.hint(kind))   # 学习影响接话
"""
from __future__ import annotations

from typing import Mapping

from . import mood as mood_mod

RECORD = "record"
ANALYZE = "analyze"
LEARN = "learn"

#: 固定顺序（日志、统计都按这个顺序说，读起来稳定）
KINDS = (RECORD, ANALYZE, LEARN)

#: 内部代号 -> 中文名（**只在控制台/自检里出现，不进界面**）
LABELS = {RECORD: "记录", ANALYZE: "分析", LEARN: "学习"}

# 「学习」的信号：问句、求解释、说自己没懂
_LEARN_MARKS = (
    "？", "?", "为什么", "为啥", "咋", "怎么", "什么是", "是什么", "啥意思", "什么意思",
    "教我", "讲讲", "解释", "科普", "不懂", "没懂", "没看懂", "是多少", "几点", "在哪",
    "能不能", "可不可以", "有没有", "吗",
)

# 「分析」的信号：评价、情绪、吐槽
_ANALYZE_MARKS = (
    "太", "真", "好", "超", "巨", "绝了", "离谱", "无语", "笑死", "牛", "服了", "真香",
    "喜欢", "讨厌", "难看", "好看", "值", "亏", "上头", "无聊", "尴尬", "气死", "难受",
    "累", "开心", "爽", "烦", "厉害",
)

# 字面看不出来时，再看情绪（好奇心 = 想弄明白东西）
_LEARN_MOODS = frozenset({"curious"})
_ANALYZE_MOODS = frozenset({"happy", "excited", "speechless", "smirk"})

# 类型 -> 这一轮怎么接（进这一轮的对话提示词）
_HINTS = {
    RECORD: "他这句是在交代事情 / 说他在干嘛：记住就行，别复述他的原话，接一句你自己的反应。",
    ANALYZE: "他这句带情绪，是在评价 / 吐槽：先跟着他的情绪起哄或共鸣一句，别讲道理、别急着给方案。",
    LEARN: "他这句是在问、或者碰到不懂的东西：先把知道的直接答清楚（有细节就说细节），"
           "不知道就直说不知道、别编；答完可以顺口补一句你的看法。",
}

# 样本太少就先别下结论（免得刚认识就说"你最近老在问"）
MIX_MIN = 4

# 类型 -> 从它的分布里学到的一句话（进记忆上下文，给模型当背景）
_MIX_HINTS = {
    RECORD: "他最近的话多是交代事情：记住他说过的事，回话时可以接着提「你上次说过…」。",
    ANALYZE: "他最近的话里情绪多：先跟着他的情绪起哄或共鸣一句，别讲道理、别急着给方案。",
    LEARN: "他最近问得多：他要的是答案——先把知道的答清楚，不知道就说不知道，别编。",
}


def label(kind: str) -> str:
    """内部代号 -> 中文名（记录 / 分析 / 学习）。"""
    return LABELS.get(kind, LABELS[RECORD])


def classify(text: str, mood: str = "", kind: str = "") -> str:
    """一句话 → 记录 / 分析 / 学习；判不出来就记「记录」（最稳妥）。

    问句优先当「学习」：他要的是一个答案，不是一句附和。
    kind 是 Comment.kind（`chat` / `proactive:…`），只用来兜底。
    """
    body = (text or "").strip()
    if not body:
        return RECORD
    learn = sum(1 for mark in _LEARN_MARKS if mark in body)
    analyze = sum(1 for mark in _ANALYZE_MARKS if mark in body)
    if learn and learn >= analyze:
        return LEARN
    if analyze:
        return ANALYZE
    name = mood_mod.normalize(mood) if mood else ""
    if name in _LEARN_MOODS:
        return LEARN
    if name in _ANALYZE_MOODS or kind.startswith("proactive"):
        # 它自己找话说（主动搭话）：多半是在起哄/关心，算「分析」那一类
        return ANALYZE
    return RECORD


def hint(kind: str) -> str:
    """这一类该怎么接话（一句话，塞进这一轮的对话提示词）。"""
    return _HINTS.get(kind, "")


def tally(counts: Mapping[str, int]) -> str:
    """「记录 3 / 分析 5 / 学习 2」——控制台/自检看的统计行（不进界面）。"""
    return " / ".join(f"{label(k)} {int(counts.get(k, 0))}" for k in KINDS)


def mix_hint(counts: Mapping[str, int]) -> str:
    """从"最近的对话类型分布"里挑一句提醒；样本不够或类型很平均就不说。

    要**严格过半**才开口：五五开的时候说明他什么都聊，硬套一类反而更容易说错。
    """
    total = sum(int(counts.get(k, 0)) for k in KINDS)
    if total < MIX_MIN:
        return ""
    top = max(KINDS, key=lambda k: int(counts.get(k, 0)))
    if int(counts.get(top, 0)) / float(total) <= 0.5:
        return ""
    return _MIX_HINTS[top]
