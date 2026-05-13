; Inno Setup script for the ZK Zone Agent POC.
; Build after running installer/windows/build.ps1 on Windows.

#define MyAppName "ZK Zone Agent"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "POC"
#define MyAppExeName "zk-zone-agent.exe"

[Setup]
AppId={{D2336D7F-6967-45E4-9D76-921481962B71}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ZK Zone Agent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=Output
OutputBaseFilename=ZKZoneAgentSetup
Compression=lzma
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "..\..\dist\zk-zone-agent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{commonappdata}\ZKZoneAgent"; Permissions: admins-full system-full

[Run]
Filename: "{app}\zk-zone-agent-service.exe"; Parameters: "install --startup auto"; StatusMsg: "Installing Windows service..."; Flags: runhidden waituntilterminated
Filename: "sc.exe"; Parameters: "failure ZKZoneAgentService reset=86400 actions=restart/5000/restart/10000/restart/60000"; StatusMsg: "Configuring service recovery..."; Flags: runhidden waituntilterminated
Filename: "{app}\zk-zone-agent-service.exe"; Parameters: "start"; StatusMsg: "Starting service..."; Flags: runhidden waituntilterminated
Filename: "{cmd}"; Parameters: "/c start http://127.0.0.1:7860/setup"; Flags: postinstall shellexec skipifsilent

[UninstallRun]
Filename: "{app}\zk-zone-agent-service.exe"; Parameters: "stop"; Flags: runhidden waituntilterminated
Filename: "{app}\zk-zone-agent-service.exe"; Parameters: "remove"; Flags: runhidden waituntilterminated
