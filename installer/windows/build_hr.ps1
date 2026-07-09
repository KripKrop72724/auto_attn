param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

& $Python -m pip install -e ".[dev]"

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

$DistExe = Join-Path $RepoRoot "dist\StateLifeHREnrollment.exe"
$BuildDir = Join-Path $RepoRoot "build\StateLifeHREnrollment"
$SpecFile = Join-Path $RepoRoot "StateLifeHREnrollment.spec"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $DistExe, $BuildDir, $SpecFile

$ZkemkeeperArgs = @()
if ($env:ZKEMKEEPER_DLL) {
  if (!(Test-Path $env:ZKEMKEEPER_DLL)) {
    throw "ZKEMKEEPER_DLL points to a missing file: $env:ZKEMKEEPER_DLL"
  }
  $ZkemkeeperArgs = @("--add-binary", "$($env:ZKEMKEEPER_DLL);.")
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

$HealthCheck = Start-Process -FilePath $DistExe -ArgumentList "--health-check" -Wait -PassThru
if ($HealthCheck.ExitCode -ne 0) {
  $HealthLog = Join-Path $env:ProgramData "State Life Insurance Corporation\HR Enrollment\logs\hr_enrollment.log"
  if (Test-Path $HealthLog) {
    Get-Content $HealthLog -Tail 120
  }
  throw "StateLifeHREnrollment.exe failed its packaged dependency health check."
}

Write-Host "State Life HR Enrollment portable EXE is in $DistExe."
