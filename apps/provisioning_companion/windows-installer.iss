#ifndef MyAppVersion
  #error MyAppVersion must be supplied by the release workflow
#endif

[Setup]
AppId={{F9DAA7BD-AC40-40DA-9F65-2EE31AD6B130}
AppName=State Life ADD Provisioning Companion
AppVersion={#MyAppVersion}
AppPublisher=State Life Insurance Corporation of Pakistan
DefaultDirName={localappdata}\Programs\State Life ADD Provisioning Companion
DefaultGroupName=State Life ADD Provisioning Companion
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=add-provisioning-companion-windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\ADD Provisioning Companion.exe

[Files]
Source: "dist\ADD Provisioning Companion\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\State Life ADD Provisioning Companion"; Filename: "{app}\ADD Provisioning Companion.exe"

[Run]
Filename: "{app}\ADD Provisioning Companion.exe"; Description: "Launch ADD Provisioning Companion"; Flags: nowait postinstall skipifsilent
