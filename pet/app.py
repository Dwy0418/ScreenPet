"""组装入口：把窗口、托盘、后台线程串起来。"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu, QMessageBox, QSystemTrayIcon, QWidget

from . import dpi, paths, selftest, winfind
from . import doing as doing_mod
from . import friends as friends_mod
from . import hide_console_window, make_console_safe
from . import style as style_mod
from .asr import ListenTask, SpeechReader
from .chatpanel import ChatPanel
from .config import ASSETS_DIR, PROVIDER_PRESETS, Config, apply_override
from .friendpanel import FriendPanel
from .guest import GuestWindow
from .overlay import RegionPicker
from .sprite import PetRenderer
from .window import PetWindow
from .worker import AnalysisWorker

if TYPE_CHECKING:  # 只在类型检查时认得它：真正 import 是用户点菜单那一刻（见 open_designer）
    from tools.design_pet import PetDesigner


class ScreenPet:
    """整个应用：一个挂件窗口 + 一个后台分析线程 + 一个托盘。"""

    TRAY_TIP_INTERVAL_MS = 20000   # 悬浮提示多久刷一次（本地拼字符串，不花接口钱）

    def __init__(self, cfg: Config, app: QApplication):
        self.cfg = cfg
        self.app = app
        # 长什么样（颜色/身形/五官）：一份定义在 pet/style.py，用户自己在
        # config.json 的 `ui.pet_style` 里改，或用 tools/design_pet.py 现调现看。
        # 配置写坏了不能静默——不然用户会以为自己改的那行没生效（见 pet/style.py）。
        pet_style = style_mod.PetStyle.from_config(cfg.ui)
        for problem in pet_style.problems:
            print(f"[style] {problem}")
        self.renderer = PetRenderer(
            ASSETS_DIR / "pet",
            period=float(getattr(cfg.ui, "anim_period", 30.0)),
            crossfade=bool(getattr(cfg.ui, "anim_crossfade", True)),
            tap=bool(getattr(cfg.ui, "hand_tap", True)),
            style=pet_style,
            prefer_vector=bool(getattr(cfg.ui, "prefer_vector", False)),
        )
        self.window = PetWindow(cfg, self.renderer)
        self.worker = AnalysisWorker(cfg)
        self.worker.handshake_ready = True
        # 打字/语音聊天面板：也挂在挂件上（挡住画面时抓屏前会一起让开）
        self.panel = ChatPanel(cfg)
        self.window.extra_occluders = [self.panel]
        self.asr = SpeechReader(cfg)
        self._listen_task: Optional[ListenTask] = None

        self._picker: Optional[RegionPicker] = None
        self._designer: Optional["PetDesigner"] = None   # 「设计我的形象…」开出来的窗口
        self._quit_after_pick = False
        self._tray: Optional[QSystemTrayIcon] = None
        self._tray_timer: Optional[QTimer] = None
        self._occluder_timer: Optional[QTimer] = None
        self._quitting = False

        # 好友陪伴：好友簿 + 门房（网络都在 hub 自己的后台线程里，见 friends.py）
        self.book = friends_mod.open_book(cfg)
        # 「我这边主人这会儿在忙什么」的小本子（见 pet/doing.py）：worker 认出"换了件事"
        # 就记一笔，串门时从这里取**那一句话**（尺度由 friends.share_doing 把关）
        self.doings = doing_mod.ActivityBoard(
            level=int(getattr(cfg.friends, "share_doing", 1) or 0),
            owner=self.book.owner,
        )
        self.worker.activity_board = self.doings
        self.hub = friends_mod.VisitHub(cfg, self.book, ASSETS_DIR / "pet", self.doings)
        self.friend_panel: Optional[FriendPanel] = None
        self._guest_windows: Dict[str, GuestWindow] = {}
        self._away_hidden = False
        self._wire()

    # ---------- 接线 ----------

    def _wire(self) -> None:
        w = self.window
        self.worker.comment.connect(self._on_comment)
        self.worker.notice.connect(self._on_notice)
        self.worker.failure.connect(self._on_failure)
        self.worker.thinking.connect(w.set_thinking)
        self.worker.hide_requested.connect(self._on_hide_requested)
        self.worker.show_requested.connect(self._on_show_requested)

        w.pauseToggled.connect(self.set_paused)
        w.proactiveToggled.connect(self.set_proactive)
        w.pickRegionRequested.connect(lambda: self.pick_region())
        w.configRequested.connect(self.open_config)
        w.quitRequested.connect(self.quit)
        w.hideRequested.connect(self.hide)
        w.memoryRequested.connect(self.open_memory)       # 「打开记忆文件」
        w.designRequested.connect(self.open_designer)     # 「设计我的形象…」
        w.memoryArchiveRequested.connect(self.open_memory_archive)  # 「打开完整存档」
        w.clearMemoryRequested.connect(self.clear_memory)  # 「清除长期记忆…」
        w.moved.connect(self._update_occluder)
        # 点一下挂件：没在聊就弹输入框，**已经在聊就收起来**（见 window._on_click）。
        # 面板开没开由面板自己说了算，挂件回头问它（快了是双击=暂停，走另一条路）。
        w.chat_open = self.panel.isVisible
        w.chatRequested.connect(self.toggle_chat)
        # 右键菜单「逗它一下」：给人点的动作入口（键见 pet/states.py 的 MENU_REACTIONS）
        w.reactionRequested.connect(self.play_reaction)
        # 热键整套撤了（全部走鼠标），这几项只从右键菜单进来：打字 / 语音 / 马上吐槽 / 锁定
        w.chatOpenRequested.connect(self.open_chat)
        w.voiceRequested.connect(self.open_voice)
        w.analyzeRequested.connect(self.analyze_now)
        w.lockRequested.connect(self._on_click_through)
        w.processLockRequested.connect(self.set_target_process)
        w.processUnlockRequested.connect(self.clear_target_process)
        self.panel.submitted.connect(self.ask)
        self.panel.voiceRequested.connect(self.start_listening)
        self.panel.closed.connect(self._update_occluder)
        w.friendsRequested.connect(self.open_friends)
        self._wire_friends()

    def _wire_friends(self) -> None:
        """好友陪伴的信号（都在 GUI 线程发出来，见 friends.VisitHub）。"""
        hub = self.hub
        hub.arrived.connect(self._on_guest_arrived)
        hub.guest_said.connect(self._on_guest_said)
        hub.guest_left.connect(self._on_guest_left)
        hub.my_line.connect(self._on_my_line)
        hub.away.connect(self._on_away)
        hub.ask_in.connect(self._on_ask_in)
        hub.asked.connect(self._on_friend_asked)
        hub.status.connect(self._on_friend_status)
        hub.changed.connect(self._refresh_friend_panel)
        hub.heard_doing.connect(self._on_heard_doing)   # 「好友那边主人刚在干嘛」
        hub.acted.connect(self._on_acted)               # 该做动作了（见 pet/play.py）
        hub.played.connect(self._on_played)             # 两只玩了一下（给人看的一行）

    def start(self) -> None:
        self.window.show()
        self.worker.start()
        # 门开着才有人能来串门；起不来（关了 / 端口被占）也只是没得串门，不当成错误
        self.hub.start()
        self._setup_tray()
        self._update_occluder()
        # 盯着某个程序时，那个窗口可能随时被拖动/缩放：隔一会儿重新算一次"我挡没挡住它"
        self._occluder_timer = QTimer(self.window)
        self._occluder_timer.setInterval(400)
        self._occluder_timer.timeout.connect(self._update_occluder)
        self._occluder_timer.start()

    # ---------- 事件 ----------

    def _on_comment(self, comment) -> None:
        text = getattr(comment, "text", "") or ""
        mood = getattr(comment, "mood", "") or ""
        if not text:
            return
        # 收在托盘里的时候不冒气泡：屏幕上都没它，凭空冒一句话出来太莫名其妙
        # （聊天面板开着的话，回答照样记在面板里）
        if self.window.isVisible():
            self.window.say(text, mood)
            # 这句话是什么情绪，顺手带个动作（见 pet/states.py 的 MOOD_ACTIONS）：
            # 开心 → 欢呼、好奇 → 疑惑、惊讶 → 被戳般惊跳。正做着动作的时候不打断。
            self.window.play_mood(mood)
        if getattr(comment, "kind", "") == "chat":
            self.panel.add_reply(text, mood)

    def _on_notice(self, text: str) -> None:
        """它想跟你说点什么（不是吐槽）：气泡里说一句；面板开着就顺手记在面板里。"""
        self.window.set_thinking(False)
        if self.window.isVisible():
            self.window.say(text)
        if self.panel.isVisible():
            self.panel.add_notice(text)

    def _on_failure(self, text: str) -> None:
        self.window.set_thinking(False)
        if self.window.isVisible():
            self.window.say(text)
        if self.panel.isVisible():
            self.panel.add_notice(text)

    def _on_hide_requested(self) -> None:
        self.window.hideForCapture()
        self.worker.ack_hidden()

    def _on_show_requested(self) -> None:
        self.window.showAfterCapture()
        self.worker.ack_shown()

    def set_paused(self, paused: bool) -> None:
        self.window.set_paused(paused)
        self.worker.set_paused(paused)
        self._sync_tray()

    def _on_click_through(self, enabled: bool) -> None:
        """钉住 / 松开（鼠标穿透）——右键菜单「把我钉在这儿 / 松开」走这条路。

        钉上之后鼠标照样操作得了它：`window.set_click_through` 是"平时穿透、鼠标压到身上
        就临时解锁"，所以菜单还能再点回来（以前这一项只留给热键，键盘一撤就没人点得到）。
        这里照旧存盘 + 冒个泡。
        """
        self.window.set_click_through(enabled)
        try:
            self.cfg.save()
        except Exception as exc:
            print(f"[app] 保存配置失败：{exc}")
        if enabled:
            self.window.say("锁上了，鼠标碰到我还能右键", "smirk")
        self._sync_tray()

    def play_reaction(self, name: str) -> None:
        """右键菜单「逗它一下」：**给人点的**互动入口（键见 `pet/states.py` 的 MENU_REACTIONS）。

        以前这些动作只有"情绪正好撞上来"才看得到（喂一口 / 夸夸它 / 摸鱼 / 伸懒腰 / 蹦一个…），
        想让它演一次反倒碰不到；现在菜单里点一下就有。这是纯本机的——不联网、不进提示词、
        也不打扰它正在看的东西，就是逗它一下。

        认不出来的名字由挂件那边当没事发生（`window.react` 只认 REACTIONS 里的键），
        所以这里不必再挑一遍；以后想让"逗它"顺手记一笔 / 回一句话，就加在这儿。
        """
        self.window.react(name)

    def toggle_lock(self) -> None:
        """锁定 / 解锁位置（菜单里的「把我钉在这儿 / 松开」最终也是走到这儿）。"""
        self._on_click_through(not self.window.locked)

    def study_summary_text(self) -> str:
        """「它有没有在学」这一句——**人话版**，不是本次运行的计数器。

        原来这里是 `已学 0 轮 / 语料 +0 段 / 记忆 +0 条`：那是**这次运行**的计数，
        进程一重启就归零，看着就像什么都没干（现场就是这么被误会的）。现在只说
        "在不在学、补过几个话题的课"，数字是账本里跨会话攒下来的（`study.ledger`）。
        更细的账（哪一轮、花了多少、语料多了几段）见 `study_detail_text()`。
        """
        try:
            state = self.worker.study.status()
        except Exception:
            return "补课：暂时读不到"
        if not self.cfg.study.enabled:
            return "补课：配置里关着（study.enabled = false）"
        if not self.window.isVisible():
            return state + "｜现在收在托盘里，正在补"
        return state

    def set_proactive(self, enabled: bool) -> None:
        """开/关主动搭话——没槽点时它也会自己找话说。"""
        self.cfg.proactive.enabled = bool(enabled)
        self.worker.proactive.reset()   # 计时器拨到现在，免得一打开就蹦一句
        try:
            self.cfg.save()
        except Exception as exc:
            print(f"[app] 保存配置失败：{exc}")
        self._sync_tray()
        if enabled:
            self.window.say("行，那我不等槽点了，自己跟你搭话", "happy")
        else:
            self.window.say("好，我只在你画面有槽点的时候才说话", "speechless")

    def analyze_now(self) -> None:
        """立刻看一眼并吐槽一句（右键菜单「马上吐槽一句（立刻看一眼）」）。"""
        self.window.set_thinking(True)
        self.worker.analyze_now()

    def visit_now(self) -> None:
        """**本机**溜一圈（换块屏 / 屏幕对面待一会儿，不联网）。

        面板上那个入口已经删掉了：现在的"出门"是**去谁家串门**（好友那一行的按钮，
        见 pet/friendpanel.py / friends.VisitHub）。这个方法留着只是给冒烟测试和
        命令行用——`window.visit()` 本身还在，谁想调都行。
        """
        self.window.visit()

    def first_run_hint(self) -> None:
        """第一次启动指个路：配置文件在哪儿、怎么填 Key。

        打包版尤其需要这一句：exe 装在别的地方，配置写在 `%APPDATA%\\ScreenPet`，
        不说一声人家根本不知道该去哪儿填 API Key。日志里再留一份完整路径。
        """
        where = self.cfg.config_path()
        print(f"[app] 第一次运行：配置写在 {where}（右键我 → 打开配置文件）")
        self.window.say("第一次用：右键我 →「打开配置文件」，填上 api_key 再重启", "curious")

    # ---------- 设计我的形象 ----------

    def open_designer(self) -> None:
        """「设计我的形象…」：开一个所见即所得的设计器（tools/design_pet.py）。

        为什么要有这个入口：长相（颜色 / 身形 / 五官）在 pet/style.py 里是一份数据，
        可那堆十六进制颜色写在配置里谁也想象不出长什么样。设计器把它摆成能拧的旋钮，
        改一下**当场**给挂件换装（走 apply_style -> renderer.set_style），满意了再落进
        config.json 的 `ui.pet_style`。

        import 写在方法里：这是点菜单才会用到的工具窗口，挂件启动时不必多加载一份 Qt
        界面代码。另外打包版里 tools/ 不进包（见 ScreenPet.spec），所以要接得住 ImportError。
        """
        if self._designer is not None:
            self._designer.show()
            self._designer.raise_()
            self._designer.activateWindow()
            return
        try:
            from tools.design_pet import PetDesigner, installed_frames
        except ImportError as exc:
            print(f"[app] 打不开形象设计器：{exc}")
            self.window.say("这台机器上没找到形象设计器（源码版里是 tools/design_pet.py）", "speechless")
            return
        designer = PetDesigner(
            style=self.renderer.pet_style,
            cfg=self.cfg,
            frames_installed=installed_frames(),
        )
        designer.styleChanged.connect(self.apply_style)
        designer.saved.connect(lambda *_: self.window.say("换好了，我照这个样子长", "happy"))
        designer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        designer.destroyed.connect(self._on_designer_closed)
        self._designer = designer
        # 抓屏时得跟着让开：设计器开在屏幕中间，不躲开就会被"我"连自己带设计器一起看见
        self.window.extra_occluders = [self.panel, designer]
        designer.show()

    def _on_designer_closed(self, *_args) -> None:
        # 窗口是 WA_DeleteOnClose，关掉就销毁了——这里必须把引用和让开名单一起清掉，
        # 不然下一次抓屏会去问一个已经没了的窗口"你可见吗"
        self._designer = None
        self.window.extra_occluders = [self.panel]

    def apply_style(self, style: style_mod.PetStyle) -> None:
        """设计器改一下就换一下：矢量形象是照着这份定义现画的，所以立刻看得出来。"""
        self.renderer.set_style(style)
        self.window.update()

    def open_config(self) -> None:
        path = self.cfg.config_path()
        if not path.exists():
            self.cfg.save()
        opener = getattr(os, "startfile", None)
        if opener is None:
            print(f"[app] 配置文件在：{path}")
            return
        try:
            opener(str(path))
        except Exception as exc:
            self.window.say(f"打不开配置文件，路径是 {path}")
            print(f"[app] 打开配置文件失败：{exc}")

    # ---------- 打字 / 说话 ----------

    def open_chat(self, with_voice: bool = False) -> None:
        """弹出输入框；with_voice=True 就顺手开始听你说一句。

        菜单里的「打字跟我唠…」「说一句（语音）」走的就是这条路——**只开不收**：
        点它就是"我要说话"，哪怕面板已经开着也只是把它叫到前面来（不会反而收掉）。
        """
        if not self.cfg.chat.enabled:
            self.window.say("聊天在配置里关掉了（chat.enabled = false）", "speechless")
            return
        self.panel.show_near(self.window)
        self._update_occluder()
        if with_voice:
            self.start_listening()

    def toggle_chat(self) -> None:
        """点一下挂件：聊 / 收。

        点第一下把输入框叫出来（挂件那边已经问过一句「想跟我聊些什么~」，见
        `window._on_click`），**再点一下就收起来**——不用特地去找 Esc。
        收的时候不吭声：他是要把框收走，不是要再聊一句。
        """
        if self.panel.isVisible():
            self.panel.close_panel()
            self._update_occluder()
            return
        self.open_chat()

    def open_voice(self) -> None:
        """「说一句（语音）」：开着输入框并直接开始听你说一句（面板上的「麦克风」同源）。"""
        self.open_chat(with_voice=True)

    def ask(self, text: str) -> None:
        """用户在面板里说了一句（打字或语音确认过的）。"""
        content = (text or "").strip()
        if not content:
            return
        self.panel.add_user(content)
        # 我这只在人家家做客：这句话让它带过去说（见 friends.tell_pet，在家时返回 False）
        if self.hub.tell_pet(content):
            self.panel.add_notice("我把这句话带到人家屏幕上说了")
            return
        self.window.set_thinking(True)
        self.worker.submit_user(content)

    def start_listening(self) -> None:
        """听一句。走 Windows 自带引擎，离线、不上传音频。"""
        if self._listen_task is not None:
            return  # 已经在听了，别叠起来
        if not self.asr.available():
            self.panel.show_near(self.window)
            self.panel.add_notice("这台机器上用不了语音输入（需要 Windows 自带的语音识别）")
            return
        self.panel.show_near(self.window)
        self._update_occluder()
        self.panel.set_listening(True)
        self.window.set_listening(True)   # 它自己也切成"竖着耳朵听"的样子（见 pet/states.py）
        task = ListenTask(self.asr, float(self.cfg.asr.seconds))
        task.heard.connect(self._on_heard)
        task.failed.connect(self._on_hear_failed)
        task.finished.connect(self._on_listen_done)
        self._listen_task = task
        task.start()

    def _on_heard(self, text: str) -> None:
        heard = (text or "").strip()
        if not heard:
            self.panel.add_notice("没听清，再说一次？")
            return
        self.panel.add_notice(f"我听到：{heard}")
        if bool(getattr(self.cfg.chat, "voice_auto_send", False)):
            self.ask(heard)
        else:
            self.panel.set_input(heard)   # 先填进输入框，你确认了再回车

    def _on_hear_failed(self, message: str) -> None:
        self.panel.add_notice(message)
        print(f"[asr] {message}")

    def _on_listen_done(self) -> None:
        self._listen_task = None
        self.panel.set_listening(False)
        self.window.set_listening(False)

    # ---------- 观看范围 ----------

    def pick_region(self, quit_after: bool = False) -> None:
        """划一次「观看范围」——**就这一个入口**，两种选法都在这个界面里：

        * 拖一个矩形 → 只看这一块；
        * 什么都不拖、直接双击（或按 F）→ 整块屏都看。

        （以前是"只看一小块地方…"和"整块屏都看"两个菜单项，现在是同一个。）
        """
        self._quit_after_pick = quit_after
        self.window.close_bubble()
        picker = RegionPicker()
        picker.picked.connect(self._on_region_picked)
        picker.wholeRequested.connect(self._on_whole_screen)
        self._picker = picker
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def _after_pick(self) -> None:
        """框选这一趟的收尾：--pick-region 是一次性任务，选完就退出。"""
        self._update_occluder()
        if self._quit_after_pick:
            self._quit_after_pick = False
            self.quit()

    def _on_region_picked(self, region) -> None:
        self._picker = None
        if isinstance(region, dict) and int(region.get("width", 0)) > 0:
            self.cfg.capture.region = {
                key: int(region[key]) for key in ("left", "top", "width", "height")
            }
            try:
                self.cfg.save()
            except Exception as exc:
                print(f"[app] 保存配置失败：{exc}")
            self.window.say(
                "好，我就盯这块 %d × %d" % (region["width"], region["height"])
            )
        self._after_pick()

    def _on_whole_screen(self) -> None:
        """框选界面里双击 / 按 F：整块屏都看（以前那个单独的菜单项）。"""
        self._picker = None
        self.clear_region()
        self._after_pick()

    def clear_region(self) -> None:
        self.cfg.capture.region = None
        try:
            self.cfg.save()
        except Exception as exc:
            print(f"[app] 保存配置失败：{exc}")
        self._update_occluder()
        self.window.say("行，那我整块屏幕都看")

    # ---------- 只盯某个程序 ----------

    def set_target_process(self, process: str, title: str = "") -> None:
        """锁定进程：只监视这个 exe 的窗口（抖音、B站、游戏…）。

        锁上之后就不再框选了——两套规则打架只会让人搞不清它到底在看哪块。
        """
        name = (process or "").strip()
        if not name:
            return
        if not winfind.available():
            self.window.say("这台机器上找不到窗口列表，没法只盯某个程序", "speechless")
            return
        self.cfg.capture.target_process = name
        self.cfg.capture.target_title = (title or "").strip()
        self.cfg.capture.region = None
        self.worker.capture_region = None      # 让它下一轮重新算
        try:
            self.cfg.save()
        except Exception as exc:
            print(f"[app] 保存配置失败：{exc}")
        # 立刻找一眼，好把"哪个窗口"说清楚（找不到也照样锁上，等它开）
        where = winfind.describe(name, self.cfg.capture.target_title)
        if winfind.find_window(name) is None:
            self.window.say(f"好，我只盯 {name}——现在没看到它的窗口，你打开它我就开始", "curious")
        else:
            self.window.say(f"好，我只盯 {name} 的画面了", "happy")
        print(f"[app] 锁定进程：{name} → {where}")
        self._update_occluder()
        self._sync_tray()

    def clear_target_process(self) -> None:
        """取消进程锁定：回到整屏 / 框选。"""
        self.cfg.capture.target_process = ""
        self.cfg.capture.target_title = ""
        self.worker.capture_region = None
        try:
            self.cfg.save()
        except Exception as exc:
            print(f"[app] 保存配置失败：{exc}")
        self.window.say("行，那我不挑程序了，整块屏幕都看")
        self._update_occluder()
        self._sync_tray()

    def _update_occluder(self) -> None:
        """挂件（或气泡）压到观看区域上时，抓屏前要先把自己藏起来。

        「盯着哪个程序」的时候区域是**跟着窗口走的**，所以这里每次都问 worker
        要当前这一块（worker 每轮都会更新 self.capture_region）。
        """
        region = self.worker.capture_region or self.cfg.capture.region
        logical = dpi.physical_rect_to_logical(region) if region else None
        if logical is None:
            self.worker.occluder_active = False
            return
        self.worker.occluder_active = self.window.occlusion_rect().intersects(logical)

    # ---------- 托盘 ----------

    def _setup_tray(self) -> None:
        """托盘只管一件事：把挂件放出来 / 收回去。

        以前这里摆的是整份菜单的复制品（20 项，一大半跟挂件右键菜单重了），
        Windows 又只能按系统样式排版，越长越难找。现在托盘菜单只剩「显示挂件」，
        其余命令全部走挂件右键菜单（`pet/window.py::_build_menu`）；
        原来那几行状态（隐身学习 / 记忆 / 看片笔记）挪到托盘图标的悬浮提示里，
        鼠标停一下照样能看到它没闲着。
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(self.renderer.icon(32), self.app)
        tray.setToolTip(self._tray_tip())

        menu = QMenu()
        self.act_show = QAction("显示挂件", menu)
        self.act_show.setCheckable(True)
        self.act_show.setChecked(True)
        self.act_show.triggered.connect(self._toggle_visible)
        menu.addAction(self.act_show)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray
        # 悬浮提示那几行（我懂你 / 现在在看什么）要跟着状态走：隔半分钟刷一次。
        # 全是本地拼字符串，不花接口钱；不刷的话"看到第 4 段"会一直停在开挂件那一刻。
        self._tray_timer = QTimer(self.window)
        self._tray_timer.setInterval(self.TRAY_TIP_INTERVAL_MS)
        self._tray_timer.timeout.connect(self._sync_tray)
        self._tray_timer.start()

    def _tray_tip(self) -> str:
        """托盘图标悬浮提示：只说"它懂你什么"和"它现在在看什么"。

        为什么换掉原来那几行（隐身学习 / 记忆 / 看片笔记）：那是**后台口径**——
        `已学 0 轮 / 语料 +0 段`（本次运行的计数，重启归零）、
        `记忆：200 条 / 常看 短视频、直播`（标签云）、
        `看片笔记：抖音《暗区突围》游戏直播（时间线 4 条）`（原始标题 + 后台计数）。
        它们说的是"它采集到什么"，不是"它想明白了什么"，端到用户面前就还是生料。
        现在这里只放人话（全是本地拼的，不花一分钱接口费）；
        想看后台账就 `python -m pet.corpus` / `python -m pet.webstudy`，
        或看控制台里 `study_detail_text()` 那一行。
        """
        rows = [
            "%s · 屏幕边上的陪伴 AI" % self.cfg.persona.name,
            self.understanding_text(),
            self.watch_summary_text(),
            self.study_summary_text(),
        ]
        if str(getattr(self.cfg.capture, "target_process", "") or "").strip():
            rows.append(self.target_summary_text())
        return "\n".join(row for row in rows if row)

    def understanding_text(self) -> str:
        """一句话说清它现在"懂你什么"——用**消化过**的口径，不是采集口径。

        素材按可信度排：① 口味档案里看得最多的类型 + 会主动点赞的类型（全是本地统计）；
        ② 他反复看的词（`taste.hot_topics`）；都攒不出来时，才退到模型总结过的画像
        （`memory.profile`，那也已经是"想明白"的一句话，不是"看到"的一串字）。
        一条都没有就老实认生——别拿"记忆：200 条"这种数字冒充"我懂你"。
        """
        taste_log = getattr(self.worker, "taste", None)
        bits: List[str] = []
        if taste_log is not None:
            try:
                stats = taste_log.stats()
                ranked = sorted(
                    ((genre, need[0]) for genre, need in stats.items() if genre and genre != "其他"),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                if ranked:
                    bits.append(f"{ranked[0][0]}看得最多（{ranked[0][1]} 支）")
                liked = taste_log.liked_genres(1)
                if liked:
                    bits.append(f"会主动点赞的是{liked[0][0]}")
                words = taste_log.hot_topics(2)
                if words:
                    bits.append("常蹲" + "、".join(f"「{word}」" for word in words))
            except Exception as exc:
                print(f"[app] 读音口档案失败：{exc}")
        if not bits:
            try:
                profile = str(self.worker.memory.stats().get("profile") or "").strip()
            except Exception:
                profile = ""
            if profile:
                head = re.split(r"[。！？；;]", profile)[0].strip()
                if head:
                    bits.append(head[:42])
        if not bits:
            return "我懂你：还谈不上，先陪你看着"
        return "我懂你：" + "，".join(bits)

    def study_detail_text(self) -> str:
        """后台口径的那一行（本次几轮 / 语料多了几段 / 还有多少没消化）。

        只写日志、不往界面上摆：它说的是"采集和消化到哪一步了"，用户要看的是
        "你懂我什么"（见 `understanding_text`）。想看的时候就翻控制台。
        """
        parts: List[str] = [self.study_summary_text()]
        try:
            parts.append(self.worker.learn.digest_line())
        except Exception:
            pass
        return "｜".join(part for part in parts if part)

    def _toggle_visible(self, checked: bool) -> None:
        """显示挂件 / 收进托盘。

        收进托盘 = "这会儿不用你"：屏幕上没它了，**抓屏也整段停掉**（见 worker.set_hidden）
        ——人都走了，拍下来也没人看。这段时间它只干一件事：自己上网学（开着的话，
        见 pet/webstudy.py）；放回来的时候先把它刚学到的那句说出来。
        """
        self.window.setVisible(bool(checked))
        self.worker.set_hidden(not bool(checked))
        if hasattr(self, "act_show"):
            self.act_show.setChecked(bool(checked))
        if checked:
            self.window.raise_()
            greeting = self.worker.take_study_greeting()
            if greeting:
                # 它刚在后台补了课：一露面就把这句说出来（顺手让你知道它没闲着）
                self.window.say(greeting, "curious")
        else:
            self.window.close_bubble()
        self._sync_tray()

    def hide(self) -> None:
        """把挂件收进托盘：托盘图标还在，点一下就能叫回来。"""
        if not self.window.isVisible():
            return
        self._toggle_visible(False)

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.act_show.setChecked(not self.window.isVisible())
            self._toggle_visible(self.act_show.isChecked())

    def _sync_tray(self) -> None:
        """托盘上那两样东西跟着状态走：勾子（在不在屏幕上）和悬浮提示里的状态行。"""
        if self._tray is None or not hasattr(self, "act_show"):
            return
        self.act_show.setChecked(bool(self.window.isVisible()))
        self._tray.setToolTip(self._tray_tip())

    # ---------- 记忆 ----------

    def open_memory(self) -> None:
        path = self.worker.memory.path
        if not path.exists():
            self.worker.memory.save(force=True)
        opener = getattr(os, "startfile", None)
        if opener is None:
            print(f"[app] 记忆文件在：{path}")
            return
        try:
            opener(str(path))
        except Exception as exc:
            print(f"[app] 打开记忆文件失败：{exc}")

    def open_memory_archive(self) -> None:
        """打开**完整存档**（只增不减的那本流水）。

        跟「打开记忆文件」不是一回事：memory.json 只留最近 `max_entries` 条，
        这一份从第一天到现在一条没少——想看"它到底陪我看了些什么"就翻这个。
        存档还没有（刚装上）就先跟用户说一声，别弹出个不存在的路径。
        """
        archive = self.worker.memory.archive
        if not archive.count():
            self.window.say("完整存档还是空的，我还没攒下东西", "curious")
            print(f"[app] 完整存档还没有内容：{archive.path}")
            return
        opener = getattr(os, "startfile", None)
        if opener is None:
            print(f"[app] 完整存档在：{archive.path}")
            return
        try:
            opener(str(archive.path))
        except Exception as exc:
            print(f"[app] 打开完整存档失败：{exc}")

    def clear_memory(self) -> None:
        stats = self.worker.memory.stats()
        if stats["entries"]:
            answer = QMessageBox.question(
                self.window,
                "清除长期记忆",
                f"现在存了 {stats['entries']} 条记录和一份观众画像，清空之后不可恢复。确定吗？\n"
                f"（完整存档里那 {stats.get('archive', 0)} 行**不会动**，只是我从此想不起来了。）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.worker.memory.clear()
        self.window.say("记忆清空了，我们重新认识一下", "curious")

    def memory_summary_text(self) -> str:
        stats = self.worker.memory.stats()
        tags = "、".join(tag for tag, _ in stats["top_tags"][:4]) or "暂无"
        return f"记忆：{stats['entries']} 条 / 常看 {tags}"

    def target_summary_text(self) -> str:
        """托盘里显示「现在盯着谁」——锁了程序的时候一眼就能看出它在不在。"""
        target = str(getattr(self.cfg.capture, "target_process", "") or "").strip()
        if not target:
            return "监视范围：整块屏 / 手动划的范围"
        state = getattr(self.worker, "target_state", "off")
        note = {
            "ok": "正在看",
            "missing": "没找到窗口",
            "background": "切到后台了",
            "minimized": "收起来了，我按最后一眼陪着",
        }.get(state, "等着")
        return f"监视范围：只盯 {target}（{note}）"

    def watch_summary_text(self) -> str:
        """托盘里那行"它现在在看什么"——带**读明白**的东西，不是标题 + 计数。

        原来那行是 `看片笔记：抖音《暗区突围》游戏直播（时间线 4 条）`：标题原样贴，
        后面还跟一个后台计数。现在把笔记里读明白的那半句（玩法 > 看点 > 内容）挑一个
        给标题当前缀/后缀，读起来是"它在看这片、而且看懂了点什么"。
        """
        try:
            stats = self.worker.watch.stats()
        except Exception:
            return ""
        if not bool(getattr(self.cfg.watch, "enabled", True)):
            return "现在：看片笔记关着（watch.enabled = false）"
        if not stats.get("has_note"):
            return "现在：还在等画面换一支视频"
        title = str(stats.get("title") or "").strip()
        note = getattr(self.worker.watch, "current", None)
        tail = ""
        for name in ("playstyle", "point", "what"):
            value = str(getattr(note, name, "") or "").strip()
            if value:
                tail = value if len(value) <= 18 else value[:17] + "…"
                break
        if title and tail:
            return f"现在：{title[:18]}（{tail}）"
        if title:
            return f"现在：{title[:22]}"
        return "现在：还没看出这是啥，接着看"

    # ---------- 好友系统 ----------

    def open_friends(self) -> None:
        """弹出好友系统面板：加好友、给门口的客人开门、去谁家串门、让它本机溜一圈。"""
        if not bool(getattr(self.cfg.friends, "enabled", False)):
            self.window.say(
                "好友系统在配置里关着（friends.enabled = false），打开它我再开门迎客", "speechless"
            )
            return
        if self.friend_panel is None:
            self.friend_panel = FriendPanel(self.cfg, self.hub, self.book)
            self.friend_panel.closed.connect(self._sync_occluders)
        self.friend_panel.show_near(self.window)
        self._sync_occluders()

    def _sync_occluders(self) -> None:
        """把「也算挡住观看区域」的小窗都挂上：聊天面板 / 好友面板 / 各位客人。"""
        extra: List[QWidget] = [self.panel]
        if self.friend_panel is not None:
            extra.append(self.friend_panel)
        extra.extend(self._guest_windows.values())
        self.window.extra_occluders = extra
        self._update_occluder()

    def _refresh_friend_panel(self) -> None:
        """好友簿 / 客人名单变了：面板开着就重画一遍。"""
        if self.friend_panel is not None and self.friend_panel.isVisible():
            self.friend_panel.refresh()

    def _on_friend_status(self, text: str) -> None:
        """串门的状态行（正在敲门 / 进不去 / 到家了…）：不抢宠物的话，只记账。"""
        print(f"[friend] {text}")
        if self.friend_panel is not None and self.friend_panel.isVisible():
            self.friend_panel.add_note(text)
        if self.panel.isVisible():
            self.panel.add_notice(text)

    def _on_heard_doing(self, pet_id: str, text: str) -> None:
        """打听到「好友那边主人刚在干嘛」（见 pet/doing.py）：给人留一行。

        宠物自己那句话走气泡（`my_line` → say），这一行只是让**你**一眼看到打听回来的结果。
        """
        if not text:
            return
        print(f"[friend] {text}")
        if self.friend_panel is not None and self.friend_panel.isVisible():
            self.friend_panel.add_note(text)
        if self.panel.isVisible():
            self.panel.add_notice(text)

    def _on_acted(self, pet_id: str, info) -> None:
        """该做个动作了（见 pet/play.py）：自己那只在挂件窗口上，客人有它自己的窗口。"""
        data = dict(info) if isinstance(info, dict) else {}
        if not data:
            return
        if str(pet_id) == self.book.pet_id:
            self.window.act(data)
            return
        window = self._guest_windows.get(str(pet_id))
        if window is not None:
            window.act(data)

    def _on_played(self, text: str) -> None:
        """两只凑一起玩了一下：给人看的一行（台词它们各自会说）。"""
        if not text:
            return
        print(f"[friend] {text}")
        if self.friend_panel is not None and self.friend_panel.isVisible():
            self.friend_panel.add_note(text)

    # ---------- 有人来串门 ----------

    def _on_guest_arrived(self, guest) -> None:
        """客人进门：给它开一个自己的小窗口（它说的话全由 hub 递过来）。"""
        pet_id = str(getattr(guest, "pet_id", "") or "")
        if not pet_id:
            return
        window = self._guest_windows.get(pet_id)
        if window is not None:
            window.raise_()
            return
        window = GuestWindow(self.cfg, guest)
        window.poked.connect(self.hub.poke_guest)
        window.talkRequested.connect(self._talk_to_guest)
        window.playRequested.connect(self._play_with_guest)
        window.nudgeRequested.connect(self.hub.nudge)
        window.sendHomeRequested.connect(self._send_guest_home)
        window.moved.connect(lambda *_: self._update_occluder())
        self._guest_windows[pet_id] = window
        if not window.restore_pos():
            window.place_near(self.window.pet_global_rect(), slot=len(self._guest_windows) - 1)
        window.show()
        self._sync_occluders()
        self.window.say(f"{str(getattr(guest, 'label', '') or '有客人')} 来串门了", "curious")

    def _on_guest_said(self, pet_id: str, text: str, mood: str) -> None:
        """客人说了一句：画在它自己的气泡里（这句话是对方那边生成、hub 递过来的）。"""
        window = self._guest_windows.get(str(pet_id))
        if window is not None:
            window.say(text, mood)

    def _on_guest_left(self, pet_id: str, why: str) -> None:
        """客人走了：跟它道个别，片刻后把窗口收掉。"""
        window = self._guest_windows.pop(str(pet_id), None)
        if window is None:
            return
        window.say(f"那我先回家啦（{why}）", "happy")

        def close_it() -> None:
            window.shutdown()

        QTimer.singleShot(1800, close_it)
        self._sync_occluders()

    def _talk_to_guest(self, pet_id: str) -> None:
        """我直接跟客人说话（右键客人 / 面板里的按钮都走这儿）。"""
        guest = self.hub.guests.get(str(pet_id))
        if guest is None:
            return
        text, ok = QInputDialog.getText(self.window, "跟客人说句话", f"对「{guest.label}」说：")
        text = (text or "").strip()
        if not ok or not text:
            return
        if not self.hub.talk_to_guest(pet_id, text):
            self.window.say("这句话没送出去（可能它已经回家了）", "speechless")

    def _send_guest_home(self, pet_id: str) -> None:
        self.hub.drop_guest(str(pet_id))

    def _play_with_guest(self, pet_id: str) -> None:
        """右键客人 / 面板里的「跟它玩一下」：两只一起做个动作（见 pet/play.py）。"""
        if not self.hub.play_guest(str(pet_id)):
            self.window.say("它好像已经不在屏幕上了", "speechless")

    # ---------- 我家这只见世面去了 ----------

    def _on_my_line(self, text: str, mood: str) -> None:
        """串门时说的那些话（不管人在不在家，都是它自己的声音）。"""
        self.window.say(text, mood)
        if self.panel.isVisible():
            self.panel.add_notice(f"{self.cfg.persona.name}：{text}")

    def _on_away(self, row) -> None:
        """出门 / 到家（row=None 就是回来了）。"""
        if row is None:
            if self._away_hidden:
                self._away_hidden = False
                self.window.show()
                self._update_occluder()
            return
        label = friends_mod.FriendBook.label(row)
        if not bool(getattr(self.cfg.friends, "at_home", True)) and self.window.isVisible():
            # 配置里说"出门了家里就不露面"：把这位置让出来，回来再站回去
            self._away_hidden = True
            self.window.hide()
        self.window.say(f"我去 {label} 家串门了，到点自己回来", "happy")

    def _on_ask_in(self, knock) -> None:
        """有人在门口，等主人点头——提醒一声，顺手把面板刷出来。"""
        profile = (knock or {}).get("profile") or {}
        label = friends_mod.FriendBook.label(profile)
        self.window.say(f"{label} 在门口，让它进来吗？（右键我 → 好友陪伴）", "curious")
        if self.friend_panel is not None and self.friend_panel.isVisible():
            self.friend_panel.refresh()

    def _on_friend_asked(self, who: str, text: str) -> None:
        """好友本人对我的宠物说话了（它正在人家家里）——我在这儿把话接住。"""
        self.window.say(f"{who} 跟我说：{text}", "curious")
        if self.panel.isVisible():
            self.panel.add_notice(f"{who}：{text}")

    def _close_guest_windows(self) -> None:
        """退出前把客人一个个送回去（别让人家屏幕上留一只没主的宠物）。"""
        for window in list(self._guest_windows.values()):
            try:
                window.shutdown()
            except Exception:
                pass
        self._guest_windows.clear()

    # ---------- 退出 ----------

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        if self._occluder_timer is not None:
            self._occluder_timer.stop()
        # 先把客人送走、门关上：让人家屏幕上别留一只没有主的宠物
        self._close_guest_windows()
        try:
            self.hub.stop()
        except Exception as exc:
            print(f"[app] 关闭好友陪伴失败：{exc}")
        try:
            self.worker.stop()
            self.worker.wait(3000)
        except Exception:
            pass
        try:
            self.worker.memory.save(force=True)
        except Exception as exc:
            print(f"[app] 保存记忆失败：{exc}")
        if self._listen_task is not None:
            try:
                self._listen_task.wait(1500)   # 别把正在录音的那个线程丢在后台
            except Exception:
                pass
            self._listen_task = None
        self.panel.close()
        self.window.shutdown()
        if self._tray is not None:
            self._tray.hide()
        try:
            self.cfg.save()
        except Exception as exc:
            print(f"[app] 退出时保存配置失败：{exc}")
        self.app.quit()


# ---------- 命令行 ----------

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="screen-pet",
        description="屏幕边上的陪伴型 AI 挂件：看着你的视频，时不时吐槽一句。",
    )
    parser.add_argument("--config", help="配置文件路径，默认项目根目录的 config.json")
    parser.add_argument("--provider", choices=sorted(PROVIDER_PRESETS), help="临时切换接口提供方")
    parser.add_argument("--interval", type=float, help="看画面的间隔（秒）")
    parser.add_argument("--pet-size", type=int, help="形象直径（像素）")
    parser.add_argument(
        "--console",
        action="store_true",
        help="留着那个控制台窗口别藏（默认会把它藏起来，只藏不关）",
    )
    parser.add_argument("--no-ocr", action="store_true", help="不读弹幕/字幕（省一点开销）")
    parser.add_argument(
        "--no-learn", action="store_true", help="关掉边看边学（不再把字幕攒成接话样本）"
    )
    parser.add_argument(
        "--learn-promote",
        action="store_true",
        help="边看边学攒够了对就自己写进 data/chat_style.json（默认只记不写；要长期开着请改 config.json）",
    )
    parser.add_argument("--no-memory", action="store_true", help="关闭长期记忆，这次不写 memory.json")
    parser.add_argument("--no-proactive", action="store_true", help="关掉主动搭话（只在画面有槽点时说话）")
    parser.add_argument(
        "--target-process",
        help="只监视这个程序（exe 名，比如 Douyin.exe / chrome.exe / Tabbit Browser.exe）",
    )
    parser.add_argument(
        "--no-target-process", action="store_true", help="取消只盯某个程序，回到整屏 / 框选"
    )
    parser.add_argument("--clear-memory", action="store_true", help="清空长期记忆后退出")
    parser.add_argument("--selftest", action="store_true", help="无界面自检（截屏 + OCR + 试调一次模型）")
    parser.add_argument("--image", help="自检时用这张图片代替实时截屏")
    parser.add_argument(
        "--pick-region",
        action="store_true",
        help="只划观看范围（拖一块 = 只看这块，双击 = 整块屏），保存后退出",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    make_console_safe()
    args = _parse_args(argv)

    # 命令行都解析完了，顺手把那个碍眼的控制台窗口藏掉（只藏不关，进程照跑）。
    # 它只管"双击 / start 起的那个窗口"，你自己终端里跑的时候它一动不动。
    if not args.console:
        hide_console_window()

    cfg = Config.load(Path(args.config) if args.config else None)
    print(f"[app] {paths.describe()}")

    # 第一次运行：照着 config.example.json 落一份 config.json（打包版落在 %APPDATA%\\ScreenPet）。
    # 用模板而不是直接 Config()：模板里是给人改的那份（带注释字段、推荐值），
    # 而且不会把这次命令行的临时参数写进去。
    first_run = False
    if not cfg.config_path().exists() and not args.selftest:
        first_run = True
        if not paths.seed_config(cfg.config_path()):
            template = Config()
            template.apply_preset()
            template.save(cfg.config_path())

    if args.provider:
        apply_override(cfg, "provider", args.provider)
        apply_override(cfg, "base_url", "")
        apply_override(cfg, "model", "")
        cfg.apply_preset()
    if args.interval:
        apply_override(cfg, "capture.interval_sec", float(args.interval))
    if args.pet_size:
        apply_override(cfg, "ui.pet_size", int(args.pet_size))
    if args.no_ocr:
        apply_override(cfg, "ocr.enabled", False)
    if args.no_learn:
        apply_override(cfg, "learn.enabled", False)
    if args.learn_promote:
        apply_override(cfg, "learn.promote", True)
    if args.no_memory:
        apply_override(cfg, "memory.enabled", False)
    if args.no_proactive:
        apply_override(cfg, "proactive.enabled", False)
    if args.target_process:
        apply_override(cfg, "capture.target_process", str(args.target_process))
        apply_override(cfg, "capture.region", None)
    if args.no_target_process:
        apply_override(cfg, "capture.target_process", "")

    if args.clear_memory:
        from .memory import Memory

        memory = Memory(cfg)
        memory.clear()
        print(f"已清空长期记忆：{memory.path}")
        return 0

    if args.selftest:
        return selftest.run(cfg, args.image)

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv[:1])
    app.setApplicationName("screen-pet")
    app.setQuitOnLastWindowClosed(False)

    pet = ScreenPet(cfg, app)
    pet.start()
    if first_run:
        # 第一次启动指个路：打包版装在 Program Files 里，用户根本不知道配置写到哪儿去了
        QTimer.singleShot(1600, pet.first_run_hint)
    if args.pick_region:
        QTimer.singleShot(300, lambda: pet.pick_region(quit_after=True))
    return app.exec()



