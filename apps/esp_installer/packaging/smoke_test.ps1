<#
.SYNOPSIS
Installs a built Setup silently, runs the installed app's checks, uninstalls
it and proves that it is gone. Run it in Windows PowerShell 5.1 (as on the
field PCs) on a machine where the app is not installed.

.EXAMPLE
powershell -File packaging\smoke_test.ps1 -Setup dist\StateLife-Device-Installer-Setup-1.1.0-TEST.exe -OutputDir smoke
#>
param(
    [Parameter(Mandatory = $true)] [string] $Setup,
    [Parameter(Mandatory = $true)] [string] $OutputDir
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3

if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'Run the smoke test in Windows PowerShell 5.1.' }

$appName = 'State Life Device Installer'
$Setup = (Resolve-Path -LiteralPath $Setup).Path
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path
$temp = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
# A folder with spaces, like the real one, so quoting mistakes show up here.
$installDir = Join-Path $temp "smoke install\$appName"
$exe = Join-Path $installDir "$appName.exe"
$dataDir = Join-Path $env:LOCALAPPDATA 'StateLife\DeviceInstaller'
$startMenuLink = Join-Path ([Environment]::GetFolderPath('Programs')) "$appName.lnk"
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) "$appName.lnk"

function Invoke-Program {
    # Start-Process joins -ArgumentList with spaces and does not quote, so
    # callers pass one string with their own quotes.
    param([string] $FilePath, [string] $Arguments, [string] $Name, [int] $TimeoutSeconds = 300)
    $out = Join-Path $OutputDir "$Name.out.txt"
    $err = Join-Path $OutputDir "$Name.err.txt"
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    $null = $process.Handle  # Windows PowerShell 5.1 loses ExitCode without this.
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $process.Kill()
        throw "$Name did not finish within $TimeoutSeconds seconds."
    }
    $process.WaitForExit()
    Write-Host "$Name exited with code $($process.ExitCode)"
    return [int] $process.ExitCode
}

function Wait-Gone {
    param([string] $Path, [int] $TimeoutSeconds = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Test-Path -LiteralPath $Path) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    return -not (Test-Path -LiteralPath $Path)
}

if (Test-Path -LiteralPath $installDir) { throw "Remove the old test install first: $installDir" }

# 1. Silent install.
$code = Invoke-Program $Setup "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR=`"$installDir`" /LOG=`"$OutputDir\setup.log`"" 'setup'
if ($code -ne 0) { throw "Setup failed with exit code $code." }
foreach ($path in @($exe, (Join-Path $installDir 'EULA.txt'), (Join-Path $installDir 'THIRD-PARTY-NOTICES.txt'),
                    (Join-Path $installDir 'unins000.exe'), $startMenuLink, $desktopLink)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Setup did not create $path" }
}

# 2. The bundled tools run from the installed app (no board needed).
$code = Invoke-Program $exe '--tool esptool version' 'tool-esptool-version'
if ($code -ne 0 -or -not (Select-String -LiteralPath (Join-Path $OutputDir 'tool-esptool-version.out.txt') -Pattern '5\.1\.0' -Quiet)) {
    throw 'The bundled esptool did not report version 5.1.0.'
}
$code = Invoke-Program $exe '--tool espefuse --chip esp32s3 --virt summary --format json' 'tool-espefuse-virtual'
if ($code -ne 0) { throw 'The bundled espefuse could not read a virtual ESP32-S3.' }

# 3. Self-test: engine checks plus opening and closing the window.
$code = Invoke-Program $exe '--self-test' 'self-test'
if ($code -ne 0 -or -not (Select-String -LiteralPath (Join-Path $OutputDir 'self-test.out.txt') -Pattern 'Self-test passed' -Quiet)) {
    Get-Content -LiteralPath (Join-Path $OutputDir 'self-test.out.txt'), (Join-Path $OutputDir 'self-test.err.txt') | Write-Host
    throw "The installed app's self-test failed."
}

# 4. A picture of every screen.
$shots = Join-Path $OutputDir 'screenshots'
$code = Invoke-Program $exe "--screenshots `"$shots`"" 'screenshots'
$pictures = @(Get-ChildItem -LiteralPath $shots -Filter '*.png' -ErrorAction SilentlyContinue)
if ($code -ne 0 -or $pictures.Count -eq 0) { throw 'The installed app did not save its screenshots.' }
Write-Host "Saved $($pictures.Count) screenshots."

# 5. Silent uninstall: the app, downloaded firmware and scratch files go;
#    install records and logs stay.
$bundleMarker = Join-Path $dataDir 'bundles\smoke-test.txt'
$recordMarker = Join-Path $dataDir 'records\smoke-test-keep.txt'
foreach ($marker in @($bundleMarker, $recordMarker)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $marker) | Out-Null
    Set-Content -LiteralPath $marker -Value 'smoke test'
}
$code = Invoke-Program (Join-Path $installDir 'unins000.exe') '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' 'uninstall'
if ($code -ne 0) { throw "Uninstall failed with exit code $code." }
# The uninstaller finishes in a copy of itself, so wait for the folder to go.
if (-not (Wait-Gone $installDir)) {
    Get-ChildItem -LiteralPath $installDir -Recurse | Select-Object -First 20 | ForEach-Object { Write-Host $_.FullName }
    throw "The install folder is still there after uninstall: $installDir"
}
foreach ($path in @($startMenuLink, $desktopLink, (Join-Path $dataDir 'bundles'),
                    (Join-Path $dataDir 'work'), (Join-Path $dataDir 'state'))) {
    if (-not (Wait-Gone $path 60)) { throw "Uninstall left $path behind." }
}
if (-not (Test-Path -LiteralPath $recordMarker)) { throw 'Uninstall deleted saved records and logs without asking.' }
Remove-Item -LiteralPath $recordMarker
Write-Host 'Smoke test passed: install, tools, self-test, screenshots and uninstall all work.'
