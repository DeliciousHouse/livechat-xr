; LiveChat XR installer (Inno Setup 6). Build: iscc /DAppVersion=x.y.z installer\livechat-xr.iss
; Expects dist\LiveChatXR\ (PyInstaller) and layer\livechat_xr_layer.dll to exist.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{6C2F4A8E-2B7D-4C38-9F0E-5A1D3B7C9E21}
AppName=LiveChat XR
AppVersion={#AppVersion}
AppPublisher=DeliciousHouse
AppPublisherURL=https://github.com/DeliciousHouse/livechat-xr
AppSupportURL=https://github.com/DeliciousHouse/livechat-xr/issues
DefaultDirName={autopf}\LiveChatXR
DefaultGroupName=LiveChat XR
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=LiveChatXR-Setup-{#AppVersion}
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
UninstallDisplayIcon={app}\LiveChatXR.exe

[Tasks]
Name: autostart; Description: "Start LiveChat XR when I sign in to Windows"

[Files]
Source: "..\dist\LiveChatXR\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\layer\livechat_xr_layer.dll"; DestDir: "{app}\layer"; Flags: ignoreversion
Source: "..\layer\livechat_xr_layer.json"; DestDir: "{app}\layer"; Flags: ignoreversion
Source: "..\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\LiveChat XR"; Filename: "{app}\LiveChatXR.exe"
Name: "{group}\Uninstall LiveChat XR"; Filename: "{uninstallexe}"

[Registry]
; Implicit OpenXR API layer: game loaders read HKLM (some, e.g. Meta's OVRPlugin, ignore HKCU).
Root: HKLM64; Subkey: "SOFTWARE\Khronos\OpenXR\1\ApiLayers\Implicit"; ValueType: dword; ValueName: "{app}\layer\livechat_xr_layer.json"; ValueData: 0; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "LiveChatXR"; ValueData: """{app}\LiveChatXR.exe"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\LiveChatXR.exe"; Description: "Launch LiveChat XR"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM LiveChatXR.exe /F"; Flags: runhidden; RunOnceId: "KillApp"
