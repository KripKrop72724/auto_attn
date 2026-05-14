param(
    [string]$Python = "python",
    [string]$NssmVersion = "2.24"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

& $Python -m pip install -e ".[dev]"

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue "$RepoRoot\build", "$RepoRoot\dist"

& $Python -m PyInstaller `
  --name zk-zone-agent `
  --onedir `
  --clean `
  --collect-all zk_zone_agent `
  --collect-all zk_common `
  --hidden-import win32timezone `
  --console `
  "apps\zone_agent\zk_zone_agent\__main__.py"

$NssmRoot = Join-Path $RepoRoot "build\nssm"
$NssmZip = Join-Path $NssmRoot "nssm-$NssmVersion.zip"
$NssmUrl = "https://nssm.cc/release/nssm-$NssmVersion.zip"
$NssmExe = Join-Path $NssmRoot "nssm-$NssmVersion\win64\nssm.exe"
New-Item -ItemType Directory -Force $NssmRoot | Out-Null
if (!(Test-Path $NssmZip)) {
  Invoke-WebRequest -Uri $NssmUrl -OutFile $NssmZip
}
Expand-Archive -Force $NssmZip $NssmRoot
if (!(Test-Path $NssmExe)) {
  throw "Could not find NSSM executable at $NssmExe"
}
Copy-Item -Force $NssmExe "$RepoRoot\dist\zk-zone-agent\nssm.exe"

Write-Host "PyInstaller output is in $RepoRoot\dist."
Write-Host "Bundled NSSM $NssmVersion into dist\zk-zone-agent\nssm.exe."
Write-Host "Open installer\windows\ZKZoneAgent.iss with Inno Setup to build the installer."
