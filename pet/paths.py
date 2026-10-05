"""程序该往哪儿读、往哪儿写。

两种跑法，两套位置（这是"打包成 exe 给别人用"必须解决的第一件事）：

    python main.py（源码里跑）
        跟以前一样：`config.json` / `memory.json` / `data/` 就在项目根目录，
        `assets/` 也在那儿。**老用户的配置和记忆一个字都不用挪。**

    ScreenPet.exe（打包之后）
        exe 自己待的地方（一般是 `C:\\Program Files\\...`）是**只读**的，
        往那儿写配置在别人的电脑上直接报错。所以配置、记忆、语料一律写到
        `%APPDATA%\\ScreenPet\\`；`assets/` 这种只读资源跟着 exe 走
        （PyInstaller 解包目录 `sys._MEIPASS`）。

想固定在一个地方跑（U 盘、便携版、测试），把环境变量 `PET_HOME` 指过去就行：
两种跑法都认它。

一句话给后面用：**要写的东西走 `user_dir()`，要读的素材走 `resource_dir()`。**
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "ScreenPet"          # 用户目录名 / 程序名（打包后 exe 也叫这个）


def is_frozen() -> bool:
    """这会儿是不是从打包好的 exe 里跑的（PyInstaller 会设 sys.frozen）。"""
    return bool(getattr(sys, "frozen", False))


def code_dir() -> Path:
    """源码目录（`pet/` 的上一层）。打包之后这个是解包目录里的路径，别往这儿写。"""
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """只读素材在哪儿：`assets/`、`config.example.json`、`pet/winocr.ps1` 都从这儿找。"""
    meipass = getattr(sys, "_MEIPASS", "")
    return Path(meipass) if meipass else code_dir()


def asset_dir() -> Path:
    return resource_dir() / "assets"


def user_dir() -> Path:
    """该写的地方：配置 / 记忆 / 语料 / 账本都在这儿。

    - 源码里跑 = 项目根目录（老行为，别把现成的配置挪走）
    - 打包后 = `%APPDATA%\\ScreenPet\\`（装在 Program Files 里也写得进去）
    - `PET_HOME` 非空时一律听它的
    """
    override = (os.environ.get("PET_HOME") or "").strip()
    if override:
        path = Path(override).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path
    if not is_frozen():
        return code_dir()
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    """配置文件：`user_dir()/config.json`（第一次运行会照着 config.example.json 生成）。"""
    return user_dir() / "config.json"


def example_config_path() -> Path:
    """默认配置模板。打包时会跟 assets 一起塞进 exe，所以打包后从 resource 里拿。"""
    packaged = resource_dir() / "config.example.json"
    if packaged.exists():
        return packaged
    return code_dir() / "config.example.json"


def describe() -> str:
    """给日志用的一行：现在从哪儿读、往哪儿写（排查"配置写了不生效"靠它）。"""
    how = "打包版" if is_frozen() else "源码版"
    return f"{how}｜写：{user_dir()}｜读：{resource_dir()}"


def seed_config(dest: Path) -> bool:
    """第一次运行：把 `config.example.json` 抄成第一份 `config.json`。

    抄不动（模板丢了 / 没权限）就返回 False，调用方用自己的内置默认值兜底——
    "第一次启动"这条路不能因为一个模板文件走不通。
    """
    example = example_config_path()
    if not example.exists():
        return False
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        return True
    except Exception as exc:
        print(f"[paths] 生成 {dest.name} 失败（改用内置默认值）：{exc}")
        return False
