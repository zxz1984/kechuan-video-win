; ============================================================
; 可乐口播视频生成器 - Windows 安装包脚本
; 用 Inno Setup 编译（GitHub Actions 自动装）
; 手动编译：装 Inno Setup 后双击此文件即可
; ============================================================

#define MyAppName "可乐口播视频生成器"
#define MyAppVersion "2.10.56"
#define MyAppPublisher "可乐剪辑"
#define MyAppURL "https://github.com/zxz1984/kechuan-video-win"
#define MyAppExeName "可乐口播视频生成器.exe"

[Setup]
; 应用基本信息
AppId={{B2C3D4E5-F6A7-4B5C-9D0E-1F2A3B4C5D6E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}

; 默认安装目录（Program Files 下的同名文件夹）
DefaultDirName={autopf}\{#MyAppName}

; 默认开始菜单文件夹名
DefaultGroupName={#MyAppName}

; 输出 setup.exe 文件名（带版本号）
OutputBaseFilename=可乐口播视频生成器_Setup_v{#MyAppVersion}

; 输出目录（PyInstaller 的 dist，相对于仓库根目录）
; .iss 文件在 scripts/，需要回退一级
OutputDir=..\dist

; 压缩方式（lzma2 + solid 体积最小）
Compression=lzma2
SolidCompression=yes

; 权限：标准用户权限就够（不需要管理员）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 仅支持 64 位 Win
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 安装向导 UI（用 Inno Setup 内置 English + Unicode 中文标签）
; 注：GitHub Actions 装的 Inno Setup 是精简版，没有 ChineseSimplified.isl 文件
; 改用 Default.isl + [Messages] 内联中文，Unicode 自动渲染
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; UI 样式（Win10 风格）
[Messages]
WelcomeLabel1=欢迎安装 {#MyAppName}
SetupWindowTitle=安装向导 - {#MyAppName} v{#MyAppVersion}

[Files]
; 把 PyInstaller 生成的文件夹整个拷进安装目录
; 源：仓库根目录的 dist\可乐口播视频生成器_win\*
; .iss 在 scripts/，所以源路径要 ..\dist\
; 目标：{app}\（用户的 Program Files 下）
Source: "..\dist\可乐口播视频生成器_win\*"; \
  DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

; 注：PyInstaller 的 _internal 子目录会被上面自动覆盖

[Icons]
; 开始菜单快捷方式
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

; v2.10.57 修复：改用 {userdesktop}（当前用户桌面，普通用户有权限）
; 原 {commondesktop} = C:\Users\Public\Desktop\，需要管理员权限，会报 0x80070005
Name: "{userdesktop}\{#MyAppName}"; \
  Filename: "{app}\{#MyAppExeName}"; \
  Tasks: desktopicon

; 卸载快捷方式
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; \
  Description: "创建桌面快捷方式"; \
  GroupDescription: "附加任务："

[Run]
; 安装完后勾"启动"才运行
Filename: "{app}\{#MyAppExeName}"; \
  Description: "立即启动 {#MyAppName}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清掉所有数据（用户配置）
Type: filesandordirs; Name: "{app}\*"

[Code]
// 安装前检查：如果程序已经在跑，先关掉
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  if CheckForMutexes('{#MyAppName}_Mutex') then
  begin
    if MsgBox('{#MyAppName} 正在运行，先关闭再安装。' + #13#10 + '现在关掉吗？',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      // 这里简单用 taskkill，需要管理员权限才行
      Exec('taskkill.exe', '/F /IM "{#MyAppExeName}"', '', SW_HIDE,
           ewWaitUntilTerminated, ResultCode);
    end
    else
    begin
      Result := False;
      Exit;
    end;
  end;
  Result := True;
end;
