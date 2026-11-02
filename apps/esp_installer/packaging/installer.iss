; Inno Setup 6 script for the State Life Device Installer.
; packaging/build.py compiles it with /DAppVersion, /DAppDir (the PyInstaller
; folder), /DBrandDir (icon and pictures), /DOutputDir, and /DTestBuild for a
; build without the field defaults secrets.

#ifndef AppVersion
  #error AppVersion must be supplied by packaging/build.py
#endif
#ifdef TestBuild
  #define SetupSuffix "-TEST"
  #define NameSuffix " (TEST BUILD)"
#else
  #define SetupSuffix ""
  #define NameSuffix ""
#endif
#define AppName "State Life Device Installer"
#define AppExe "State Life Device Installer.exe"
#define Publisher "State Life Insurance Corporation of Pakistan"
; Where the app keeps its data (sl_installer/config.py uses the same folders).
#define DataDir "{localappdata}\StateLife\DeviceInstaller"

[Setup]
; Never change AppId: Windows uses it to find this app for updates and removal.
AppId={{49E622E9-C86D-45A8-B698-80A6E828B344}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}{#NameSuffix}
AppPublisher={#Publisher}
AppCopyright=Copyright (C) {#Publisher}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#Publisher}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
; Per-user install: no administrator password is needed.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
LicenseFile=EULA.txt
SetupIconFile={#BrandDir}\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardImageFile={#BrandDir}\wizard-100.bmp,{#BrandDir}\wizard-125.bmp,{#BrandDir}\wizard-150.bmp,{#BrandDir}\wizard-175.bmp,{#BrandDir}\wizard-200.bmp,{#BrandDir}\wizard-225.bmp,{#BrandDir}\wizard-250.bmp
WizardSmallImageFile={#BrandDir}\wizard-small-100.bmp,{#BrandDir}\wizard-small-125.bmp,{#BrandDir}\wizard-small-150.bmp,{#BrandDir}\wizard-small-175.bmp,{#BrandDir}\wizard-small-200.bmp,{#BrandDir}\wizard-small-225.bmp,{#BrandDir}\wizard-small-250.bmp
; The app's window holds this mutex (APP_MUTEX in sl_installer/__main__.py), so
; Setup and the uninstaller both stop and ask for the app to be closed first: a
; setup that is running never loses its files. The window cannot be closed while a
; board is at a step that must not stop, so the person waits for it.
AppMutex=StateLifeDeviceInstaller
; Setup also asks Windows to close other programs that use the app's files. Never
; force it: a forced close would stop a board halfway through a write.
CloseApplications=yes
RestartApplications=no
Compression=lzma2/max
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=StateLife-Device-Installer-Setup-{#AppVersion}{#SetupSuffix}
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedLabel=[name] is ready. You can open it any time from the Start menu.

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on the desktop"; GroupDescription: "Shortcuts:"

[InstallDelete]
; Files of an older version must not mix with the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Downloaded firmware, scratch files and the sign-in pause can all go; the
; install records and logs are kept unless the person says otherwise (below).
Type: filesandordirs; Name: "{#DataDir}\bundles"
Type: filesandordirs; Name: "{#DataDir}\work"
Type: filesandordirs; Name: "{#DataDir}\state"
Type: dirifempty; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataRoot, Question: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataRoot := ExpandConstant('{#DataDir}');
  if DirExists(DataRoot) then
  begin
    // A line must not start with "#": the preprocessor would read it as a directive.
    Question := 'Do you also want to delete the install records and logs saved on this computer?' + #13#10#13#10 +
      'Choose No to keep them. Head office may ask for them later.';
    // Silent uninstalls (/SUPPRESSMSGBOXES) take the default answer: No.
    if SuppressibleMsgBox(Question, mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      DelTree(DataRoot, True, True, True);
  end;
  // Only removes the folders when nothing is left in them.
  RemoveDir(DataRoot);
  RemoveDir(ExpandConstant('{localappdata}\StateLife'));
end;
