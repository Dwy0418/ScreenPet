"""真人聊天的手感：把「人和人到底怎么聊」变成提示词能用的东西。

三块内容：
    PATTERNS  真人聊天的**具体做法**（半句、改口、自曝、接梗、抬杠、算账……）。
              每次随机挑一条塞进提示词，它就不会永远是同一种口气。
    SAMPLES   一小段一小段的真人式对话（短句、语气词、会跑题、会自己接自己）。
              只给模型「学味道」，提示词里明确写了不许照抄。
    OPENERS   语气词 / 起手词池，随手撒一点，句子就不像文案了。

外加一个相似度函数：用它拦住「换汤不换药」的车轱辘话
（memory.json 里同一句话刷十来条，就是模型一直在重复，没人拦它）。

语料想加自己的，就编辑 `data/chat_style.json`——里面的条目会**叠加**到内置语料上，
所以你可以把自己的微信/QQ 聊天记录整段贴进去，它会照着你的说话方式学。

PATTERNS / SAMPLES 里有一部分是照真实微信聊天记录拆出来的习惯（接最后一句、反问求证、
共鸣换伤、承认不知道、一句话收尾……），所以口气更接近"熟人"而不是"解说"。
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
USER_CORPUS = ROOT / "data" / "chat_style.json"

# ---------------------------------------------------------------- 真人聊天的做法

# (名字, 做法说明, 例子)
PATTERNS: Tuple[Tuple[str, str, str], ...] = (
    ("只给反应", "先甩一个反应词，别的都省了", "「嚯」「不是吧」「我服」「可以可以」"),
    ("半句", "话说到一半就停，剩下的让对方自己补", "「就那个……你懂我意思吧」"),
    ("改口", "先说完，再自己补一句改口", "「他这也太……不是，我说的是那个蓝衣服的」"),
    ("抬杠", "先不信，怀疑对方在演/在吹", "「你确定？我咋觉得是剧本」"),
    ("自曝", "扯一件自己的事，哪怕只沾一点边", "「我上次也这样，结果被我姐笑了半天」"),
    ("算账", "把数字往大了夸张地算", "「你这俩小时刷了多少条啊我数不过来」"),
    ("跑题", "顺着一个词跳到完全不相干的地方", "「说到这个，我冰箱里还有半个西瓜」"),
    ("阴阳", "嘴上夸，实际在损", "「行行行，你最懂了」"),
    ("打赌", "猜接下来会怎样，堵一句", "「我赌三步之内他必翻车」"),
    ("催", "嫌对方磨叽，或者催下文", "「你倒是说啊」「下一条下一条」"),
    ("复读加料", "把画面里的词原样念一遍再加自己的", "「稳了？我看是稳稳地翻」"),
    ("盯细节", "不看主角，盯着角落里的小东西", "「后面那只猫全程在扒拉他键盘」"),
    ("学人说话", "直接学画面里的人说话，阴阳怪气那种", "「来来来，'我这不是菜，是稳'」"),
    ("拉回现实", "忽然提醒对方现实里的事", "「你明天不上班啊」「水杯空了，去接点水」"),
    ("只丢一个词", "整句就是一个词，靠语气撑", "「离谱」「好家伙」「绝了」"),
    # —— 下面这些是从真人聊天记录里拆出来的习惯（朋友之间怎么接话）——
    ("接最后一句", "只抓对方最后那句话里的一个词来接，别从他上一句复述起", "「固定出装？」那还不等于没推荐"),
    ("反问求证", "用问句接话，不用结论接；问完就停，别自己答满", "「这玩意真有效果吗」「你还记得你吃过吗」"),
    ("拆术语", "把对方嘴里的术语原样拎出来问是啥意思", "「通用大模型打底是啥意思」「调用模型又是啥意思」"),
    ("共鸣换伤", "对方说难受，你也说一件自己的，别只说「加油」", "「我屁股打针打肿了 也痛了两星期」"),
    ("安慰带办法", "安慰一句 + 顺手给个能落地的小办法，别只喊挺住", "「救救吧」「也可能天气凉快了就好了」"),
    ("拿自己举例", "拿自己做过的事当例子讲，讲完自曝一下时效", "「我们当时就是拿历史数据人工分类」「都是十年前的事了」"),
    ("承认不知道", "不懂就说不懂，别硬编圆回来", "「不知道现在怎么玩了」「我也没试过」"),
    ("顺手换台", "话说到一半自己岔开，或突然想起别的", "「对了」「而且我奶奶他们还要回湖南的」"),
    ("短句连发", "一句话拆成两三条发，想到哪发到哪", "「是啊」「这也是要解决的问题」"),
    ("同意再补", "先「可以啊」，再补一句自己的顾虑或条件", "「可以先做吧 后面再想怎么丰富」"),
    ("一句话收尾", "用一句很轻的话结束，不总结陈词", "「总会好的」「那就先这样吧」"),
    ("报生活流水", "用自己的小事打断一下正事，别一直聊正题", "「刚刚睡着了」「我今天专门去医院开药」"),
    ("夸张量词", "把程度往大了说，但不说脏话", "「吃了一箱子的药」「喝到ptsd了」"),
    ("不客气", "不说「请」「您好」「感谢」这类客套词，直接来", "「发我看看」「你截图给我看」"),
    ("追一个细节", "顺着对方说的东西追问一个很具体的小细节", "「哪个 你截图给我看看」"),
    # —— 下面这些是"别像机器"（现场反馈：逻辑太严密、按部就班、没有生气）——
    ("想一出是一出", "话说到一半自己拐弯，想到别的就说别的，前后不搭也没关系", "「他那球……诶你午饭吃了吗」"),
    ("不接逻辑", "别用「因为所以」把话说圆，直接甩反应", "「为啥？没为啥，就是离谱」"),
    ("顺嘴联想", "被画面里一个小细节勾走，说件不太相干的事", "「这衣服我好像也有一件」"),
    ("半路放弃", "话起个头自己又算了，不解释也不收尾", "「我说这个……算了不说了」"),
)

# 一小段一小段真人式对话（只取味道，不照抄）
SAMPLES: Tuple[Tuple[str, ...], ...] = (
    ("刚刷到个离谱的", "多离谱", "主播自己把键盘砸了", "啊？为啥啊", "不知道 反正我看着挺爽"),
    ("你还没睡啊", "快了快了", "你上次也这么说", "……行吧我这就关"),
    ("这条我刷到过", "好看吗", "就那样 主要是那bgm上头", "发我"),
    ("你说他这算赢了吗", "算吧？", "我怎么觉得裁判都要笑场了", "那裁判也听不懂他说的啥"),
    ("我不行了 笑死", "笑啥", "你看那狗 一直在旁边扒拉他", "哈哈哈哈哈我看到了"),
    ("这集我看过 后面他翻车了", "你别剧透啊", "好好好我不说了", "……那你倒是说清楚点啊"),
    ("这谁啊", "不知道 脸挺熟", "是不是那个演过啥的", "算了反正也不重要"),
    ("你看他手", "咋了", "一直在抖", "紧张呗 底下那么多人"),
    # 下面这些是照真人聊天记录改写的（微信那种短句、追问、换伤、收尾）
    ("我看这推荐不对劲", "咋不对", "它只看我玩啥 不看对面阵容", "那不就白推荐", "就是说啊"),
    ("这个真有效果吗", "谁知道呢", "我妈见人就推荐", "哈哈那你妈赚了"),
    ("你还记得你吃过吗", "记得啊", "那你说说啥味", "……反正不好喝"),
    ("今天又去医院了", "咋了", "老毛病 打针打肿了", "太难了", "没事 慢慢会好的"),
    ("喝中药喝到ptsd了", "我懂", "去年喝了仨月", "呕 光听就难受", "不讲了 讲多了要吐"),
    ("那个弄得咋样了", "刚做第一步", "慢慢来 先跑起来再说", "嗯 先这样"),
    ("我刚刚睡着了", "我就说怎么没动静", "哈哈 实在撑不住", "早点睡吧"),
    ("这词啥意思啊", "你说哪个", "通用大模型打底是啥意思", "就是拿现成模型改 不用从零练"),
    ("你说这算bug吗", "算吧", "那我去反馈一下", "记得截图"),
    ("国庆还得加班", "太惨了", "同事全休假了 我被套住了", "哈哈 我在家躺着"),
)

# 起手词 / 语气词 / 收尾词：撒在句子里，书面腔立刻就散了
OPENERS: Tuple[str, ...] = (
    "诶", "哎", "嚯", "哟", "我天", "不是", "不是吧", "我说", "你看", "你说",
    "好家伙", "我跟你说", "讲真", "等等", "哎我说", "好嘛", "结果", "然后",
    "是啊", "对", "或者", "不过", "而且", "对了", "当时", "那我", "说起来",
)
ENDERS: Tuple[str, ...] = ("吧", "呀", "呢", "啊", "哈", "呗", "嘛", "了", "哒", "喏", "咯", "啦", "咧", "嗷", "喔")
FILLERS: Tuple[str, ...] = (
    "就那个", "你懂吧", "怎么说呢", "反正", "说实话", "不是我说", "我寻思",
    "怎么说", "大概", "好像", "估计", "我咋觉得", "其实", "然后呢", "反正你知道",
    "我是觉得", "差不多",
)

# 文案腔 / 总结腔：出现就算说错（persona 里也会再念一遍）
BANNED: Tuple[str, ...] = (
    "值得一提", "值得注意的是", "总的来说", "综上所述", "整体来看", "由此可见",
    "不得不说", "令人", "展现了", "呈现出", "营造出", "氛围感", "仪式感",
    "堪称", "极其", "十分", "非常", "换言之", "与此同时",
    "首先", "其次", "综上", "从某种意义", "在这里", "我们能看到",
    "这意味着", "这体现了", "这就说明", "干货", "复盘", "赛道", "赋能", "闭环",
    # 客服腔 / 寒暄腔：熟人之间不会这么写
    "为您服务", "很高兴为您服务", "请问", "亲亲", "感谢您", "请您", "如有需要",
    "请注意", "建议您", "希望对你有帮助", "以上就是", "致力于", "解决您的", "满足您的",
)

# 「说人话」兜底表补充：模型偶尔还是蹦书面词，这里再过一道手
EXTRA_FORMAL: Tuple[Tuple[str, str], ...] = (
    ("总而言之", "反正"), ("综上所述", "反正"), ("总的来说", "反正"), ("整体来看", "反正"),
    ("由此可见", "看来"), ("值得注意的是", ""), ("值得一提", ""), ("不得不说", ""),
    ("堪称", "真是"), ("极其", "超"), ("十分", "挺"), ("非常", "挺"),
    ("换言之", "就是说"), ("换句话说", "就是说"), ("从某种意义来说", ""), ("从某种意义上", ""),
    ("在此过程中", ""), ("与此同时", "同时"), ("这体现了", "这说明"), ("这意味着", "就是说"),
    ("可以说", "算得"), ("众所周知", "谁都知道"), ("个人认为", "我看"),
)


# ---------------------------------------------------------------- 读用户自己的语料

def _clean_rows(rows) -> Tuple[Tuple[str, ...], ...]:
    """把 JSON 里的对话样本整理成「一组几行」的样子。"""
    out: List[Tuple[str, ...]] = []
    for row in rows or ():
        if isinstance(row, str):
            lines = [part.strip() for part in re.split(r"\s*[|/]\s*|\n", row) if part.strip()]
        elif isinstance(row, (list, tuple)):
            lines = [str(part).strip() for part in row if str(part).strip()]
        else:
            continue
        if len(lines) >= 2:
            out.append(tuple(lines))
    return tuple(out)


def _clean_patterns(rows) -> Tuple[Tuple[str, str, str], ...]:
    out: List[Tuple[str, str, str]] = []
    for row in rows or ():
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            name, desc = str(row[0]).strip(), str(row[1]).strip()
            example = str(row[2]).strip() if len(row) > 2 else ""
            if name and desc:
                out.append((name, desc, example))
        elif isinstance(row, dict):
            name = str(row.get("name") or "").strip()
            desc = str(row.get("desc") or row.get("how") or "").strip()
            example = str(row.get("example") or "").strip()
            if name and desc:
                out.append((name, desc, example))
    return tuple(out)


def _clean_words(values, max_len: int = 16) -> Tuple[str, ...]:
    out: List[str] = []
    for value in values or ():
        text = str(value).strip()
        if text and len(text) <= max_len and text not in out:
            out.append(text)
    return tuple(out)


_corpus_cache: Optional[Dict[str, tuple]] = None


def user_corpus() -> Dict[str, tuple]:
    """读 `data/chat_style.json`（没有 / 写坏了就当空的，绝不因为语料文件起不来）。"""
    global _corpus_cache
    if _corpus_cache is not None:
        return _corpus_cache
    data: Dict[str, object] = {}
    try:
        if USER_CORPUS.exists():
            loaded = json.loads(USER_CORPUS.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except Exception as exc:
        print(f"[humanstyle] 读 {USER_CORPUS.name} 失败，只用内置语料：{exc}")

    _corpus_cache = {
        "dialogue": _clean_rows(data.get("dialogue")),
        "patterns": _clean_patterns(data.get("patterns")),
        "openers": _clean_words(data.get("openers"), max_len=8),
        "enders": _clean_words(data.get("enders"), max_len=4),
        "fillers": _clean_words(data.get("fillers")),
        "banned": _clean_words(data.get("banned")),
    }
    return _corpus_cache


def reload() -> None:
    """让下一次 user_corpus() 重新读一遍文件。

    语料是启动时读一次就缓存住的（拼提示词每次都要用，不能每次读盘）。
    但 corpus.py 那边"边看边学"会把新语料写进 chat_style.json，写完就得立刻生效——
    调这个把缓存清掉就行，不用重启挂件。
    """
    global _corpus_cache
    _corpus_cache = None


def patterns() -> Tuple[Tuple[str, str, str], ...]:
    return PATTERNS + user_corpus()["patterns"]


def samples() -> Tuple[Tuple[str, ...], ...]:
    return SAMPLES + user_corpus()["dialogue"]


def openers() -> Tuple[str, ...]:
    return OPENERS + user_corpus()["openers"]


def enders() -> Tuple[str, ...]:
    return ENDERS + user_corpus()["enders"]


def fillers() -> Tuple[str, ...]:
    return FILLERS + user_corpus()["fillers"]


def banned() -> Tuple[str, ...]:
    return BANNED + user_corpus()["banned"]


# ---------------------------------------------------------------- 每次随机换花样

def pick_angle(rng: Optional[random.Random] = None) -> Tuple[str, str, str]:
    """随机挑一个「这次从哪个角度说」——同一帧画面换个角度，话就不一样。"""
    return (rng or random).choice(patterns())


def sample_dialogue(count: int = 2, rng: Optional[random.Random] = None) -> Tuple[Tuple[str, ...], ...]:
    """随机抓一两段真人式对话当例子（只给模型学味道）。"""
    pool = list(samples())
    if not pool:
        return ()
    picker = rng or random
    count = max(1, min(int(count), len(pool)))
    return tuple(picker.sample(pool, count))


def sprinkle(rng: Optional[random.Random] = None) -> str:
    """给提示词用的语气词串：起手词 + 填充词 + 收尾词各抽几个。"""
    picker = rng or random
    parts = [
        "、".join(picker.sample(list(openers()), min(6, len(openers())))),
        "、".join(picker.sample(list(fillers()), min(4, len(fillers())))),
        "、".join(picker.sample(list(enders()), min(5, len(enders())))),
    ]
    return "，".join(part for part in parts if part)


def _format_samples(picked) -> List[str]:
    lines: List[str] = []
    for group in picked:
        for text in group:
            lines.append(f"  {text}")
        lines.append("  ——")
    if lines:
        lines.pop()
    return lines


def style_block(rng: Optional[random.Random] = None, chat: bool = False) -> str:
    """「像人一样说话」那一段提示词。

    chat=True 是用户直接跟它说话那一轮（重点是接话茬），
    否则是陪看吐槽那一轮（重点是冒出一句反应）。
    """
    angle_name, angle_desc, angle_example = pick_angle(rng)
    picked = sample_dialogue(2, rng)
    head = (
        "朋友之间是怎么聊天的（照这个节奏来，别写成文案或评论）：\n"
        if chat
        else "真人看视频随口说话是什么样（照这个来，别写成解说词）：\n"
    )
    body = [
        "· 短句连着来，一句一个点；允许只说半句、允许只蹦一个词。\n"
        "· 情绪先出来，事后再补：先「我服了」再说什么事，别倒过来。\n"
        "· 别把话说得太严密：不要「因为…所以…」「先…再…」「虽然…但是…」地把因果铺满，"
        "也别按「先讲看到什么、再讲我什么感受」的顺序来——那是最像机器的写法。\n"
        "· 允许想一出是一出：刚说一件事，突然想到别的就拐过去；拐不回来、说一半算了，都行。\n"
        "· 允许改口、允许自说自话、允许突然想到别的——真人不会每句都接得很完整。\n"
        "· 微信里没人打句号，也别用分号，用逗号、空格和语气词断句。\n"
        "· 一句话拆成两条发也行（「是啊」「这也正常」），不用硬凑成一条长的。\n"
        "· 没把握就直说「不知道」「忘了」「我也没试过」，不用圆场，更别编。\n"
        "· 每次挑一个角度说，这次就照这个角度："
        f"【{angle_name}】{angle_desc}（比如 {angle_example}）\n"
        f"· 手上可以用的语气词：{sprinkle(rng)}\n"
    ]
    if picked:
        body.append("· 真人大概是这么聊的（只学味道，一个字都别抄）：\n")
        body.extend(_format_samples(picked))
        body.append("")
    return head + "".join(body)


def chat_block(rng: Optional[random.Random] = None) -> str:
    """用户说话那一轮的「接话茬」提示词。"""
    return (
        "接话的节奏（你俩是通过一个输入框聊天，不是在念稿）：\n"
        "· 先接住他最后那句话的重点，再补你自己的；别从头把他的话复述一遍。\n"
        "· 允许「嗯」「啊」「行」这种短回应开头，但别句句都这样。\n"
        "· 他会用错字、会半句、会突然换话题——你顺着他就好，别纠正他。\n"
        "· 也可以在结尾丢一个小钩子（反问他一句、或者留一半没说完），让他能接着回。\n"
        "· 他问什么就先答什么：能答「嗯」「可以」「不知道」就别展开，要展开也别超过两句。\n"
        "· 答不上来、或者没听明白，就直接说（「你说哪个」「这词啥意思」）——真人就是这么接的。\n"
        "· 别列一二三、别给方案清单、别总结他的话；你们是在聊天，不是在交作业。\n"
        "· 不用「亲」「您好」「请问」「感谢」「收到」这种词，你俩是熟人。\n"
        + style_block(rng, chat=True)
    )


# 群聊小助手式的短反应：话不够、但场子不能冷的时候顶上（"就这？""哈哈"）
ACKS: Tuple[str, ...] = (
    "就这？", "哈哈", "啊这", "我服了", "好家伙", "绷不住了", "不是吧", "行吧",
    "有点东西", "笑死", "稳住", "哟", "可以可以", "这就有点意思了",
)


def acks() -> List[str]:
    """短反应池（顺序固定，调用方自己挑那句没说过的）。"""
    return list(ACKS)


def looks_like_echo(text: str, sources: Sequence[str], min_chars: int = 8) -> bool:
    """这句话是不是把提示词里的某一行原样抄回来了。

    小模型（比如 glm-4v-flash）偶尔会把提示词里的说明句、块头、记忆行当成台词返回，
    你看到的就是「如果画面基本还是同一件事，就别重复刚才的说法」这种莫名其妙的句子——
    在屏幕上看着就像它突然不会说话了。

    判断依据很简单：把候选台词和刚发出去的那几段提示词逐行比，只要它是某一行的一部分，就丢掉。
    只比长度够的行（默认 8 字以上），免得误伤正常短句。
    """
    line = (text or "").strip()
    if len(line) < min_chars:
        return False
    key = normalize(line)
    if not key:
        return False
    for source in sources or ():
        for raw in (source or "").split("\n"):
            other = normalize(raw)
            if len(other) < min_chars:
                continue
            if key in other or other in key:
                return True
    return False


# ---------------------------------------------------------------- 他在诉苦吗

# 「需要安慰」的信号词：他跟你说话时命中了这些，这一轮就别接梗、别讲道理，
# 先接住情绪再抱抱他（见 persona._COMFORT_TAIL）。
_COMFORT_HINTS: Tuple[str, ...] = (
    "难受", "好累", "累了", "累死", "累坏了", "撑不住", "撑不下去", "顶不住",
    "郁闷", "烦死", "烦躁", "好烦", "心里堵", "堵得慌", "憋屈", "委屈",
    "想哭", "哭了", "难过", "不开心", "没意思", "崩溃", "熬不住", "压力好大", "压力大",
    "焦虑", "睡不着", "失眠", "头疼", "头痛", "胃疼", "生病", "发烧", "被骂", "挨骂",
    "又加班", "加班到", "搞砸", "失败了", "被裁", "分手", "吵架", "倒霉", "生气",
    "气死", "想骂人", "不想干", "不想上班", "不想活了", "呜呜", "555", "唉",
)


def needs_comfort(text: str) -> bool:
    """他这句话是不是在诉苦 / 想让人哄一句。

    判据故意做得**很糙**（几个词命中就算）：这一条只决定"这一轮要不要专心安慰他"，
    判宽了顶多多哄他一句（没坏处），判窄了才是真难受——所以宁可松一点。
    """
    line = normalize(text or "")
    if not line:
        return False
    return any(hint in line for hint in _COMFORT_HINTS)


# ---------------------------------------------------------------- 内部词汇，别念给用户听

# 提示词里的块头 / 我们抓来的资料标题：这些只该出现在**我们发给模型的上下文**里，
# 绝不该出现在气泡里。现场抓到过一次，气泡里显示的是：
#     画面关键信息：画面关键信息：画面关键信息：/ 哇，这倒计时器，节
# ——模型把 keyinfo 那一段的块头念了出来。它只有 6 个字，短于 looks_like_echo 的 8 字门槛，
# 所以从旧那道闸里漏过去了。这里补一道：**剥掉开头的块头；还夹着内部字眼的，整句不要**。
INTERNAL_MARKERS: Tuple[str, ...] = (
    "画面关键信息",            # keyinfo 那一段的块头（现场漏的就是这个）
    "你记得的事", "你刚才说过", "你最近说过的", "你俩刚聊过",
    "你摸清的他的口味", "你刚看完的视频", "你上一支看过的视频",
    "你上一遍记下来的", "看片记录", "用户跟你说",
    "现在轮到你", "他刚动手了", "刚刷到一支新的",
    "这一集讲的是什么",         # episode.py 那张"整集资料卡"的块头
    # 「他这支片子看到哪了」那一块（worker._viewing_block，跟着进度看的）
    "他这支片子看到哪了", "一路看过来读到的",
)

_INTERNAL_ANY = re.compile("|".join(re.escape(word) for word in INTERNAL_MARKERS))
# 只用于"剥掉开头的块头"：这些词太常见，**不能**拿来判定"整句夹带内部信息"
# （不然你问一句「现在几点了」都会被丢掉）。它们只以【现在】这种带括号的形式出现。
INTERNAL_PREFIX_ONLY: Tuple[str, ...] = ("现在", "上一眼", "你上一眼")
_PREFIX_WORDS = "|".join(re.escape(word) for word in INTERNAL_MARKERS + INTERNAL_PREFIX_ONLY)
_STRONG_WORDS = "|".join(re.escape(word) for word in INTERNAL_MARKERS)
# 开头那串块头，两种形式：
#   ① 带括号的：`【现在】`「你记得的事」——括号里的词可以很普通；
#   ② 不带括号但带冒号的：`画面关键信息：`——只认那几个专属词，免得误伤正常台词。
_INTERNAL_PREFIX = re.compile(
    r"^\s*(?:"
    r"[【\[（(]\s*(?:" + _PREFIX_WORDS + r")\s*[】\]）)]\s*[:：,，、\-—/|｜]?\s*"
    r"|(?:" + _STRONG_WORDS + r")\s*[:：]\s*"
    r")+"
)

MAX_INTERNAL_STRIP = 8

# 「抖音、第九季、第一季、第25集」这种：keyinfo 那一行的样子——一串**顿号**连起来的短标签，
# 没有语气、没有句子。它是"抓来的信息"，不是"要说的话"（现场那句漏的正是这个形状）。
# 只认顿号：人说话用逗号（"哇，这倒计时器，节"），拿逗号当分隔符会误伤真话。
_INFO_DUMP = re.compile(r"^[^，,。！？!?…；;：:\n、]{1,8}(?:、[^，,。！？!?…；;：:\n、]{1,10}){2,}$")


def looks_like_internal(text: str) -> bool:
    """这句话里是不是夹着我们发给它的"内部字眼"（块头、抓来的资料标题）。

    这类词对用户毫无意义，出现在气泡里最像是"它坏了"——宁可这一轮不说。
    """
    line = (text or "").strip()
    return bool(line) and bool(_INTERNAL_ANY.search(line))


def looks_like_info_dump(text: str) -> bool:
    """这串是不是"抓来的信息"（`抖音、第九季、第28集` 这种纯标签列举）。

    只在一段话**本来就是从块头里剥出来**的场合用（见 vlm 的解析链），
    所以不用担心误伤正常台词里带顿号的句子。
    """
    line = (text or "").strip()
    return bool(line) and bool(_INFO_DUMP.match(line))


# ---------------------------------------------------------------- 「抓来的标题」不是话

# 现场（气泡里就长这样）：
#     《复仇者联盟3》剧情设定细节探案幕后解读
# ——这是页面上那支视频的标题，它照着念了一遍。对用户来说这不是"它说的话"，是它刚抓来
# 的一串信息；屏幕上冒出来最像是"它坏了"。可块头那道闸（looks_like_internal）认不出它
# （一个内部字眼都不带），"念屏幕"那道闸又要**先剥掉块头**才生效——整条标题就这么漏了。
# 判据只挑**标题的特征**：书名号里的作品名 / 话题标签 / 标题噪声词 / 尾巴上的括号补充；
# 而且要求**看不出在跟人说话**（没有问号感叹号、没有「我你咱真太别不了」这种口气词），
# 免得误伤正常说话里提到片名的那句——「这解说讲得真啰嗦」照说不误。
_TITLE_BOOK = re.compile(r"《[^《》]{1,24}》")
_TITLE_TAG = re.compile(r"#[^\s#]{1,24}")
_TITLE_NOISE = re.compile(
    r"万字深拆|深拆|幕后解读|幕后懈读|剧情设定|设定细节|细节彩蛋|剧情解析|剧情解说|"
    r"彩蛋|花絮|预告|完整版|合集|精选|重播|重温|全程|全片|高能|名场面|"
    r"第\s*[0-9一二三四五六七八九十百]+\s*[集期季话部篇]|"
    r"高清|超清|中字|双语|重制|4K|\d{3,4}\s*[pP]"
)
#: 尾巴上的括号补充（`…（再次重温内容更饱满）`）：标题的写法，不是说话的写法
_TITLE_TAIL = re.compile(r"[（(][^（()）]{4,24}[）)]\s*$")
#: 有这些就说明它在**跟人说话**，不是在念标题（问号感叹号也算）。
#: 只收「语气重」的那几个：**句尾语气词（吧 / 啊 / 呀 / 哈…）一个都不收**——节目名里就有
#: （现场那条话头正是《奔跑吧》第十四季第11期），收了会把正经标题当成话放过去。
_SPEECH_TELLS = re.compile(
    r"[？?！!…~～]|哈哈|笑死|服了|好家伙|绝了|无语|离谱|不是吧|"
    r"我|你|咱|他|她|这|那|真|太|别|不|了|想|吗|呢"
)
#: 短于这个字数的先不判：短标签另有 corpus 那道闸管
TITLE_MIN_CHARS = 8


def looks_like_title(text: str) -> bool:
    """这句是不是**页面上抓来的标题**，而不是一句说给用户听的话。

    为什么单列一道闸：标题可以一点「内部字眼」都不带——
    `《复仇者联盟3》剧情设定细节探案幕后解读`——块头那道闸和念屏幕那道闸都拦不住它，
    整条标题就进了气泡。判据见上面那几行的注释：**有标题特征 + 没有说话口气**。

    只认"整句就是标题"这种形状：带了自己的反应（「这解说讲得真啰嗦」）照说。
    """
    line = (text or "").strip()
    if len(line) < TITLE_MIN_CHARS:
        return False
    if _SPEECH_TELLS.search(line):
        return False
    if _TITLE_TAG.search(line) or _TITLE_NOISE.search(line):
        return True
    if _TITLE_BOOK.search(line):
        return True
    return bool(_TITLE_TAIL.search(line))


# ---------------------------------------------------------------- 别把它那套机器说出来

# 现场（气泡里就长这样）：
#     [好奇] 我刚学到一个知识点：长按就能保存
#     [吐槽] 键名：类型 / 答案：影视综艺 / 键名：主题
# ——前一句是它在**汇报自己的学习进度**，后一句是把看片笔记的字段**念了一遍**。
# 用户要的是一个陪看的朋友，不是一台会报进度的机器：这两类话一个字都不该上气泡
# （气泡和聊天面板同一条道理，见 vlm._to_comment 里那道闸）。
#
# 判据分三层，每层都只认"人不会这么说话"的形状：
#   ① 机器词：语料 / 知识点 / 资料卡 / 数据库 …… 熟人之间压根不会用这些词；
#   ② 自我汇报，**只认进行体**："我在学 / 我在记 / 我在补课"——那是在报自己正在干这件事。
#      完成体的"我记住了 / 我记下了"**不算**：熟人聊天里"行，我记住了"再正常不过，
#      写进语料也是好样本（语料收的是真人台词，见 corpus.py）。只有跟机器词一起出现才拦，
#      比如"我把它记进**记忆里**了"——那是 ① 抓住的；
#   ③ 笔记字段：一行里两个以上「类型：/ 看点：」这种字段标签，或者**整句以字段名开头**
#      （`看点：任务失败那一下`）——人说话不会这么起头。
MACHINERY_WORDS: Tuple[str, ...] = (
    "语料",                    # 我们自己的内部词，它说出口就等于把后台端上桌
    "记忆库", "长期记忆", "我的记忆", "记忆里", "数据库", "资料库",
    "知识点", "知识库", "资料卡", "词条", "索引", "检索",
    "笔记里", "做笔记", "记笔记", "我的笔记", "记录里", "资料里",
    "归档", "存档", "补课", "功课", "题库", "课本",
)
_MACHINERY_RE = re.compile("|".join(re.escape(word) for word in MACHINERY_WORDS))
#: 自我汇报（只认进行体 + "学到了"这一类）：它正在学 / 记，或者刚学完——都是机器在报进度
_SELF_REPORT = re.compile(
    r"我\s*(?:刚|刚刚|已经|早就|正在|正|现在|还|又|先|今天|昨晚|刚才)?\s*"
    r"(?:在\s*(?:学|记|背|整理|归类|补课)|"
    r"学会了|学到了|学过|学习一下|学习|"
    r"整理好|整理过|整理一下|归好类|分好类|归了类|分了类|打了个标签|打了标签|"
    r"存下来|存进|存档|归档|补了课|做功课|做了功课|做完功课)"
)
#: 看片笔记 / 资料卡的字段名：`类型：影视综艺`、`看点=…`（冒号、等号都算）
#: 玩法 / 画风 / 玩家群体 是后来加的（游戏类的看片笔记），一并算进来——
#: 模型把提示词那几行抄回来时（`玩法：**是游戏**才写`），拦的就是它们。
_NOTE_FIELD_WORDS = (
    r"键名|答案|类型|主题|内容|看点|梗概|人物|地点|关键词|标签|玩法|玩什么|怎么玩|"
    r"画风|风格|玩家群体|受众|观众群体|接话|知识|知识点"
)
_NOTE_FIELD_RE = re.compile(
    r"(?:" + _NOTE_FIELD_WORDS + r")"
    r"\s*[=＝:：]"
)
#: 整句以字段名开头：`看点：任务失败那一下`、`【看点】有人当场拆台`——人说话不会这么起头，
#: 这是资料卡 / 看片笔记的形状（冒号式和方括号式都认）
_NOTE_LEAD_RE = re.compile(
    r"^\s*(?:"
    r"[【\[（(]\s*(?:" + _NOTE_FIELD_WORDS + r")\s*[】\]）)]"
    r"|(?:" + _NOTE_FIELD_WORDS + r")\s*[=＝:：]"
    r")"
)
#: 自问自答的形状：`别人问：… / 我接：…`（小模型自己发明的一问一答那套写法）
_SELF_QA_RE = re.compile(r"别人问|我(?:来)?接\s*[：:]|一问一答|问\s*[：:][^\n]{1,40}答\s*[：:]")


def looks_like_note_skeleton(text: str) -> bool:
    """这段字是不是**看片笔记 / 资料卡的字段骨架**，而不是内容。

    现场（模型把提示词骨架原样抄了回来，看片笔记的「主题」就变成了这一串）：

        键名：类型 / 答案：影视综艺 / 键名：主题

    判据跟 `looks_like_meta_talk` 的第三层同一套，但**不带机器词那两层**：
    笔记的内容里出现「知识点 / 语料 / 补课」这种词很正常（"这集讲的是航天知识点"），
    不该因为一个词就把整项内容丢掉。所以这里只认"字段形状"：

        ① 整句以字段名开头（`类型：影视综艺`、`【看点】有人当场拆台`）；
        ② 一行里两个以上字段标签（`类型=游戏｜主题=…`）；
        ③ 自问自答的形状（`别人问：… 我接：…`）。

    用在哪：`watchlog.parse_note`。那串骨架不是内容——它会被当成"他刚在看的那一支"
    的话头去学（`worker._study_topics`），学回来的东西又回到气泡里，等于把提示词端上桌。
    """
    line = (text or "").strip()
    if not line:
        return False
    if _SELF_QA_RE.search(line):
        return True
    return bool(_NOTE_LEAD_RE.match(line)) or len(_NOTE_FIELD_RE.findall(line)) >= 2


def looks_like_meta_talk(text: str) -> bool:
    """这句是不是**把它自己那套机器说出来了**（汇报学习进度 / 念语料 / 念资料卡）。

    用户要的是个陪看的朋友，不是一台会汇报自己进度的机器。这道闸只管**它说出口的话**：
    气泡和聊天面板里都不该出现"我在学 / 我在记 / 我分过类"这种痕迹。

    反过来，**学语料收进来**的句子不走这道闸：语料收的是真人台词，"我记住了"写进语料
    完全是好样本——只有它把这句话当成自己的台词说出来，才像机器。判据见上面那几行的注释。
    """
    line = (text or "").strip()
    if not line:
        return False
    if _MACHINERY_RE.search(line) or _SELF_REPORT.search(line):
        return True
    return looks_like_note_skeleton(line)


# 去标点用的：判断"念屏幕"时只比字本身
_NOISE_CHARS = re.compile(r"[\s，,。.!！?？~～、;；:：\"'“”‘’()（）\[\]【】…—\-_*#<>/|｜《》·•+×·]+")


def _letters(text: str) -> str:
    """只留下字本身（中英文数字都算），标点和空白全去掉。"""
    return _NOISE_CHARS.sub("", (text or "").lower())


def looks_like_screen_echo(
    text: str, screens: Sequence[str], min_chars: int = 6, threshold: float = 0.80
) -> bool:
    """这句是不是"把屏幕上的字念出来了"——**不一定是整行照抄，也可能几个碎片拼的**。

    现场那句（气泡里长这样）：

        画面关键信息：第孬期 . / > 0 收/A《抖音 ×/ 第集：

    剥掉块头之后，剩下的字**几乎全都在画面上写着**——它是把 OCR 碎片重新拼了一串，
    所以"整行是不是某一行的一部分"那道闸（looks_like_echo）抓不住它。
    抽象成一句话：**你说的每一个字，屏幕上都有**。

    所以判据就是覆盖率：把台词和「屏幕上的那些字」都去掉标点，
    数一数台词的每个字能不能在屏幕文字里找到（按出现次数算）。
    够长（默认 6 字）+ 覆盖率够高（默认 80%）= 它在念屏幕，不是在说话。

    阈值定在 80% 而不是 100%：OCR 会有错字（现场那句里的「孬」就是认错的），
    要求"一个不差"就永远拦不住。

    注意：screens 只传**画面上的字**（OCR / 本地认出来的关键信息），
    千万别把整份提示词塞进来——那里面什么字都有，会把所有台词都判成"念屏幕"。
    """
    line = _letters(text)
    if len(line) < max(2, int(min_chars)):
        return False
    pool: Counter = Counter()
    for screen in screens or ():
        pool.update(_letters(str(screen)))
    if not pool:
        return False
    hit = 0
    for char in line:
        if pool[char] > 0:
            pool[char] -= 1
            hit += 1
    return (hit / float(len(line))) >= float(threshold)


def strip_internal_prefix(text: str) -> str:
    """把开头那串「画面关键信息：」反复剥掉，留下后面真正要说的话。

    现场那句剥完是「哇，这倒计时器」——这句本身是能看的，就别整句扔了。
    """
    line = (text or "").strip()
    for _ in range(MAX_INTERNAL_STRIP):
        stripped = _INTERNAL_PREFIX.sub("", line, count=1)
        if stripped == line:
            break
        line = stripped.strip()
    # 块头后面常跟着一个分隔符（「画面关键信息：/ 哇…」），一并清掉
    return line.lstrip("/|｜:：、,， \t")

# ---------- 「解说词」长什么样 ----------
# 「范丞丞和郑恺站在两个显示时间为 21:15 的闹钟前，似乎是在参与某个环节」——
# 这不是聊天，是**导播在介绍画面**。小模型（尤其手上还多了画面文字/关键信息之后）
# 特别爱写这种把「谁 + 在哪 + 在干嘛」串成一句、再补个推测的完整句子。
# 判据分三条，**够长才算**：短句一律不误伤（「就这？」「哈哈」这种永远安全）。
_MIN_NARRATION_CHARS = 20
_NARRATION_SPECULATE = re.compile(
    r"似乎|好像|看样子|看着像|听着像|应该是|应该是在|像是在|可能是在|大概是|大概是在"
    r"|估计是|估计在|想来是|说不准是在|八成是"
)
_NARRATION_SCENE = re.compile(
    r"站在|坐在|蹲在|躺在|走到|走进|来到|进了|出现在|转过身|抬起头|手里|旁边|面前|身后"
    r"|穿(?:着|的)|拿着|端着|正在|开始|准备|参与"
)
_NARRATION_NOUN_TAIL = re.compile(r"(?:环节|流程|过程|阶段|时刻|气氛|氛围|设定|剧情|桥段|节奏)$")


def looks_like_narration(text: str) -> bool:
    """这句是不是「导播在介绍画面」，而不是朋友在聊天。

    真人聊天不会这么说话：
        × 「某某和某某站在两个显示时间为 21:15 的闹钟前，似乎是在参与某个环节」

    三条判据（命中任一即算，但都得先过 20 字这道长度门槛）：
        · 有推测词（似乎 / 好像 / 看样子 / 应该是在……）——你又不是导演，猜什么；
        · 结尾落在「环节 / 流程 / 气氛」这种名词化总结上；
        · 是"谁和谁在干嘛"的场面描述（站在 / 走进 / 手里 / 穿着…，且句中有「和 / 与 / 跟」）。
    """
    line = (text or "").strip()
    if len(line) < _MIN_NARRATION_CHARS:
        return False
    if _NARRATION_SPECULATE.search(line):
        return True
    if _NARRATION_NOUN_TAIL.search(line):
        return True
    return bool(_NARRATION_SCENE.search(line) and re.search(r"[和与跟]", line))


# ---------- 「屏幕内容获取」的描述：机器识图的过程，不是话 ----------
# 现场（用户截图，气泡里就长这样）：
#     [好奇] 电脑屏幕截图，包含Visual Studio Code编辑器界面和一些中文文本
#     [好奇] 电脑桌面截图，Visual Studio Code和一些文件资源管理器中的物品…
# ——这不是聊天，是**机器识图的过程**：模型把"我收到了怎样一张图"念了出来。
# 用户要看的是陪看的朋友，不是识图日志；这种句子一个字都不该进气泡。
# 判据只认"识图 / 截屏描述"的形状（电脑/屏幕/桌面 + 截图/显示/包含/界面），
# **短句**和**明显在跟人说话**的那句（问号感叹号、哈哈笑死这类）一律不误伤。
_SCREEN_CAPTION = re.compile(
    r"(?:电脑|桌面|屏幕|显示器|画面|整屏|全屏)\s*(?:的)?\s*(?:截图|截屏|快照)"
    r"|(?:截图|截屏|快照)\s*[，,、:：]?\s*(?:包含|显示|里有|中有|里是|中是)"
    r"|(?:屏幕|桌面|显示器|画面)\s*(?:上|里|中)?\s*(?:显示|包含|呈现)"
    r"|(?:显示|包含|呈现)\s*了?\s*(?:一个|一张|一段|一些)?\s*"
    r"(?:编辑器|文件资源管理器|代码|界面|窗口|桌面|屏幕|图片|截图|画面)"
    r"|(?:这是|一张|一幅)\s*(?:电脑|桌面|屏幕|游戏|代码)?\s*(?:的)?\s*(?:截图|截屏|图片|照片)"
)
# 明显在跟人说话（问句 / 反应词 / 招呼）：那交给别的闸门，这里不拦
_SCREEN_CAPTION_CHAT = re.compile(
    r"[？?！!]|哈哈|笑死|不是吧|好家伙|我服|绝了|离谱|无语|兄弟|哥们"
)


def looks_like_screen_caption(text: str, min_chars: int = 8) -> bool:
    """这句是不是**机器识图 / 截屏描述**，而不是一句说给用户听的话。

    现场（用户截图，气泡里就长这样）：

        × 「电脑屏幕截图，包含Visual Studio Code编辑器界面和一些中文文本」
        × 「电脑桌面截图，Visual Studio Code和一些文件资源管理器中的物品…」

    这是模型在念"它收到的画面是什么样"——机器识别过程，不是人该说的话：
    屏幕边上要的是个陪看的朋友，不是识图日志。所以整句丢掉、让 worker 当场再要一句。

    判据：命中上面那几种"识图形状"就算；**短句**和**明显在跟人说话**的那句不拦
    （「你截图给我看看」这种没带屏幕/包含、又不算描述形状的照说）。
    """
    line = (text or "").strip()
    if len(line) < min_chars:
        return False
    if _SCREEN_CAPTION_CHAT.search(line):
        return False
    return bool(_SCREEN_CAPTION.search(line))


def avoid_block(lines, limit: int = 6) -> str:
    """「这些刚说过，别再来一遍」那一段；没有就返回空串。"""
    rows = [str(line).strip() for line in (lines or ()) if str(line).strip()]
    if not rows:
        return ""
    shown = rows[-max(1, int(limit)):]
    return (
        "【你最近说过的（都别再说了，也别换几个字重说）】\n"
        + "\n".join(shown)
        + "\n如果这次想不出新的话，就直接回 [沉默]，也别重复。"
    )


# ---------------------------------------------------------------- 反车轱辘话

_PUNCT = re.compile(r"[\s，,。.!！?？~～、;；:：\"'“”‘’()（）\[\]【】…—\-_*#>]+")


def normalize(text: str) -> str:
    """去掉标点和空白、统一小写，用来比「是不是同一句话」。"""
    return _PUNCT.sub("", (text or "").lower())


# 人称代词：判断「把他的问句换个字抛回来」时先抹平（你在干嘛呢 → 他在干嘛呢）
_PERSON = str.maketrans({"你": "·", "您": "·", "我": "·", "他": "·", "她": "·", "它": "·", "咱": "·"})


def looks_like_question_echo(text: str, user_text: str, max_chars: int = 12) -> bool:
    """这句话是不是把他的问句换个字、原样抛了回来（答非所问里最气人的一种）。

    现场（用户截图）：用户问「你在干嘛呢」，气泡里回的是「他在干嘛呢？」——
    只把「你」换成了「他」，一个字都没答，看着像它坏了。

    提示词里说归说，小模型漏出来的概率不低，所以这里补一道**本地**闸（不联网、不花钱）：
    两边先按 `normalize` 抹掉标点空白，再把**人称代词**一起抹平，一模一样就认定它是
    把问句抄回来了。只认短句（默认 12 字以内）——长句子难免撞上，短的才敢断言是抄的。
    命中之后由 worker 当场再要一句（见 `worker._answer_user`）。
    """
    line = normalize(text)
    asked = normalize(user_text)
    if not line or not asked or len(line) > int(max_chars):
        return False
    return line.translate(_PERSON) == asked.translate(_PERSON)


# 「这画面我真看不懂」这种万能句：换哪个名词都是同一句废话，说过一次就不该再说
_HOLLOW = re.compile(
    r"^(?:这|那|这个|那个)*(?:画面|操作|界面|节目|局面|场面|东西|内容|情况|行为)*"
    r"(?:我)?(?:真|是|也|都)*(?:看|搞)*(?:不|没)(?:太)?(?:懂|明白|理解).*$"
)


def _bigrams(text: str) -> Set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """0~1：两句话有多像（按字符二元组算 Jaccard 相似度）。

    「这操作我真看不懂」和「这操作我真看不懂啊」会得到 0.7 以上——
    换汤不换药的车轱辘话就该被这个拦住。
    """
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    a_pairs, b_pairs = _bigrams(left), _bigrams(right)
    if not a_pairs or not b_pairs:
        return 0.0
    return len(a_pairs & b_pairs) / float(len(a_pairs | b_pairs))


def is_repeat(text: str, recent, threshold: float = 0.62) -> bool:
    """这句话是不是在重复最近说过的（阈值可调，越小越严格）。"""
    word = normalize(text)
    if not word:
        return True
    if _HOLLOW.match(word) and any(_HOLLOW.match(normalize(other)) for other in recent or ()):
        # 「这画面我真看不懂」这种万能句：第一次说没关系，换个字再说一次就算重复
        return True
    for other in recent or ():
        if similarity(word, other) >= float(threshold):
            return True
    return False
