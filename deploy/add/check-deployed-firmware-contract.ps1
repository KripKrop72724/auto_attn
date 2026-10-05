param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]+$')][string]$AddContainer = 'attendance-device-dashboard-add-api-1'
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $SourceDirectory).Path
$manifest = Join-Path $source 'manifest.json'
$signature = Join-Path $source 'manifest.sig'
$script = (Resolve-Path (Join-Path $PSScriptRoot '../../scripts/check_deployed_firmware_contract.py')).Path
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf) -or
    -not (Test-Path -LiteralPath $signature -PathType Leaf)) {
    throw 'Firmware admission requires the exact signed manifest.'
}
if ((Get-Item -LiteralPath $manifest).Length -gt 65536 -or
    (Get-Item -LiteralPath $signature).Length -gt 8192) {
    throw 'Firmware admission metadata exceeds its bounded format.'
}
$digest = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
$temporary = '/tmp/add-firmware-admission-' + [guid]::NewGuid().ToString('N')
$created = $false
$failed = $false
try {
    & docker exec $AddContainer python -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(mode=0o700)' $temporary
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare deployed ADD admission check.' }
    $created = $true
    # Copy bounded public metadata and an inspector, never firmware credentials
    # or backend code. File execution avoids Windows stdin truncation issues.
    foreach ($item in @(@($manifest, 'manifest.json'), @($signature, 'manifest.sig'), @($script, 'check.py'))) {
        & docker cp $item[0] "${AddContainer}:$temporary/$($item[1])"
        if ($LASTEXITCODE -ne 0) { throw 'Could not transfer deployed ADD admission check.' }
    }
    $report = @(& docker exec $AddContainer python "$temporary/check.py" "$temporary/manifest.json" "$temporary/manifest.sig" $digest 2>$null)
    if ($LASTEXITCODE -ne 0 -or $report.Count -ne 1 -or
        $report[0] -cne "ADD_FIRMWARE_CONTRACT_ACCEPTED:$digest") {
        throw 'Deployed ADD rejects this signed firmware contract. Deploy its tested backend support before publication.'
    }
    Write-Host "Deployed ADD accepts signed manifest SHA256 $digest"
} catch {
    $failed = $true
    throw
} finally {
    if ($created) {
        & docker exec $AddContainer python -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' $temporary
        if ($LASTEXITCODE -ne 0 -and -not $failed) { throw 'Could not remove temporary ADD admission files.' }
    }
}
