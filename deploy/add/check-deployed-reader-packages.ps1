param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{64}$')][string]$MatrixSha256,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]+$')][string]$AddContainer = 'attendance-device-dashboard-add-api-1'
)

$ErrorActionPreference = 'Stop'
$script = (Resolve-Path (Join-Path $PSScriptRoot '../../scripts/check_deployed_reader_packages.py')).Path
$bytes = [IO.File]::ReadAllBytes($script)
if ($bytes.Length -eq 0 -or $bytes.Length -gt 65536) { throw 'Reader verifier exceeds its bounded format.' }
$digest = (Get-FileHash -LiteralPath $script -Algorithm SHA256).Hash.ToLowerInvariant()
$temporary = '/tmp/add-reader-packages-' + [guid]::NewGuid().ToString('N')
$created = $false
$failed = $false
try {
    & docker exec $AddContainer python -c 'import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(mode=0o700)' $temporary
    if ($LASTEXITCODE -ne 0) { throw 'Could not prepare deployed ADD reader verification.' }
    $created = $true
    # Same bounded public-source transfer as check-deployed-firmware-contract.
    # The checker uses ADD settings; no store, database or key secrets leave ADD.
    $encoded = [Convert]::ToBase64String($bytes)
    for ($offset = 0; $offset -lt $encoded.Length; $offset += 3072) {
        $chunk = $encoded.Substring($offset, [Math]::Min(3072, $encoded.Length - $offset))
        $decodedOffset = [int]($offset / 4 * 3)
        & docker exec $AddContainer python -c 'import base64,os,pathlib,sys; data=base64.b64decode(sys.argv[2],validate=True); offset=int(sys.argv[3]); p=pathlib.Path(sys.argv[1]); assert 0<len(data)<=2304 and 0<=offset and offset+len(data)<=65536; assert (p.stat().st_size if p.exists() else 0)==offset; fd=os.open(p,os.O_WRONLY|(os.O_CREAT|os.O_EXCL if offset==0 else os.O_APPEND),0o600); written=os.write(fd,data); os.close(fd); assert written==len(data)' "$temporary/check.py" $chunk $decodedOffset
        if ($LASTEXITCODE -ne 0) { throw 'Could not transfer deployed ADD reader verification.' }
    }
    & docker exec $AddContainer python -c 'import hashlib,pathlib,sys; data=pathlib.Path(sys.argv[1]).read_bytes(); assert 0<len(data)<=65536 and hashlib.sha256(data).hexdigest()==sys.argv[2]' "$temporary/check.py" $digest
    if ($LASTEXITCODE -ne 0) { throw 'Deployed reader verifier identity did not verify.' }
    $report = @(& docker exec $AddContainer python "$temporary/check.py" $MatrixSha256 2>$null)
    if ($LASTEXITCODE -ne 0 -or $report.Count -ne 1 -or
        $report[0] -cne "ADD_READER_PACKAGES_ACCEPTED:$MatrixSha256") {
        throw 'Exact reader packages are missing, revoked, unverified or inconsistent with deployed ADD. Writer signing is blocked.'
    }
    Write-Host "Deployed ADD verified exact signed reader packages for matrix SHA256 $MatrixSha256"
} catch {
    $failed = $true
    throw
} finally {
    if ($created) {
        & docker exec $AddContainer python -c 'import shutil,sys; shutil.rmtree(sys.argv[1])' $temporary
        if ($LASTEXITCODE -ne 0 -and -not $failed) { throw 'Could not remove temporary reader verification files.' }
    }
}
