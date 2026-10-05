; screen-pet 安装包（Inno Setup 6，官网 jrsoftware.org）
;
; 用法：先跑 build.cmd 出 dist\ScreenPet\，再双击本文件让 Inno 编译
;       → dist\ScreenPet-Setup-<版本>.exe（有开始菜单、桌面快捷方式、卸载项）
;
; 绿色版（直接发 zip）不需要这个脚本；这份只是为了"像正经软件一样装一次"。
; 版本号由 tools/build.py 自动同步（打包时改写下面这行），别手改。

#define MyAppName "screen-pet"
#define MyAppVersion "0.8.1"
#define MyAppExeName "ScreenPet.exe"
#define MyDist "..\dist\ScreenPet"

[Setup]
AppId={{8F0A2C51-6B1D-4E77-9C2A-3D5E7A9B1C40}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=screen-pet
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=ScreenPet-Setup-{#MyAppVersion}
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 默认按"当前用户"装（%LOCALAPPDATA%\Programs），不需要管理员；
; 想装到 Program Files 就让安装向导里选"为所有用户安装"。
; 配置 / 记忆 / 语料在 %APPDATA%\ScreenPet —— 卸载时**不动**，重装还认得你。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 想要中文安装界面：装上 Inno 的中文语言包，再把下面这行去掉注释
; Languages: Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
; [Languages]
; Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："

[Files]
Source: "{#MyDist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "现在就用起来"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
