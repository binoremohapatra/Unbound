; ==============================================================================
; Inno Setup Script for OmniControl
; Installs to {autopf}\OmniControl
; Creates Start Menu shortcuts
; Optional HKCU Run key for autostart (OFF by default)
; Full clean uninstall
; ==============================================================================

#define MyAppName "OmniControl"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "OmniControl Team"
#define MyAppURL "https://github.com/omnicontrol"
#define MyAppExeName "OmniControl.exe"

[Setup]
AppId={{D37F8E42-9A31-4F29-BA70-6E35C8E8B490}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
OutputDir=Output
OutputBaseFilename=OmniControl_Setup_v{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Start OmniControl automatically when Windows starts"; GroupDescription: "Windows Startup:"; Flags: unchecked

[Files]
; Dist folder contents produced by build.bat (admin build by default)
Source: "dist\OmniControl_Admin\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\OmniControl Diagnostics (Doctor)"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--doctor"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Optional start with Windows (HKCU Run key, unchecked/OFF by default)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OmniControl"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up appdata logs and configs created during runtime
Type: filesandordirs; Name: "{userappdata}\OmniControl\logs"
Type: files; Name: "{userappdata}\OmniControl\config.json"
Type: dirifempty; Name: "{userappdata}\OmniControl"

