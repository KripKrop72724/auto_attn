param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

& $Python -m pip install --upgrade pip
& $Python -m pip install --upgrade `
  pyinstaller `
  psutil `
  pydantic-settings `
  pyzk `
  pywin32 `
  tzdata
& $Python -m pip install --no-deps -e "."

function Assert-PythonModule {
  param([string]$Module)
  & $Python -c "import importlib; importlib.import_module('$Module')"
  if ($LASTEXITCODE -ne 0) {
    throw "Required Python module '$Module' is not importable in the build environment."
  }
}

foreach ($Module in @(
  "psutil",
  "zk",
  "zk_common",
  "zk_zone_agent.network_scanner",
  "zk_hr_enrollment.app",
  "zk_hr_enrollment.official_sdk",
  "pythoncom",
  "win32com.client",
  "win32timezone"
)) {
  Assert-PythonModule $Module
}

$PythonBits = (& $Python -c "import struct; print(struct.calcsize('P') * 8)").Trim()
if ($PythonBits -ne "32") {
  Write-Warning "Building a $PythonBits-bit HR EXE. ZKTeco zkemkeeper.dll is commonly 32-bit; use 32-bit Python if face enrollment cannot load the COM SDK."
}

$DistExe = Join-Path $RepoRoot "dist\StateLifeHREnrollment.exe"
$BuildDir = Join-Path $RepoRoot "build\StateLifeHREnrollment"
$SpecFile = Join-Path $RepoRoot "StateLifeHREnrollment.spec"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $DistExe, $BuildDir, $SpecFile

$DefaultZkemkeeperDll = Join-Path $RepoRoot "installer\windows\vendor\zkemkeeper.dll"
if (!$env:ZKEMKEEPER_DLL -and (Test-Path $DefaultZkemkeeperDll)) {
  $env:ZKEMKEEPER_DLL = $DefaultZkemkeeperDll
}
if (!$env:ZKEMKEEPER_DLL) {
  throw "ZKEMKEEPER_DLL is required for the HR Enrollment EXE. Provide installer\windows\vendor\zkemkeeper.dll or set ZKEMKEEPER_DLL to the licensed ZKTeco SDK DLL."
}
if (!(Test-Path $env:ZKEMKEEPER_DLL)) {
  throw "ZKEMKEEPER_DLL points to a missing file: $env:ZKEMKEEPER_DLL"
}
$ZkemkeeperSdkDir = Split-Path -Parent (Resolve-Path $env:ZKEMKEEPER_DLL)
$RequiredZkemkeeperDlls = @(
  "commpro.dll",
  "comms.dll",
  "plcommpro.dll",
  "plcomms.dll",
  "plrscagent.dll",
  "plrscomm.dll",
  "pltcpcomm.dll",
  "rscagent.dll",
  "rscomm.dll",
  "tcpcomm.dll",
  "usbcomm.dll",
  "zkemkeeper.dll",
  "zkemsdk.dll"
)
$MissingZkemkeeperDlls = @(
  foreach ($DllName in $RequiredZkemkeeperDlls) {
    if (!(Test-Path (Join-Path $ZkemkeeperSdkDir $DllName))) {
      $DllName
    }
  }
)
if ($MissingZkemkeeperDlls.Count -gt 0) {
  throw "The ZKTeco SDK DLL folder is incomplete. Missing: $($MissingZkemkeeperDlls -join ', '). Provide the official 32-bit SDK DLL set in installer\windows\vendor or next to ZKEMKEEPER_DLL."
}

$ZkemkeeperArgs = @()
foreach ($Dll in Get-ChildItem -Path $ZkemkeeperSdkDir -Filter "*.dll" -File) {
  $ZkemkeeperArgs += @("--add-binary", "$($Dll.FullName);.")
}

& $Python -m PyInstaller `
  --name StateLifeHREnrollment `
  --onefile `
  --clean `
  --noconsole `
  --paths "apps\hr_enrollment" `
  --paths "apps\zone_agent" `
  --paths "packages\common" `
  --collect-all zk_hr_enrollment `
  --collect-all zk_zone_agent `
  --collect-all zk_common `
  --collect-all zk `
  --collect-all psutil `
  --hidden-import tkinter `
  --hidden-import psutil `
  --hidden-import psutil._psutil_windows `
  --hidden-import pythoncom `
  --hidden-import win32com.client `
  --hidden-import win32timezone `
  @ZkemkeeperArgs `
  "apps\hr_enrollment\zk_hr_enrollment\__main__.py"

if (!(Test-Path $DistExe)) {
  throw "Expected portable HR enrollment EXE was not created at $DistExe"
}

$env:HR_REQUIRE_ZKEMKEEPER_DLL = "1"
$HealthCheck = Start-Process -FilePath $DistExe -ArgumentList "--health-check" -Wait -PassThru
if ($HealthCheck.ExitCode -ne 0) {
  $HealthLog = Join-Path $env:ProgramData "State Life Insurance Corporation\HR Enrollment\logs\hr_enrollment.log"
  if (Test-Path $HealthLog) {
    Get-Content $HealthLog -Tail 120
  }
  throw "StateLifeHREnrollment.exe failed its packaged dependency health check."
}

Write-Host "State Life HR Enrollment portable EXE is in $DistExe."
