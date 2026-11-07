; OmniControl installer. Build the app first (pyinstaller omnicontrol.spec), then:
;     iscc /DAppVersion=0.1.0 installer.iss          (Inno Setup 6.3 or newer)
; build.ps1 does both. The installer is per-user by default (no admin needed); the "for all users"
; choice in the first dialog installs to Program Files and does ask for admin.
#define AppName "OmniControl"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{81C58217-7CFE-4046-A5E4-3ACAC4A5D65C}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=OmniControl Team
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
OutputDir=dist-installer
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=assets\omnicontrol.ico
UninstallDisplayIcon={app}\{#AppName}.exe
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked
Name: "autostart"; Description: "Start {#AppName} when I sign in to Windows"; Flags: unchecked

[Files]
Source: "dist\{#AppName}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{group}\{#AppName} (Administrator)"; Filename: "{app}\{#AppName}-Admin.exe"
Name: "{group}\{#AppName} Safe Mode"; Filename: "{app}\{#AppName}.exe"; Parameters: "--safe"
Name: "{group}\{#AppName} Doctor"; Filename: "{app}\{#AppName}.exe"; Parameters: "--doctor"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Registry]
; Autostart uses the normal exe on purpose: the Admin exe would raise a UAC prompt at every sign-in.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppName}.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

; The user's settings (%APPDATA%\OmniControl) and logs are deliberately NOT removed on uninstall.
