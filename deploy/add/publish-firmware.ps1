param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$StoreDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version
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
if ($manifest.image_file -ne "zone-lite-$Version.bin") { throw 'Manifest image file is invalid' }
$image = Join-Path $source $manifest.image_file
$actualHash = (Get-FileHash $image -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $manifest.image_sha256) { throw 'Firmware image SHA-256 does not match manifest' }
if ((Get-Item $image).Length -ne $manifest.image_size) { throw 'Firmware image size does not match manifest' }

$final = Join-Path $store $Version
if (Test-Path $final) {
    $existingHash = (Get-FileHash (Join-Path $final $manifest.image_file) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($existingHash -eq $actualHash) {
        Write-Host "Firmware $Version is already published with the same immutable hash."
        exit 0
    }
    throw "Firmware $Version already exists with different content; published versions are immutable"
}

$staging = Join-Path $store (".staging-$Version-" + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $staging | Out-Null
    Copy-Item (Join-Path $source '*') $staging -Recurse -Force
    Move-Item -Path $staging -Destination $final
    Write-Host "Published immutable firmware $Version to $final"
}
finally {
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
}
