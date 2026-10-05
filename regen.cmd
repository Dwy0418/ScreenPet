@echo off
rem 重新生成形象 + 重启挂件：改完源图（或调完 make_pet 的参数）之后跑这个，
rem 一条命令就能看到效果，不用自己先关挂件再开一次。
rem
rem   regen.cmd                        用下面 SRC 指定的源图重新生成一遍，然后重启挂件
rem   regen.cmd --heal 6 --shade 0.7   后面的参数原样转给 tools\make_pet.py
rem
rem 换源图：改下面 SRC 那一行，或者直接跑 tools\make_pet.py（见 README「换形象」那节）。
rem
rem 提示语一律用 ASCII：cmd 在中文 Windows 上是 GBK 控制台，UTF-8 的中文会显示成乱码。
setlocal
cd /d "%~dp0"

set "SRC=assets\source\pet_new.png"

echo [regen] stopping the running pet ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul
rem 等它把窗口关干净（timeout 在 stdin 被重定向时会直接失败，所以用 ping 拖一下）
ping -n 2 127.0.0.1 >nul

echo [regen] rebuilding sprite frames ...
python tools\make_pet.py "%SRC%" %*
if errorlevel 1 (
    echo [regen] build failed - pet left stopped, see the error above.
    exit /b 1
)

echo [regen] starting the pet ...
rem same as run.cmd: pythonw (no black console box) + PET_LOG so logs go to run.out
set "PET_LOG=1"
set "PYW=pythonw"
where pythonw >nul 2>nul || set "PYW=python"
start "" %PYW% main.py
echo [regen] done - take a look at the right edge of your screen.
