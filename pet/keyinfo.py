"""画面关键信息：从本地 OCR 的文字里认出"这是什么"。

为什么要单独做这一步
    OCR 吐出来的是一坨噪声（弹幕、按钮、推荐位、水印），小模型盯着这坨字，
    最后往往只憋出一句"画面里有个人"。可画面上其实写着答案：角落的**台标**
    （浙江卫视、抖音）、标题里的**节目名**（奔跑吧 第十四季）、封面上的
    **话题标签**（#范丞丞 #孟子义）、进度条上的**集数/时间**
    （更新至第187集、03:10 / 12:26）——这些才是"这是什么内容"的抓手。

    所以这里先用纯本地规则**提取**一遍（不联网、不花钱、不占接口），拼成一小块
    结构化文字，摆在原始识别文字前面交给模型：

        画面关键信息（本地从画面上的字认出来的，认得出就拿它当抓手，认不出别硬说）：
        · 台标/平台：浙江卫视、抖音
        · 游戏/应用：FC ONLINE
        · 节目/作品：奔跑吧
        · 集数/进度：第187集、03:10 / 12:26
        · 话题/人名：#跑男、#范丞丞、#孟子义
        · 字幕/弹幕：来了

    认不出来就返回空串——宁可什么都不加，也绝不瞎编：编出来的"关键信息"比没有更糟，
    模型会把它当成事实说给用户听。

已知边界（别指望它做不到的事）
    · 人名只从**话题标签/书名号**里拿（`#范丞丞`、`《奔跑吧》`）。衣服上那块名牌
      如果被 OCR 读成一行光杆名字，这里认不出是谁——没有名单就不猜。
    · 台标是图片时（不是文字）OCR 读不到，那就什么都不会出现。
    · 「跑男」这类看着像名字、其实是标签的，靠 `_TAG_STOP` 挡掉，挡不干净是正常的。
    · 话题标签里**带 ASCII 字母数字的一律不要**（`#TODO`、`# 注释` 是代码，不是话题）。
    · 台标 / 游戏名都是照表认的（PLATFORMS / APPS），表里没有的新番新游就认不出来——
      要加就在那两张表里补一行，别在这儿加启发式。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Sequence, Tuple

# ---------- 台标 / 平台 ----------
# 画面角落那几个字：认出来就知道"在哪看的、谁家的节目"。
# 右边是别名，统一报左边那个规范名（画面写「B站」也按「哔哩哔哩」记）。
PLATFORMS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("浙江卫视", ("浙江卫视", "浙江台")),
    ("湖南卫视", ("湖南卫视", "湖南台")),
    ("江苏卫视", ("江苏卫视",)),
    ("东方卫视", ("东方卫视",)),
    ("北京卫视", ("北京卫视",)),
    ("安徽卫视", ("安徽卫视",)),
    ("CCTV", ("cctv", "中央电视台", "央视")),
    ("芒果TV", ("芒果tv", "mgtv", "mango")),
    ("哔哩哔哩", ("哔哩哔哩", "bilibili", "b站")),
    ("抖音", ("抖音", "douyin")),
    ("快手", ("快手", "kuaishou")),
    ("西瓜视频", ("西瓜视频",)),
    ("腾讯视频", ("腾讯视频",)),
    ("爱奇艺", ("爱奇艺", "iqiyi")),
    ("优酷", ("优酷", "youku")),
    ("小红书", ("小红书",)),
    ("微博", ("微博", "weibo")),
    ("斗鱼", ("斗鱼", "douyu")),
    ("虎牙", ("虎牙", "huya")),
    ("YouTube", ("youtube",)),
    ("Netflix", ("netflix",)),
)

# 「合集：奔跑吧…」「第164集: #…」——行首那个键名不是内容，先摘掉再认节目名
_LABEL = re.compile(r"^[^\s：:\d]{1,6}\s*[：:]\s*")
# 话题标签尾巴上的「（10）」这种副编号
_TAIL_PAREN = re.compile(r"[（(][^）)]{0,12}[）)]$")

# ---------- 游戏 / 应用 ----------
# 同上，只是这里认的是"他现在在玩什么、在用什么"。ASCII 别名按整词匹配，
# 免得 "wow"/"dnf" 这种短词在英文句子里乱命中。
APPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("FC ONLINE", ("fc online", "fconline", "fifa online", "ea sports fc")),
    ("王者荣耀", ("王者荣耀",)),
    ("英雄联盟", ("英雄联盟", "league of legends")),
    ("云顶之弈", ("云顶之弈",)),
    ("金铲铲之战", ("金铲铲之战", "金铲铲")),
    ("和平精英", ("和平精英",)),
    ("绝地求生", ("绝地求生", "pubg")),
    ("无畏契约", ("无畏契约", "valorant")),
    ("反恐精英", ("反恐精英", "csgo", "cs2")),
    ("穿越火线", ("穿越火线", "crossfire")),
    ("三角洲行动", ("三角洲行动",)),
    ("暗区突围", ("暗区突围",)),
    ("永劫无间", ("永劫无间",)),
    ("原神", ("原神", "genshin")),
    ("崩坏：星穹铁道", ("星穹铁道",)),
    ("第五人格", ("第五人格",)),
    ("蛋仔派对", ("蛋仔派对",)),
    ("我的世界", ("我的世界", "minecraft")),
    ("迷你世界", ("迷你世界",)),
    ("魔兽世界", ("魔兽世界", "world of warcraft")),
    ("炉石传说", ("炉石传说", "hearthstone")),
    ("守望先锋", ("守望先锋", "overwatch")),
    ("刀塔", ("刀塔", "dota")),
    ("逃离塔科夫", ("逃离塔科夫", "塔科夫")),
    ("QQ飞车", ("qq飞车",)),
    ("地下城与勇士", ("地下城与勇士",)),
    ("梦幻西游", ("梦幻西游",)),
    ("逆水寒", ("逆水寒",)),
    ("剑网3", ("剑网3", "剑网三")),
    ("Steam", ("steam",)),
    ("微信", ("微信", "wechat")),
    ("网易云音乐", ("网易云音乐",)),
)


# ---------- 他现在在干嘛（主动搭话要用） ----------
# 「夸夸我 / 抱抱我」并进主动搭话之后，得先知道他此刻在干什么，才夸得具体、关心得着调：
# 在看视频 → 夸片子里的场景/人物；在打游戏 → 夸他那一下操作；在干活 → 夸他这个人、
# 顺手关心他的腰和眼睛。所以这里把 APPS 拆成「游戏」和「其它应用」两类。
GAME_APPS: frozenset = frozenset({
    "FC ONLINE", "王者荣耀", "英雄联盟", "云顶之弈", "金铲铲之战", "和平精英", "绝地求生",
    "无畏契约", "反恐精英", "穿越火线", "三角洲行动", "暗区突围", "永劫无间", "原神",
    "崩坏：星穹铁道", "第五人格", "蛋仔派对", "我的世界", "迷你世界", "魔兽世界",
    "炉石传说", "守望先锋", "刀塔", "逃离塔科夫", "QQ飞车", "地下城与勇士", "梦幻西游",
    "逆水寒", "剑网3",
})

# 窗口标题里出现这些词 = 他在干活（写代码 / 写文档 / 做表 / 剪片子那一类）
WORK_HINTS: Tuple[str, ...] = (
    "visual studio code", "vscode", "vs code", "pycharm", "intellij", "idea",
    "webstorm", "eclipse", "android studio", "sublime", "notepad", "vim", "jupyter",
    "matlab", "rstudio", "terminal", "powershell", "cmd.exe", "git bash", "postman",
    "docker", "word", "excel", "powerpoint", "onenote", "wps", "ppt", "visio", "xmind",
    "figma", "photoshop", "illustrator", "premiere", "after effects", "blender", "cad",
    "钉钉", "飞书", "企业微信", "teams", "slack", "outlook", "foxmail", "邮箱",
    "文档", "表格", "演示", "代码", "工程", "项目",
)

ACTIVITY_GAME = "game"     # 在打游戏
ACTIVITY_VIDEO = "video"   # 在看视频 / 直播 / 剧
ACTIVITY_WORK = "work"     # 在干活
ACTIVITY_OTHER = "other"   # 认不出来（认不出来就什么都别夸，夸空的更尴尬）


def _label(text: str, limit: int = 60) -> str:
    """给"他现在在干嘛"用的名字。

    只收拾空白和两头的引号，**不动书名号**——《奔跑吧》第九季 得原样留着，
    不然拼进提示词就成了「奔跑吧》第九季」，模型看了也不知道那是部片子。
    """
    value = re.sub(r"\s+", " ", (text or "").strip())
    value = value.strip(" \t\"'“”「」『』")
    return value[:limit].strip()


def activity_of(title: str, apps: Sequence[str] = (), video: str = "") -> Tuple[str, str]:
    """粗略认一下"他现在在干嘛"，返回 (类型, 具体是什么)。

    判据都是本地免费的东西（窗口标题 + 已经认出来的应用名 + 当前那支视频的标题），
    不额外花接口钱。优先顺序：游戏 > 干活 > 视频 > 其它。

    「干活」排在「视频」前面是有原因的：看片笔记是留在那儿的，人切去写代码之后
    它还在，不先认窗口标题的话，就会拿半小时前那部剧去夸他现在的工作。
    """
    text = _label(title).lower()
    for name in apps or ():
        if str(name) in GAME_APPS:
            return ACTIVITY_GAME, str(name)
    label = _label(title)
    if text and any(hint in text for hint in WORK_HINTS):
        return ACTIVITY_WORK, label
    watching = _label(video)
    if watching:
        return ACTIVITY_VIDEO, watching
    return ACTIVITY_OTHER, label

# 书名号里的作品名：「《奔跑吧》」
_BOOK = re.compile(r"《([^《》]{1,20})》")
# 话题标签：`#范丞丞` / `#奔跑吧#` 都算（有的平台前后都带 #，有的只带一个）
_TOPIC = re.compile(r"#([^\s#]{1,16})")
# 「奔跑吧第十四季」里的节目名 = 「第 N 季」前面那截
_SEASON_NAME = re.compile(r"^([^\s第]{2,12}?)\s*第\s*[0-9一二三四五六七八九十百零两]{1,4}\s*季")
# 集数 / 期数 / 季数：「第164集」「第十一期」「更新至第187集」
_EPISODE = re.compile(r"第\s*([0-9一二三四五六七八九十百零两]{1,4})\s*([集期季部回话])")
# 播放进度时间码：「03:10 / 12:26」
_CLOCK = re.compile(r"(?<!\d)(\d{1,3}:\d{2})\s*/\s*(\d{1,3}:\d{2})(?!\d)")

# 界面词 / 水印：出现在画面里但**不是内容**，一律不当字幕，也不当话题
_UI_WORDS = (
    "搜索", "关注", "朋友", "我的", "直播", "放映厅", "短剧", "小游戏",
    "精选", "推荐", "投稿", "客户端", "壁纸", "通知", "消息", "设置", "举报", "反馈",
    "弹幕", "发送", "连播", "清屏", "智能", "听抖音", "下一集", "上一集", "查看更多",
    "下载", "打开", "安装", "登录", "注册", "充值", "广告", "京ICP", "ICP备", "许可证",
    "版权", "免责声明", "隐私", "用户协议", "倍速", "全屏", "抖音", "合集", "更新至", "©",
)
# 看着像人名、其实是标签的，别当人物记
_TAG_STOP = frozenset((
    "跑男", "综艺", "合集", "直播", "短片", "电影", "电视剧", "动漫", "漫画", "游戏",
    "搞笑", "美食", "旅行", "日常", "萌宠", "音乐", "舞蹈", "体育", "纪录片", "vlog",
    "影视", "追剧", "剪辑", "二创", "解说", "预告", "花絮",
))
# 剥掉这些首尾字符（引号、书名号、标签的 # 之类）
_WRAP = " \t「」『』\"'“”《》【】()（）[]#，,。、;；:：!！?？"

_CJK = re.compile(r"[\u3400-\u9fff]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

MAX_PLATFORMS = 3
MAX_APPS = 2
# 台标 / 游戏名只认"独立成行的短标签"：视频里的台标、水印、游戏 logo 都是单独一块字。
# 编辑器里读到的是源码里的字符串（`"浙江卫视", ("浙江卫视", "浙江台")` 这种长行），
# 那一行长又杂，不该被当成"他现在在看浙江卫视"。
MAX_BADGE_CHARS = 14
MAX_SHOWS = 3
MAX_EPISODE = 5
MAX_TAGS = 6
MAX_SUBTITLES = 3
MAX_SUBTITLE_CHARS = 24
MAX_TAG_CHARS = 16

_HEAD = (
    "画面关键信息（本地从画面上的字里认出来的——**认出来是为了让你说的话对得上内容，"
    "不是让你把它念一遍**，更不是让你去描述画面；认不出就别硬说）：\n"
)


def _clean(text: str, limit: int = 0) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip()).strip(_WRAP)
    if limit and len(value) > limit:
        value = value[:limit]
    return value.strip(_WRAP)


def _push(bucket: List[str], value: str, limit: int) -> None:
    """塞进去并去重（保持出现顺序），满了就不再收。"""
    word = _clean(value)
    if word and word not in bucket and len(bucket) < limit:
        bucket.append(word)


def _cjk_count(text: str) -> int:
    return len(_CJK.findall(text or ""))


_ASCII_ALIAS = re.compile(r"^[a-z0-9 .:+-]+$")


def _hit(blob: str, alias: str) -> bool:
    """别名命中：纯 ASCII 的按**整词**匹配（`wow` 别在英文句子里乱撞），中文的直接找。"""
    key = alias.lower()
    if _ASCII_ALIAS.match(key):
        return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", blob) is not None
    return key in blob


def _looks_like_tag(tag: str) -> bool:
    """像不像一个真话题标签：不要 ASCII 字母数字（代码里的 `# 注释`、`#TODO` 全在这儿挡掉）。"""
    return not re.search(r"[0-9A-Za-z]", tag or "")


def _noise(text: str) -> bool:
    """界面词 / 水印 / 纯数字 / 纯符号 —— 这些都不是"字幕"。"""
    line = (text or "").strip()
    if not line:
        return True
    if any(word in line for word in _UI_WORDS):
        return True
    if not _LETTER.search(line) and not _CJK.search(line):
        return True          # 「12」「3:10 / 12:26」「、」这种
    return False


def _looks_like_name(tag: str) -> bool:
    """话题标签里挑人名：2~4 个汉字、纯中文、不是常见非人名标签。"""
    word = _clean(tag)
    if not 2 <= len(word) <= 4 or word in _TAG_STOP:
        return False
    return all("\u3400" <= ch <= "\u9fff" for ch in word)


@dataclass
class KeyInfo:
    """一眼画面里认出来的关键信息（认不出来就是空的，什么都不写）。"""

    platforms: List[str] = field(default_factory=list)   # 台标 / 平台（浙江卫视、抖音）
    apps: List[str] = field(default_factory=list)        # 游戏 / 应用（FC ONLINE、王者荣耀）
    shows: List[str] = field(default_factory=list)       # 节目 / 作品名（奔跑吧、我的世界）
    episode: List[str] = field(default_factory=list)     # 集数 / 期数 / 播放时间码
    tags: List[str] = field(default_factory=list)        # 话题标签（范丞丞、孟子义、跑男）
    subtitles: List[str] = field(default_factory=list)   # 字幕 / 弹幕短句（来了）

    def is_empty(self) -> bool:
        return not (self.platforms or self.apps or self.shows or self.episode
                    or self.tags or self.subtitles)

    def people(self) -> List[str]:
        """标签里看着像人名的（没有名单，只能按字数/词性猜，说不准就别当真）。"""
        return [tag for tag in self.tags if _looks_like_name(tag)]

    def keywords(self) -> List[str]:
        """给日志/记忆用的一行关键词（台标本身也是线索，所以一起带上）。"""
        return self.platforms + self.apps + self.shows + self.episode + list(self.tags)

    def line(self) -> str:
        words = self.keywords()
        return "、".join(words) if words else "（没认出什么）"

    def block(self) -> str:
        """拼成给模型的提示词块；什么都没认出来就返回空串（不加空壳）。"""
        rows: List[str] = []
        if self.platforms:
            rows.append("· 台标/平台：" + "、".join(self.platforms))
        if self.apps:
            rows.append("· 游戏/应用：" + "、".join(self.apps))
        if self.shows:
            rows.append("· 节目/作品：" + "、".join(self.shows))
        if self.episode:
            rows.append("· 集数/进度：" + "、".join(self.episode))
        if self.tags:
            rows.append("· 话题/人名：" + "、".join("#" + tag for tag in self.tags))
        if self.subtitles:
            rows.append("· 字幕/弹幕：" + " / ".join(self.subtitles))
        if not rows:
            return ""
        return _HEAD + "\n" + "\n".join(rows)


def extract(lines: Sequence[str], window: str = "") -> KeyInfo:
    """从这一眼的 OCR 文字（+ 窗口标题）里认出关键信息；认不出就是空 KeyInfo。

    window 里往往直接写着答案（「浙江卫视 - 奔跑吧 - 抖音」这种标题），所以一起扫；
    但**字幕只从画面文字里取**——窗口标题是程序写死的，不算画面内容。
    """
    info = KeyInfo()
    screen = [_clean(str(line or "")) for line in lines]
    screen = [line for line in screen if line]
    title = _clean(window or "")
    if not screen and not title:
        return info

    # 台标 / 平台 / 游戏应用 / 节目名，都从"独立成行的短标签 + 窗口标题"里认
    # （见 MAX_BADGE_CHARS 上面那段：不这么收着点，编辑器里读到的源码字符串会全被当成台标）
    short = [line for line in screen if len(line) <= MAX_BADGE_CHARS]
    badge = " ".join(short + ([title] if title else [])).lower()

    # ① 台标 / 平台 / 游戏应用：别名表扫一遍
    for name, aliases in PLATFORMS:
        if len(info.platforms) >= MAX_PLATFORMS:
            break
        if any(_hit(badge, alias) for alias in aliases):
            info.platforms.append(name)
    for name, aliases in APPS:
        if len(info.apps) >= MAX_APPS:
            break
        if any(_hit(badge, alias) for alias in aliases):
            info.apps.append(name)

    # ② 节目名 / 作品名 / 话题标签（先把行首的「合集：」这类键名摘掉再认）
    for line in screen:
        text = _LABEL.sub("", line)
        for book in _BOOK.findall(text):
            _push(info.shows, book, MAX_SHOWS)
        season = _SEASON_NAME.search(text)
        if season:
            _push(info.shows, season.group(1), MAX_SHOWS)
        for tag in _TOPIC.findall(text):
            word = _clean(_TAIL_PAREN.sub("", tag), MAX_TAG_CHARS)
            # 代码里的 `# 注释` / `#TODO` 不该被当成话题标签（桌宠也会盯着编辑器看）
            if not word or not _looks_like_tag(word):
                continue
            inner = _SEASON_NAME.search(word)
            if inner:
                _push(info.shows, inner.group(1), MAX_SHOWS)
            _push(info.tags, word, MAX_TAGS)

    # ③ 集数 / 期数 / 时间码
    for line in screen:
        for number, unit in _EPISODE.findall(line):
            _push(info.episode, f"第{number}{unit}", MAX_EPISODE)
        for played, total in _CLOCK.findall(line):
            _push(info.episode, f"{played} / {total}", MAX_EPISODE)

    # ④ 字幕 / 弹幕：短句、不是界面词，也不许跟上面刚认出来的台标/名字重复
    taken = set(info.platforms) | set(info.apps) | set(info.shows) | set(info.tags)
    for line in screen:
        if len(info.subtitles) >= MAX_SUBTITLES:
            break
        if _noise(line) or len(line) > MAX_SUBTITLE_CHARS or _cjk_count(line) < 2:
            continue
        if line in taken:
            continue          # 「浙江卫视」已经记成台标了，别再当字幕说一遍
        _push(info.subtitles, line, MAX_SUBTITLES)

    return info
