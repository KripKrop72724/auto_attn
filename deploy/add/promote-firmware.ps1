param(
    [Parameter(Mandatory = $true)][string]$StoreDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$GitSha,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
$store = (Resolve-Path $StoreDirectory).Path
$release = Join-Path $store $Version
$markerPath = Join-Path $release '.hil-only.json'
$diagnosticMarkerPath = Join-Path $release '.diagnostic-only.json'
if (Test-Path -LiteralPath $diagnosticMarkerPath -PathType Leaf) {
    throw "Firmware $Version is a non-promotable diagnostic release"
}
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
    throw "Firmware $Version is not an HIL_ONLY release"
}
$manifestPath = Join-Path $release 'manifest.json'
$signaturePath = Join-Path $release 'manifest.sig'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $signaturePath -PathType Leaf)) {
    throw "Firmware $Version is missing its signed manifest"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
if ([string]$manifest.version -ne $Version -or [string]$manifest.git_sha -ne $GitSha) {
    throw 'Promotion SHA or version does not match the signed release'
}
if ([string]$marker.git_sha -ne $GitSha -or [string]$marker.image_sha256 -ne [string]$manifest.image_sha256) {
    throw 'HIL marker does not match the immutable signed release'
}
$image = Join-Path $release ([string]$manifest.image_name)
$actualHash = (Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne [string]$manifest.image_sha256) {
    throw 'Firmware image changed after HIL publication'
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$output = (Resolve-Path $OutputDirectory).Path
Copy-Item -Path (Join-Path $release '*') -Destination $output -Recurse -Force
$outputMarkerPath = Join-Path $output '.hil-only.json'
if (Test-Path -LiteralPath $outputMarkerPath -PathType Leaf) {
    Remove-Item -LiteralPath $outputMarkerPath -Force
}
Remove-Item -LiteralPath $markerPath -Force
Write-Host "Promoted the exact HIL-tested Zone Lite $Version bytes to AVAILABLE"
