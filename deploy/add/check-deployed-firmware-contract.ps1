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
$previousEncoding = $OutputEncoding
# Windows PowerShell may inherit a BOM-producing encoding from its runner.
# The payload is ASCII base64; an injected BOM must not reach strict decoding.
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    & docker exec $AddContainer python -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(mode=0o700)' $temporary
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare deployed ADD admission check.' }
    $created = $true
    # /tmp is tmpfs in production; docker cp does not see that mount reliably.
    # Transfer bounded public bytes through exec, checking each digest before
    # executing the saved inspector. Base64 is lossless through PowerShell 5.1's
    # text pipeline; Python code is still executed from a file, not stdin.
    foreach ($item in @(@($manifest, 'manifest.json'), @($signature, 'manifest.sig'), @($script, 'check.py'))) {
        $bytes = [IO.File]::ReadAllBytes($item[0])
        if ($bytes.Length -gt 65536) { throw 'Firmware admission file exceeds its bounded format.' }
        $fileDigest = (Get-FileHash -LiteralPath $item[0] -Algorithm SHA256).Hash.ToLowerInvariant()
        [Convert]::ToBase64String($bytes) | & docker exec -i $AddContainer python -c 'import base64,hashlib,os,pathlib,sys; os.umask(0o077); encoded=sys.stdin.buffer.read(131073).strip(); assert len(encoded)<=131072; data=base64.b64decode(encoded,validate=True); assert len(data)<=65536 and hashlib.sha256(data).hexdigest()==sys.argv[2]; assert pathlib.Path(sys.argv[1]).write_bytes(data)==len(data)' "$temporary/$($item[1])" $fileDigest
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
    $OutputEncoding = $previousEncoding
    if ($created) {
        & docker exec $AddContainer python -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' $temporary
        if ($LASTEXITCODE -ne 0 -and -not $failed) { throw 'Could not remove temporary ADD admission files.' }
    }
}
