param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]+$')][string]$AddContainer = 'attendance-device-dashboard-add-api-1',
    [switch]$RequirePublishedHilRelease,
    [ValidateRange(0,16)][int]$AllowPreviousPrefixCount = 0
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
    # /tmp is tmpfs in production; docker cp does not see that mount reliably.
    # Transfer public metadata through bounded ASCII arguments. Windows runner
    # stdin transformations cannot affect these bytes; each argument stays well
    # below its native command-line limit. Never use this for firmware or secrets.
    $items = @(@($manifest, 'manifest.json'), @($signature, 'manifest.sig'), @($script, 'check.py'))
    if ($RequirePublishedHilRelease) { $items += ,@((Join-Path $source '.hil-only.json'), 'hil-marker.json') }
    elseif ($AllowPreviousPrefixCount) { throw 'Previous prefix requires published HIL mode.' }
    foreach ($item in $items) {
        $bytes = [IO.File]::ReadAllBytes($item[0])
        if ($bytes.Length -eq 0 -or $bytes.Length -gt 65536) { throw 'Firmware admission file exceeds its bounded format.' }
        $fileDigest = (Get-FileHash -LiteralPath $item[0] -Algorithm SHA256).Hash.ToLowerInvariant()
        $encoded = [Convert]::ToBase64String($bytes)
        for ($offset = 0; $offset -lt $encoded.Length; $offset += 3072) {
            $chunk = $encoded.Substring($offset, [Math]::Min(3072, $encoded.Length - $offset))
            $decodedOffset = [int]($offset / 4 * 3)
            & docker exec $AddContainer python -c 'import base64,os,pathlib,sys; data=base64.b64decode(sys.argv[2],validate=True); offset=int(sys.argv[3]); p=pathlib.Path(sys.argv[1]); assert 0<len(data)<=2304 and 0<=offset and offset+len(data)<=65536; assert (p.stat().st_size if p.exists() else 0)==offset; fd=os.open(p,os.O_WRONLY|(os.O_CREAT|os.O_EXCL if offset==0 else os.O_APPEND),0o600); written=os.write(fd,data); os.close(fd); assert written==len(data)' "$temporary/$($item[1])" $chunk $decodedOffset
            if ($LASTEXITCODE -ne 0) { throw 'Could not transfer deployed ADD admission check.' }
        }
        & docker exec $AddContainer python -c 'import hashlib,pathlib,sys; data=pathlib.Path(sys.argv[1]).read_bytes(); assert 0<len(data)<=65536 and hashlib.sha256(data).hexdigest()==sys.argv[2]' "$temporary/$($item[1])" $fileDigest
        if ($LASTEXITCODE -ne 0) { throw 'Deployed admission file identity did not verify.' }
    }
    $checkArguments = @("$temporary/check.py")
    if ($RequirePublishedHilRelease) {
        $checkArguments += @('--require-published-hil', '--publication-marker', "$temporary/hil-marker.json", '--previous-prefix-count', [string]$AllowPreviousPrefixCount)
    }
    $checkArguments += @("$temporary/manifest.json", "$temporary/manifest.sig", $digest)
    $report = @(& docker exec $AddContainer python @checkArguments 2>$null)
    $allowed = @("ADD_FIRMWARE_CONTRACT_ACCEPTED:$digest")
    if ($RequirePublishedHilRelease) {
        $allowed = @("ADD_FIRMWARE_CONTRACT_ACCEPTED:${digest}:CATALOG_CURRENT")
        if ($AllowPreviousPrefixCount) { $allowed += "ADD_FIRMWARE_CONTRACT_ACCEPTED:${digest}:CATALOG_REFRESH_PENDING" }
    }
    if ($LASTEXITCODE -ne 0 -or $report.Count -ne 1 -or $allowed -cnotcontains $report[0]) {
        throw 'Deployed ADD rejects this signed firmware contract. Deploy its tested backend support before publication.'
    }
    Write-Host "Deployed ADD accepts signed manifest SHA256 $digest"
    if ($RequirePublishedHilRelease) { Write-Output ($report[0].Split(':')[-1]) }
} catch {
    $failed = $true
    throw
} finally {
    if ($created) {
        & docker exec $AddContainer python -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' $temporary
        if ($LASTEXITCODE -ne 0 -and -not $failed) { throw 'Could not remove temporary ADD admission files.' }
    }
}
