param(
    [string]$Python = "python"
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

& $Python -m PyInstaller `
  --name zk-zone-agent-service `
  --onefile `
  --distpath "dist\zk-zone-agent" `
  --clean `
  --collect-all zk_zone_agent `
  --collect-all zk_common `
  --hidden-import win32timezone `
  --console `
  "apps\zone_agent\zk_zone_agent\service.py"

Write-Host "PyInstaller output is in $RepoRoot\dist."
Write-Host "Open installer\windows\ZKZoneAgent.iss with Inno Setup to build the installer."
