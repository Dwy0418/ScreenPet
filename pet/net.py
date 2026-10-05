"""好友串门的"线路"：一张名片、一个小服务、一把发请求的手。

这一层只管搬东西，不懂"串门"是什么意思（那是 pet/friends.py 的事）：

* **名片**：一行文字，把「谁 + 在哪台机器 + 口令」装进去，方便直接微信发过去。
      SPET1|<宠物名>|<主人>|<地址 或 IP>|<端口>|<口令>
  真正的机器识别靠 `pet_id`（首次启动随机生成，写进 friends.json），
  所以对方改名字、换 IP，你这边还是同一个人（见 FriendBook.remember）。

* **服务端**：标准库 http.server，只认 POST + JSON，只回 JSON。
  口令不对直接 403；请求体超过 MAX_BODY 直接掐掉（别让谁家宠物塞个 100MB 过来）。

* **客户端**：项目里已经有 requests（见 pet/vlm.py），这里接着用它，
  全部在后台线程里发——UI 线程一步都不能停。

为什么不做中转服务器：整套东西的卖点是"本地、不联网也能跑"。局域网直连
（或者自己拿 ZeroTier / Tailscale 组个虚拟局域网）最贴合这个定位，
代价是双方得能互相看见对方的端口，这点在 README 里讲清楚了。
"""
from __future__ import annotations

import base64
import functools
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

CARD_PREFIX = "SPET1"
MAX_BODY = 4 * 1024 * 1024        # 一次最多收这么多（形象帧打包后在几百 KB 量级）
MAX_FRAMES = 8                    # 形象帧最多收几张

# 服务端对外暴露的路径。名字起得直白一点，抓包看也明白在干嘛。
PATH_PING = "/ping"      # 你还在吗
PATH_HELLO = "/hello"    # 打个招呼：报名片、对暗号
PATH_VISIT = "/visit"    # 我要来你这边坐坐（可以带形象帧）
PATH_SAY = "/say"        # 说一句（谁说的、说什么、什么情绪）
PATH_POKE = "/poke"      # 戳一下
PATH_BYE = "/bye"        # 走了，回家
PATH_DOING = "/doing"    # 你家主人刚在干嘛（回的是一句**本地拼好的话**，见 pet/doing.py）
PATH_PLAY = "/play"      # 两只一起做点什么（击掌这种，见 pet/play.py）


# ---------- 名片 ----------

def lan_ip() -> str:
    """本机在局域网里的地址（拿不到就返回 127.0.0.1，别当回事）。

    做法是往一个公网地址建个 UDP"连接"——不会真的发包，只是让系统帮我们挑一张
    出口网卡，再从那张网卡上读自己的地址。比遍历网卡稳，也不会误报 127.0.0.1。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def encode_card(profile: Dict[str, object], host: str = "") -> str:
    """把名片压成一行（SPET1|名字|主人|地址|端口|口令|身份码）。host 留空就自己猜一个本机地址。

    最后的身份码（pet_id）是给机器认人的：对方改名字、换网段都还是同一位好友。
    前面的字段是给人看的，所以故意排在前面。
    """
    address = host or lan_ip()
    parts = [
        CARD_PREFIX,
        str(profile.get("name") or "桌宠"),
        str(profile.get("owner") or ""),
        address,
        str(profile.get("port") or ""),
        str(profile.get("token") or ""),
        str(profile.get("pet_id") or ""),
    ]
    return "|".join(part.replace("|", "/") for part in parts)


def parse_card(text: str) -> Optional[Dict[str, str]]:
    """读一行名片；不是名片就返回 None（用户可能顺手粘了别的）。

    认的是前 6 段；没有第 7 段（身份码）的老名片也能读，只是交给调用方去判断。
    """
    line = (text or "").strip()
    if not line:
        return None
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 5 or parts[0] != CARD_PREFIX:
        return None
    name, owner, host, port = parts[1], parts[2], parts[3], parts[4]
    token = parts[5] if len(parts) > 5 else ""
    pet_id = parts[6] if len(parts) > 6 else ""
    if not host or not port.isdigit():
        return None
    return {"name": name, "owner": owner, "host": host, "port": port, "token": token, "pet_id": pet_id}



# ---------- 形象帧 ----------

def read_frames(folder: Path, limit: int = 4, prefix: str = "idle") -> List[str]:
    """把形象的几张待机帧读出来，base64 打包（没有图片资产就返回空表 → 用内置形象）。

    只发待机帧：别的表情对方用不上（访客的表情由 mood 标签驱动，见 pet/mood.py），
    发多了纯粹是浪费带宽。
    """
    if limit <= 0 or not folder.is_dir():
        return []
    files = sorted(folder.glob(f"{prefix}_*.png"), key=lambda p: p.name)
    if not files:                       # 有人把帧命名成 1.png / 2.png 之类
        files = sorted(folder.glob("*.png"), key=lambda p: p.name)
    out: List[str] = []
    for path in files[: max(0, min(int(limit), MAX_FRAMES))]:
        try:
            out.append(base64.b64encode(path.read_bytes()).decode("ascii"))
        except OSError:
            continue
    return out


def save_frames(folder: Path, frames: List[str], prefix: str = "idle") -> int:
    """把收到的帧落到本地目录，供 PetRenderer 当普通形象用；返回写成功几张。"""
    if not frames:
        return 0
    folder.mkdir(parents=True, exist_ok=True)
    written = 0
    for index, blob in enumerate(frames[:MAX_FRAMES]):
        try:
            data = base64.b64decode(blob, validate=False)
        except Exception:
            continue
        if len(data) < 128:             # 明显不是 PNG，跳过
            continue
        (folder / f"{prefix}_{index:02d}.png").write_bytes(data)
        written += 1
    return written


# ---------- 客户端 ----------

def post_json(
    host: str,
    port: int,
    path: str,
    payload: Dict[str, object],
    timeout: float = 8.0,
) -> Tuple[bool, Dict[str, object]]:
    """给某个好友的机器发一条 JSON，返回 (成功没有, 内容或错误说明)。

    这一层永远不抛异常：串门失败是可以接受的小事，绝不能把主程序带崩。
    """
    url = f"http://{host}:{int(port)}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=float(timeout))
    except requests.RequestException as exc:
        return False, {"error": f"连不上 {host}:{port}（{exc.__class__.__name__}）"}
    if resp.status_code >= 400:
        try:
            detail = str(resp.json().get("error") or "")
        except Exception:
            detail = (resp.text or "")[:120]
        if resp.status_code == 403:
            return False, {"error": "口令对不上，让对方把名片重发一份给你", "detail": detail}
        return False, {"error": f"对方回了 {resp.status_code}：{detail}"}
    try:
        data = resp.json()
    except Exception:
        return False, {"error": "对方回的不是 JSON（端口是不是被别的程序占了？）"}
    if not isinstance(data, dict):
        return False, {"error": "对方回的格式看不懂"}
    return True, data


# ---------- 服务端 ----------

class _Handler(BaseHTTPRequestHandler):
    """一个请求进、一个 JSON 出。业务全部丢给 peer.handle()。

    `peer` 是**每个请求自己的**：同一个进程里可能同时开着不止一个 PeerServer
    （比如自测、或者换端口重开），如果把它挂在类上，后起的那个会把先起的那个的
    口令和业务全顶掉——表现就是"明明发的是对方的口令，对方却说口令不对"。
    """

    server_version = "screen-pet-friend/1"

    def __init__(self, request, client_address, server, peer=None):  # noqa: D107 - http.server 的签名
        self.peer: Optional["PeerServer"] = peer
        super().__init__(request, client_address, server)

    # 默认日志会往控制台打一堆东西；串门是常事，只留错误
    def log_message(self, fmt: str, *args) -> None:
        if self.peer is not None and self.peer.verbose:
            print(f"[friend] {self.address_string()} {fmt % args}")

    def _reply(self, code: int, data: Dict[str, object]) -> None:
        blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        try:
            self.wfile.write(blob)
        except OSError:
            pass

    def do_GET(self) -> None:  # noqa: N802 - http.server 就是这么命名的
        if self.path.rstrip("/") in ("", PATH_PING):
            self._reply(200, {"ok": True, "app": "screen-pet"})
            return
        self._reply(405, {"error": "这里只认 POST"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._reply(413, {"error": "请求体太大或者没有内容"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._reply(400, {"error": "看不出来这是 JSON"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"error": "顶层得是个对象"})
            return

        peer = self.peer
        path = self.path.split("?")[0].rstrip("/") or PATH_PING
        if path != PATH_PING and peer is not None:      # 除了探活，什么都要先对暗号
            if str(payload.get("token") or "") != peer.token:
                self._reply(403, {"error": "口令不对"})
                return
        try:
            answer = peer.handle(path, payload) if peer is not None else {"error": "还没准备好"}
        except Exception as exc:    # 这边出错也得让对方看懂，别回一片 500
            answer = {"error": f"这边处理出错：{exc}"}
        self._reply(200, answer)

    def do_PUT(self) -> None:  # noqa: N802
        self._reply(405, {"error": "这里只认 POST"})

    do_DELETE = do_PUT


class PeerServer:
    """串门的服务端：在后台线程里听着端口，收到什么就交给 handle()。"""

    def __init__(
        self,
        token: str,
        listen: str = "0.0.0.0",
        port: int = 8799,
        handler: Optional[Callable[[str, Dict[str, object]], Dict[str, object]]] = None,
        verbose: bool = False,
    ):
        self.token = str(token or "")
        self.listen = listen or "0.0.0.0"
        self.port = int(port)
        self.handle = handler or (lambda path, payload: {})
        self.verbose = verbose
        self.error = ""            # 起不来的时候为什么（端口被占之类）
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---------- 起 / 停 ----------

    def start(self) -> bool:
        """起来就返回 True；端口被占之类返回 False（原因写在 self.error）。"""
        if self._httpd is not None:
            return True
        try:
            # 每个服务端配一份自己的 Handler：peer 从构造参数传进去，不挂类上（见 _Handler）
            handler = functools.partial(_Handler, peer=self)
            httpd = ThreadingHTTPServer((self.listen, self.port), handler)
        except OSError as exc:
            self.error = f"{self.listen}:{self.port} 起不来（{exc}）"
            return False
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = int(httpd.server_address[1])   # 端口写 0 时系统会分一个，得读回来
        self._thread = threading.Thread(target=httpd.serve_forever, name="pet-friends", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

    @property
    def running(self) -> bool:
        return self._httpd is not None
