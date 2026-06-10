[Setup]
AppName=NetMirror Pro Downloader
AppVersion=2.0
AppPublisher=Faizan Ali
AppPublisherURL=https://github.com/faizanali49
DefaultDirName={autopf}\NetMirror
DefaultGroupName=NetMirror
OutputDir=output
OutputBaseFilename=NetMirror-Setup
Compression=lzma
SolidCompression=yes

[Files]
; Backend server executable
Source: "installer-files\netmirror-server.exe"; DestDir: "{app}"; Flags: ignoreversion

; Backend server dependencies (CRITICAL FIX)
Source: "installer-files\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

; FFmpeg binary
Source: "installer-files\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion

; Chrome extension files
Source: "installer-files\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{group}\Start NetMirror Server"; Filename: "{app}\netmirror-server.exe"

Name: "{group}\Open Extension Folder"; Filename: "{app}\extension"

Name: "{group}\Uninstall NetMirror"; Filename: "{uninstallexe}"

Name: "{commondesktop}\NetMirror Server"; Filename: "{app}\netmirror-server.exe"

Name: "{commondesktop}\NetMirror Extension"; Filename: "{app}\extension"

[Run]
Filename: "{app}\netmirror-server.exe"; Description: "Start NetMirror Server now"; Flags: postinstall nowait skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "NetMirrorServer"; ValueData: """{app}\netmirror-server.exe"""; Flags: uninsdeletevalue

[Code]
// You can add custom Pascal script here if needed
