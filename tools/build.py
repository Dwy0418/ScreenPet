"""把挂件打包成 Windows 软件：`python tools/build.py`

    python tools/build.py              打包成 dist/ScreenPet/ScreenPet.exe（一个文件夹，推荐）
    python tools/build.py --onefile    打包成单个 dist/ScreenPet.exe（方便拷贝，启动慢一点）
    python tools/build.py --zip        压成 dist/ScreenPet-<版本>.zip（发给别人就是这个）
    python tools/build.py --clean      打包前先清掉 build/ 和 dist/

打包出来是**绿色版**：解压 → 双击 `ScreenPet.exe` 就能用，不用装 Python。
配置、记忆、语料写在 `%APPDATA%\\ScreenPet\\`（见 pet/paths.py），所以放在
`C:\\Program Files` 里也写得进去；要"开始菜单 + 卸载项"就用 `installer/ScreenPet.iss`
（Inno Setup，本机装了 Inno 才做得出来）。

打包机上要装的东西：`pip install -r requirements-dev.txt`（PyInstaller 只有打包才用，
玩家不用装）。图标是现画的，缺 `assets/app.ico` 时会顺手调 tools/make_icon.py 生成。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pet import __version__          # noqa: E402
from pet import paths as pet_paths   # noqa: E402

APP_NAME = "ScreenPet"
ENTRY = "main.py"

#: 要一起塞进 exe 的**素材**（只读的东西）：形象帧、默认配置、Windows OCR / 语音那两层脚本。
#: 写的东西（config/memory/data）不进包，运行时落在 user_dir()，见 pet/paths.py。
#: 注意只带 `assets/pet`：`assets/source` 是画形象用的素材（3.6MB），运行时用不到。
BUNDLED = (
    ("assets/pet", "assets/pet"),
    ("config.example.json", "."),
    ("pet/winocr.ps1", "pet"),
    ("pet/asr.ps1", "pet"),
)


def _run(cmd) -> None:
    print("[build] " + " ".join(str(part) for part in cmd))
    result = subprocess.run([str(part) for part in cmd], cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(f"[build] 打包失败（退出码 {result.returncode}）")


def _sync_iss_version() -> None:
    """把安装脚本里的版本号对齐到 `pet.__version__`。

    安装包（Inno）和 exe 是两次编译，版本号写两处迟早对不上：装出来显示的还是老版本。
    这里打包时顺手改写那一行——一处真源（pet/__init__.py），别的地方都跟着它走。
    """
    iss = ROOT / "installer" / "ScreenPet.iss"
    if not iss.exists():
        return
    try:
        text = iss.read_text(encoding="utf-8")
        new = re.sub(r'(#define MyAppVersion ")[^"]*(")', rf"\g<1>{__version__}\g<2>", text, count=1)
        if new != text:
            iss.write_text(new, encoding="utf-8")
            print(f"[build] 安装脚本版本号已同步成 {__version__}")
    except Exception as exc:
        print(f"[build] 同步安装脚本版本号失败（不影响打包）：{exc}")


def _version_file() -> Path:
    """给 exe 写一份文件属性（右键 → 属性 → 详细信息里能看到版本、说明、版权）。

    这是 PyInstaller 的 version-file 格式，必须长这样，写错了它直接报错。
    """
    target = ROOT / "build" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    parts = [int(part) for part in (__version__.split(".") + ["0", "0", "0"])[:4]]
    numbers = ", ".join(str(part) for part in (parts + [0])[:4])
    target.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({numbers}),
    prodvers=({numbers}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'screen-pet'),
        StringStruct('FileDescription', '{APP_NAME} · 屏幕边上的陪伴 AI'),
        StringStruct('FileVersion', '{__version__}'),
        StringStruct('InternalName', '{APP_NAME}'),
        StringStruct('OriginalFilename', '{APP_NAME}.exe'),
        StringStruct('ProductName', '{APP_NAME}'),
        StringStruct('ProductVersion', '{__version__}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    return target


def main(argv=None) -> int:
    args = [str(arg) for arg in (sys.argv[1:] if argv is None else argv)]
    onefile = "--onefile" in args or "-F" in args
    want_zip = "--zip" in args

    if "--clean" in args:
        for name in ("build", "dist"):
            shutil.rmtree(ROOT / name, ignore_errors=True)
            print(f"[build] 清掉 {name}/")

    try:
        import PyInstaller.__main__  # noqa: F401
    except ImportError:
        print("[build] 没装 PyInstaller。先装一次（只有打包机需要）：")
        print("        pip install -r requirements-dev.txt")
        return 1

    icon = ROOT / "assets" / "app.ico"
    if not icon.exists():
        print("[build] 还没有 assets/app.ico，先生成图标")
        _run([sys.executable, ROOT / "tools" / "make_icon.py"])

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",                 # 不弹黑框（GUI 程序）
        "--name", APP_NAME,
        "--icon", str(icon),
        "--version-file", str(_version_file()),
        # 少背点没用的东西（tkinter / 测试框架），包能小一圈
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "test",
    ]
    cmd.append("--onefile" if onefile else "--onedir")
    for src, dest in BUNDLED:
        if (ROOT / src).exists():
            cmd += ["--add-data", f"{src}{os.pathsep}{dest}"]
        else:
            print(f"[build] 跳过不存在的素材：{src}")
    cmd.append(str(ROOT / ENTRY))
    _run(cmd)
    _sync_iss_version()

    if onefile:
        exe = ROOT / "dist" / f"{APP_NAME}.exe"
    else:
        exe = ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe"
    if not exe.exists():
        print(f"[build] 没看到 {exe}，检查上面的日志")
        return 1
    print(f"[build] 好了：{exe}（{exe.stat().st_size / 1024 / 1024:.1f} MB）")

    if want_zip:
        target = ROOT / "dist" / f"{APP_NAME}-{__version__}.zip"
        base = ROOT / "dist" / APP_NAME
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            if onefile:
                zf.write(exe, exe.name)
            else:
                for path in sorted(base.rglob("*")):
                    if path.is_file():
                        zf.write(path, Path(APP_NAME) / path.relative_to(base))
        print(f"[build] 压缩包：{target}（{target.stat().st_size / 1024 / 1024:.1f} MB）")

    print(f"[build] 位置说明：{pet_paths.describe()}")
    print("[build] 提醒：exe 是绿色版，配置写在 %APPDATA%\\ScreenPet；要安装包用安装脚本")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
