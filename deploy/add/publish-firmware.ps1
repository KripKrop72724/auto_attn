param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$StoreDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version,
    [ValidateSet('AVAILABLE', 'HIL_ONLY')][string]$PublicationMode = 'AVAILABLE',
    [ValidatePattern('^$|^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$')][string]$HilTargetMac = ''
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
if ($PublicationMode -eq 'HIL_ONLY' -and [string]::IsNullOrWhiteSpace($HilTargetMac)) {
    throw 'HIL_ONLY publication requires one exact ESP MAC'
}
if ($PublicationMode -eq 'AVAILABLE' -and -not [string]::IsNullOrWhiteSpace($HilTargetMac)) {
    throw 'A production publication cannot carry a HIL target MAC'
}

$final = Join-Path $store $Version
if (Test-Path $final) {
    $existingHash = (Get-FileHash (Join-Path $final $manifest.image_name) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($existingHash -eq $actualHash) {
        $marker = Join-Path $final '.hil-only.json'
        if ($PublicationMode -eq 'HIL_ONLY') {
            if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
                throw "Firmware $Version already exists outside HIL quarantine"
            }
            $existingMarker = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
            if ([string]$existingMarker.target_mac -ne $HilTargetMac.ToLowerInvariant()) {
                throw "Firmware $Version HIL target does not match the requested ESP"
            }
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
        } | ConvertTo-Json -Compress
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
