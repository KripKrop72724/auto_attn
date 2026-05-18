; Inno Setup script for the ZK Zone Agent POC.
; Build after running installer/windows/build.ps1 on Windows.

#define MyAppName "ZK Zone Agent"
#define MyAppVersion "0.1.5"
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
Name: "{commonappdata}\ZKZoneAgent\logs"; Permissions: admins-full system-full

[Run]
Filename: "{app}\nssm.exe"; Parameters: "install ZKZoneAgentService ""{app}\zk-zone-agent.exe"" ""--host"" ""127.0.0.1"" ""--port"" ""7860"""; StatusMsg: "Installing Windows service with NSSM..."; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService DisplayName ""ZK Zone Agent Service"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService Description ""Local ZKTeco attendance fraud-monitoring zone agent."""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService AppDirectory ""{app}"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService Start SERVICE_AUTO_START"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService AppExit Default Restart"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService AppRestartDelay 5000"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService AppStdout ""{commonappdata}\ZKZoneAgent\logs\service.out.log"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService AppStderr ""{commonappdata}\ZKZoneAgent\logs\service.err.log"""; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService AppRotateFiles 1"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService AppRotateOnline 1"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set ZKZoneAgentService AppRotateBytes 1048576"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "start ZKZoneAgentService"; StatusMsg: "Starting service..."; Flags: runhidden waituntilterminated
Filename: "{cmd}"; Parameters: "/c start http://localhost:7860/setup"; Flags: postinstall shellexec skipifsilent

[UninstallRun]
Filename: "{app}\nssm.exe"; Parameters: "stop ZKZoneAgentService"; Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "remove ZKZoneAgentService confirm"; Flags: runhidden waituntilterminated
