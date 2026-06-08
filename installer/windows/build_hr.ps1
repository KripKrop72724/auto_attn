param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

& $Python -m pip install -e ".[dev]"

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
  --hidden-import tkinter `
  --hidden-import win32timezone `
  "apps\hr_enrollment\zk_hr_enrollment\__main__.py"

if (!(Test-Path $DistExe)) {
  throw "Expected portable HR enrollment EXE was not created at $DistExe"
}

Write-Host "State Life HR Enrollment portable EXE is in $DistExe."

