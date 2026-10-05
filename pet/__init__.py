"""screen-pet：会看视频、会吐槽的桌面陪伴型 AI 挂件。

模块划分：
    paths.py    程序该往哪儿读、往哪儿写（源码跑 = 项目根目录；打包版 = %APPDATA%\\ScreenPet）
    config.py   配置读写（config.json + 环境变量）
    dpi.py      Qt 逻辑坐标 <-> 屏幕物理像素换算
    capture.py  截屏、缩放、感知哈希（dHash）、JPEG 编码
    ocr.py      弹幕/字幕识别（Windows 自带 OCR / rapidocr，带缓存与降级）
    keyinfo.py  画面关键信息提取（本地认台标 / 节目名 / 集数 / 话题人名，喂给提示词当抓手）
    mood.py     情绪标签解析（开心/无语/激动/吐槽/好奇）——只驱动表情和配色，不上界面
    dialog.py   对话类型（记录/分析/学习，私下分类：怎么记、怎么分析、怎么学）——不上界面
    scene.py    5W1H 场景解析（人物/事件/时间/地点/原因，从模型第二行剥出来）
    memory.py   长期记忆：打标签、观众画像、memory.json 读写
    memarchive.py 完整记忆存档：memory.json 会裁剪，这本流水只增不减（一条都不丢）
    hotkey.py   全局热键（Windows RegisterHotKey + 消息循环线程）
    proactive.py 主动搭话（什么时候该自己开口：闲置判定 + 冷却 + 每小时上限）
    foreground.py 前台窗口标题
    winfind.py  按进程找窗口（"只盯某个程序"：抖音 / B站 / 游戏；最小化的窗口给"还原后"的矩形）
    wincap.py   直接抓某个窗口自己的画面（被别的窗口盖住也能看；最小化的窗口抓不到就是抓不到）
    humanstyle.py 真人聊天语料 + 反车轱辘话 + 短反应池（口语、说话角度、相似度）
    taste.py    口味档案（每支视频分类 + 点赞收藏关注打分 + 玩法/画风/玩家群体 → 摸清他爱看啥）
    corpus.py   边看边学：每一眼把字幕攒成"别人怎么接话"的样本，够格了自己消化进语料（采集 + 消化两条线）
    watchlog.py 看片笔记（换视频先读一遍；主题/类型/内容/看点/玩法/画风/玩家群体 + 时间线检索）
    webstudy.py 隐身学习：收进托盘后自己上网补课（搜 → 读正文 → 过闸 → 写语料和记忆；补过几个话题看账本）
    persona.py  人设提示词（看准 5W1H + 说人话 + 群聊小助手腔）
    vlm.py      视觉大模型客户端（OpenAI 兼容 / mock / 画像总结 / 用户对话）
    asr.py      语音输入（Windows 自带识别引擎，离线；asr.ps1 是那层壳）
    sprite.py   形象渲染（内置矢量形象 + 图片帧）
    style.py    长相（颜色 / 身形 / 五官 / 小开关；用户在 config.json 的 ui.pet_style 里改，
                或用 tools/design_pet.py 现调现换）
    overlay.py  手动划"观看范围"的全屏遮罩（划一块，或者双击选整块屏）
    window.py   挂件窗口 + 吐槽气泡窗口
    chatpanel.py 打字/语音聊天面板
    net.py      好友串门的线路（名片 / HTTP 小服务 / 形象帧）
    friends.py  好友陪伴的规矩（好友簿、出访、接待、谁负责生成哪句台词）
    guest.py    屏幕上那只客人的样子（一位客人一个窗口）
    friendpanel.py 好友陪伴的控制台（名片 / 好友 / 门口 / 客人）
    doing.py    「我这边主人这会儿在忙什么」→ 一句能过网的话（串门时捎带 / 打听，尺度在这儿）
    play.py     两只桌宠一起做点什么（动作表 + 给动画留的那个口子）
    worker.py   后台分析线程（抓屏/抓窗口 + 连拍 + 定期重读看片笔记 + 换视频/互动当场接话）
    selftest.py 无界面自检
    app.py      组装与入口
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

__version__ = "0.8.0"


def make_console_safe() -> None:
    """让 print 在 GBK 控制台里不至于崩；输出被重定向到文件时统一写 UTF-8。

    中文 Windows 的控制台是 GBK，OCR/模型返回里偶尔会有 GBK 编不出来的字符
    （比如 É），不处理的话 print 会直接抛 UnicodeEncodeError 把程序打断。

    编码分两种情况：**终端**上保持系统默认（硬改成 UTF-8 反而会在 GBK 控制台里显示乱码），
    只把错误策略改成替换；但 `python main.py > run.out` 这种**重定向到文件**时改成 UTF-8，
    这样日志文件用编辑器或工具打开不会是一堆乱码。

    另外，用 `pythonw` 起的时候压根没有控制台（双击 run.cmd 就是这条路）——
    这时先把 print 接到 run.out 上，免得日志"说没就没"。
    """
    _attach_log_file()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        kwargs = {"errors": "replace"}
        if not getattr(stream, "isatty", lambda: False)():
            kwargs["encoding"] = "utf-8"
        try:
            reconfigure(**kwargs)
        except Exception:
            pass


def _attach_log_file() -> Optional[TextIO]:
    """没有控制台时，把 print 接到 run.out 上（双击 run.cmd 走的就是这条路）。

    为什么要跑这一趟：双击 run.cmd 是用 `pythonw` 起挂件的——屏幕上不该有黑框，
    但日志不能说没就没（`[watch]` `[scene]` `[dialog]` 这些行是排查问题的唯一线索）。

    触发条件有两种，二者其一即可：

    * `run.cmd` 设了 `PET_LOG=1`——最可靠的一条。因为用 `start` 起的 pythonw 会**继承**
      调用方的标准句柄（从终端里跑时它照样有 stdout），光看"有没有 stdout"判断不出来；
    * 压根没有 stdout / stderr（纯 GUI 启动）。

    **其余情况一律不动**：`python main.py` 在终端里跑就打在终端，
    `python main.py > run.out` 这种自己重定向的也照旧。
    """
    forced = bool(os.environ.get("PET_LOG"))
    if not forced and sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        # 日志落在"该写的地方"：源码里跑 = 项目根目录（跟以前一样），
        # 打包版 = %APPDATA%\ScreenPet（exe 自己待的目录是只读的，写不进去）
        from . import paths

        log_path = paths.user_dir() / "run.out"
        # buffering=1 = 行缓冲：print 一行就落一行，别等缓冲区满
        # （挂件是挂着跑几天的进程，攒着不写等于没有日志）。
        handle = open(  # noqa: SIM115 —— 这个句柄要跟进程同生共死，不能 with
            log_path,
            "a",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
    except OSError:
        return None
    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    handle.flush()
    sys.stdout = handle
    sys.stderr = handle
    return handle


def own_console_window() -> int:
    """控制台窗口句柄：**只在「这个控制台是给我们单独开的」时才返回它**，否则返回 0。

    判断依据是 GetConsoleProcessList——挂在同一个控制台下的进程一共有哪些：

    * 只有我们自己：说明这个窗口是跟着挂件一起冒出来的（双击脚本、`start` 起的），
      可以放心处理，返回句柄 > 0；
    * 还挂着 cmd.exe / powershell.exe：那是用户自己的终端（他就是在里面敲的命令），
      说它不是我们的，返回 0——绝不能动别人的窗口。

    非 Windows、或者压根没有控制台（`pythonw` / 缩成一团的 GUI 进程），同样返回 0。
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        hwnd = int(kernel32.GetConsoleWindow() or 0)
        if not hwnd:
            return 0
        pid = int(kernel32.GetCurrentProcessId())
        buffer = (ctypes.c_uint32 * 8)()
        count = int(kernel32.GetConsoleProcessList(buffer, 8))
        count = max(0, min(count, 8))
        if any(int(buffer[i]) != pid for i in range(count)):
            return 0
        return hwnd
    except Exception:
        return 0


def hide_console_window() -> bool:
    """把跟着挂件冒出来的那个控制台窗口**藏起来**（只藏，绝不关）。

    为什么只藏不关：控制台窗口是挂件进程的宿主窗口，关掉它等于把挂件一起杀掉——
    用户要的是"别弹这个黑框"，不是"别跑"。`ShowWindow(SW_HIDE)` 之后窗口看不见了，
    进程照跑、日志照打（重定向到 run.out 的也照写）。

    用户自己终端里跑的时候（见 own_console_window）什么都不做，返回 False。
    """
    if not own_console_window():
        return False
    try:
        import ctypes

        ctypes.windll.user32.ShowWindow(own_console_window(), 0)  # SW_HIDE
        return True
    except Exception:
        return False
