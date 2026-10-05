# screen-pet — 给 AI 助手的约定

桌面挂件（PySide6 + OCR + 视觉模型）。人看的说明在 `README.md`，用户能看懂的改动记录在 `CHANGELOG.md`。

## 改代码的固定流程（每次都要走）

1. **先关挂件**：`restart.cmd --stop`（= `python tools/restart_pet.py --stop`）。
   不关的后果有三条，别省这一步：
   - 挂件是**启动时读源码**的，正在跑的那个进程用的是旧代码，改动对它无效；
   - 它退出时会把内存里的配置/记忆**写回文件**，开着改会被盖掉；
   - 它注册的全局热键是**独占**的，两个进程抢同一个组合键会让冒烟测试里的热键用例**假失败**
     （现象是"注册成功了但收不到按键"）。
2. **改代码**。
3. **验证**：`python -m compileall -q pet tools main.py`，
   再 `set QT_QPA_PLATFORM=offscreen` 后跑 `python tools/smoke_test.py`（应输出「全部通过。」，
   退出码 0；当前 79 项）。
4. **再起挂件**：`restart.cmd`（先停后起；只起的开关是 `--start`，`--status` 只看跑没跑）。
   收尾时挂件必须处于**运行**状态——用户平时就靠它在屏幕边上。

停/起的实现只有一份：`tools/set_key.py` 里的 `find_running_pet` / `stop_pets` / `start_pet`，
`tools/restart_pet.py` 复用它，不要另写一套。

## 别踩的坑

- **热键那两项冒烟用例会假失败**：`hotkey 真注册 + 合成按键` 和 `app 整体组装` 里
  「划观看范围」那条都靠**合成全局按键**（`keybd_event`）。前台要是压着提权窗口、
  或者有别的进程占着同一个组合键，就会收到「注册了 ['test=Ctrl+Alt+F9']，但没收到按键：[]」
  这种报错——那是环境问题，不是代码坏了。先确认没有别的挂件 / 上一次的 `smoke_test.py`
  还在跑（`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'smoke_test' }`），
  再重跑一次；其余各项与该环境状态无关。
- 控制台是 GBK，`print` 中文要经 `pet.make_console_safe()`；`*.cmd` 一律纯 ASCII。
- 跑测试时不要用 PowerShell 管道接 Python 输出（编码会花），要留文件就让程序自己写。
- `data/*.json` 是用户真实档案（语料 / 记忆 / 学习账本），调试一律另存到临时路径，别就地改。
- 日志在 `run.out`（挂件用 `pythonw` 起，没有控制台）；想在终端里看实时日志就 `python main.py`。
