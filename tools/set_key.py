"""把 API Key 写进 config.json 的小工具，顺带试连一次看通不通。

    python tools/set_key.py                     # 交互式：选 provider → 粘 Key → 试连
    python tools/set_key.py --provider zhipu    # 指定 provider（跳过第一步）
    python tools/set_key.py --check             # 只测当前配置能不能通，不改文件
    python tools/set_key.py --mock              # 回到离线模式（清掉 Key）
    python tools/set_key.py --restart           # 填完顺手把挂件重启到新配置

双击项目根目录的 set-key.cmd 就等于交互式跑一遍。

为什么不建议你把 Key 发在聊天里：Key 等于账号凭证，只有写进本机的
config.json（已在 .gitignore 里，不会进 git）最稳。这个工具在本地读写，
粘贴时也不回显。

另外有个坑它会帮你看着：挂件退出时会把内存里的配置写回 config.json，
所以"挂件还开着的时候改配置文件"会被它盖掉。要动配置就先退出挂件，
或者直接加 --restart 让本工具先停掉再启动。
"""
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pet import make_console_safe  # noqa: E402
from pet.config import APP_DIR, CONFIG_PATH, PROVIDER_PRESETS, Config  # noqa: E402
from pet.vlm import VlmError, VisionClient  # noqa: E402

DEFAULT_PROVIDER = "zhipu"

# 去哪儿拿 Key（本工具替你把步骤也打出来，省得来回翻文档）
PROVIDER_GUIDE = {
    "zhipu": (
        "https://open.bigmodel.cn/",
        "用手机号注册/登录 → 左侧「API Keys」→「创建 API Key」（首次可能要求实名）→ 复制那串 xxxxxxxx.yyyyyyyy",
    ),
    "dashscope": (
        "https://bailian.console.aliyun.com/",
        "阿里云百炼控制台 →「API-KEY 管理」→ 创建新的 API-KEY（形如 sk-xxxxxxxx）",
    ),
    "siliconflow": (
        "https://cloud.siliconflow.cn/",
        "硅基流动控制台 →「API 密钥」→ 新建 API 密钥（形如 sk-xxxxxxxx）",
    ),
    "openai": (
        "https://platform.openai.com/api-keys",
        "OpenAI 控制台 → API keys → Create new secret key",
    ),
    "ollama": (
        "https://ollama.com/download",
        "装好 ollama 后执行 `ollama pull qwen2.5vl:7b`，本地服务不需要 Key",
    ),
}

# 各家 Key 长什么样（只用来提醒，不拦着）
KEY_HINTS = {
    "zhipu": "智谱的 Key 长这样：xxxxxxxx.yyyyyyyy，中间有一个点",
    "dashscope": "DashScope 的 Key 以 sk- 开头",
    "siliconflow": "硅基流动的 Key 以 sk- 开头",
    "openai": "OpenAI 的 Key 以 sk- 开头",
}

# 试连失败时按状态码给一句人话
FAIL_HINTS = {
    "401": "Key 不对或者过期了，回控制台重新复制一次",
    "403": "Key 没权限，或者这个模型没开通",
    "404": "接口地址或模型名不对，删掉 config.json 里的 base_url/model 让预设生效试试",
    "429": "被限流了，或者免费额度用完了，等一会儿或去控制台看额度",
}


def mask(key: str) -> str:
    """Key 只露头尾：方便核对有没有粘错，又不至于落在日志和截图里。"""
    text = (key or "").strip()
    if not text:
        return "(空)"
    if len(text) <= 8:
        return text[0] + "*" * (len(text) - 1)
    return f"{text[:4]}…{text[-4:]}（共 {len(text)} 位）"


def sanitize(raw: str) -> str:
    """顺手处理复制常见的手滑：前后空格、引号、Bearer 前缀、换行。"""
    text = (raw or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text.strip().strip('"').strip("'").strip()


def looks_like_key(provider: str, key: str) -> bool:
    if provider == "ollama":
        return True
    if provider == "zhipu":
        return "." in key and len(key) >= 20
    return key.startswith("sk-") or len(key) >= 24


def find_running_pet() -> List[int]:
    """找出正在跑的挂件进程（它退出时会把内存里的旧配置写回文件）。"""
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
        "Where-Object { $_.CommandLine -like '*main.py*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            # 中文系统上 ps / taskkill 的回话是 GBK，解释器要是跑在 UTF-8 模式
            # （`python -X utf8`、PYTHONUTF8=1），按 UTF-8 解会炸在读取线程里。
            # 这里只认行里的数字，认不出的字换成占位符就行。
            errors="replace",
            timeout=20,
        )
    except Exception:
        return []
    ids: List[int] = []
    for line in (done.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            ids.append(int(line))
    return ids


def stop_pets(ids: List[int]) -> None:
    """结束挂件。window.py 没处理 WM_CLOSE，只能 /F；记忆里最多丢最后一小段。"""
    for pid in ids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, text=True, errors="replace", timeout=15)
        except Exception as exc:
            print(f"  结束 PID {pid} 失败：{exc}")


def start_pet() -> Optional[int]:
    """后台把挂件拉起来，输出接到 run.out / run.err（这两个文件在 .gitignore 里）。"""
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        out = open(APP_DIR / "run.out", "ab")
        err = open(APP_DIR / "run.err", "ab")
    except Exception as exc:
        print(f"  打不开日志文件：{exc}")
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, "main.py"],
            cwd=str(APP_DIR),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=flags,
        )
    except Exception as exc:
        print(f"  启动挂件失败：{exc}")
        return None
    finally:
        out.close()
        err.close()
    return proc.pid


def check(cfg: Config) -> bool:
    """拿真实接口试一次纯文本问答：只验 Key + 地址 + 模型名，不涉及截屏。"""
    if cfg.provider == "mock":
        print("离线模式（mock）本来就不联网，没什么可测的。")
        return True
    if not cfg.ready:
        print(f"provider={cfg.provider} 但没填 API Key，先填 Key 再测。")
        return False
    print(f"试连 {cfg.model} @ {cfg.endpoint} ...")
    started = time.monotonic()
    try:
        reply = VisionClient(cfg).summarize("只回四个字：连接正常", max_tokens=16)
    except VlmError as exc:
        detail = str(exc)
        print(f"✗ 不通：{detail}")
        for code, hint in FAIL_HINTS.items():
            if code in detail:
                print(f"  提示：{hint}")
                break
        else:
            print("  提示：检查网络/代理；用本地模型的话确认 ollama serve 在跑")
        return False
    except Exception as exc:  # 兜底，别让工具崩在奇怪的异常上
        print(f"✗ 出错：{exc}")
        return False
    spent = time.monotonic() - started
    print(f"✓ 通了（{spent:.1f}s），模型回了：{sanitize(str(reply))[:40] or '(空)'}")
    return True


def ask_provider() -> str:
    choices = [name for name in PROVIDER_PRESETS if name != "mock"]
    print("可选的接口提供方（都是 OpenAI 兼容接口，换一家只是换个地址和模型名）：")
    for index, name in enumerate(choices, 1):
        preset = PROVIDER_PRESETS[name]
        mark = " ←推荐，有免费额度" if name == DEFAULT_PROVIDER else ""
        print(f"  {index}. {name:<12} {preset['model']:<30} {preset['base_url'] or '(本地)'}{mark}")
    raw = (input(f"选一个（直接回车 = {DEFAULT_PROVIDER}）：") or "").strip().lower()
    if not raw:
        return DEFAULT_PROVIDER
    if raw.isdigit() and 1 <= int(raw) <= len(choices):
        return choices[int(raw) - 1]
    if raw in choices:
        return raw
    print(f"没认出「{raw}」，就用默认的 {DEFAULT_PROVIDER}。")
    return DEFAULT_PROVIDER


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="set-key",
        description="把 API Key 写进 config.json 并试连一次（不动其它配置）。",
    )
    parser.add_argument("--provider", choices=sorted(PROVIDER_PRESETS), help="接口提供方，默认交互选择")
    parser.add_argument("--key", help="直接给 Key（会留在命令行历史里，不建议）")
    parser.add_argument("--mock", action="store_true", help="回到离线模式：provider=mock 并清掉 Key")
    parser.add_argument("--check", action="store_true", help="只测当前配置能不能通，不改配置文件")
    parser.add_argument("--restart", action="store_true", help="写完顺手重启挂件（会先结束在跑的那个）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    make_console_safe()
    args = _parse_args(argv)
    cfg = Config.load(CONFIG_PATH)
    path = cfg.config_path()
    print("=== screen-pet 填 Key ===")
    print(f"配置文件：{path}")

    if args.check:
        return 0 if check(cfg) else 1

    provider = "mock" if args.mock else (args.provider or ask_provider())
    key = ""
    if provider != "mock":
        guide = PROVIDER_GUIDE.get(provider)
        if guide:
            print(f"\n还没拿到 Key 的话（{provider}，大概 1 分钟）：")
            print(f"  1. 打开 {guide[0]}")
            print(f"  2. {guide[1]}")
        key = sanitize(args.key or os.environ.get("PET_KEY", ""))
        if not key and provider == "ollama":
            key = "ollama"
            print("\n本地 Ollama 不需要 Key，我填了个占位符。")
        if not key:
            print("\n把 Key 粘进来（输入不回显，粘完按回车；Ctrl+C 取消）：")
            try:
                key = sanitize(getpass.getpass("API Key: "))
            except (EOFError, KeyboardInterrupt):
                print("\n已取消，配置文件没动。")
                return 1
        if not key:
            print("没填 Key，已取消（配置文件没动）。")
            return 1
        print(f"收到：{mask(key)}")
        hint = KEY_HINTS.get(provider, "")
        if hint and not looks_like_key(provider, key):
            print(f"提醒：{hint}。看着不太像，我先照样写进去，等会试连就知道对不对。")

    cfg.provider = provider
    cfg.api_key = key
    cfg.base_url = ""   # 交给预设填，顺便清掉上次换 provider 时留下的旧地址
    cfg.model = ""
    cfg.apply_preset()
    try:
        cfg.save(path)
    except Exception as exc:
        print(f"写配置文件失败：{exc}")
        return 1

    print(f"\n已写入 {path}")
    print(f"  provider : {cfg.provider}")
    print(f"  base_url : {cfg.base_url or '(不联网)'}")
    print(f"  model    : {cfg.model or '(不联网)'}")
    print(f"  api_key  : {mask(cfg.api_key)}")
    if os.environ.get("PET_API_KEY"):
        print("  注意：环境变量 PET_API_KEY 优先级更高，它会盖掉文件里的 Key。")

    ok = check(cfg)
    print()

    running = find_running_pet()
    if running and args.restart:
        pids = "、".join(str(pid) for pid in running)
        print(f"先停掉正在跑的挂件（PID {pids}）...")
        stop_pets(running)
        time.sleep(1.5)
        new_pid = start_pet()
        print(f"已用新配置重新启动（PID {new_pid}）。" if new_pid else "重启失败，双击 run.cmd 手动起一下吧。")
    elif running:
        pids = "、".join(str(pid) for pid in running)
        print(f"检测到挂件正在运行（PID {pids}）：")
        print("  · 它用的还是旧配置，要重启才生效；")
        print("  · 而且它退出时会把内存里的配置写回文件，会盖掉刚填的 Key。")
        print("  建议：跑 `set-key.cmd --restart`（先停再起），或者托盘右键退出后双击 run.cmd。")
    elif args.restart:
        new_pid = start_pet()
        print(f"已启动挂件（PID {new_pid}）。" if new_pid else "启动失败，双击 run.cmd 手动起一下吧。")
    else:
        print("接下来：双击 run.cmd（或 python main.py）就能用新模型吐槽了。")

    if not ok:
        print("提示：Key 没通，配置我先留着了（改好再跑 `set-key.cmd --check`）；想先回离线模式就 `set-key.cmd --mock`。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
