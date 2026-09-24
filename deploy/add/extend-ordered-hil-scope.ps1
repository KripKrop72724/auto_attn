param(
    [Parameter(Mandatory = $true)][string]$StoreDirectory,
    [Parameter(Mandatory = $true)][ValidateSet('2.5.4', '2.6.0')][string]$Version,
    [Parameter(Mandatory = $true)][string]$ExpectedGitSha,
    [Parameter(Mandatory = $true)][string]$ExpectedImageSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedApplicationSha256,
    [Parameter(Mandatory = $true)][string]$ExistingTargetsJson,
    [Parameter(Mandatory = $true)][string]$ExtendedTargetsJson,
    [switch]$PreviewOnly
)

$ErrorActionPreference = 'Stop'

function Read-ExactTargets([string]$Json) {
    if (-not $Json.Trim().StartsWith('[')) { throw 'HIL targets must be a JSON array' }
    $parsed = ConvertFrom-Json -InputObject $Json
    $parsed = @($parsed)
    if ($parsed.Count -lt 1 -or $parsed.Count -gt 8) { throw 'HIL requires one to eight exact targets' }
    $targets = @()
    foreach ($row in $parsed) {
        $keys = @($row.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if ($keys.Count -ne 3 -or $keys -cnotcontains 'connector_id' -or
            $keys -cnotcontains 'mac' -or $keys -cnotcontains 'terminal_serial') {
            throw 'HIL targets require only connector_id, mac and terminal_serial'
        }
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            $value = $row.$field
            if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value) -or
                $value -cne $value.Trim() -or $value -match '[\x00-\x1f]') {
                throw "Invalid exact HIL $field"
            }
        }
        if ($row.connector_id.Length -gt 100 -or $row.terminal_serial.Length -gt 120 -or
            $row.mac -cnotmatch '^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$') {
            throw 'Invalid exact HIL identity'
        }
        $targets += [ordered]@{
            connector_id = $row.connector_id
            mac = $row.mac
            terminal_serial = $row.terminal_serial
        }
    }
    foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
        if (@($targets | ForEach-Object { $_[$field] } | Sort-Object -Unique).Count -ne $targets.Count) {
            throw "Repeated HIL $field"
        }
    }
    return ,$targets
}

function Assert-SameTargets($Actual, $Expected) {
    if ($Actual.Count -ne $Expected.Count) { throw 'HIL target count changed unexpectedly' }
    for ($index = 0; $index -lt $Expected.Count; $index++) {
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if ($Actual[$index][$field] -cne $Expected[$index][$field]) {
                throw 'HIL target identity or order changed unexpectedly'
            }
        }
    }
}

$existingTargets = Read-ExactTargets $ExistingTargetsJson
$extendedTargets = Read-ExactTargets $ExtendedTargetsJson
if ($extendedTargets.Count -le $existingTargets.Count) { throw 'HIL extension must append targets' }
for ($index = 0; $index -lt $existingTargets.Count; $index++) {
    foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
        if ($extendedTargets[$index][$field] -cne $existingTargets[$index][$field]) {
            throw 'HIL extension must preserve the original ordered prefix'
        }
    }
}
foreach ($digest in @($ExpectedGitSha, $ExpectedImageSha256, $ExpectedApplicationSha256)) {
    if ($digest -cnotmatch '^[0-9a-f]{40}$|^[0-9a-f]{64}$') { throw 'Invalid expected release digest' }
}

$release = Join-Path $StoreDirectory $Version
$manifestPath = Join-Path $release 'manifest.json'
$signaturePath = Join-Path $release 'manifest.sig'
$markerPath = Join-Path $release '.hil-only.json'
foreach ($path in @($manifestPath, $signaturePath, $markerPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing published release file: $path" }
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
if ($manifest.version -cne $Version -or $manifest.firmware_family -eq 'hikvision' -or
    $manifest.git_sha -cne $ExpectedGitSha -or
    $manifest.image_sha256 -cne $ExpectedImageSha256 -or
    $manifest.application_sha256 -cne $ExpectedApplicationSha256 -or
    $manifest.image_name -cne "zone-lite-$Version.bin") {
    throw 'Published signed release identity differs from the approved HIL release'
}
$imagePath = Join-Path $release $manifest.image_name
if (-not (Test-Path -LiteralPath $imagePath -PathType Leaf) -or
    (Get-FileHash -LiteralPath $imagePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedImageSha256 -or
    (Get-Item -LiteralPath $imagePath).Length -ne $manifest.image_size) {
    throw 'Published signed image differs from the approved HIL release'
}
if ($marker.schema_version -ne 2 -or $marker.target_mac -or
    $marker.version -cne $Version -or $marker.git_sha -cne $ExpectedGitSha -or
    $marker.image_sha256 -cne $ExpectedImageSha256 -or
    $marker.application_sha256 -cne $ExpectedApplicationSha256) {
    throw 'Existing HIL quarantine marker differs from the approved release'
}
$currentTargets = Read-ExactTargets (ConvertTo-Json -InputObject @($marker.targets) -Depth 6 -Compress)
if ($currentTargets.Count -eq $extendedTargets.Count) {
    Assert-SameTargets $currentTargets $extendedTargets
    Write-Host "Firmware $Version already has the approved extended HIL scope."
    exit 0
}
Assert-SameTargets $currentTargets $existingTargets
if ($PreviewOnly) {
    Write-Host "Firmware $Version extension preflight passed: $($existingTargets.Count) to $($extendedTargets.Count) ordered targets."
    exit 0
}

$marker.targets = @($extendedTargets)
$replacement = Join-Path $release ('.hil-extension-' + [guid]::NewGuid().ToString('N') + '.tmp')
$backup = Join-Path $release ('.hil-scope-before-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '-' + [guid]::NewGuid().ToString('N') + '.json')
try {
    [IO.File]::WriteAllText($replacement, (($marker | ConvertTo-Json -Depth 6 -Compress) + "`n"), (New-Object Text.UTF8Encoding($false)))
    [IO.File]::Replace($replacement, $markerPath, $backup)
} finally {
    if (Test-Path -LiteralPath $replacement) { Remove-Item -LiteralPath $replacement -Force }
}
Write-Host "Extended immutable firmware $Version HIL quarantine to $($extendedTargets.Count) exact ordered targets; previous marker preserved at $backup."
