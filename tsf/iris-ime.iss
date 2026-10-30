; 鸢尾输入法 (Iris IME) 安装包脚本
; 包含: 托盘客户端(含引擎/模型/词库) + TSF 原生前端 DLL + 自动注册

#define MyAppName "鸢尾输入法"
#define MyAppNameEn "Iris IME"
#define MyAppVersion "2.0.0.15"
#define MyAppExeName "IrisIME.exe"

[Setup]
AppId={{B4E7C1A2-9D30-4E5A-9F61-2C8E4A7D5F10}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Iris IME
DefaultDirName={autopf}\IrisIME
DefaultGroupName={#MyAppName}
PrivilegesRequired=admin
OutputDir=.
OutputBaseFilename=IrisIME-Setup-v2.0.0-alpha.15
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl,compiler:Languages\ChineseSimplified.isl"

[Files]
; 托盘客户端(PyInstaller 单目录, 含 _internal 模型/词库)
Source: "..\dist\IrisIME\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; TSF 原生前端 DLL
Source: "IrisIME\x64\Release\IrisIME.dll"; DestDir: "{app}\tsf"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加:"
Name: "autostart"; Description: "开机自动启动托盘客户端"; Flags: unchecked

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "IrisIME"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{sys}\regsvr32.exe"; Parameters: "/s ""{app}\tsf\IrisIME.dll"""; Flags: runhidden; Description: "注册 TSF 原生前端"
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\regsvr32.exe"; Parameters: "/u /s ""{app}\tsf\IrisIME.dll"""; Flags: runhidden; RunOnceId: "UnregTSF"

[UninstallDelete]
; 用户词/设置保留在 %APPDATA%\pinyin-ime, 卸载不删除(换机迁移可直接拷贝)
Type: files; Name: "{app}\tsf_debug.log"
