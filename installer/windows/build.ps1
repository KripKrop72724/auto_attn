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
New-Item -ItemType Directory -Force $NssmRoot | Out-Null

function Find-NssmExe {
  $command = Get-Command "nssm.exe" -ErrorAction SilentlyContinue
  if ($command -and (Test-Path $command.Source)) {
    return $command.Source
  }
  $roots = @(
    $env:ChocolateyInstall,
    "$env:ProgramData\chocolatey"
  ) | Where-Object { $_ -and (Test-Path $_) }
  foreach ($root in $roots) {
    $matches = Get-ChildItem -Path $root -Recurse -Filter "nssm.exe" -ErrorAction SilentlyContinue |
      Sort-Object @{ Expression = { if ($_.FullName -match "win64") { 0 } else { 1 } } }, FullName
    if ($matches) {
      return $matches[0].FullName
    }
  }
  return $null
}

$NssmExe = Find-NssmExe
if (!$NssmExe -and (Get-Command "choco.exe" -ErrorAction SilentlyContinue)) {
  & choco install nssm --no-progress -y
  $NssmExe = Find-NssmExe
}
if (!$NssmExe) {
  $NssmZip = Join-Path $NssmRoot "nssm-$NssmVersion.zip"
  $NssmUrl = "https://nssm.cc/release/nssm-$NssmVersion.zip"
  for ($attempt = 1; $attempt -le 3 -and !(Test-Path $NssmZip); $attempt++) {
    try {
      Invoke-WebRequest -Uri $NssmUrl -OutFile $NssmZip
    } catch {
      if ($attempt -eq 3) { throw }
      Start-Sleep -Seconds (5 * $attempt)
    }
  }
  Expand-Archive -Force $NssmZip $NssmRoot
  $NssmExe = Join-Path $NssmRoot "nssm-$NssmVersion\win64\nssm.exe"
}
if (!(Test-Path $NssmExe)) {
  throw "Could not find NSSM executable at $NssmExe"
}
Copy-Item -Force $NssmExe "$RepoRoot\dist\zk-zone-agent\nssm.exe"

Write-Host "PyInstaller output is in $RepoRoot\dist."
Write-Host "Bundled NSSM $NssmVersion into dist\zk-zone-agent\nssm.exe."
Write-Host "Open installer\windows\ZKZoneAgent.iss with Inno Setup to build the installer."
