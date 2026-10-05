# screen-pet — 给 AI 助手的约定

桌面挂件（PySide6 + OCR + 视觉模型）。人看的说明在 `README.md`，用户能看懂的改动记录在 `CHANGELOG.md`。

## 改代码的固定流程（每次都要走）

1. **先关挂件**：`restart.cmd --stop`（= `python tools/restart_pet.py --stop`）。
   不关的后果有两条，别省这一步：
   - 挂件是**启动时读源码**的，正在跑的那个进程用的是旧代码，改动对它无效；
   - 它退出时会把内存里的配置/记忆**写回文件**，开着改会被盖掉。
2. **改代码**。
3. **验证**：`python -m compileall -q pet tools main.py`，
   再 `set QT_QPA_PLATFORM=offscreen` 后跑 `python tools/smoke_test.py`（应输出「全部通过。」，
   退出码 0；当前 82 项）。
4. **再起挂件**：`restart.cmd`（先停后起；只起的开关是 `--start`，`--status` 只看跑没跑）。
   收尾时挂件必须处于**运行**状态——用户平时就靠它在屏幕边上。

停/起的实现只有一份：`tools/set_key.py` 里的 `find_running_pet` / `stop_pets` / `start_pet`，
`tools/restart_pet.py` 复用它，不要另写一套。

## 别踩的坑

- **热键那套已经撤掉了**（全部走鼠标，见 README「④ 全部用鼠标」）：没有 `pet/hotkey.py`、
  没有 `hotkey.*` 配置项，冒烟测试也不再合成全局按键——所以那两条"注册了却收不到按键"的
  假失败彻底没有了，冒烟测试跟前台压着什么窗口（提权 / 全屏游戏）无关。
  那些入口现在只在右键菜单里（`pet/window.py::_build_menu` + `pet/states.py::MENU_REACTIONS`），
  加/删入口时两边的表要一起改。
- 控制台是 GBK，`print` 中文要经 `pet.make_console_safe()`；`*.cmd` 一律纯 ASCII。
- **动作分两层**：整张图的仿射（`keys` / `motion` 的正弦抖）和**逐部件**的关节
  （`pet/states.py` 的 `rig_keys`：头绕脖子、手臂绕肩）。关节位置全在 `tools/make_pet.py`
  顶上的 `RIG_*` 比例里，是照**当前这个形象**量的；换形象要一起改，或者生成时加 `--no-rig`
  （动作退回"只套整体参数"，不崩）。**眼珠跟头、食指跟躯干**——`motion_tables` 是两张表，
  把头的变换漏掉，一点头眼珠就飘到额头上。
- 跑测试时不要用 PowerShell 管道接 Python 输出（编码会花），要留文件就让程序自己写。
- `data/*.json` 是用户真实档案（语料 / 记忆 / 学习账本），调试一律另存到临时路径，别就地改。
- 日志在 `run.out`（挂件用 `pythonw` 起，没有控制台）；想在终端里看实时日志就 `python main.py`。
