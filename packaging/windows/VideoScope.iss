#ifndef MyAppVersion
  #define MyAppVersion "0.8.0"
#endif
#ifndef MyVersionInfoVersion
  #define MyVersionInfoVersion "0.8.0.0"
#endif
#ifndef MyBundleDir
  #define MyBundleDir "..\..\build\windows\dist\VideoScopeConnector"
#endif
#ifndef MyOutputDir
  #define MyOutputDir "..\..\build\windows\installer"
#endif

[Setup]
AppId={{A2B33D51-7D40-4D9D-A1D1-6D5B4357A712}
AppName=VideoScope Local Connector
AppVersion={#MyAppVersion}
AppPublisher=what912
AppPublisherURL=https://github.com/what912/VideoScope
AppSupportURL=https://github.com/what912/VideoScope/issues
AppUpdatesURL=https://github.com/what912/VideoScope/releases
DefaultDirName={localappdata}\Programs\VideoScope
DefaultGroupName=VideoScope
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#MyOutputDir}
OutputBaseFilename=VideoScope-Setup-x64
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UninstallDisplayIcon={app}\VideoScopeConnector.exe
VersionInfoVersion={#MyVersionInfoVersion}
VersionInfoCompany=what912
VersionInfoDescription=VideoScope Local Connector installer
VersionInfoProductName=VideoScope
VersionInfoProductVersion={#MyVersionInfoVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupAppTitle=安装 / Setup
UninstallAppTitle=卸载 / Uninstall
ButtonBack=< 上一步 / Back
ButtonNext=下一步 / Next >
ButtonInstall=安装 / Install
ButtonCancel=取消 / Cancel
ButtonFinish=完成 / Finish
ButtonBrowse=浏览 / Browse...
ClickNext=点击“下一步 / Next”继续，或点击“取消 / Cancel”退出。
WelcomeLabel1=欢迎安装 [name] / Welcome to [name]
WelcomeLabel2=本向导将在当前 Windows 账户安装 [name/ver]。%n%nThis wizard installs [name/ver] for the current Windows account.
SelectDirDesc=选择安装位置 / Choose the installation folder
SelectDirLabel3=[name] 将安装到以下文件夹。 / [name] will be installed in this folder.
SelectDirBrowseLabel=点击“下一步 / Next”继续；如需更改位置，请点击“浏览 / Browse”。
ReadyLabel1=已准备好安装 [name]。 / Ready to install [name].
ReadyLabel2a=点击“安装 / Install”继续；如需检查设置，请点击“上一步 / Back”。
ReadyLabel2b=点击“安装 / Install”继续。
PreparingDesc=正在准备安装 [name]。 / Preparing to install [name].
InstallingLabel=正在安装 [name]，请稍候。 / Please wait while [name] is installed.
FinishedHeadingLabel=[name] 安装完成 / [name] setup complete
FinishedLabelNoIcons=[name] 已安装到这台电脑。 / [name] has been installed.
FinishedLabel=[name] 已安装；可通过快捷方式启动。 / [name] has been installed and can be launched from its shortcut.
ClickFinish=点击“完成 / Finish”退出安装向导。
ExitSetupTitle=退出安装 / Exit Setup
ExitSetupMessage=安装尚未完成。现在退出将不会安装程序。%n%nSetup is not complete. Exit now?
ConfirmUninstall=确定要删除 %1 及其组件吗？ / Remove %1 and its components?
UninstallStatusLabel=正在删除 %1，请稍候。 / Please wait while %1 is removed.
UninstalledAll=%1 已成功删除。 / %1 was successfully removed.

[CustomMessages]
CreateDesktopIcon=创建桌面图标 / Create a desktop icon
AdditionalIcons=快捷方式 / Shortcuts

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#MyBundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\VideoScope"; Filename: "{app}\VideoScopeConnector.exe"
Name: "{autodesktop}\VideoScope"; Filename: "{app}\VideoScopeConnector.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Classes\videoscope"; ValueType: string; ValueData: "URL:VideoScope"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\videoscope"; ValueName: "URL Protocol"; ValueType: string; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\videoscope\DefaultIcon"; ValueType: string; ValueData: "{app}\VideoScopeConnector.exe,0"
Root: HKCU; Subkey: "Software\Classes\videoscope\shell\open\command"; ValueType: string; ValueData: """{app}\VideoScopeConnector.exe"" ""%1"""

[Run]
Filename: "{app}\VideoScopeConnector.exe"; Description: "启动 VideoScope 本地连接器"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\VideoScopeConnector.exe"; Parameters: "--shutdown"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "StopConnector"
