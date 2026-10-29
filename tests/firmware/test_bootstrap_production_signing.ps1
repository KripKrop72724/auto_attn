# Synthetic refusal tests only. No signing key, vault, Docker or eFuse.
# A production bootstrap must carry an older AVAILABLE release for ota_1 and
# the build's empty storage image; every refusal happens before key access.
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$work = Join-Path ([IO.Path]::GetTempPath()) ('bootstrap-production-test-' + [guid]::NewGuid().ToString('N'))
$unsigned = Join-Path $work 'unsigned'
$vault = Join-Path $work 'vault'
$output = Join-Path $work 'output'
$signer = Join-Path $root 'deploy/add/bootstrap-firmware-signing.ps1'
$utf8 = New-Object Text.UTF8Encoding($false)

function New-SyntheticRelease {
    param(
        [Parameter(Mandatory = $true)][string]$Version,
        [string]$Family = 'zkt',
        [string]$Marker = '',
        [switch]$NoSignature,
        [switch]$Tampered
    )
    $directory = Join-Path $work ('store-' + [guid]::NewGuid().ToString('N') + '/' + $Version)
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $image = Join-Path $directory 'zone_lite.bin'
    $bytes = [Text.Encoding]::ASCII.GetBytes("synthetic application $Version")
    [IO.File]::WriteAllBytes($image, $bytes)
    $manifest = [ordered]@{
        release_id = "zone-lite-$Version"
        version = $Version
        git_sha = ('b' * 40)
        firmware_family = $Family
        partition_layout = 'zone-lite-ota-v1'
        release_channel = 'STABLE'
        image_name = 'zone_lite.bin'
        image_sha256 = (Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant()
        image_size = $bytes.Length
        application_sha256 = ('c' * 64)
    }
    [IO.File]::WriteAllText((Join-Path $directory 'manifest.json'), ($manifest | ConvertTo-Json), $utf8)
    if (-not $NoSignature) {
        [IO.File]::WriteAllText((Join-Path $directory 'manifest.sig'), 'synthetic fixture; not a signature', $utf8)
    }
    if ($Marker) { [IO.File]::WriteAllText((Join-Path $directory $Marker), '{"state":"HIL_ONLY"}', $utf8) }
    if ($Tampered) { [IO.File]::WriteAllBytes($image, [byte[]]@(1, 2, 3)) }
    return $directory
}

function Set-StorageImage {
    param([long]$Length)
    $path = Join-Path $unsigned 'storage-empty.bin'
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    if ($Length -lt 0) { return }
    $stream = [IO.File]::Create($path)
    try { $stream.SetLength($Length) } finally { $stream.Dispose() }
}

function Invoke-Signer {
    param([hashtable]$Arguments)
    $common = @{
        VaultDirectory = $vault
        UnsignedDirectory = $unsigned
        OutputDirectory = $output
        DeviceMac = 'ac:27:6e:a4:4a:d8'
        GitSha = ('a' * 40)
    }
    $message = ''
    try { & $signer @common @Arguments | Out-Null } catch { $message = $_.Exception.Message }
    if ((Test-Path -LiteralPath $vault) -or (Test-Path -LiteralPath $output)) {
        throw 'Production bootstrap validation touched the vault or output before refusing'
    }
    return $message
}

function Assert-Refused {
    param([string]$Case, [hashtable]$Arguments, [string]$Pattern)
    $message = Invoke-Signer $Arguments
    if ($message -notmatch $Pattern) { throw "$Case was not refused as expected; got '$message'" }
}

New-Item -ItemType Directory -Path $unsigned -Force | Out-Null
[IO.File]::WriteAllBytes((Join-Path $unsigned 'zone_lite.bin'), [byte[]]@(0, 1, 2, 3))
Push-Location $root
try {
    $production = New-SyntheticRelease -Version '2.7.8'
    $predecessor = New-SyntheticRelease -Version '2.5.2'
    Set-StorageImage 0x800000

    Assert-Refused 'Predecessor without a production release' @{
        RollbackPredecessorDirectory = $predecessor
    } 'only together with a production release'
    Assert-Refused 'Production release without a predecessor' @{
        ProductionReleaseDirectory = $production
    } 'must carry a rollback predecessor for ota_1'
    Assert-Refused 'Production release for a rescue image' @{
        ProductionReleaseDirectory = $production
        RollbackPredecessorDirectory = $predecessor
        ImageRole = 'usb-wifi-rescue'
    } 'Only a factory bootstrap can carry a production release'
    foreach ($marker in @('.hil-only.json', '.diagnostic-only.json')) {
        Assert-Refused "Predecessor marked $marker" @{
            ProductionReleaseDirectory = $production
            RollbackPredecessorDirectory = (New-SyntheticRelease -Version '2.5.2' -Marker $marker)
        } ('Rollback predecessor .* is not AVAILABLE \(' + [regex]::Escape($marker) + '\)')
    }
    Assert-Refused 'Predecessor without a signed manifest' @{
        ProductionReleaseDirectory = $production
        RollbackPredecessorDirectory = (New-SyntheticRelease -Version '2.5.2' -NoSignature)
    } 'Rollback predecessor is missing its signed manifest'
    Assert-Refused 'Predecessor of another family' @{
        ProductionReleaseDirectory = $production
        RollbackPredecessorDirectory = (New-SyntheticRelease -Version '2.5.2' -Family 'hikvision')
    } 'Rollback predecessor family, layout or channel is not a factory production image'
    Assert-Refused 'Predecessor whose image differs from its manifest' @{
        ProductionReleaseDirectory = $production
        RollbackPredecessorDirectory = (New-SyntheticRelease -Version '2.5.2' -Tampered)
    } 'Rollback predecessor image does not match its signed manifest'
    foreach ($version in @('2.7.8', '2.7.9')) {
        Assert-Refused "Predecessor $version beside production 2.7.8" @{
            ProductionReleaseDirectory = $production
            RollbackPredecessorDirectory = (New-SyntheticRelease -Version $version)
        } "Rollback predecessor $([regex]::Escape($version)) must be older than production release 2\.7\.8"
    }
    Assert-Refused 'Production release whose image differs from its manifest' @{
        ProductionReleaseDirectory = (New-SyntheticRelease -Version '2.7.8' -Tampered)
        RollbackPredecessorDirectory = $predecessor
    } 'Production release image does not match its signed manifest'

    Set-StorageImage -Length -1
    Assert-Refused 'Production bootstrap without the empty storage image' @{
        ProductionReleaseDirectory = $production
        RollbackPredecessorDirectory = $predecessor
    } 'Unsigned firmware package is missing storage-empty\.bin'
    Set-StorageImage 0x400000
    Assert-Refused 'Empty storage image of the wrong size' @{
        ProductionReleaseDirectory = $production
        RollbackPredecessorDirectory = $predecessor
    } 'Empty storage image does not cover the 8 MiB storage partition'

    # A complete production claim passes every check above; the synthetic
    # application then fails the later identity check, before key access.
    Set-StorageImage 0x800000
    $message = Invoke-Signer @{
        ProductionReleaseDirectory = $production
        RollbackPredecessorDirectory = $predecessor
    }
    if ($message -match 'rollback predecessor|Production release|storage|not AVAILABLE|signed manifest') {
        throw "A complete production claim was refused by its own validation: '$message'"
    }
    Write-Output 'PASS: production bootstrap requires an older AVAILABLE ota_1 predecessor and the 8 MiB empty storage image before key access'
    # The synthetic application makes the identity check's native command
    # fail on purpose; clear only that expected exit code.
    $global:LASTEXITCODE = 0
} finally {
    Pop-Location
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
