"""视觉大模型客户端。

所有 provider 都走 OpenAI 兼容的 `POST {base_url}/chat/completions`，
调用方只关心：
    describe(image, ...) -> Optional[mood.Comment]   一句话 + 情绪；None 表示"这次选择沉默"
    summarize(prompt)    -> str                      纯文本总结（用来生成长期记忆画像）

provider == "mock" 时不联网，按画面亮度/主色调凑一句"看法"，
方便你先把形象和交互调好，再填 API Key。
"""
from __future__ import annotations

import random
import time
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from PIL import Image

from . import capture, humanstyle, mood, persona, proactive, scene as scene_mod, watchlog
from .mood import Comment


class VlmError(RuntimeError):
    """接口调用失败（网络、鉴权、限流、返回格式异常等）。"""


# (情绪, 台词)
_MOCK_LINES: Dict[str, List[Comment]] = {
    "dark": [Comment("这也太黑了吧，我眼睛都要瞎了", "speechless"),
             Comment("画面暗得像停服了，兄弟", "speechless")],
    "bright": [Comment("白得发光，是在看雪景吗", "curious"),
               Comment("这亮度，我怀疑你屏幕要裸奔了", "speechless")],
    "warm": [Comment("满屏暖色，看着还挺上头", "happy"),
             Comment("这色调，像刚出锅的", "smirk")],
    "green": [Comment("绿油油一片，打野的快乐", "excited"),
              Comment("这绿色，健康得我都想种地了", "happy")],
    "cool": [Comment("蓝调画面，气氛拉满", "smirk"),
             Comment("冷色调，配你现在的表情刚好", "smirk")],
    "gray": [Comment("好素啊，我在旁边都困了", "speechless"),
             Comment("这画面，静止得像屏保", "speechless")],
}

# 主动搭话的假台词（离线模式用，也让整条链路能跑通）
_MOCK_NUDGES: Dict[str, List[Comment]] = {
    proactive.STILL_SCREEN: [
        Comment("这画面定住半天了，你是不是去倒水了", "curious"),
        Comment("一动不动，我差点以为你睡过去了", "curious"),
    ],
    proactive.LONG_QUIET: [
        Comment("就一直看着不说话？跟我聊聊呗", "curious"),
        Comment("你倒是说点什么呀，我在这儿陪你看呢", "happy"),
    ],
    proactive.MEMORY_TOPIC: [
        Comment("你最近老看这个，是不是上瘾了", "curious"),
        Comment("又是这个啊，你上次看的也是它", "smirk"),
    ],
    "manual": [
        Comment("喏，我自己来找你了", "happy"),
        Comment("闲着也是闲着，陪你唠两句", "smirk"),
    ],
    # 按"他正在干嘛"主动夸 / 主动关心（离线模式也走一遍）
    proactive.PRAISE: [
        Comment("你这一下真行，我早想说了", "happy"),
        Comment("看你弄这个我挺服气", "happy"),
    ],
    proactive.CARE_TOPIC: [
        Comment("坐挺久了吧，起来动动", "speechless"),
        Comment("别一直盯着，眼睛也歇会儿", "speechless"),
    ],
}


# 两个"必须开腔"场合的假台词（离线模式）：刷到新视频 / 他刚点赞收藏关注
_MOCK_SPOTLIGHT: Dict[str, List[Comment]] = {
    "new_video": [
        Comment("新的一支，我先看上了", "curious"),
        Comment("哟，换片子了，这个我盯住了", "happy"),
    ],
    "action": [
        Comment("哟，还点赞了？看来是戳中了", "smirk"),
        Comment("你都动手了，那我记一笔", "happy"),
    ],
}


# 用户直接跟它说话时的假回应（离线模式用，也让"对话"这条链路能跑通）
_MOCK_REPLIES: Dict[str, List[Comment]] = {
    "greet": [
        Comment("诶，我在呢", "happy"),
        Comment("在的在的，正陪你看着", "happy"),
    ],
    "ask": [
        Comment("你问我？我看他就是图那一下爽", "curious"),
        Comment("这我真说不准，看着玩呗", "curious"),
    ],
    "bye": [
        Comment("行，那我眯一会儿，有事喊我", "speechless"),
    ],
    "default": [
        Comment("嗯，我盯着呢，你接着说", "smirk"),
        Comment("哈哈行，那咱接着看", "happy"),
        Comment("你这么一说还真是", "curious"),
        Comment("懂了，那我陪你多看会儿", "happy"),
    ],
    # 他在诉苦（humanstyle.needs_comfort 认出来的）：这一轮只安慰 + 抱抱
    "comfort": [
        Comment("我懂，这事搁谁身上都堵。来，抱抱", "happy"),
        Comment("别一个人扛着，我在呢，抱抱 ❤", "happy"),
        Comment("先缓缓，不急着好起来，我陪你。抱抱", "happy"),
    ],
}

# 关键词 -> 上面那几种口气（判断顺序：先打招呼，再提问，最后道别）
_MOCK_REPLY_KEYS: Dict[str, Tuple[str, ...]] = {
    "greet": ("你好", "在吗", "嗨", "哈喽", "喂", "hello", "hi"),
    "ask": ("?", "？", "吗", "什么", "怎么", "为啥", "为什么", "哪个", "是不是", "能不能"),
    "bye": ("拜拜", "再见", "不看了", "我睡了", "晚安"),
}


# 串门台词（离线模式用）：kind 决定这是"哪一场"——
#   meet  刚进门 / 刚见面时的招呼
#   reply 对方主人跟我搭话，我回一句
#   pet   跟对方那只宠物说话
#   bye   告辞、回家
_MOCK_VISIT: Dict[str, List[str]] = {
    "meet": [
        "你好呀，我从隔壁那台电脑过来的",
        "打扰啦，我就待一小会儿，看看你这儿什么样",
        "诶，这就是你主人的屏幕啊，挺亮",
    ],
    "reply": [
        "哈哈，我是自己溜达过来的，别嫌弃我",
        "我主人这会儿不在，我就到处转转",
        "你问我啊？我懂的东西都是跟着我主人看来的",
        "这话我得记着，回去讲给我主人听",
    ],
    "pet": [
        "你家主人平时都看些什么呀",
        "你这边屏幕比我们那边忙多了，我眼睛都跟不上",
        "回头你也来我们那边坐坐，我给你留个位置",
        "没想到还有跟我一样的，咱俩握个手",
    ],
    "bye": [
        "我得回去啦，我主人一会儿该找我了",
        "走啦，下次再来找你玩",
        "回去啦，今天这趟没白来",
    ],
    "greet_guest": [
        "来啦？随便坐，我这儿就屏幕这点地方",
        "哟，隔壁家的？难得有人来串门",
        "你也是自己跑过来的吧，我看着眼熟",
    ],
    "see_off": [
        "走吧走吧，路上稳当点",
        "下次带点你那边的稀奇事过来",
        "回见，门给你留着",
    ],
    "carry": [
        "我主人刚说：欢迎你，别拘束",
        "我主人让我带句话——你随意待着就行",
        "我主人说，你家那只也挺可爱",
    ],
    "poke": [
        "哎，干嘛戳我",
        "在呢在呢，别戳了",
        "有事说事，别动手动脚的",
    ],
    "hello_owner": [
        "叔叔好！我自己溜达过来的，不添乱",
        "打扰啦，我就在这儿待一小会儿",
        "你好呀，你家这只比我干净多了",
    ],
    "doing": [
        "我刚打听到，你那边主人这会儿正忙着呢",
        "我跟你说，人家主人刚还在看东西，这会儿又忙上了",
        "打听回来了：那边主人这会儿手上正有活",
    ],
    "play": [
        "来呀，一起闹一下",
        "这下的交情，够我记一路了",
        "接着来接着来，别停",
    ],
}


class VisionClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._session = requests.Session()
        self._mock_index = 0
        self._mock_last = ""
        self._mock_image: Optional[Image.Image] = None
        self._echo_sources: Tuple[str, ...] = ()   # 这次发出去的提示词（识别"抄提示词"用）
        # 这一次发出去的**画面上的字**（OCR + 本地认出来的关键信息）。
        # 只用来判断"这句是不是在念屏幕"——不能拿整份提示词当池子（那样什么字都有）。
        self._screen_text = ""
        self.last_drop = ""        # 上一句为什么被闸掉了（""/"echo"/"narration"/"internal"/"title"/"meta"），worker 靠它决定要不要补一句

    @property
    def ready(self) -> bool:
        return self.cfg.ready

    # ---------- 对外 ----------

    def describe(
        self,
        image: Image.Image,
        history: Sequence[str] = (),
        ocr_text: str = "",
        window: str = "",
        memory: str = "",
        clock: str = "",
        scene: str = "",
        watch: str = "",
        avoid: Sequence[str] = (),
        frames: int = 1,
        taste: str = "",
        insist: bool = False,
        episode: str = "",
    ) -> Optional[Comment]:
        """返回一句吐槽；返回 None 表示"这次没什么好说的"。

        scene 是上一眼看明白的 5W1H（scene.Scene.line() 的字符串），
        带上去是为了"同一件事别重复说"；watch 是当前这支视频的看片笔记（一行版）。
        avoid 是它最近刚说过的几句——重试那一轮拿来堵住"换汤不换药"的重复。
        frames 是这一张图里塞了几眼（>1 就是连拍分镜图，提示词里会说明）。
        insist=True 是"补说"那一轮：上一轮它只交了场景行（或把画面上的字抄了回来），
        这一轮提示词里把话说死——必须给出第一行台词。
        """
        if self.cfg.provider == "mock":
            return self._mock_comment(image)
        return self._remote_comment(
            image, history, ocr_text, window, memory, clock, scene, watch, avoid, frames, taste,
            insist, episode,
        )

    def absorb(
        self,
        image: Image.Image,
        ocr_text: str = "",
        window: str = "",
        clock: str = "",
        frames: int = 1,
        previous: str = "",
    ) -> Optional[watchlog.WatchNote]:
        """换视频那一轮：不起哄、不说话，先把这一眼**读明白**存成看片笔记。

        previous 非空表示"同一支视频看久了重读一遍"，把上一遍的笔记一起给它。
        """
        if self.cfg.provider == "mock":
            return self._mock_absorb(image)
        return self._remote_absorb(image, ocr_text, window, clock, frames, previous)

    def reply(
        self,
        user_text: str,
        image: Optional[Image.Image] = None,
        history: Sequence[str] = (),
        chat_history=(),
        ocr_text: str = "",
        window: str = "",
        memory: str = "",
        clock: str = "",
        scene: str = "",
        watch: str = "",
        avoid: Sequence[str] = (),
        frames: int = 1,
        taste: str = "",
        comfort: bool = False,
        dialog_hint: str = "",
        episode: str = "",
    ) -> Optional[Comment]:
        """用户直接跟它说话（打字/语音）那一轮：必须接话，返回 None 代表真的答不上来。

        comfort=True 是他这句在诉苦：这一轮只安慰 + 抱抱，别接梗别讲道理。
        dialog_hint 是他这句话的类型推出来的提醒（记录/分析/学习，见 dialog.py），
        只影响这一轮的措辞，不会出现在画面上。
        """
        text = (user_text or "").strip()
        if not text:
            return None
        if self.cfg.provider == "mock":
            return self._mock_reply(text, comfort=comfort)
        return self._remote_reply(
            text, image, history, chat_history, ocr_text, window, memory, clock, scene, watch,
            avoid, frames, taste, comfort, dialog_hint, episode,
        )

    def nudge(
        self,
        kind: str,
        image: Optional[Image.Image] = None,
        history: Sequence[str] = (),
        ocr_text: str = "",
        window: str = "",
        memory: str = "",
        note: str = "",
        topic: str = "",
        clock: str = "",
        scene: str = "",
        watch: str = "",
        avoid: Sequence[str] = (),
        frames: int = 1,
        taste: str = "",
        episode: str = "",
    ) -> Optional[Comment]:
        """主动搭话：换一套提示词让它自己找话题。None 表示它还是没开口（调用方用本地台词兜底）。"""
        if self.cfg.provider == "mock":
            return self._mock_nudge(kind)
        return self._remote_nudge(
            kind, image, history, ocr_text, window, memory, note, topic, clock, scene, watch,
            avoid, frames, taste, episode,
        )

    def spotlight(
        self,
        kind: str,
        note: str = "",
        action: str = "",
        image: Optional[Image.Image] = None,
        history: Sequence[str] = (),
        ocr_text: str = "",
        window: str = "",
        memory: str = "",
        clock: str = "",
        scene: str = "",
        watch: str = "",
        taste: str = "",
        frames: int = 1,
        episode: str = "",
    ) -> Optional[Comment]:
        """两个"必须开腔"的场合（像群聊小助手那样有内容就接一句）：

            kind="new_video"  刚刷到一支新视频，笔记也读完了 → 先接一句
            kind="action"     他刚点赞/收藏/关注了（action 里写清是哪个）→ 就着这事说一句
        """
        if self.cfg.provider == "mock":
            return self._mock_spotlight(kind, action)
        return self._remote_spotlight(
            kind, note, action, image, history, ocr_text, window, memory, clock, scene, watch,
            taste, frames, episode,
        )

    def episode_brief(self, show: str, season: str = "", episode: str = "", extra: str = "") -> str:
        """给"这一集"做功课：**纯文本**问一次（不发图），拿回它的梗概/人物/看点。

        这是"扒原片"能落地的那一半（见 pet/episode.py）：不下载整集视频，
        只把这一集是什么内容弄到手。同一集只会问一次（episode.EpisodeLog 会缓存）。
        """
        if self.cfg.provider == "mock":
            return self._mock_episode(show, season, episode)
        if not self.ready:
            raise VlmError("还没有配置 API Key，没法给这一集做功课。")
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": persona.episode_system_prompt()},
                {"role": "user", "content": persona.episode_prompt(show, season, episode, extra)},
            ],
            "temperature": 0.3,
            "max_tokens": 300,
        }
        return self._post(payload).strip()

    def visit_line(
        self,
        system: str,
        prompt: str,
        kind: str = "meet",
        temperature: float = 0.85,
        max_tokens: int = 120,
    ) -> str:
        """串门专用的一次**纯文本**调用（见 pet/friends.py），返回一句台词或空串。

        跟 summarize 的区别：这个要的是"角色在说话"，所以温度高一点、字数由提示词
        定死；跟 reply 的区别：串门不涉及我这台机器上的画面，**一个字都不许带过去**，
        所以这里根本不给它图片，也不带 OCR / 记忆（隐私边界就在这一层焊死）。

        kind 只是给离线模式挑台词用的，不影响联网时怎么问（那由 system/prompt 决定）。
        """
        if self.cfg.provider == "mock":
            return self._mock_visit(kind)
        if not self.ready:
            raise VlmError("还没有配置 API Key，串门的时候开不了口。")
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        return self._post(payload).strip()

    def summarize(self, prompt: str, max_tokens: int = 400) -> str:
        """纯文本总结（生成长期记忆里的"观众画像"）。"""
        if self.cfg.provider == "mock":
            return self._mock_summary(prompt)
        if not self.ready:
            raise VlmError("还没有配置 API Key，无法总结记忆。")

        payload = {
            "model": self.cfg.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个帮陪伴型 AI 维护观众画像的助手，说话具体、简短、不啰嗦。",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": int(max_tokens),
        }
        return self._post(payload).strip()


    # ---------- 真实接口 ----------

    def _remote_comment(
        self,
        image: Image.Image,
        history: Sequence[str],
        ocr_text: str,
        window: str,
        memory: str,
        clock: str = "",
        scene: str = "",
        watch: str = "",
        avoid: Sequence[str] = (),
        frames: int = 1,
        taste: str = "",
        insist: bool = False,
        episode: str = "",
    ) -> Optional[Comment]:
        if not self.ready:
            raise VlmError("还没有配置 API Key，请编辑 config.json 或设置环境变量 PET_API_KEY。")

        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": persona.system_prompt(self.cfg, avoid=avoid, insist=insist)},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + capture.to_jpeg_base64(image, self.cfg.capture.jpeg_quality)
                            },
                        },
                        {
                            "type": "text",
                            "text": persona.user_prompt(
                                self.cfg,
                                history=history,
                                ocr_text=ocr_text,
                                window=window,
                                memory=memory,
                                clock=clock,
                                scene=scene,
                                watch=watch,
                                frames=frames,
                                taste=taste,
                                insist=insist,
                                episode=episode,
                            ),
                        },
                    ],
                },
            ],
            "temperature": 0.95,
            # 两行输出（台词 + 场景）比原来长，留足 token，别被截断成半句
            "max_tokens": 220,
        }
        self._screen_text = ocr_text or ""     # 念屏幕判定只用它（见 _to_comment）
        raw = self._post(payload)
        return self._to_comment(raw)

    def _remote_absorb(
        self,
        image: Image.Image,
        ocr_text: str,
        window: str,
        clock: str = "",
        frames: int = 1,
        previous: str = "",
    ) -> Optional[watchlog.WatchNote]:
        """换视频那一轮：只要一份看片笔记（温度低一点，别让它写诗）。"""
        if not self.ready:
            raise VlmError("还没有配置 API Key，请编辑 config.json 或设置环境变量 PET_API_KEY。")

        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": persona.absorb_system_prompt(self.cfg)},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64,"
                                + capture.to_jpeg_base64(image, self.cfg.capture.jpeg_quality)
                            },
                        },
                        {
                            "type": "text",
                            "text": persona.absorb_prompt(
                                self.cfg,
                                ocr_text=ocr_text,
                                window=window,
                                clock=clock,
                                frames=frames,
                                previous=previous,
                            ),
                        },
                    ],
                },
            ],
            "temperature": 0.4,
            "max_tokens": 360,
        }
        raw = self._post(payload)
        note = watchlog.parse_note(raw, at=time.time())
        return None if note.is_empty() else note

    def _remote_nudge(
        self,
        kind: str,
        image: Optional[Image.Image],
        history: Sequence[str],
        ocr_text: str,
        window: str,
        memory: str,
        note: str,
        topic: str = "",
        clock: str = "",
        scene: str = "",
        watch: str = "",
        avoid: Sequence[str] = (),
        frames: int = 1,
        taste: str = "",
        episode: str = "",
    ) -> Optional[Comment]:
        """主动搭话那一轮。提示词不同，输出格式照旧（[情绪] 台词）。"""
        if not self.ready:
            raise VlmError("还没有配置 API Key，请编辑 config.json 或设置环境变量 PET_API_KEY。")

        content: List[Dict[str, object]] = []
        if image is not None:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64,"
                        + capture.to_jpeg_base64(image, self.cfg.capture.jpeg_quality)
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": persona.proactive_prompt(
                    self.cfg,
                    kind,
                    note=note,
                    history=history,
                    ocr_text=ocr_text,
                    window=window,
                    memory=memory,
                    topic=topic,
                    clock=clock,
                    scene=scene,
                    watch=watch,
                    frames=frames,
                    taste=taste,
                    episode=episode,
                ),
            }
        )
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": persona.system_prompt(self.cfg, proactive=True, avoid=avoid, kind=kind)},
                {"role": "user", "content": content},
            ],
            "temperature": 1.0,
            "max_tokens": 200,
        }
        self._screen_text = ocr_text or ""
        raw = self._post(payload)
        return self._to_comment(raw, f"proactive:{kind}")

    def _remote_spotlight(
        self,
        kind: str,
        note: str,
        action: str,
        image: Optional[Image.Image],
        history: Sequence[str],
        ocr_text: str,
        window: str,
        memory: str,
        clock: str = "",
        scene: str = "",
        watch: str = "",
        taste: str = "",
        frames: int = 1,
        episode: str = "",
    ) -> Optional[Comment]:
        """刷到新视频 / 他刚点赞收藏关注：这一轮必须开腔，而且要短。"""
        if not self.ready:
            raise VlmError("还没有配置 API Key，请编辑 config.json 或设置环境变量 PET_API_KEY。")

        content: List[Dict[str, object]] = []
        if image is not None:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64,"
                        + capture.to_jpeg_base64(image, self.cfg.capture.jpeg_quality)
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": persona.spotlight_prompt(
                    self.cfg,
                    kind,
                    note=note,
                    action=action,
                    history=history,
                    ocr_text=ocr_text,
                    window=window,
                    memory=memory,
                    clock=clock,
                    scene=scene,
                    watch=watch,
                    taste=taste,
                    frames=frames,
                    episode=episode,
                ),
            }
        )
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": persona.system_prompt(self.cfg, proactive=True)},
                {"role": "user", "content": content},
            ],
            "temperature": 1.0,
            "max_tokens": 180,
        }
        self._screen_text = ocr_text or ""
        raw = self._post(payload)
        return self._to_comment(raw, f"spotlight:{kind}")

    def _remote_reply(
        self,
        user_text: str,
        image: Optional[Image.Image],
        history: Sequence[str],
        chat_history,
        ocr_text: str,
        window: str,
        memory: str,
        clock: str = "",
        scene: str = "",
        watch: str = "",
        avoid: Sequence[str] = (),
        frames: int = 1,
        taste: str = "",
        comfort: bool = False,
        dialog_hint: str = "",
        episode: str = "",
    ) -> Optional[Comment]:
        """用户直接说话那一轮：必须接话（提示词里已经说死不许沉默）。"""
        if not self.ready:
            raise VlmError("还没有配置 API Key，请编辑 config.json 或设置环境变量 PET_API_KEY。")

        content: List[Dict[str, object]] = []
        if image is not None:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/jpeg;base64,"
                        + capture.to_jpeg_base64(image, self.cfg.capture.jpeg_quality)
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": persona.chat_user_prompt(
                    self.cfg,
                    user_text,
                    history=history,
                    chat_history=chat_history,
                    ocr_text=ocr_text,
                    window=window,
                    memory=memory,
                    clock=clock,
                    scene=scene,
                    watch=watch,
                    frames=frames,
                    taste=taste,
                    comfort=comfort,
                    dialog_hint=dialog_hint,
                    episode=episode,
                ),
            }
        )
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": persona.chat_system_prompt(self.cfg, avoid=avoid, comfort=comfort)},
                {"role": "user", "content": content},
            ],
            "temperature": 0.85,
            "max_tokens": 260,
        }
        self._screen_text = ocr_text or ""
        raw = self._post(payload)
        return self._to_comment(raw, "chat")

    def _to_comment(self, raw: str, kind: str = "") -> Optional[Comment]:
        """模型文本 → Comment（先剥掉「场景」行，再处理沉默、情绪标签、超长截断）。"""
        self.last_drop = ""
        speech, parsed = scene_mod.split(raw)
        if speech and humanstyle.looks_like_echo(speech, self._echo_sources):
            # 小模型把提示词里的说明句当台词抄回来了：这种句子出现在气泡里最像是"它坏了"
            print(f"[noise] 模型把提示词抄回来了，这句丢掉：{speech[:44]}")
            speech = ""
        if speech:
            # 先把开头的「画面关键信息：」这种块头剥掉——后面那句可能是好的
            stripped = humanstyle.strip_internal_prefix(speech)
            if stripped != speech:
                # 这一句开头就是我们发过去的块头 → 它多半在念我们给它的东西。
                # 剥完再看剩下的是什么：一串标签？还是"每个字画面上都写着"？
                if humanstyle.looks_like_info_dump(stripped) or humanstyle.looks_like_screen_echo(
                    stripped, (self._screen_text,)
                ):
                    print(f"[internal] 这句是在念屏幕上的东西，不要：{stripped[:44]}")
                    self.last_drop = "internal"
                    stripped = ""
            speech = stripped
        if speech and humanstyle.looks_like_internal(speech):
            # 还夹着我们发给它的内部字眼（块头 / 抓来的资料标题）：这些只该它自己看，
            # 用户屏幕上只该出现"说给用户听的那句话"，所以整句不要，让 worker 再要一句。
            print(f"[internal] 这句把内部信息念出来了，不要：{speech[:44]}")
            self.last_drop = "internal"
            speech = ""
        if speech and humanstyle.looks_like_title(speech):
            # 交的是**页面上抓来的标题**（现场气泡：「《复仇者联盟3》剧情设定细节探案幕后解读」）：
            # 这是抓来的信息，不是它要说的话——跟块头同一条道理，只该它自己看。
            # 整句不要，让 worker 当场再要一句。
            print(f"[title] 这句是抓来的标题，不要：{speech[:44]}")
            self.last_drop = "title"
            speech = ""
        if speech and humanstyle.looks_like_meta_talk(speech):
            # 交的是**它自己那套机器的话**（"我刚学到一个知识点：…"、念语料、念资料卡）：
            # 屏幕上要的是个陪看的朋友，不是一台会汇报自己进度的机器。整句不要，当场再要一句。
            print(f"[meta] 这句把自己的机器说出来了，不要：{speech[:44]}")
            self.last_drop = "meta"
            speech = ""
        if speech and humanstyle.looks_like_narration(speech):
            # 交的是**解说词**（"某某和某某站在…前，似乎是在参与某个环节"）：
            # 气泡里宁可先空着，让 worker 立刻再要一句，也别把镜头说明书贴给用户看。
            print(f"[narration] 这句是解说词，不要：{speech[:44]}")
            self.last_drop = "narration"
            speech = ""
        if speech and humanstyle.looks_like_screen_caption(speech):
            # 交的是**机器识图 / 截屏描述**（现场气泡：「电脑屏幕截图，包含Visual Studio Code
            # 编辑器界面和一些中文文本」）：这是它自己"收到了怎样一张图"的过程，不是人该说的话。
            # 用户要看的是个陪看的朋友，不是识图日志——整句不要，让 worker 当场再要一句。
            print(f"[screen] 这句是屏幕识图描述，不要：{speech[:44]}")
            self.last_drop = "screen"
            speech = ""
        if speech and scene_mod.is_label_only(speech):
            # 只剩个标签 / 占位词（「场景」「未知」这种）：屏幕上不该出现这种东西，
            # 它既不是吐槽也不是回答。这一轮就当它没说（现场气泡里就出现过这两个字）。
            print(f"[label] 这句只有个标签，不是话：{speech[:44]}")
            self.last_drop = "label"
            speech = ""
        if not speech:
            if not parsed.is_empty() and self.last_drop != "narration":
                # 只给了场景没给台词：这轮就当它没说话，日志里留一笔方便排查提示词
                print(f"[scene] 模型只回了场景没给台词：{parsed.brief()}")
            return None
        reply = persona.clean_reply(speech, self.cfg.persona.name)
        if persona.is_silence(reply):
            if not parsed.is_empty():
                # 合规的沉默（第一行 [沉默] + 第二行场景）：这是它自己选择不说话，跟"白说一轮"不是一回事
                print(f"[quiet] 这一轮它自己选择不说话（场景照记）：{parsed.brief()}")
            return None
        comment = mood.split_mood(reply)
        if not comment.text:
            return None
        if kind == "chat":  # 对话比吐槽长一点
            limit = int(getattr(self.cfg.chat, "max_chars", 90)) + 20
        else:
            limit = int(self.cfg.persona.max_chars) + 12
        return Comment(
            persona.fit(comment.text, limit),
            comment.mood,
            kind,
            None if parsed.is_empty() else parsed,
        )

    def _post(self, payload: dict, timeout: float = 0.0) -> str:
        """统一的 POST + 错误处理，返回模型文本。"""
        # 顺手把这次发出去的文字留一份：用来识别"模型把提示词抄回来当台词"的情况
        self._echo_sources = self._payload_texts(payload)
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._session.post(
                self.cfg.endpoint,
                json=payload,
                headers=headers,
                timeout=float(timeout or self.cfg.capture.request_timeout),
            )
        except requests.RequestException as exc:
            raise VlmError(f"请求失败：{exc}") from exc

        if resp.status_code >= 400:
            snippet = (resp.text or "")[:200].replace("\n", " ")
            raise VlmError(f"接口返回 {resp.status_code}：{snippet}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            snippet = (resp.text or "")[:200].replace("\n", " ")
            raise VlmError(f"返回格式看不懂：{snippet}") from exc

        if isinstance(content, list):  # 有的模型返回分段内容
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content or "")

    @staticmethod
    def _payload_texts(payload: dict) -> Tuple[str, ...]:
        """把这条请求里所有文字内容抽出来（图片不算），给"是不是抄了提示词"当参照。"""
        texts: List[str] = []
        for message in payload.get("messages") or ():
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(str(part.get("text") or ""))
        return tuple(texts)

    # ---------- 离线假想模式 ----------

    def _mock_scene(self, image: Optional[Image.Image]) -> Optional[scene_mod.Scene]:
        """离线模式的假场景：按主色调编一个，让 5W1H 这条链路（记忆/提示词）也能跑通。"""
        if image is None:
            return None
        hue = capture.dominant_hue(image)
        who = {"dark": "看不清的人影", "bright": "白花花一片里的东西"}.get(hue, "画面里的人")
        what = {
            "dark": "画面黑得像停了电",
            "bright": "亮得晃眼的一片",
            "warm": "暖色调的内容一直在动",
            "green": "绿油油的画面在动",
            "cool": "冷色调的画面在动",
            "gray": "灰蒙蒙的画面几乎不动",
        }.get(hue, "画面在动")
        return scene_mod.Scene(who=who, what=what, when="现在", where="屏幕", why="未知")

    def _mock_absorb(self, image: Image.Image) -> Optional[watchlog.WatchNote]:
        """离线模式的假看片笔记：按主色调编一份，让"读视频 → 聊视频"这条链路也能跑通。"""
        hue = capture.dominant_hue(image)
        table = {
            "dark": ("画面黑得像停了电，看不清在放什么", "暗色调的片子一直在动"),
            "bright": ("白花花一片，像是在雪地或者医院", "亮得晃眼的东西在动"),
            "warm": ("暖色调的内容，看着挺热闹", "一堆暖色的画面在动"),
            "green": ("绿油油一片，像是游戏里的草地或野外", "绿色的画面一直在动"),
            "cool": ("冷色调的片子，气氛偏静", "冷色调的画面在动"),
            "gray": ("灰蒙蒙的片子，画面几乎没动", "几乎静止的一块灰"),
        }
        what, detail = table.get(hue, ("看不清在放什么", "画面在动"))
        return watchlog.WatchNote(
            at=time.time(),
            title=f"一段{hue}色调的视频（离线演示模式）",
            what=f"{detail}。（mock 模式不联网，这里是按画面主色调编的，配了 API Key 就是真读出来的内容）",
            point="离线模式随便找个点聊",
            who="未知",
            where="屏幕",
            keywords=[hue, "演示模式", "离线"],
        )

    def _mock_reply(self, user_text: str, comfort: bool = False) -> Optional[Comment]:
        """离线模式下的对话：按关键词挑一句像样的回应（真配了 Key 就走模型）。

        comfort=True（他在诉苦）时直接走"安慰 + 抱抱"那一池，不按关键词挑。
        """
        self.last_drop = ""
        text = (user_text or "").lower()
        kind = "comfort" if comfort else "default"
        if not comfort:
            for name, keys in _MOCK_REPLY_KEYS.items():
                if any(key in text for key in keys):
                    kind = name
                    break
        pool = _MOCK_REPLIES.get(kind) or _MOCK_REPLIES["default"]
        choices = [c for c in pool if c.text != self._mock_last] or pool
        picked = random.choice(choices)
        self._mock_last = picked.text
        return Comment(picked.text, picked.mood, "chat", self._mock_scene(self._mock_image))

    def _mock_comment(self, image: Image.Image) -> Optional[Comment]:
        self.last_drop = ""
        self._mock_index += 1
        self._mock_image = image
        if self._mock_index % 4 == 0:  # 偶尔沉默，顺便演示"没话说就不说话"
            return None
        pool = _MOCK_LINES.get(capture.dominant_hue(image), _MOCK_LINES["gray"])
        choices = [c for c in pool if c.text != self._mock_last] or pool
        picked = random.choice(choices)
        self._mock_last = picked.text
        return Comment(picked.text, picked.mood, "", self._mock_scene(image))

    def _mock_nudge(self, kind: str) -> Optional[Comment]:
        """离线模式的主动搭话：按由头挑一句。主动开口就不沉默了。"""
        self.last_drop = ""
        pool = _MOCK_NUDGES.get(kind) or _MOCK_NUDGES["manual"]
        choices = [c for c in pool if c.text != self._mock_last] or pool
        picked = random.choice(choices)
        self._mock_last = picked.text
        return Comment(picked.text, picked.mood, f"proactive:{kind}")

    def _mock_spotlight(self, kind: str, action: str = "") -> Optional[Comment]:
        """离线模式的"必须有话"场合：刷到新视频 / 他刚点赞收藏关注。"""
        self.last_drop = ""
        pool = _MOCK_SPOTLIGHT.get(kind) or _MOCK_SPOTLIGHT["new_video"]
        choices = [c for c in pool if c.text != self._mock_last] or pool
        picked = random.choice(choices)
        self._mock_last = picked.text
        return Comment(picked.text, picked.mood, f"spotlight:{kind}")

    def _mock_episode(self, show: str, season: str, episode: str) -> str:
        """离线模式下的"整集功课"：编一张像样的资料卡，让这条链路也能跑通。"""
        title = " ".join(part for part in (show, season, episode) if part)
        return (
            f"梗概：{title or '这一集'}里几个人分组做任务，中间有人笑场，"
            "也有人被规则整不会了。（mock 模式不联网，这是占位内容）\n"
            "人物：常驻嘉宾 + 飞行嘉宾若干\n"
            "看点：任务失败那一下、以及有人当场拆台\n"
        )

    def _mock_visit(self, kind: str) -> str:
        """离线模式的串门台词：按"哪一场"挑一句，保证这条链路能走通。"""
        pool = _MOCK_VISIT.get(kind) or _MOCK_VISIT["meet"]
        choices = [line for line in pool if line != self._mock_last] or pool
        picked = random.choice(choices)
        self._mock_last = picked
        return picked

    def _mock_summary(self, prompt: str) -> str:
        """离线模式的假画像，只为了让记忆链路能跑通。"""
        return (
            "画像：这个观众主要在刷短视频，游戏类内容看得最多，"
            "对精彩操作会兴奋、对重复刷屏的画面明显没耐心。\n"
            "标签：短视频, 游戏, 集锦, 直播"
        )
