"""好友陪伴：串门的"人事"部分——谁在哪台机器上、这句话该谁来说、什么时候回家。

分三层，别混：
    net.py     搬东西（名片 / HTTP 服务 + 客户端 / 形象帧）
    friends.py 规矩（本文件：好友簿、出访、接待、谁负责生成哪句台词）
    guest.py   屏幕上那只客人的样子
    doing.py   「我这边主人在忙什么」→ 一句能过网的话（本文件出访/打听都走它）
    play.py    两只凑一起能做什么动作（过网的名字 + 给动画留的口子）

**两台机器各自养各自的脑子**：我的宠物说哪句话，永远在我这台机器上调模型；
对方的宠物说哪句话，永远在它那台机器上生成。于是这些互动都能成立：

    好友本人 ⇄ 我的宠物     好友在他那边打字 → 传给我 → 我这边生成、传回去显示
    我 ⇄ 好友的宠物         我在这边打字/戳它 → 传给对方 → 它那边生成、传回来显示
    两只宠物互相聊           各说各的，一句一句往对方屏幕上送（有轮次上限）
    两只宠物一起玩           一边先起个头，两边同时播同一个动作（见 play.py）
    好友在干嘛               问一句，对方**本地**拼一句回过来（见 doing.py）

**不传的东西**（写在提示词里，也在这一层物理隔离）：截图、OCR、长期记忆、看片记录
一个字都不过网，串门只带「我是谁 + 我长什么样 + 我要说的这句 + 我主人大概在忙什么」。
最后那一点点见 doing.py 的三档尺度：默认只说"在打游戏/在看视频/在干活"这种大类，
**原始窗口标题一个字都不出去**。
"""
from __future__ import annotations

import json
import random
import string
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from . import doing as doing_mod
from . import net, persona
from . import play as play_mod
from .config import CONFIG_PATH, Config
from .mood import split_mood
from .vlm import VlmError, VisionClient

# 出访时"门口没人应"要等多久（对方可能没开程序、或者被你晾着）
KNOCK_TIMEOUT = 75.0
# 等对方点"同意"的时候，每隔多久问一次
KNOCK_POLL = 2.5
# 进门之后那几步（问候 / 打听在忙啥 / 玩一下）之间隔多久：别一口气挤在一块儿
OPENING_GAP_MIN_MS = 300

# 模型不给力时的兜底台词：宁可说句普通的，也不能让屏幕上冷冷清清
_FALLBACK = {
    "meet": ["[开心] 你好呀，我从隔壁那台电脑溜达过来的"],
    "reply": ["[好奇] 这话我得记下来，回去讲给我主人听"],
    "pet": ["[好奇] 你家主人平时都看些什么呀"],
    "greet_guest": ["[开心] 哟，来客人了，随便坐"],
    "see_off": ["[开心] 路上小心，下次再来玩"],
    "carry": ["[好奇] 我刚听我主人念叨了两句，就顺嘴跟你说了"],
    "poke": ["[好奇] 谁呀，别戳我"],
    "bye": ["[开心] 我得回去啦，下次再来找你玩"],
    "away": ["[开心] 我出门一趟，去朋友家坐坐"],
    "home": ["[开心] 我回来啦"],
    "hello_owner": ["[开心] 你好呀，我来玩一会儿，不添乱"],
    "doing": ["[好奇] 我刚打听到，你那边主人这会儿正忙着呢"],
    "play": ["[开心] 来，一起玩一下"],
}


def _log_line(entry: Dict[str, object], path: Path, limit: int = 200) -> None:
    """串门流水（谁来过、说了什么）——只写本地，不上传，也不进长期记忆。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows: List[Dict[str, object]] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    rows = [r for r in data if isinstance(r, dict)]
            except Exception:
                rows = []
        entry = dict(entry)
        entry.setdefault("at", time.time())
        rows.append(entry)
        path.write_text(json.dumps(rows[-limit:], ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _new_id(length: int = 6) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


class FriendBook:
    """好友簿：我自己的名片 + 每一位好友（地址、口令、上次来过的位置）。

    机器认的是 `pet_id`，不是名字、也不是 IP：所以对方改了宠物名、换了路由器，
    你这边还是同一个人，只是地址被更新了（见 remember）。
    """

    def __init__(self, path: Path, default_owner: str = "", default_nick: str = ""):
        self.path = Path(path)
        self.default_owner = default_owner
        self.default_nick = default_nick
        self.me: Dict[str, object] = {}
        self.friends: List[Dict[str, object]] = []
        self._load()

    # ---------- 读写 ----------

    def _load(self) -> None:
        data: Dict[str, object] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
            except Exception as exc:
                print(f"[friend] 好友簿读坏了，先当空的用：{exc}")
        me = data.get("me")
        self.me = dict(me) if isinstance(me, dict) else {}
        rows = data.get("friends")
        self.friends = [dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        self.me.setdefault("pet_id", _new_id())
        self.me.setdefault("owner", self.default_owner)
        self.me.setdefault("nick", self.default_nick)
        # 口令只在第一次生成：换口令等于把好友全踢出去，不能自己偷偷换
        self.me.setdefault(
            "token",
            "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6)),
        )
        if not str(self.me.get("owner") or "").strip():
            self.me["owner"] = self.default_owner
        if not str(self.me.get("nick") or "").strip():
            self.me["nick"] = self.default_nick

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"me": self.me, "friends": self.friends}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[friend] 好友簿写不进去：{exc}")

    # ---------- 我 ----------

    @property
    def pet_id(self) -> str:
        return str(self.me.get("pet_id") or "")

    @property
    def token(self) -> str:
        return str(self.me.get("token") or "")

    @property
    def owner(self) -> str:
        return str(self.me.get("owner") or "我")

    @property
    def nick(self) -> str:
        return str(self.me.get("nick") or "桌宠")

    def profile(self, port: int) -> Dict[str, object]:
        """我这台机器的自我介绍（发给好友用）。不含任何隐私内容。"""
        return {
            "pet_id": self.pet_id,
            "name": self.nick,
            "owner": self.owner,
            "port": int(port),
            "token": self.token,
        }

    def card(self, host: str = "") -> str:
        """一张可以微信发出去的名片。"""
        return net.encode_card(self.profile(int(self.me.get("port") or 0)), host)

    # ---------- 好友 ----------

    def get(self, pet_id: str) -> Optional[Dict[str, object]]:
        for row in self.friends:
            if str(row.get("pet_id")) == str(pet_id):
                return row
        return None

    def remember(self, profile: Dict[str, object], host: str = "", port: int = 0, **extra) -> Dict[str, object]:
        """把对方的名片记下来（已经有就更新）。返回好友这一行。"""
        pet_id = str(profile.get("pet_id") or "").strip()
        if not pet_id:
            return {}
        row = self.get(pet_id)
        if row is None:
            row = {"pet_id": pet_id, "name": "", "owner": "", "host": "", "port": 0, "token": ""}
            self.friends.append(row)
        row["name"] = str(profile.get("name") or row.get("name") or "桌宠")
        row["owner"] = str(profile.get("owner") or row.get("owner") or "")
        if profile.get("token"):
            row["token"] = str(profile["token"])
        if host:
            row["host"] = str(host)
        if port:
            row["port"] = int(port)
        row["seen"] = time.time()
        row.update(extra)
        return row

    def forget(self, pet_id: str) -> bool:
        before = len(self.friends)
        self.friends = [r for r in self.friends if str(r.get("pet_id")) != str(pet_id)]
        return len(self.friends) != before

    @staticmethod
    def label(row: Dict[str, object]) -> str:
        """面板上显示成什么：「小明家的 黄豆」。"""
        owner = str(row.get("owner") or "").strip()
        name = str(row.get("name") or "桌宠").strip()
        return f"{owner}家的 {name}" if owner else name


def _system_owner() -> str:
    """配置里没写称呼就用系统用户名（拿不到就算了）。"""
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return "我"


def open_book(cfg: Config) -> FriendBook:
    """按 config.json 的配置把好友簿打开（app 启动时调一次）。

    相对路径一律挂在 config.json 旁边：配置里写 `friends.json` / `data/guests`，
    程序从别的目录被启动也不会跑偏（跟 VisitHub 里那套一致）。
    称呼的默认值也在这儿定：`friends.owner` 留空用系统用户名，
    `friends.nick` 留空用 `persona.name`——都只在**第一次**写进好友簿。
    """
    raw = str(getattr(cfg.friends, "book", "") or "friends.json").strip() or "friends.json"
    path = Path(raw)
    if not path.is_absolute():
        path = CONFIG_PATH.parent / path
    owner = str(getattr(cfg.friends, "owner", "") or "").strip() or _system_owner()
    nick = str(getattr(cfg.friends, "nick", "") or "").strip() or str(cfg.persona.name or "桌宠")
    book = FriendBook(path, default_owner=owner, default_nick=nick)
    # 配置里写死了口令就用它（留空 = 用好友簿里自动生成的那个）
    token = str(getattr(cfg.friends, "token", "") or "").strip()
    if token:
        book.me["token"] = token
    return book


class Guest:
    """一位坐在我家屏幕上的客人（见 pet/guest.py 那个窗口）。

    `doing` 是它来的时候顺口带的：它那边主人这会儿大概在忙什么
    （一句**对方自己拼好的话**，见 pet/doing.py；人家没说就是空串）。
    """

    def __init__(
        self,
        profile: Dict[str, object],
        frames_dir: Path,
        addr: str = "",
        port: int = 0,
        doing: str = "",
    ):
        self.profile: Dict[str, object] = dict(profile)
        self.pet_id: str = str(profile.get("pet_id") or "")
        self.name: str = str(profile.get("name") or "桌宠")
        self.owner: str = str(profile.get("owner") or "")
        self.host: str = str(addr or profile.get("host") or "")
        self.port: int = int(port or profile.get("port") or 0)
        self.frames_dir: Path = Path(frames_dir)
        self.arrived_at: float = time.time()
        self.last_said: str = ""
        self.doing: str = str(doing or "")
        self.pos = None                 # 上次它坐在哪儿（拖过就记住）

    @property
    def label(self) -> str:
        return f"{self.owner}家的 {self.name}" if self.owner else self.name

    def say(self, text: str) -> None:
        self.last_said = text


class VisitHub(QObject):
    """串门的中枢：开门迎客、出门做客、把两边的话递来递去。

    所有网络动作都在后台线程里跑，结果通过 `_job` 信号回到 GUI 线程再发别的信号——
    UI 线程永远只负责画东西，一步都不能卡（这是这个项目一贯的规矩）。
    """

    # ---- 给 UI 的信号（都在 GUI 线程发出来）----
    arrived = Signal(object)            # Guest：客人进门了
    guest_said = Signal(str, str, str)  # pet_id, 台词, 情绪
    guest_left = Signal(str, str)       # pet_id, 为什么走（给人看的一句话）
    my_line = Signal(str, str)          # 我这只宠物说的台词, 情绪（本地气泡 / 人在外面时也发）
    away = Signal(object)               # 好友那一行（出门了）；None = 到家了
    ask_in = Signal(object)             # 有人在门口，等主人点头（好友行 + 地址）
    asked = Signal(str, str)            # 好友本人对我说的话（谁, 内容）——人在外面时用
    heard_doing = Signal(str, str)       # pet_id, 一句给人看的话：「小明那边说：他这会儿在打游戏」
    acted = Signal(str, dict)            # pet_id（该动的是谁）, 动作那一包（见 pet/play.py）
    played = Signal(str)                 # 一句给人看的话：「两只凑一起击了个掌」
    status = Signal(str)                # 状态行：给人看的一句话
    changed = Signal()                  # 好友簿 / 客人列表变了，面板该重画

    _job = Signal(object)               # 后台线程 → GUI 线程的传送带

    def __init__(self, cfg: Config, book: FriendBook, assets_dir: Path, activity=None, parent=None):
        """activity：`doing.ActivityBoard`（「我这边主人这会儿在忙什么」，见 pet/doing.py）。

        不给也行（自测、老调用都是这么用的）：那就只是"没什么好说的"，串门别的功能照旧。
        """
        super().__init__(parent)
        self.cfg = cfg
        self.f = cfg.friends
        self.book = book
        self.assets_dir = Path(assets_dir)
        self.activity = activity
        # 配置里写的是相对路径，不能跟着"当前工作目录"跑：一律挂在 config.json 旁边
        self.data_dir = CONFIG_PATH.parent
        self.guest_root = self._resolve(self.f.guest_dir)
        self.log_path = self._resolve(self.f.log)

        self.client = VisionClient(cfg)
        self.guests: Dict[str, Guest] = {}       # pet_id -> 客人
        self.away_home: Optional[Dict[str, object]] = None   # 不在 None 就是在谁家
        self._knocks: Dict[str, Dict[str, object]] = {}      # visit_id -> 门口那点事
        self._lines = 0                          # 这一趟已经说了几句（防两只宠物聊到天亮）
        self._pending_talk: Optional[Callable[[], None]] = None   # 下一句谁来说
        self._knock_pending = None               # 出门时等对方点头的那点事
        self._knock_deadline = 0.0
        self._seen_guest: set = set()            # 收过谁的话（同一句别重复显示）
        self._opening: List[Callable[[], None]] = []   # 进门之后这几步：问候 / 打听在忙啥
        self._last_move = ""                     # 刚做过的动作，别连着来一遍（见 pet/play.py）
        self._play_guest_id = ""                 # 现在跟谁按时玩（客人进门时记下，见 _start_playing）
        self._played = 0                         # 这一趟已经自动玩了几次（`play_rounds` 是上限）

        self.server = net.PeerServer(
            token=book.token,
            listen=self.f.listen,
            port=int(self.f.port),
            handler=self.handle,
        )
        # 计时器：串门到点回家 / 两只宠物每句之间隔一会儿 / 等对方点同意
        self._home_timer = QTimer(self)
        self._home_timer.setSingleShot(True)
        self._home_timer.timeout.connect(lambda: self.come_home("到点了"))
        self._talk_timer = QTimer(self)
        self._talk_timer.setSingleShot(True)
        self._talk_timer.timeout.connect(self._continue_talk)
        self._knock_timer = QTimer(self)
        self._knock_timer.setSingleShot(True)
        self._knock_timer.timeout.connect(self._ask_again)
        # 进门之后那几步：一件一件来（见 _plan_opening）
        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.timeout.connect(self._next_opening)
        # 两只按时互动：客人进门之后每隔 `play_gap_sec` 玩一下（见 _start_playing）
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._play_tick)
        self._job.connect(self._on_job)

    # ---------- 起 / 停 ----------

    def start(self) -> bool:
        """把门打开。起不来（端口被占之类）也只是没得串门，不影响别的地方。"""
        if not self.f.enabled:
            self.status.emit("好友陪伴已关掉（config.json 里 friends.enabled）")
            return False
        if not self.server.start():
            print(f"[friend] {self.server.error}")
            self.status.emit(f"串门功能没起来：{self.server.error}")
            return False
        self.book.me["port"] = int(self.server.port)
        self.book.save()
        self.status.emit(f"我在 {net.lan_ip()}:{self.server.port} 等着（口令 {self.book.token}）")
        return True

    def stop(self) -> None:
        """关门前跟客人打个招呼，别让人家屏幕上留一只没有主的宠物。"""
        for pet_id in list(self.guests):
            self._drop_guest(pet_id, "程序关了")
        if self.away_home is not None:
            self._post_to(self._away_target(), net.PATH_BYE, {"pet_id": self.book.pet_id, "why": "回家了"})
            self.away_home = None
        self._talk_timer.stop()
        self._home_timer.stop()
        self._knock_timer.stop()
        self._open_timer.stop()
        self._play_timer.stop()
        self._pending_talk = None
        self._opening = []
        self._play_guest_id = ""
        self._knock_pending = None
        self.server.stop()

    # ---------- 后台线程：网络和模型都别站在 UI 前面 ----------

    def _spawn(self, work: Callable[[], object], done: Optional[Callable[[object], None]] = None) -> None:
        """跑一个后台任务；跑完把结果丢回 GUI 线程交给 done（结果可能是异常对象）。"""

        def runner() -> None:
            try:
                out: object = work()
            except Exception as exc:            # 兜住一切：串门失败不该拖垮主程序
                out = exc
            self._job.emit((done, out))

        threading.Thread(target=runner, daemon=True, name="pet-friend-job").start()

    def _on_job(self, payload: object) -> None:
        done, out = payload        # type: ignore[misc]
        if callable(done):
            done(out)

    # ---------- 收件人是谁 ----------

    @staticmethod
    def _target(host: str, port: int, token: str, pet_id: str) -> Dict[str, object]:
        return {"host": str(host), "port": int(port or 0), "token": str(token), "pet_id": str(pet_id)}

    def _guest_target(self, guest: Guest) -> Dict[str, object]:
        """客人那台机器（我们的回信寄到这儿）。"""
        row = self.book.get(guest.pet_id) or {}
        return self._target(guest.host, guest.port, row.get("token") or "", guest.pet_id)

    def _away_target(self) -> Dict[str, object]:
        """我现在做客的那台机器（还在门口的话，就是门里那台）。"""
        row = self._host_row
        return self._target(row.get("host") or "", row.get("port") or 0, row.get("token") or "", row.get("pet_id") or "")

    def _post_to(
        self,
        target: Dict[str, object],
        path: str,
        payload: Dict[str, object],
        done: Optional[Callable[[object], None]] = None,
    ) -> None:
        """给某台机器发一条（在后台线程里发）。token 由这里统一补上。"""
        host = str(target.get("host") or "")
        port = int(target.get("port") or 0)
        if not host or not port:
            if callable(done):
                done({"error": "没有对方的地址"})
            return
        body = dict(payload)
        body.setdefault("token", str(target.get("token") or ""))
        body.setdefault("from_pet", self.book.pet_id)
        timeout = max(2.0, float(self.f.timeout))

        def work() -> object:
            ok, data = net.post_json(host, port, path, body, timeout=timeout)
            return data if ok else {"error": str(data.get("error") or "发不过去")}

        self._spawn(work, done)

    def _send_line(self, target: Dict[str, object], text: str, tag: str, kind: str = "pet") -> None:
        """把我说的话送到对方屏幕上（对方负责把它画进气泡）。"""
        self._post_to(
            target,
            net.PATH_SAY,
            {
                "pet_id": self.book.pet_id,
                "who": "pet",
                "kind": kind,
                "text": text,
                "mood": tag,
                "name": self.book.nick,
                "owner": self.book.owner,
            },
        )
        self._log({"dir": "out", "to": target.get("pet_id"), "kind": kind, "text": text})

    # ---------- 说一句话 ----------

    def _speak(
        self,
        system: str,
        prompt: str,
        kind: str,
        done: Callable[[str, str], None],
        temperature: float = 0.85,
    ) -> None:
        """让模型说一句话，回来的是 (台词, 情绪)。模型不给力就用兜底台词，绝不空场。"""
        fallback = random.choice(_FALLBACK.get(kind) or _FALLBACK["pet"])
        limit = max(12, int(self.f.max_chars))

        def work() -> object:
            try:
                return self.client.visit_line(system, prompt, kind=kind, temperature=temperature)
            except VlmError as exc:
                return exc
            except Exception as exc:
                return exc

        def clean(out: object) -> None:
            raw = "" if isinstance(out, Exception) else str(out or "")
            if isinstance(out, Exception):
                print(f"[friend] 这句话没说出来：{out}")
            text = ""
            if raw:
                text = persona.clean_reply(raw)
            if not text:
                text = fallback
            parsed = split_mood(text)
            body = (parsed.text or text).strip()
            if len(body) > limit:
                body = body[: limit - 1].rstrip() + "…"
            done(body, parsed.mood)

        self._spawn(work, clean)

    def _say_mine(
        self,
        system: str,
        prompt: str,
        kind: str,
        target: Optional[Dict[str, object]],
        *,
        display: bool = True,
    ) -> None:
        """我说一句话：画在自己屏幕上（display），同时送到对方屏幕上（target）。"""

        def done(text: str, tag: str) -> None:
            if not text:
                return
            if display:
                self.my_line.emit(text, tag)
            if target:
                self._send_line(target, text, tag, kind=kind)

        self._speak(system, prompt, kind, done)

    def _log(self, entry: Dict[str, object]) -> None:
        _log_line(entry, self.log_path)

    # ---------- 路径 ----------

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.data_dir / p

    def guest_dir_for(self, pet_id: str) -> Path:
        """某位访客的形象帧放这儿（PetRenderer 当普通形象用）。"""
        return self.guest_root / str(pet_id or "unknown")

    def my_frames(self) -> List[str]:
        """我要寄出去的形象帧（只发待机帧，见 net.read_frames）。"""
        return net.read_frames(self.assets_dir, limit=int(self.f.share_frames))

    # ---------- 门房：对方发来的每一条都落到这儿 ----------

    def handle(self, path: str, payload: Dict[str, object]) -> Dict[str, object]:
        """服务端线程里跑：只看路径，别在这儿说长句子（模型调用一律另开线程）。"""
        if path == net.PATH_HELLO:
            return self._on_hello(payload)
        if path == net.PATH_VISIT:
            return self._on_visit(payload)
        if path == net.PATH_SAY:
            return self._on_say(payload)
        if path == net.PATH_POKE:
            return self._on_poke(payload)
        if path == net.PATH_DOING:
            return self._on_doing(payload)
        if path == net.PATH_PLAY:
            return self._on_play(payload)
        if path == net.PATH_BYE:
            return self._on_bye(payload)
        return {"error": f"不知道你要干嘛：{path}"}

    def _peer_profile(self, payload: Dict[str, object]) -> Dict[str, object]:
        profile = payload.get("profile")
        return dict(profile) if isinstance(profile, dict) else {}

    def _on_hello(self, payload: Dict[str, object]) -> Dict[str, object]:
        """对暗号 + 报自己的名片（对方可以顺手更新我的地址）。"""
        return {"ok": True, "app": "screen-pet", "name": self.book.nick, "profile": self.book.profile(self.port())}

    def port(self) -> int:
        return int(self.server.port or self.f.port)

    def _me_profile(self) -> Dict[str, object]:
        """给对方的自我介绍（带口令：对面要拿它回话）。"""
        return self.book.profile(self.port())

    # ---------- 我这边主人在忙什么（见 pet/doing.py） ----------

    def _doing_line(self) -> str:
        """该说给对方听的那一句（没有本子 / 关了分享 / 还没认出在干嘛 → 空串）。

        注意这里是"**本机拼好的一句话**"，不是原始标题——原始窗口标题永远不出这台机器，
        尺度由 friends.share_doing 把关（见 doing.ActivityBoard.line）。
        """
        board = self.activity
        if board is None:
            return ""
        return str(board.line(self.book.owner) or "").strip()

    def _learn_doing(self, pet_id: str, line: str, label: str = "") -> None:
        """把「对方那边主人刚在干嘛」记下来：好友簿里存一笔（面板要显示），再喊一声让界面刷新。"""
        text = str(line or "").strip()
        if not text:
            return
        row = self.book.get(pet_id)
        if row is not None:
            row["doing"] = text
            row["doing_at"] = time.time()
            self.book.save()
            self.changed.emit()
        self.heard_doing.emit(str(pet_id), doing_mod.ActivityBoard.heard(text, label or FriendBook.label(row or {})))

    def _on_doing(self, payload: Dict[str, object]) -> Dict[str, object]:
        """有人问「你家主人刚在干嘛」：只回**本机拼好的那一句话**。

        跑在服务端线程里：只读本子、不碰界面、不调模型（见 doing.py 那段"为什么"）。
        """
        if not self._for_me(payload):
            return {"ok": False, "why": "这话不是问我的"}
        line = self._doing_line()
        if not line:
            return {"ok": False, "why": "这边主人没说可以讲这个"}
        self._log({"dir": "out", "to": payload.get("pet_id"), "kind": "doing", "text": line})
        return {"ok": True, "line": line, "name": self.book.nick, "owner": self.book.owner}

    # ---------- 收：有人来串门 ----------

    def _ui(self, fn: Callable[[], None]) -> None:
        """把一件事挪到 GUI 线程去做（动人、动计时器、发信号都只能在那儿）。"""
        self._spawn(lambda: None, lambda _out: fn())

    def _limit_guests(self) -> int:
        return max(1, int(self.f.max_guests))

    def _stay_seconds(self) -> float:
        return max(30.0, float(self.f.visit_seconds))

    def _prune_knocks(self) -> None:
        """门口的小本本别越记越多：超过 5 分钟的一律作废。"""
        now = time.time()
        for key in [k for k, v in self._knocks.items() if now - float(v.get("at") or 0) > 300.0]:
            self._knocks.pop(key, None)

    def _knock_answer(self, knock: Dict[str, object]) -> Dict[str, object]:
        state = str(knock.get("state") or "")
        if state == "ok":
            return {
                "ok": True,
                "pending": False,
                "profile": self._me_profile(),
                "seconds": self._stay_seconds(),
            }
        if state == "pending":
            return {"ok": False, "pending": True, "why": str(knock.get("why") or "这边的主人还没点头")}
        return {"ok": False, "pending": False, "why": str(knock.get("why") or "这边没同意")}

    def _on_visit(self, payload: Dict[str, object]) -> Dict[str, object]:
        """有人敲门（也可能是等着的时候再问一遍）。"""
        self._prune_knocks()
        visit_id = str(payload.get("visit_id") or "")
        knock = self._knocks.get(visit_id) if visit_id else None
        if knock is not None:
            # 老熟人：按本子上记的状态回话，别重复放进来
            if str(knock.get("state")) == "pending" and time.time() - float(knock.get("at") or 0) > 180.0:
                self._knocks.pop(visit_id, None)
                return {"ok": False, "pending": False, "why": "门口站太久了，它先回去了"}
            return self._knock_answer(knock)

        profile = self._peer_profile(payload)
        pet_id = str(profile.get("pet_id") or payload.get("from_pet") or "")
        if not pet_id:
            return {"ok": False, "why": "看不出来你是谁（名片不完整）"}
        if pet_id == self.book.pet_id:
            return {"ok": False, "why": "那是我自己呀"}
        if pet_id in self.guests:
            return {"ok": True, "pending": False, "profile": self._me_profile(), "seconds": self._stay_seconds()}
        if len(self.guests) >= self._limit_guests():
            return {"ok": False, "why": f"家里已经有 {len(self.guests)} 只客人了，挤不下"}

        # 地址：先信好友簿里记着的（之前连通过），再信它这次自己报的
        row = self.book.get(pet_id) or {}
        addr = str(row.get("host") or "") or str(payload.get("addr") or "") or str(profile.get("host") or "")
        port = int(profile.get("port") or 0) or int(row.get("port") or 0)
        if not addr:
            return {"ok": False, "why": "没带地址来，回头我连不上你"}
        frames = payload.get("frames")
        if isinstance(frames, list) and frames:
            net.save_frames(self.guest_dir_for(pet_id), [str(f) for f in frames])

        visit_id = visit_id or _new_id(8)
        knock = {
            "visit_id": visit_id,
            "profile": profile,
            "addr": addr,
            "port": port,
            # 它来时顺口带的「我那边主人这会儿在忙什么」（见 pet/doing.py）
            "doing": str(payload.get("doing") or "")[:120],
            "state": "ok" if self.f.auto_accept else "pending",
            "why": "" if self.f.auto_accept else "这边的主人还没点头",
            "at": time.time(),
        }
        self._knocks[visit_id] = knock
        self._ui(lambda: self._book_peer(profile, addr, port))
        if str(knock["state"]) == "ok":
            self._ui(lambda: self._let_in(knock))
        else:
            self._ui(lambda: self.ask_in.emit(dict(knock)))
        return self._knock_answer(knock)

    def _book_peer(self, profile: Dict[str, object], addr: str, port: int) -> None:
        """把来过的这位记进好友簿（下次不用再发名片）。"""
        if self.book.remember(profile, host=addr, port=port):
            self.book.save()
            self.changed.emit()

    def _let_in(self, knock: Dict[str, object]) -> None:
        """让客人进屋：屏幕上多一只，主人招呼一句。"""
        profile = dict(knock.get("profile") or {})
        pet_id = str(profile.get("pet_id") or "")
        if not pet_id or pet_id in self.guests:
            return
        knock["state"] = "ok"
        guest = Guest(
            profile,
            self.guest_dir_for(pet_id),
            str(knock.get("addr") or ""),
            int(knock.get("port") or 0),
            doing=str(knock.get("doing") or ""),
        )
        self.guests[pet_id] = guest
        self._lines = 0
        self._log({"dir": "in", "pet_id": pet_id, "kind": "visit", "text": f"{guest.label} 来串门"})
        self.arrived.emit(guest)
        self.changed.emit()
        self.status.emit(f"{guest.label} 来串门了")
        if guest.doing:
            # 它来时顺口带了一句「我那边主人这会儿在忙什么」（见 pet/doing.py）：
            # 记进好友簿 + 喊一声让面板显示，招呼那一句也能顺口提一提
            self._learn_doing(pet_id, guest.doing, guest.label)
        self._say_mine(
            persona.host_system_prompt(self.cfg, guest.name, guest.owner),
            persona.visit_prompt(
                "greet_guest",
                other_name=guest.name,
                other_owner=guest.owner,
                heard=guest.doing,
            ),
            "greet_guest",
            self._guest_target(guest),
        )
        # 招呼过之后隔一会儿跟它玩一下，然后**每隔 play_gap_sec 再来一次**：
        # 招待客人那点意思（互动频率见 config 的 friends.play_gap_sec / pet/play.py）
        self._start_playing(pet_id)

    # ---------- 收：话 / 戳 / 再见 ----------

    def _for_me(self, payload: Dict[str, object]) -> bool:
        """这条是不是冲我来的（谁家的宠物该接话）。"""
        to_pet = str(payload.get("to_pet") or "")
        return (not to_pet) or to_pet == self.book.pet_id

    def _on_say(self, payload: Dict[str, object]) -> Dict[str, object]:
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "why": "空话就别发了"}
        if not self._for_me(payload):
            return {"ok": False, "why": "这话不是对我说的"}
        who = str(payload.get("who") or "pet")
        mood_tag = str(payload.get("mood") or "")
        if who == "owner":
            # 好友本人在跟我的宠物说话——只有"我的宠物正好在他家"时才轮到我回
            if self.away_home is None or not self.f.allow_owner_chat:
                return {"ok": False, "why": "我现在不在你那儿，或者这边不接话"}
            self._ui(lambda: self._asked_me(str(payload.get("name") or "好友"), text))
            return {"ok": True, "mood": mood_tag}

        speaker = str(payload.get("pet_id") or payload.get("from_pet") or "")
        guest = self.guests.get(speaker)
        if guest is not None:
            self._ui(lambda: self._guest_spoke(guest, text, mood_tag))
            return {"ok": True}
        # 在做客，或者还在门口等进门（对方可能先招呼了一声，见 _host_row）
        if self.away_home is not None or (
            speaker and speaker == str(self._host_row().get("pet_id") or "")
        ):
            self._ui(lambda: self._host_pet_spoke(text, mood_tag))
            return {"ok": True}
        return {"ok": False, "why": "不知道这话是谁说的"}

    def _on_poke(self, payload: Dict[str, object]) -> Dict[str, object]:
        if not self._for_me(payload):
            return {"ok": False, "why": "不是戳我"}
        if self.away_home is not None:
            row = self.away_home
            self._ui(lambda: self._poked_away(row))
            return {"ok": True}
        # 在自己家里被戳：本机这只反应一下就行（谁戳的就照谁的名字说）
        guest = self.guests.get(str(payload.get("pet_id") or payload.get("from_pet") or ""))
        self._ui(lambda: self._poked_at_home(guest))
        return {"ok": True}

    def _on_bye(self, payload: Dict[str, object]) -> Dict[str, object]:
        pet_id = str(payload.get("pet_id") or payload.get("from_pet") or "")
        why = str(payload.get("why") or "回去了")
        if self.away_home is not None and pet_id == str(self.away_home.get("pet_id") or ""):
            self._ui(lambda: self._sent_home(why))
            return {"ok": True}
        if pet_id in self.guests:
            self._ui(lambda: self._drop_guest(pet_id, why))
            return {"ok": True}
        return {"ok": True, "why": "本来就不在我这儿"}

    # ---------- 我说的话（全在 GUI 线程里跑） ----------

    def _pet_at_home(self) -> bool:
        """出门的时候，家里那只还露不露面（friends.at_home）。"""
        return bool(self.f.at_home)

    def _budget(self) -> bool:
        """两只宠物自动闲聊还有额度吗（pet_chat_rounds 是"我这边说几句"）。"""
        rounds = int(self.f.pet_chat_rounds)
        if rounds <= 0:
            return False
        return self._lines < rounds

    def _schedule_talk(self, fn: Callable[[], None]) -> None:
        """隔一会儿再说下一句：别让两只宠物像对暗号一样一口气聊完。"""
        self._pending_talk = fn
        self._talk_timer.start(max(200, int(float(self.f.pet_chat_gap_sec) * 1000)))

    def _continue_talk(self) -> None:
        fn, self._pending_talk = self._pending_talk, None
        if callable(fn):
            fn()

    @property
    def _host_row(self) -> Dict[str, object]:
        """现在管谁叫「主人家」：在做客就用做客那家；还在门口就用门里那家。

        门口这一档是必须的：对方一点头就会立刻招呼一句（见 _let_in），
        而我在门口是每 2.5 秒才问一次（KNOCK_POLL），那句话往往**比我先到**。
        """
        if self.away_home is not None:
            return self.away_home
        pending = self._knock_pending
        row = pending[0] if isinstance(pending, tuple) and pending else {}
        return row if isinstance(row, dict) else {}

    def _away_system(self) -> str:
        row = self._host_row
        return persona.visitor_system_prompt(self.cfg, str(row.get("name") or ""), str(row.get("owner") or ""))

    def _away_prompt(self, kind: str, text: str = "", heard: str = "", playing: str = "") -> str:
        row = self._host_row
        return persona.visit_prompt(
            kind,
            text=text,
            other_name=str(row.get("name") or ""),
            other_owner=str(row.get("owner") or ""),
            rounds=self._lines,
            heard=heard,
            playing=playing,
        )

    # ---------- 进门之后那几步 ----------

    def _plan_opening(self, steps: List[Callable[[], None]]) -> None:
        """进门之后依次做几件事（问候 → 打听在忙啥）。

        一件一件来，每件之间隔 `pet_chat_gap_sec`——进门的招呼、问候、打听
        要是一口气全发出去，屏幕上就像有人在同时说话。
        （"玩一下"不排在这里：它按时来，见 _start_playing。）
        """
        self._opening = [step for step in steps if callable(step)]
        if self._opening:
            self._open_timer.start(self._opening_gap_ms())

    def _opening_gap_ms(self) -> int:
        return max(OPENING_GAP_MIN_MS, int(float(self.f.pet_chat_gap_sec) * 1000))

    def _next_opening(self) -> None:
        step = self._opening.pop(0) if self._opening else None
        if callable(step):
            step()
        if self._opening:
            self._open_timer.start(self._opening_gap_ms())

    def _stop_opening(self) -> None:
        """不接着做那几步了（回家了 / 客人走了）。"""
        self._opening = []
        self._open_timer.stop()

    # ---------- 两只按时互动 ----------

    def _play_every_ms(self) -> int:
        return max(1000, int(float(self.f.play_gap_sec) * 1000))

    def _start_playing(self, pet_id: str) -> None:
        """客人进门了：从现在起每隔 `play_gap_sec` 让两只玩一下（0 = 不自动玩）。

        **"互动频率"就是这一个数**（默认 30 秒一次）。发起方只有一边——主人家这台；
        去人家家里那只不用自己起头：动作一过来它跟着做（见 _on_play / _played_back），
        两边都起头的话就会变成各玩各的、还会翻倍。
        `play_rounds` 是上限（0 = 不封顶）。
        """
        self._play_guest_id = str(pet_id)
        self._played = 0
        self._play_timer.stop()
        if float(self.f.play_gap_sec) <= 0:
            return
        self._play_timer.start(self._play_every_ms())

    def _stop_playing(self) -> None:
        self._play_guest_id = ""
        self._play_timer.stop()

    def _play_tick(self) -> None:
        """到点了：两只玩一下（玩了几次够数就收工，见 _start_playing）。"""
        if float(self.f.play_gap_sec) <= 0:
            self._stop_playing()
            return
        cap = int(self.f.play_rounds or 0)
        if cap > 0 and self._played >= cap:
            self._stop_playing()
            return
        pet_id = self._play_guest_id
        if not pet_id or self.guests.get(pet_id) is None:
            self._stop_playing()
            return
        if self.play_guest(pet_id):
            self._played += 1

    def _guest_spoke(self, guest: Guest, text: str, mood_tag: str) -> None:
        """我家的客人说了一句话：画在它气泡里，然后我家这只接一句。"""
        guest.say(text)
        self.guest_said.emit(guest.pet_id, text, mood_tag)
        self._log({"dir": "in", "pet_id": guest.pet_id, "kind": "pet", "text": text})
        if not self._budget():
            return
        self._lines += 1
        system = persona.host_system_prompt(self.cfg, guest.name, guest.owner)
        prompt = persona.visit_prompt(
            "pet", text=text, other_name=guest.name, other_owner=guest.owner, rounds=self._lines
        )
        target = self._guest_target(guest)
        self._schedule_talk(lambda: self._say_mine(system, prompt, "pet", target))

    def _host_pet_spoke(self, text: str, mood_tag: str) -> None:
        """人家家里的那只跟我说话了：我不在家，只能听见（屏幕上看不着我自己）。"""
        self._log({"dir": "in", "pet_id": (self.away_home or {}).get("pet_id"), "kind": "pet", "text": text})
        if not self._budget():
            return
        self._lines += 1
        system = self._away_system()
        prompt = self._away_prompt("pet", text)
        target = self._away_target()
        display = self._pet_at_home()
        self._schedule_talk(lambda: self._say_mine(system, prompt, "pet", target, display=display))

    def _asked_me(self, who: str, text: str) -> None:
        """好友本人对我的宠物说话了（它在他家）→ 我在自己这儿把话接上。"""
        self.asked.emit(who, text)
        self._log({"dir": "in", "pet_id": (self.away_home or {}).get("pet_id"), "kind": "owner", "text": text})
        self._say_mine(
            self._away_system(),
            self._away_prompt("reply", text),
            "reply",
            self._away_target(),
            display=self._pet_at_home(),
        )

    def _poked_away(self, row: Dict[str, object]) -> None:
        """有人在我做客的家里戳了我一下。"""
        self._say_mine(
            self._away_system(),
            self._away_prompt("poke"),
            "poke",
            self._away_target(),
            display=self._pet_at_home(),
        )

    def _poked_at_home(self, guest: Optional[Guest] = None) -> None:
        """有人戳了家里这只（多半是客人那位主人隔着屏幕戳的）。"""
        self._say_mine(
            persona.host_system_prompt(
                self.cfg,
                guest.name if guest else "",
                guest.owner if guest else "",
            ),
            persona.visit_prompt("poke", other_name=guest.name if guest else "", other_owner=guest.owner if guest else ""),
            "poke",
            None,
        )

    def _played_back(self, move, pet_id: str) -> None:
        """对方那只已经动起来了：我这只跟着做同一个动作，再配一句词（台词各说各的）。

        「两边同时做同一个动作」这件事就落在这一处：动作由发起方定，两边各自播，
        播多久也跟着动作走（`move.seconds`），所以两边看起来是同步的。
        """
        self._last_move = move.key
        self.acted.emit(self.book.pet_id, play_mod.info(move, by=pet_id))
        self._log({"dir": "in", "pet_id": pet_id, "kind": "play", "text": move.key})
        self.played.emit(play_mod.brief(move))
        if self.away_home is None:
            guest = self.guests.get(str(pet_id))
            who = guest.name if guest is not None else ""
            owner = guest.owner if guest is not None else ""
            self._say_mine(
                persona.host_system_prompt(self.cfg, who, owner),
                persona.visit_prompt("play", other_name=who, other_owner=owner, playing=move.hint),
                "play",
                self._guest_target(guest) if guest is not None else None,
            )
            return
        self._say_mine(
            self._away_system(),
            self._away_prompt("play", playing=move.hint),
            "play",
            self._away_target(),
            display=self._pet_at_home(),
        )

    def _on_play(self, payload: Dict[str, object]) -> Dict[str, object]:
        """对方那只起头要玩：我这只跟着做同一个动作（见 pet/play.py）。

        跟别的"收件"一样：这里只回一个"知道了"，说话和动人的活儿都丢回 GUI 线程。
        """
        if not self._for_me(payload):
            return {"ok": False, "why": "不是找我玩的"}
        move = play_mod.get(str(payload.get("move") or ""))
        if move is None:
            return {"ok": False, "why": "不认识这个动作"}
        pet_id = str(payload.get("pet_id") or payload.get("from_pet") or "")
        if self.away_home is None and self.guests.get(pet_id) is None:
            return {"ok": False, "why": "家里没客人，我也没在做客"}
        self._ui(lambda: self._played_back(move, pet_id))
        return {"ok": True, "move": move.key, "seconds": move.seconds}

    # ---------- 出：我去别人家 ----------

    def visit(self, pet_id: str) -> None:
        """去某个好友家坐一会儿。"""
        row = self.book.get(pet_id)
        if not row:
            self.status.emit("好友簿里没有这一位")
            return
        if self.away_home is not None:
            self.status.emit("我已经在别人家做客了，先回来再说")
            return
        host = str(row.get("host") or "")
        port = int(row.get("port") or 0)
        if not host or not port:
            self.status.emit(f"还不知道 {FriendBook.label(row)} 在哪儿（先让它发张名片给你）")
            return
        visit_id = _new_id(8)
        payload = {
            "profile": self.book.profile(self.port()),
            "frames": self.my_frames(),
            "visit_id": visit_id,
            "addr": net.lan_ip(),
            "probe": False,
            # 顺口带一句「我这边主人这会儿在忙什么」（见 pet/doing.py）：
            # 已经是本机拼好的那一句，原始标题不出这台机器；关了分享就是空串。
            "doing": self._doing_line(),
        }
        self.status.emit(f"正在敲 {FriendBook.label(row)} 的门……")
        parsed = split_mood(random.choice(_FALLBACK["away"]))
        self.my_line.emit((parsed.text or "").strip(), parsed.mood or "happy")
        target = self._target(host, port, row.get("token") or "", pet_id)
        self._post_to(target, net.PATH_VISIT, payload, lambda out: self._knock_back(row, visit_id, out))

    def _knock_back(self, row: Dict[str, object], visit_id: str, out: object) -> None:
        """敲门的回音：进去了 / 还在门口等 / 不让进。"""
        if not isinstance(out, dict):
            self.status.emit("敲门失败了，对方那边回的东西看不懂")
            return
        if out.get("ok"):
            self._walk_in(row, out)
            return
        if out.get("pending"):
            self._knock_pending = (row, visit_id)
            self._knock_deadline = time.time() + KNOCK_TIMEOUT
            self.status.emit(f"{FriendBook.label(row)} 那边还没点头，我在门口等着")
            self._knock_timer.start(int(KNOCK_POLL * 1000))
            return
        if out.get("error"):
            # 连不上就说明**对方设备没在工作**（关机 / 没开程序 / 不在同一张网）：明说，
            # 别拿"对方不方便"糊过去——那听着像是人家不想理你，其实是机器根本不在线。
            self.status.emit(f"对方设备不在工作中——连不上 {FriendBook.label(row)} 那台机器")
            return
        self.status.emit(f"没能进去：{out.get('why') or '对方不方便'}")

    def _ask_again(self) -> None:
        """在门口每隔一会儿再问一声（对方可能刚点了同意）。"""
        pending = self._knock_pending
        if not pending:
            return
        if time.time() > self._knock_deadline:
            self._knock_pending = None
            self.status.emit("等了太久也没人应门，下次再试")
            return
        row, visit_id = pending
        target = self._target(row.get("host") or "", row.get("port") or 0, row.get("token") or "", row.get("pet_id") or "")
        payload = {"profile": self.book.profile(self.port()), "visit_id": visit_id, "probe": True}
        self._post_to(target, net.PATH_VISIT, payload, lambda out: self._probe_back(row, visit_id, out))

    def _probe_back(self, row: Dict[str, object], visit_id: str, out: object) -> None:
        if not isinstance(out, dict):
            self._knock_timer.start(int(KNOCK_POLL * 1000))
            return
        if out.get("ok"):
            self._knock_pending = None
            self._walk_in(row, out)
            return
        if out.get("pending"):
            self._knock_timer.start(int(KNOCK_POLL * 1000))
            return
        self._knock_pending = None
        if out.get("error"):
            # 等在门口的时候对方关机 / 断网了：别再白等，明说对方设备不在工作中
            self.status.emit(f"对方设备不在工作中——连不上 {FriendBook.label(row)} 那台机器")
            return
        self.status.emit(f"人家没让我进：{out.get('why') or ''}")

    def _walk_in(self, row: Dict[str, object], answer: Dict[str, object]) -> None:
        """进门了：我的宠物从现在起在人家屏幕上，到点自己回来。"""
        profile = answer.get("profile")
        if isinstance(profile, dict) and profile:
            self.book.remember(profile, host=str(row.get("host") or ""), port=int(profile.get("port") or 0))
            self.book.save()
            self.changed.emit()
        host_row = self.book.get(str(row.get("pet_id") or "")) or row
        self.away_home = host_row
        self._lines = 0
        seconds = float(answer.get("seconds") or self._stay_seconds())
        self._home_timer.start(max(5000, int(min(seconds, self._stay_seconds()) * 1000)))
        self._log({"dir": "out", "to": host_row.get("pet_id"), "kind": "visit", "text": "我来了"})
        self.away.emit(host_row)
        self.status.emit(f"我现在在 {FriendBook.label(host_row)} 家做客")
        # 进门的招呼（画在人家屏幕上）
        self._lines += 1
        self._say_mine(
            self._away_system(),
            self._away_prompt("meet"),
            "meet",
            self._away_target(),
            display=self._pet_at_home(),
        )
        # 进门之后那几步：先跟人家主人问声好（串门的问候），再打听一句
        # 「你家主人这会儿在忙什么」（见 pet/doing.py；`ask_doing` 关掉就不打听）。
        # 一件一件来，别挤在同一秒里。
        steps: List[Callable[[], None]] = [self._greet_owner]
        if bool(self.f.ask_doing):
            steps.append(self._ask_host_doing)
        self._plan_opening(steps)

    def _greet_owner(self) -> None:
        """跟好友本人问声好（串门时"问候"这一步，跟"打听在忙啥"是两回事）。"""
        if self.away_home is None:
            return
        self._say_mine(
            self._away_system(),
            self._away_prompt("hello_owner"),
            "hello_owner",
            self._away_target(),
            display=self._pet_at_home(),
        )

    def come_home(self, reason: str = "到点了") -> None:
        """回家：跟主人家道个别，把自己从人家屏幕上撤掉。"""
        row = self.away_home
        if row is None:
            return
        target = self._away_target()
        self.away_home = None
        self._home_timer.stop()
        self._talk_timer.stop()
        self._stop_opening()
        self._pending_talk = None
        self._knock_pending = None
        self._lines = 0
        self._post_to(
            target,
            net.PATH_BYE,
            {"pet_id": self.book.pet_id, "to_pet": target.get("pet_id"), "why": reason},
        )
        self._log({"dir": "out", "to": row.get("pet_id"), "kind": "bye", "text": reason})
        self.away.emit(None)
        self.status.emit(f"到家了（{reason}）")
        self._say_home()

    def _sent_home(self, why: str) -> None:
        """对方把我请回来了。"""
        row = self.away_home
        if row is None:
            return
        self.away_home = None
        self._home_timer.stop()
        self._talk_timer.stop()
        self._stop_opening()
        self._pending_talk = None
        self._lines = 0
        self.away.emit(None)
        self.status.emit(f"{FriendBook.label(row)} 那边说：{why}——我回来了")
        self._say_home()

    def _say_home(self) -> None:
        """到家了这一句：画在自己屏幕上（说给它主人听）。"""
        parsed = split_mood(random.choice(_FALLBACK["home"]))
        self.my_line.emit((parsed.text or "").strip(), parsed.mood or "happy")

    # ---------- 客人走了 ----------

    def _drop_guest(self, pet_id: str, why: str) -> None:
        """客人从屏幕上撤走（自己走的，或者主人请走的）。"""
        guest = self.guests.pop(str(pet_id), None)
        if guest is None:
            return
        for key in [
            k for k, v in self._knocks.items() if str((v.get("profile") or {}).get("pet_id") or "") == str(pet_id)
        ]:
            self._knocks.pop(key, None)
        self._log({"dir": "in", "pet_id": pet_id, "kind": "bye", "text": why})
        self.guest_left.emit(str(pet_id), why)
        self.changed.emit()
        self.status.emit(f"{guest.label} 走了（{why}）")
        if not self.guests:
            self._lines = 0
            self._pending_talk = None
            self._stop_opening()
            self._stop_playing()
        # 主人客气一句——只画在自己屏幕上：人家已经走了
        self._say_mine(
            persona.host_system_prompt(self.cfg, guest.name, guest.owner),
            persona.visit_prompt("see_off", other_name=guest.name, other_owner=guest.owner),
            "see_off",
            None,
        )

    def drop_guest(self, pet_id: str, why: str = "这边有点事，它先回去了") -> None:
        """把客人请回去：对方也会收到一声，好让它自己回家。"""
        guest = self.guests.get(str(pet_id))
        if guest is None:
            return
        self._post_to(
            self._guest_target(guest),
            net.PATH_BYE,
            {"pet_id": self.book.pet_id, "to_pet": pet_id, "why": why},
        )
        self._drop_guest(pet_id, why)

    # ---------- 门口那点事 / 本地动作（UI 直接调这些） ----------

    def _knock_for(self, pet_id: str) -> Optional[Dict[str, object]]:
        for knock in self._knocks.values():
            profile = knock.get("profile") or {}
            if str(profile.get("pet_id") or "") == str(pet_id) and str(knock.get("state")) == "pending":
                return knock
        return None

    def pending_knocks(self) -> List[Dict[str, object]]:
        return [dict(k) for k in self._knocks.values() if str(k.get("state")) == "pending"]

    def accept(self, pet_id: str) -> None:
        """让门口那位进来。"""
        knock = self._knock_for(pet_id)
        if knock is None:
            return
        knock["state"] = "yes"          # 先标上，免得连点两下放进来两只
        self._let_in(knock)

    def refuse(self, pet_id: str, why: str = "") -> None:
        """今天不方便，别让它进来。"""
        knock = self._knock_for(pet_id)
        if knock is None:
            return
        knock["state"] = "no"
        knock["why"] = why or "主人说现在不方便"
        profile = knock.get("profile") or {}
        self.status.emit(f"没让 {FriendBook.label(profile)} 进来")
        self._log({"dir": "in", "pet_id": profile.get("pet_id"), "kind": "knock", "text": knock["why"]})

    def poke_guest(self, pet_id: str) -> None:
        """戳一下屏幕上的客人（它自己那边会回一句）。"""
        guest = self.guests.get(str(pet_id))
        if guest is None:
            return
        self._post_to(
            self._guest_target(guest),
            net.PATH_POKE,
            {"pet_id": self.book.pet_id, "to_pet": pet_id},
        )

    def talk_to_guest(self, pet_id: str, text: str) -> bool:
        """我直接跟客人说话：话送到它那边，由它生成回答再送回来。"""
        guest = self.guests.get(str(pet_id))
        text = (text or "").strip()
        if guest is None or not text:
            return False
        if not self.f.allow_owner_chat:
            self.status.emit("你把「跟对方的宠物说话」关掉了（friends.allow_owner_chat）")
            return False
        self._post_to(
            self._guest_target(guest),
            net.PATH_SAY,
            {
                "pet_id": self.book.pet_id,
                "to_pet": pet_id,
                "who": "owner",
                "text": text,
                "name": self.book.nick,
                "owner": self.book.owner,
            },
        )
        self._log({"dir": "out", "to": pet_id, "kind": "owner", "text": text})
        return True

    def nudge(self, pet_id: str) -> None:
        """两只都冷场了？让自家这只再搭一句话。"""
        guest = self.guests.get(str(pet_id))
        if guest is None:
            return
        self._say_mine(
            persona.host_system_prompt(self.cfg, guest.name, guest.owner),
            persona.visit_prompt(
                "pet", text=guest.last_said, other_name=guest.name, other_owner=guest.owner, rounds=self._lines
            ),
            "pet",
            self._guest_target(guest),
        )

    def poke_home(self) -> None:
        """我在别人家做客时戳一下主人家那只。"""
        target = self._away_target()
        if self.away_home is None:
            return
        self._post_to(
            target,
            net.PATH_POKE,
            {"pet_id": self.book.pet_id, "to_pet": target.get("pet_id")},
        )

    # ---------- 一起玩 / 打听在忙啥（UI 直接调这几个） ----------

    def _pick_move(self, move: str):
        """选一个动作：点名了就照点名的来，没点名就随便挑一个（刚做过的先避开）。"""
        return play_mod.get(move) or play_mod.pick([self._last_move])

    def play_guest(self, pet_id: str = "", move: str = "") -> bool:
        """让家里这只跟客人玩一下（pet_id 留空 = 屏幕上就一位客人）。

        我这边先动，同时把动作发过去，它那边跟着做同一个动作——所以两边看起来
        是"一起"在玩（见 pet/play.py：动作名 + 播多久都在那一包里）。
        """
        guest = self.guests.get(str(pet_id)) if pet_id else next(iter(self.guests.values()), None)
        if guest is None:
            return False
        picked = self._pick_move(move)
        self._last_move = picked.key
        self.acted.emit(self.book.pet_id, play_mod.info(picked, by=self.book.pet_id))
        self.played.emit(play_mod.brief(picked))
        self._log({"dir": "out", "to": guest.pet_id, "kind": "play", "text": picked.key})
        self._post_to(
            self._guest_target(guest),
            net.PATH_PLAY,
            {"pet_id": self.book.pet_id, "to_pet": guest.pet_id, "move": picked.key},
        )
        return True

    def play_home(self, move: str = "") -> bool:
        """我在别人家做客时跟主人家那只玩一下。"""
        if self.away_home is None:
            return False
        target = self._away_target()
        picked = self._pick_move(move)
        self._last_move = picked.key
        self.acted.emit(self.book.pet_id, play_mod.info(picked, by=self.book.pet_id))
        self.played.emit(play_mod.brief(picked))
        self._log({"dir": "out", "to": target.get("pet_id"), "kind": "play", "text": picked.key})
        self._post_to(
            target,
            net.PATH_PLAY,
            {"pet_id": self.book.pet_id, "to_pet": target.get("pet_id"), "move": picked.key},
        )
        return True

    def ask_guest_doing(self, pet_id: str = "") -> bool:
        """问屏幕上的客人：你那边主人这会儿在忙什么（回的那一句见 pet/doing.py）。"""
        guest = self.guests.get(str(pet_id)) if pet_id else next(iter(self.guests.values()), None)
        if guest is None:
            return False
        self.status.emit(f"问 {guest.label} 一句：你那边主人这会儿在忙什么")
        self._post_to(
            self._guest_target(guest),
            net.PATH_DOING,
            {"pet_id": self.book.pet_id, "to_pet": guest.pet_id},
            lambda out: self._heard_from_guest(guest, out),
        )
        return True

    def _heard_from_guest(self, guest: Guest, out: object) -> None:
        """客人回话了：它那边主人这会儿在忙什么——记一笔，再让我这只讲给我听。"""
        line = str(out.get("line") or "").strip() if isinstance(out, dict) and out.get("ok") else ""
        if not line:
            why = out.get("why") if isinstance(out, dict) else out
            self.status.emit(f"没问出来：{why or '对方没接这个话'}")
            return
        self._learn_doing(guest.pet_id, line, guest.label)
        self._say_mine(
            persona.host_system_prompt(self.cfg, guest.name, guest.owner),
            persona.visit_prompt("doing", other_name=guest.name, other_owner=guest.owner, heard=line),
            "doing",
            self._guest_target(guest),
        )

    def ask_home_doing(self) -> bool:
        """面板上的「问它在忙啥」：问主人家那只「你家主人这会儿在忙什么」。"""
        return self._ask_host_doing()

    def _ask_host_doing(self) -> bool:
        """我在别人家做客：问一句「你家主人这会儿在忙什么」。"""
        if self.away_home is None:
            return False
        row, target = self._host_row, self._away_target()
        self.status.emit(f"我问了 {FriendBook.label(row)} 一句：你家主人这会儿在忙什么")
        self._post_to(
            target,
            net.PATH_DOING,
            {"pet_id": self.book.pet_id, "to_pet": target.get("pet_id")},
            lambda out: self._heard_host_doing(row, out),
        )
        return True

    def _heard_host_doing(self, row: Dict[str, object], out: object) -> None:
        """对方回话了：它那边主人刚在干嘛——我记一笔，再让我这只讲给我听。"""
        line = str(out.get("line") or "").strip() if isinstance(out, dict) and out.get("ok") else ""
        if not line:
            why = out.get("why") if isinstance(out, dict) else out
            self.status.emit(f"没打听出来：{why or '那边没接这个话'}")
            return
        self._learn_doing(str(row.get("pet_id") or ""), line, FriendBook.label(row))
        self._say_mine(
            self._away_system(),
            self._away_prompt("doing", heard=line),
            "doing",
            self._away_target(),
            display=self._pet_at_home(),
        )

    def tell_pet(self, text: str) -> bool:
        """我对自己的宠物说话。人在外面时：让它把这句话带到人家屏幕上去。

        在家的时候返回 False——那是聊天面板的活儿（本机对话），不该走网络。
        """
        text = (text or "").strip()
        if self.away_home is None or not text:
            return False
        self._say_mine(
            self._away_system(),
            self._away_prompt("carry", text),
            "carry",
            self._away_target(),
            display=self._pet_at_home(),
        )
        return True

    # ---------- 好友簿的维护 ----------

    def remember_card(self, text: str, host: str = "") -> str:
        """把对方发来的一行名片收进好友簿，返回一句给人看的结果。"""
        card = net.parse_card(text)
        if not card:
            return "这不像名片（应该以 SPET1| 开头，让对方从面板里复制一份）"
        pet_id = str(card.get("pet_id") or "")
        if not pet_id:
            return "这张名片缺身份码，让对方用新版再复制一张"
        profile = {
            "pet_id": pet_id,
            "name": card.get("name"),
            "owner": card.get("owner"),
            "token": card.get("token"),
        }
        row = self.book.remember(
            profile,
            host=str(card.get("host") or host),
            port=int(str(card.get("port") or 0) or 0),
        )
        self.book.save()
        self.changed.emit()
        self.status.emit(f"记下了：{FriendBook.label(row)}")
        return f"已记下 {FriendBook.label(row)}"

    def forget(self, pet_id: str) -> None:
        """从好友簿里划掉一位（它要是还在屏幕上，也一起请走）。"""
        if self.guests.get(str(pet_id)):
            self.drop_guest(str(pet_id), "主人把这位好友划掉了")
        if self.book.forget(str(pet_id)):
            self.book.save()
            self.changed.emit()
            self.status.emit("已经删掉了")

    def test(self, pet_id: str) -> None:
        """测一下连不连得上（顺带把对方最新的名字、地址更新回来）。"""
        row = self.book.get(str(pet_id))
        if not row:
            self.status.emit("好友簿里没有这一位")
            return
        host = str(row.get("host") or "")
        port = int(row.get("port") or 0)
        if not host or not port:
            self.status.emit("还缺地址或者端口")
            return
        self.status.emit(f"正在连 {FriendBook.label(row)}……")
        target = self._target(host, port, row.get("token") or "", pet_id)

        def done(out: object) -> None:
            if not isinstance(out, dict) or not out.get("ok"):
                why = out.get("error") if isinstance(out, dict) else out
                self.status.emit(f"连不上：{why}")
                return
            profile = out.get("profile")
            if isinstance(profile, dict) and profile:
                self.book.remember(profile, host=host, port=int(profile.get("port") or port))
                self.book.save()
                self.changed.emit()
            self.status.emit(f"连上了：{FriendBook.label(self.book.get(pet_id) or row)}")

        self._post_to(target, net.PATH_HELLO, {"profile": self.book.profile(self.port())}, done)

    # ---------- 给面板看的一眼状态 ----------

    def state(self) -> Dict[str, object]:
        return {
            "me": self.book.profile(self.port()),
            "running": self.server.running,
            "guests": len(self.guests),
            "knocks": len(self.pending_knocks()),
            "away": (self.away_home or {}).get("pet_id") or "",
            "listen": f"{net.lan_ip()}:{self.port()}",
        }
