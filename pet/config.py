"""配置：config.json + 环境变量。

优先级：环境变量 >> config.json >> 内置默认值。
API Key 也可以只放在环境变量 PET_API_KEY 里，避免写进文件。
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths

#: 程序位置：写的东西在 user_dir()，读的素材在 resource_dir()（见 pet/paths.py）。
#: 源码里跑的时候这两个都是项目根目录，跟以前完全一样。
APP_DIR = paths.code_dir()
CONFIG_PATH = paths.config_path()
ASSETS_DIR = paths.asset_dir()

# 只需要 base_url + model，接口都是 OpenAI 兼容的 chat/completions
PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    "mock": {"base_url": "", "model": "mock"},
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-flash",
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max-latest",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-VL-32B-Instruct",
    },
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5vl:7b"},
}


@dataclass
class CaptureConfig:
    """region 是"物理像素"矩形；None 表示整个主屏。"""

    region: Optional[Dict[str, int]] = None
    interval_sec: float = 2.5      # 两次看画面之间的间隔
    cooldown_sec: float = 3.0      # 说完一句话后的冷却时间（越小话越多）
    max_width: int = 1280          # 送进大模型的图片宽度（太小就认不出画面内容和字幕）
    jpeg_quality: int = 82
    change_threshold: float = 0.045  # 画面变化小于这个比例就不调用大模型
    max_calls_per_min: int = 20      # 每分钟最多几次请求
    request_timeout: float = 40.0
    history_size: int = 6            # 记住最近几句吐槽，避免重复
    scene_cut_threshold: float = 0.10  # 画面大跳变（换视频/大场景切换）的判定：超过就把新画面读完
    absorb_enabled: bool = True        # 换视频时先"读一遍"再聊（多花一次请求，换来它真看懂）
    absorb_min_gap_sec: float = 15.0   # 两次"读一遍"之间至少隔这么久，防止片头一闪一闪连读好几次
    absorb_max_per_min: int = 6        # 每分钟最多"读"几次
    absorb_refresh_sec: float = 75.0   # 同一支视频看这么久之后，重新读一遍更新笔记（0 = 不刷新）
    # 「跟着他的进度看」：他拖到 30% / 50% / 70% 是**不同的内容**，只拿开头那份笔记应付不行。
    # 进度每往前走过这么多（默认 10%）就重读一遍，把这一段也读进去（0 = 不看进度，只按时间刷新）。
    absorb_progress_step: float = 0.10
    absorb_progress_min_gap_sec: float = 20.0  # 按进度重读也得隔一会儿，防着拖进度条时连读
    seq_frames: int = 3                # 每次给模型几张连拍（拼成一张图）；1 = 只看这一眼
    insist_after_quiet_sec: float = 30.0  # 它憋了这么久还没说出台词（只交了场景行）就再要一次（0 = 关掉补说）

    # ---- 只盯着某个程序（抖音 / B站 / 游戏窗口）----
    target_process: str = ""       # 只监视这个 exe（比如 "Douyin.exe"、"chrome.exe"）；空 = 关掉这个能力
    target_title: str = ""         # 可选：再按窗口标题关键字筛一次（同一个 exe 开了好几个窗口时用得上）
    target_foreground_only: bool = False  # 只有它在前台时才看；默认 False = 你切去干别的它照样盯着（推荐）
    target_client_area: bool = True      # 只拍窗口内容区，不拍标题栏和边框
    window_capture: bool = True          # 不在前台时直接抓窗口自己的画面（被别的窗口盖住也能看），失败才退回拍屏幕


@dataclass
class UIConfig:
    pet_size: int = 132            # 形象直径（逻辑像素）
    opacity: float = 1.0
    bubble_ms: int = 6500          # 气泡停留时间
    bubble_max_width: int = 280
    font_size: int = 13
    side: str = "right"            # 初始靠边：right / left
    edge_margin: int = 48          # 离屏幕边缘的距离
    vertical_ratio: float = 0.55   # 垂直位置（0=顶部 1=底部）
    click_through: bool = False    # 整个挂件点击穿透（锁住位置用）
    lock_hover_unlock: bool = True  # 锁定后鼠标压到身上时临时解锁，右键/拖动照常用
    lock_hover_delay_ms: int = 200  # 移到身上多久才算"想跟它交互"（防手滑误触）
    anim_crossfade: bool = True    # 帧与帧之间做渐变过渡：动作才连贯，不会一闪一闪
    anim_period: float = 30.0      # 待机"呼吸"一轮几秒（越大越慢越安静，默认慢到看不出在动）；情绪会按比例加快
    visit_seconds: float = 25.0    # "去别的屏幕逛逛"待多久（到点自己走回原位）
    eye_tracking: bool = True      # 眼珠跟着鼠标转（形象带眼珠层 eye_layer.* 时才生效）
    eye_follow_px: int = 320       # 鼠标离它多远算"看到最边上"（越小眼珠越灵敏）
    hand_tap: bool = True          # 食指轻轻敲下巴（形象带 hand_layer.* 时才生效）
    # 「我自己设计的形象」：长相（颜色 / 身形 / 五官），见 pet/style.py。
    # 可以只写想改的那几项，没写的从 preset 里取：
    #     "pet_style": {"preset": "peach", "eyes": "arc"}
    # 图省事也可以整节只写一个预设名：`"pet_style": "mint"`。
    # 用 tools/design_pet.py 调最省事——所见即所得，点「保存到配置」就写在这儿。
    pet_style: Dict[str, Any] = field(default_factory=dict)
    # 就算 assets/pet 里装了图片帧，也先用上面这套矢量形象（设计器"先看看"用它）。
    prefer_vector: bool = False


@dataclass
class PersonaConfig:
    name: str = "黄豆"
    style: str = "毒舌但心软，像一起看视频的朋友，看到槽点会忍不住开口"
    max_chars: int = 28


@dataclass
class OcrConfig:
    """弹幕 / 字幕识别（本地，图片不出本机）。"""

    enabled: bool = True
    backend: str = "auto"        # auto | winocr | rapidocr | off
    language: str = "zh-Hans"
    max_lines: int = 12          # 送给模型的最多行数（从下往上取，弹幕一般堆在底部）
    min_chars: int = 2           # 少于这么多有效字符的行丢掉
    cache_size: int = 8          # 按画面指纹缓存几帧
    timeout: float = 10.0
    window_title: bool = True    # 顺手把前台窗口标题也给模型（很便宜的线索）


@dataclass
class LearnConfig:
    """边看边学：把它读到的字幕/台词，攒成"怎么接话"的语料（本地，不花接口钱）。

    两件事分开：
      · **采集**（`enabled`）：每一眼读到的台词/接话对先攒进 `data/learn.json`；
      · **消化**（`auto_promote` / `digest_interval_sec`）：攒够了把接话对升格进
        `data/chat_style.json`，下一句话就用得上（`humanstyle.reload()`，不用重启）。

    为什么消化默认**开着**：只攒不消化，档案会顶到 `max_lines` / `max_candidates`
    的天花板，之后**新读到的反而是被丢掉的**（池子满了丢最低频 / 最旧的那批）——
    现场就是这么攒了 2117 次观察、0 段进语料。升格照旧要过闸门（问→答 ×2、
    前后 ×3，另加 `corpus.looks_like_speech` 那套），所以自动消化不等于往语料里倒垃圾。

    `promote` 是更早的写法（按观察次数触发）；两个有一个开着就会消化，留着是为了
    不打断别人的配置。攒到什么了随时可以看：`python -m pet.corpus`。
    """

    enabled: bool = True
    promote: bool = False         # 老的开关：攒够 promote_every 次观察就写语料
    auto_promote: bool = True     # 新的开关：照上面的规矩自己消化（默认开）
    digest_interval_sec: float = 300.0   # 最多隔这么久消化一次（不必等观察次数攒够）
    path: str = "data/learn.json"          # 学习档案（台词池 + 接话对）
    corpus: str = "data/chat_style.json"   # 要写的语料（就是 humanstyle 读的那个）
    min_chars: int = 4            # 短于这个字数的行不当台词（「哈哈」这种短反应另有池子管）
    max_chars: int = 40           # 长于这个字数的行多半是字幕连成一坨，丢掉
    max_lines: int = 400          # 台词池上限
    max_candidates: int = 200     # 接话对上限
    promote_min_hits: int = 2     # 同一对至少见过几次才准进语料（问→答；其它要 +1）
    promote_every: int = 40       # 每观察这么多次试一次升格
    max_dialogue: int = 60        # 语料里 dialogue 最多留几段
    pair_gap_sec: float = 20.0    # 上下两句隔超过这么久就不算一对


@dataclass
class StudyConfig:
    """隐身学习：收进托盘之后，自己上网找料学（见 pet/webstudy.py）。

    什么时候用：你把它收进托盘 = "这会儿不用你"。人都走了，继续拍屏幕没有意义，
    所以那段时间它**不看你屏幕**，改成自己上网：拿长期记忆里"你最近老在看什么"
    当话头，搜几页、读一遍，让模型整成 ① 口语语料（写进 data/chat_style.json，
    跟边看边学同一个出口）② 知识点（写进 memory.json）。

    **默认开着**：收进托盘就自己上网补课，不用谁点头（右键菜单里那个开关已经撤了）。
    它要联网、要花接口钱，**不想让它花就把这里的 enabled 改成 false**——改完收进托盘就
    纯歇着，一秒醒一次等你放回屏幕。单独试一轮：`python -m pet.webstudy --force`。
    """

    enabled: bool = True           # 隐身时自动上网学（默认开；改 false 就纯歇着）
    interval_sec: float = 1800.0   # 两轮之间至少隔多久（默认半小时，别老惦记着上网）
    max_per_hour: int = 2          # 一小时最多学几轮（防它猛刷接口）
    topics: List[str] = field(default_factory=list)  # 想固定学什么就写这儿；留空 = 从记忆里挑
    topic_limit: int = 2           # 一轮从记忆里挑几个话头
    engine: str = "auto"           # 搜索引擎：auto / bing / duckduckgo / none（none = 只问模型）
    results: int = 3               # 一个话头最多读几页
    page_chars: int = 2400         # 每页最多截多少字给模型（省点 token）
    timeout: float = 12.0          # 一次网络请求等多久（秒）
    model_fallback: bool = True    # 搜不到 / 抓不到正文时，允许只问模型（一样算"上网"）
    max_dialogue: int = 60         # 语料里 dialogue 最多留几段（跟 learn.max_dialogue 保持一致）
    max_rows: int = 3              # 一轮最多收几段接话（模型爱一口气写十几组，宁缺毋滥）
    max_notes: int = 2             # 一轮最多收几条知识点
    min_gap_sec: float = 30.0      # 单次运行里两轮之间最短间隔（防重入）

    # ---- 学得更准（见 pet/webstudy.py 顶上那段「三道闸」）----
    # 补课是纯文本活儿，用不着视觉模型：主配置那个 glm-4v-flash 写小作文又慢又爱编。
    # 这三项留空 = 跟主配置同一个模型；填了（比如 glm-4-flash / qwen-turbo）就单开一份
    # 客户端去补课，屏幕那边照旧用视觉模型——两边互不影响。
    model: str = ""                # 补课专用模型名
    base_url: str = ""             # 补课专用接口地址（想换一家时才填）
    provider: str = ""             # 补课专用 provider（换一家时才填；填了要连 base_url 一起）
    verify: bool = True            # 有网页材料时，再让模型核一遍：材料里没依据的一律不写
    min_page_chars: int = 300      # 正文短于这个字数不算料（导航页 / 加载页 / 404）
    min_page_match: int = 1        # 正文至少要有几个"话头的两字词"才算跟话题沾边（0 = 不查）
    block_hosts: List[str] = field(default_factory=list)   # 额外不想读的站（子串匹配，比如 "csdn.net"）
    topic_cooldown_sec: float = 21600.0   # 同一个话头多久之内不再学（默认 6 小时，别老啃同一个梗）
    fail_penalty: int = 3          # 一个话头白卷这么多次之后，排到最后去（省得一直啃搜不出东西的话头）
    ledger: str = "data/study.json"  # 话题账本：谁学过、学成没学成（相对 config.json 所在目录）

    def __post_init__(self) -> None:
        # 配置文件里手写的 topics 可能是字符串（"王者荣耀"），也可能是个 list：
        # 统一成 list，免得后面到处判断类型。
        if isinstance(self.topics, str):
            self.topics = [part.strip() for part in self.topics.replace("，", ",").split(",") if part.strip()]


@dataclass
class MemoryConfig:
    """长期记忆：本地 memory.json，让「黄豆」对你有点熟人感。

    `memory.json` 只留最近的 `max_entries` 条（给模型看的那一份，别越拖越慢）；
    同一时刻还在往 `archive_path` 追加一本**只增不减**的完整存档
    （见 pet/memarchive.py）——裁剪、崩溃、重启都不影响它。
    """

    enabled: bool = True
    path: str = "memory.json"
    max_entries: int = 200
    summary_every_n: int = 25     # 每攒够多少句吐槽就让模型总结一次画像
    summary_every_sec: float = 1800.0
    profile_max_chars: int = 220
    archive_enabled: bool = True  # 完整存档：memory.json 会裁剪，存档一条都不丢
    archive_path: str = ""        # 空 = 放在 memory.json 旁边（默认位置 data/memory/archive.jsonl）


@dataclass
class HotkeyConfig:
    """全局热键（Windows RegisterHotKey，不需要管理员权限）。"""

    enabled: bool = True
    pause: str = "ctrl+alt+p"
    region: str = "ctrl+alt+r"  # 划观看范围：拖一块 / 双击 = 整块屏
    say: str = "ctrl+alt+s"
    chat: str = "ctrl+alt+t"    # 打开输入框，打字跟它聊
    voice: str = "ctrl+alt+v"   # 打开输入框并开始听你说话
    lock: str = "ctrl+alt+l"    # 锁定/解锁位置（鼠标穿透），锁了也能用这个键开回来


@dataclass
class WatchConfig:
    """看片笔记：视频一刷出来就先读一遍，之后聊到它才答得上话。"""

    enabled: bool = True
    timeline_size: int = 12      # 时间线记住最近多少眼（场景 / 弹幕 / 它说过的话）
    note_history: int = 5        # 前面几支视频的笔记留几支
    context_items: int = 6       # 回答用户时，按相关度挑几条看片记录回喂给模型
    speak_on_new_video: bool = True   # 换视频读完就先接一句（像群里手最快的那个人）
    react_to_actions: bool = True     # 看到你点赞/收藏/关注，当场说一句


@dataclass
class TasteConfig:
    """口味档案：每支视频记一条，靠点赞/收藏/关注/评论学"你到底爱看啥"。"""

    enabled: bool = True
    path: str = "taste.json"      # 相对 config.json 所在目录
    max_records: int = 200        # 最多留多少支视频的档案
    progress_bar: bool = True     # 靠画面底部的进度条判断"这支看完没有"（本地、免费）
    end_ratio: float = 0.85       # 进度条走到这个比例就算看完（0~1）


@dataclass
class ChatConfig:
    """用户直接跟它说话（打字 / 语音）。"""

    enabled: bool = True
    max_chars: int = 90        # 一次回答最多几个字（对话比吐槽长一点）
    history_size: int = 6      # 提示词里带上最近几轮对话
    max_per_min: int = 10      # 每分钟最多答几次，防止一直按着问
    voice_auto_send: bool = False  # 语音识别完直接发出去（false = 先填进输入框让你确认）


@dataclass
class AsrConfig:
    """语音输入：Windows 自带的语音识别（完全离线，零安装）。"""

    enabled: bool = True
    backend: str = "sapi"      # sapi（Windows System.Speech）| off
    culture: str = "zh-CN"     # 想用英文识别就改成 en-US（系统里得有对应语音包）
    seconds: float = 6.0       # 一次听多久（说完它自己会提前结束）
    timeout: float = 40.0      # 等 PowerShell 进程的上限
    script: str = ""           # 留空 = pet/asr.ps1


@dataclass
class ProactiveConfig:
    """主动搭话：不等画面变，自己找话说。

    默认值都偏保守——宁可它少说一句，也别变成话痨。
    """

    enabled: bool = True
    still_after_sec: float = 90.0     # 画面多久没变化，就自己开一次口
    quiet_after_sec: float = 180.0    # 它自己多久没说话，就憋不住说一句
    away_after_sec: float = 420.0     # 键鼠空闲 + 画面不动这么久，算人走了
    back_idle_sec: float = 5.0        # 空闲掉回这个值以内，算"刚回来"
    cooldown_sec: float = 240.0       # 两次主动搭话之间至少隔这么久
    back_cooldown_sec: float = 60.0   # "哟，回来了"这种招呼可以勤一点
    max_per_hour: int = 8             # 每小时最多主动开口几次
    model_nudge: bool = True          # 允许为了搭话多调一次模型（关掉就只用免费台词）

    # ---- 进阶：翻长期记忆找话头（只读记忆，搭话本身不会写进记忆）----
    topic_from_memory: bool = True    # 沉默太久时，优先聊"你最近老在看的那个"
    topic_min_count: int = 3          # 一个词至少出现过几次，才算"老在看"

    # ---- 进阶：按时间段换说话方式 ----
    time_aware: bool = True           # 深夜还盯着屏幕就劝一句睡觉
    night_start_hour: int = 23        # 深夜时段从几点开始
    night_end_hour: int = 6           # 到几点结束（跨零点）
    night_cooldown_sec: float = 3600.0  # 深夜唠叨的最短间隔

    # ---- 进阶：按"他正在干嘛"来夸 / 来关心（"夸夸我 / 抱抱我"并进主动搭话的那一半）----
    # 他看视频 / 打游戏 / 干活做久了，就主动夸一句具体的（夸片子里的场景人物、夸他那一下
    # 操作、夸他手头这摊活），再久一点就关心一句（身体 + 针对他正在做的事给点建议）。
    # 要不要说、说什么由 keyinfo.activity_of 分辨出来（本地免费，不额外截图）。
    content_nudge: bool = True         # 总开关
    praise_after_min: float = 20.0     # 同一件事做了这么久 → 主动夸一次（0 = 不夸）
    care_after_min: float = 50.0       # 同一件事做这么久 → 主动关心一次（0 = 不关心）
    content_cooldown_min: float = 30.0  # 同一件事夸过/关心过之后，至少隔这么久才再来一次


@dataclass
class CareConfig:
    """上班时的健康提醒：喝水 / 站起来走动 / 闭眼休息。

    跟 proactive 分开：那套看的是"画面变没变、它憋多久"，这套只看钟点。
    文案全是本地写好的，一条都不花接口钱（见 pet/care.py）。
    """

    enabled: bool = True
    work_start_hour: int = 9        # 只在工作时间提醒（跨零点也认）；两头相等 = 全天
    work_end_hour: int = 18
    water_every_sec: float = 2700.0   # 喝水：45 分钟
    stand_every_sec: float = 3600.0   # 站起来走两步：1 小时
    eyes_every_sec: float = 5400.0    # 闭眼歇一会儿：1.5 小时
    min_gap_sec: float = 600.0        # 两条提醒之间至少隔这么久（免得三件事连播）
    only_when_active: bool = True     # 人不在（键鼠空闲太久）就先不提醒
    active_idle_sec: float = 300.0    # 空闲超过这么久就算人不在了


@dataclass
class EpisodeConfig:
    """整集资料卡（**默认关着**）：认出"这是哪一集"之后，先把这一集是什么内容弄到手。

    见 pet/episode.py——**不去下载整集视频**（那需要视频源、登录态、大带宽，还有版权问题），
    而是拿「节目名 + 季 + 集」问一次模型"这一集讲了什么"，存成资料卡一直用。
    每次只多一次**纯文本**调用，同一集只问一次（存进 episodes.json）。

    为什么默认关：用户的原话是「既然没法去扒原片，那就把正在看的部分给看完，然后跟随
    用户的进度给出互动」——**跟着进度看**（pet/watchlog.py + capture.absorb_progress_step）
    才是主线，这张资料卡只在"想让它提前知道整集背景"时手动打开。
    """

    enabled: bool = False
    path: str = "episodes.json"
    max_records: int = 120        # 最多留几集的资料卡
    source: str = ""              # 想接自己的数据源：命令或 URL 模板（{show}/{season}/{episode}）
    min_gap_sec: float = 20.0     # 两次"做功课"之间至少隔这么久（别在片头连问好几次）
    refresh_days: float = 30.0    # 过了这么多天可以重做一遍（0 = 永不重做）


@dataclass
class FriendConfig:
    """好友陪伴：两台电脑上的桌宠互相"去对方设备上"待一会儿（见 pet/friends.py）。

    传输只用标准库 http.server 起一个很小的服务 + 项目里已有的 requests 当客户端，
    不需要中转服务器：同一个局域网里填上对方的 IP + 口令就能来回。
    想跨公网就把端口映射出去，或者用 ZeroTier / Tailscale 这类虚拟局域网——
    对这套代码来说，对方永远只是"一个地址 + 一个端口"。

    只传**文字**和**形象的几张待机帧**。截图、OCR、长期记忆一个字都不发出去。
    """

    enabled: bool = True
    port: int = 8799               # 本机监听的端口（对方连的就是它）
    listen: str = "0.0.0.0"        # 绑哪张网卡；只想在本机自己试就写 127.0.0.1
    token: str = ""                # 口令：留空 = 首次启动自动生成并写回 config.json
    owner: str = ""                # 你的称呼（对方看到的是"小明家的 黄豆"）；留空用系统用户名
    nick: str = ""                 # 这只宠物对外叫什么；留空用 persona.name
    auto_accept: bool = False      # 好友的宠物来了直接放进来（false = 先问你一句）
    visit_seconds: float = 900.0   # 串门待多久，到点自己回家
    max_guests: int = 2            # 同时最多几只客人在屏幕上
    at_home: bool = True           # 我出门的时候，家里这只还留不留着（false = 它也躲起来）
    share_frames: int = 4          # 把形象的几张待机帧一起发过去（0 = 只报个名字，用内置形象）
    allow_owner_chat: bool = True  # 允许对方用户直接跟我（的宠物）说话
    pet_chat_rounds: int = 3       # 见面时两只宠物自动来回几句（0 = 不自动聊）
    pet_chat_gap_sec: float = 7.0  # 两只宠物每句之间隔多久
    share_doing: int = 2           # 告诉对方「我主人这会儿在忙什么」的尺度（见 pet/doing.py）：
    #                                0 = 一个字都不说；1 = 只说大类（在打游戏/在看视频/在干活）；
    #                                2（默认）= 连游戏名、片名也说（「干活」那件永远不带名字——
    #                                    那本来就是窗口标题原文，不能出去）
    ask_doing: bool = True         # 串门时问一句「你家主人刚在干嘛」
    play_rounds: int = 0           # 一趟串门最多自动玩几次（0 = 不封顶，按 play_gap_sec 一直玩；
    #                                只在你点菜单时玩就把它和 play_gap_sec 一起写 0，见 pet/play.py）
    play_gap_sec: float = 30.0     # 串门时两只隔多久自动互动一次（互动频率；0 = 不自动玩）
    timeout: float = 8.0           # 一次网络请求等多久（秒）
    max_chars: int = 60            # 访客嘴里每句话最长几个字
    book: str = "friends.json"     # 好友簿 + 我自己的名片
    guest_dir: str = "data/guests"  # 收下来的访客形象帧放这儿
    log: str = "data/visits.json"  # 串门流水：谁来过、谁去过、说了什么


@dataclass
class Config:
    provider: str = "mock"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    system_prompt: str = ""  # 留空则由 persona.py 生成
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    ocr: OcrConfig = field(default_factory=OcrConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    hotkey: HotkeyConfig = field(default_factory=HotkeyConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    episode: EpisodeConfig = field(default_factory=EpisodeConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    care: CareConfig = field(default_factory=CareConfig)
    taste: TasteConfig = field(default_factory=TasteConfig)
    learn: LearnConfig = field(default_factory=LearnConfig)
    study: StudyConfig = field(default_factory=StudyConfig)
    friends: FriendConfig = field(default_factory=FriendConfig)

    # ---------- 读写 ----------

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        p = Path(path) if path else CONFIG_PATH
        data: Dict[str, Any] = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:  # 配置写坏了也不能让程序起不来
                print(f"[config] 读取 {p} 失败，改用默认配置：{exc}")
        if not isinstance(data, dict):
            data = {}

        cfg = cls(
            provider=str(data.get("provider") or "mock"),
            base_url=str(data.get("base_url") or ""),
            model=str(data.get("model") or ""),
            api_key=str(data.get("api_key") or ""),
            system_prompt=str(data.get("system_prompt") or ""),
            capture=_build(CaptureConfig, data.get("capture")),
            ui=_build(UIConfig, data.get("ui")),
            persona=_build(PersonaConfig, data.get("persona")),
            ocr=_build(OcrConfig, data.get("ocr")),
            memory=_build(MemoryConfig, data.get("memory")),
            hotkey=_build(HotkeyConfig, data.get("hotkey")),
            chat=_build(ChatConfig, data.get("chat")),
            asr=_build(AsrConfig, data.get("asr")),
            watch=_build(WatchConfig, data.get("watch")),
            episode=_build(EpisodeConfig, data.get("episode")),
            proactive=_build(ProactiveConfig, data.get("proactive")),
            care=_build(CareConfig, data.get("care")),
            taste=_build(TasteConfig, data.get("taste")),
            learn=_build(LearnConfig, data.get("learn")),
            study=_build(StudyConfig, data.get("study")),
            friends=_build(FriendConfig, data.get("friends")),
        )
        cfg.apply_preset()

        env_key = (os.environ.get("PET_API_KEY") or "").strip()
        if env_key:
            cfg.api_key = env_key
        cfg.source_path = str(p)  # type: ignore[attr-defined]
        return cfg

    def apply_preset(self) -> None:
        """按 provider 补齐 base_url / model（用户显式写了的就不覆盖）。"""
        preset = PROVIDER_PRESETS.get(self.provider)
        if not preset:
            return
        if not self.base_url:
            self.base_url = preset["base_url"]
        if not self.model:
            self.model = preset["model"]

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    def save(self, path: Optional[Path] = None) -> Optional[Path]:
        data = self.to_dict()
        # 命令行临时覆盖的项，写文件时恢复成原值（见 apply_override）
        for dotted, original in getattr(self, "restore_on_save", {}).items():
            section, _, key = dotted.partition(".")
            if key and isinstance(data.get(section), dict):
                data[section][key] = original
            else:
                data[section] = original

        if path is None:
            source = getattr(self, "source_path", "")
            if not source:
                # 不是从文件读出来的配置（比如测试里临时 Config()），
                # 没给路径就绝不落盘，免得手滑覆盖掉人家真正的 config.json
                print("[config] 这份配置没有来源文件，未指定路径，已跳过保存")
                return None
            path = source

        p = Path(path)
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p

    def config_path(self) -> Path:
        return Path(getattr(self, "source_path", CONFIG_PATH))

    @property
    def ready(self) -> bool:
        """mock 模式不需要 Key；其它 provider 必须有 Key。"""
        if self.provider == "mock":
            return True
        return bool(self.api_key.strip())

    @property
    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/chat/completions"


def _build(cls, data: Any):
    if not isinstance(data, dict):
        return cls()
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def apply_override(cfg: Config, field: str, value: Any) -> None:
    """命令行临时覆盖某一项（支持 "capture.interval_sec" 这种写法）。

    覆盖的值只在本次运行生效，退出时 save() 会写回原值，
    免得"这次带了个 --interval 4"被永久写进配置文件。
    """
    section, _, key = field.partition(".")
    if not key:
        original = getattr(cfg, section, None)
        restore = getattr(cfg, "restore_on_save", None)
        if restore is None:
            restore = cfg.restore_on_save = {}
        restore.setdefault(section, original)
        setattr(cfg, section, value)
        return

    target = getattr(cfg, section, None)
    if target is None:
        return
    original = getattr(target, key, None)
    restore = getattr(cfg, "restore_on_save", None)
    if restore is None:
        restore = cfg.restore_on_save = {}
    restore.setdefault(field, original)
    setattr(target, key, value)
