param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$StoreDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version,
    [ValidateSet('AVAILABLE', 'HIL_ONLY')][string]$PublicationMode = 'AVAILABLE',
    [ValidatePattern('^$|^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$')][string]$HilTargetMac = '',
    [string]$HilTargetsJson = ''
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path $SourceDirectory).Path
New-Item -ItemType Directory -Path $StoreDirectory -Force | Out-Null
$store = (Resolve-Path $StoreDirectory).Path
$required = @('manifest.json', 'manifest.sig', "zone-lite-$Version.bin", 'SHA256SUMS')
foreach ($name in $required) {
    if (-not (Test-Path (Join-Path $source $name) -PathType Leaf)) {
        throw "Firmware package is missing $name"
    }
}

$manifest = Get-Content (Join-Path $source 'manifest.json') -Raw | ConvertFrom-Json
if ($manifest.version -ne $Version) { throw 'Manifest version does not match requested version' }
if ($manifest.image_name -ne "zone-lite-$Version.bin") { throw 'Manifest image name is invalid' }
$image = Join-Path $source $manifest.image_name
$actualHash = (Get-FileHash $image -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $manifest.image_sha256) { throw 'Firmware image SHA-256 does not match manifest' }
if ((Get-Item $image).Length -ne $manifest.image_size) { throw 'Firmware image size does not match manifest' }
$targets = @()
if (-not [string]::IsNullOrWhiteSpace($HilTargetsJson)) {
    if (-not [string]::IsNullOrWhiteSpace($HilTargetMac)) { throw 'Choose legacy MAC or ordered targets, not both' }
    if (-not $HilTargetsJson.Trim().StartsWith('[')) { throw 'Ordered HIL targets must be a JSON array' }
    $parsedTargets = @($HilTargetsJson | ConvertFrom-Json)
    if ($parsedTargets.Count -lt 1 -or $parsedTargets.Count -gt 8) { throw 'HIL requires one to eight ordered exact targets' }
    foreach ($target in $parsedTargets) {
        $keys = @($target.PSObject.Properties.Name | Sort-Object)
        if (($keys -join ',') -cne 'connector_id,mac,terminal_serial') { throw 'HIL target must contain only connector_id, mac and terminal_serial' }
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if ($target.$field -isnot [string]) { throw "HIL $field must be text" }
            if ([string]::IsNullOrWhiteSpace($target.$field) -or $target.$field -cne $target.$field.Trim() -or $target.$field -match '[\x00-\x1f]') { throw "HIL $field is invalid" }
        }
        if ($target.connector_id.Length -gt 100 -or $target.terminal_serial.Length -gt 120) { throw 'HIL identity is too long' }
        if ($target.mac -notmatch '^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$') { throw 'HIL target requires an exact ESP MAC' }
        $targets += [ordered]@{connector_id = $target.connector_id; mac = $target.mac.ToLowerInvariant(); terminal_serial = $target.terminal_serial}
    }
    foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
        if (@($targets | ForEach-Object { $_[$field] } | Sort-Object -Unique).Count -ne $targets.Count) { throw "HIL scope repeats $field" }
    }
}
if ($PublicationMode -eq 'HIL_ONLY' -and [string]::IsNullOrWhiteSpace($HilTargetMac) -and $targets.Count -eq 0) {
    throw 'HIL_ONLY publication requires one exact ESP MAC or an ordered exact target list'
}
if ($PublicationMode -eq 'AVAILABLE' -and (-not [string]::IsNullOrWhiteSpace($HilTargetMac) -or $targets.Count)) {
    throw 'A production publication cannot carry HIL targets'
}
$scopeJson = ConvertTo-Json -InputObject @($targets) -Depth 5 -Compress

$final = Join-Path $store $Version
if (Test-Path $final) {
    $existingHash = (Get-FileHash (Join-Path $final $manifest.image_name) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($existingHash -eq $actualHash) {
        foreach ($name in @('manifest.json', 'manifest.sig')) {
            $existingMetadata = (Get-FileHash (Join-Path $final $name) -Algorithm SHA256).Hash
            $incomingMetadata = (Get-FileHash (Join-Path $source $name) -Algorithm SHA256).Hash
            if ($existingMetadata -cne $incomingMetadata) { throw "Firmware $Version signed metadata is immutable" }
        }
        $marker = Join-Path $final '.hil-only.json'
        if ($PublicationMode -eq 'HIL_ONLY') {
            if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
                throw "Firmware $Version already exists outside HIL quarantine"
            }
            $existingMarker = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
            if ($targets.Count) {
                $existingScope = ConvertTo-Json -InputObject @($existingMarker.targets) -Depth 5 -Compress
                if ($existingScope -cne $scopeJson -or $existingMarker.target_mac) { throw "Firmware $Version HIL ordered scope is immutable" }
                if ([string]$existingMarker.application_sha256 -cne [string]$manifest.application_sha256) { throw 'HIL application digest changed' }
            } elseif ([string]$existingMarker.target_mac -ne $HilTargetMac.ToLowerInvariant() -or $existingMarker.targets) {
                throw "Firmware $Version HIL target does not match the requested ESP"
            }
            if ([string]$existingMarker.git_sha -cne [string]$manifest.git_sha -or [string]$existingMarker.image_sha256 -cne $actualHash) { throw 'HIL publication provenance changed' }
        } elseif (Test-Path -LiteralPath $marker -PathType Leaf) {
            throw "Firmware $Version remains HIL_ONLY and must be promoted after a successful hardware gate"
        }
        Write-Host "Firmware $Version is already published with the same immutable hash."
        exit 0
    }
    throw "Firmware $Version already exists with different content; published versions are immutable"
}

$staging = Join-Path $store (".staging-$Version-" + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $staging | Out-Null
    Copy-Item (Join-Path $source '*') $staging -Recurse -Force
    if ($PublicationMode -eq 'HIL_ONLY') {
        $marker = [ordered]@{
            schema_version = 1
            target_mac = $HilTargetMac.ToLowerInvariant()
            version = $Version
            git_sha = [string]$manifest.git_sha
            image_sha256 = [string]$manifest.image_sha256
        }
        if ($targets.Count) {
            $marker.Remove('target_mac')
            $marker.schema_version = 2
            $marker.targets = @($targets)
            $marker.application_sha256 = [string]$manifest.application_sha256
        }
        $marker = $marker | ConvertTo-Json -Depth 6 -Compress
        [IO.File]::WriteAllText(
            (Join-Path $staging '.hil-only.json'),
            $marker + "`n",
            (New-Object Text.UTF8Encoding($false))
        )
    }
    Move-Item -Path $staging -Destination $final
    Write-Host "Published immutable firmware $Version as $PublicationMode to $final"
}
finally {
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
}
