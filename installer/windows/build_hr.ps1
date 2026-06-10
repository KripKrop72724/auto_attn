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
  "win32timezone"
)) {
  Assert-PythonModule $Module
}

$DistExe = Join-Path $RepoRoot "dist\StateLifeHREnrollment.exe"
$BuildDir = Join-Path $RepoRoot "build\StateLifeHREnrollment"
$SpecFile = Join-Path $RepoRoot "StateLifeHREnrollment.spec"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $DistExe, $BuildDir, $SpecFile

& $Python -m PyInstaller `
  --name StateLifeHREnrollment `
  --onefile `
  --clean `
  --noconsole `
  --collect-all zk_hr_enrollment `
  --collect-all zk_zone_agent `
  --collect-all zk_common `
  --collect-all zk `
  --collect-all psutil `
  --hidden-import tkinter `
  --hidden-import psutil `
  --hidden-import psutil._psutil_common `
  --hidden-import psutil._psutil_windows `
  --hidden-import win32timezone `
  "apps\hr_enrollment\zk_hr_enrollment\__main__.py"

if (!(Test-Path $DistExe)) {
  throw "Expected portable HR enrollment EXE was not created at $DistExe"
}

$HealthCheck = Start-Process -FilePath $DistExe -ArgumentList "--health-check" -Wait -PassThru
if ($HealthCheck.ExitCode -ne 0) {
  throw "StateLifeHREnrollment.exe failed its packaged dependency health check."
}

Write-Host "State Life HR Enrollment portable EXE is in $DistExe."
