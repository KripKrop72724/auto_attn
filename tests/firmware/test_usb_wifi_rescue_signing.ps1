# Synthetic refusal tests only. No signing key, vault decrypt, Docker or eFuse.
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$work = Join-Path ([IO.Path]::GetTempPath()) ('rescue-sign-test-' + [guid]::NewGuid().ToString('N'))
$unsigned = Join-Path $work 'unsigned'
$vault = Join-Path $work 'vault'
$output = Join-Path $work 'output'
New-Item -ItemType Directory -Path $unsigned -Force | Out-Null
$signer = Join-Path $root 'deploy/add/bootstrap-firmware-signing.ps1'
try {
    $failed = $false
    try {
        & $signer -VaultDirectory $vault -UnsignedDirectory $unsigned -OutputDirectory $output `
          -DeviceMac '14:c1:9f:46:ae:1c' -GitSha ('a' * 40) -ImageRole usb-wifi-rescue
    } catch { $failed = $_.Exception.Message -match 'cannot create or replace signing keys' }
    if (-not $failed -or (Test-Path $vault) -or (Test-Path $output)) { throw 'Missing-vault guard failed' }
    New-Item -ItemType Directory -Path $vault | Out-Null
    '{"keys":[{"number":1,"state":"ACTIVE"},{"number":2,"state":"STANDBY"},{"number":3,"state":"STANDBY"}]}' | Set-Content (Join-Path $vault 'vault-manifest.json')
    foreach ($n in 1..3) {
        foreach ($name in @("key-$n.dpapi", "key-$n.entropy", "key-$n-public.pem")) {
            [IO.File]::WriteAllText((Join-Path $vault $name), 'synthetic fixture; not a key')
        }
    }
    [IO.File]::WriteAllBytes((Join-Path $unsigned 'zone_lite.bin'), [byte[]]@(0,1,2,3))
    $before = @{}
    Get-ChildItem $vault -File | ForEach-Object { $before[$_.Name] = (Get-FileHash $_.FullName).Hash }
    $failed = $false
    try {
        & $signer -VaultDirectory $vault -UnsignedDirectory $unsigned -OutputDirectory $output `
          -DeviceMac '14:c1:9f:46:ae:1c' -GitSha ('a' * 40) -ImageRole usb-wifi-rescue
    } catch { $failed = $_.Exception.Message -match 'artifact validation failed before key access' }
    if (-not $failed -or (Test-Path $output)) { throw 'Invalid-image guard failed' }
    Get-ChildItem $vault -File | ForEach-Object {
        if ((Get-FileHash $_.FullName).Hash -ne $before[$_.Name]) { throw 'Vault fixture changed' }
    }
    $failed = $false
    try {
        & $signer -VaultDirectory $vault -UnsignedDirectory $unsigned -OutputDirectory $output `
          -DeviceMac 'ac:27:6e:a3:16:d4' -GitSha ('a' * 40) -ImageRole usb-wifi-rescue-seed
    } catch { $failed = $_.Exception.Message -match 'HIL-only for the exact spare' }
    if (-not $failed) { throw 'Original-device signing scope guard failed' }
    Write-Output 'PASS: no replacement vault, invalid image refused before key access, spare-only HIL scope'
    # The invalid-artifact test intentionally made Python return 1. GitHub's
    # PowerShell wrapper propagates LASTEXITCODE even after the refusal was
    # caught and all assertions passed, so clear only that expected test code.
    $global:LASTEXITCODE = 0
} finally { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
