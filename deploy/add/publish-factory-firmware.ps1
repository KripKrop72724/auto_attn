param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$StoreDirectory,
    [ValidateSet('HIL_ONLY', 'AVAILABLE')][string]$PublicationMode = 'HIL_ONLY'
)

$ErrorActionPreference = 'Stop'
$source = (Resolve-Path $SourceDirectory).Path
$manifestPath = Join-Path $source 'manifest.json'
$signaturePath = Join-Path $source 'manifest.sig'
if (-not (Test-Path $manifestPath -PathType Leaf) -or -not (Test-Path $signaturePath -PathType Leaf)) {
    throw 'Factory bundle is missing its canonical manifest or signature'
}
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
if ($manifest.hardware_profile -ne 'esp32s3-16mb-zone-lite-v1') { throw 'Factory hardware profile is invalid' }
if (-not $manifest.setup_password_supplied) { throw 'Protected setup-password evidence is missing' }
foreach ($image in $manifest.images) {
    $path = Join-Path $source ([IO.Path]::GetFileName([string]$image.name))
    if (-not (Test-Path $path -PathType Leaf)) { throw "Factory image $($image.name) is missing" }
    $digest = (Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -ne [string]$image.sha256) { throw "Factory image $($image.name) hash mismatch" }
}
New-Item -ItemType Directory -Path $StoreDirectory -Force | Out-Null
$store = (Resolve-Path $StoreDirectory).Path
$destination = Join-Path $store ([string]$manifest.bundle_id)
if (Test-Path $destination) {
    $existing = (Get-FileHash (Join-Path $destination 'manifest.json') -Algorithm SHA256).Hash
    $incoming = (Get-FileHash $manifestPath -Algorithm SHA256).Hash
    if ($existing -ne $incoming) { throw 'Published factory bundle identities are immutable' }
} else {
    $staging = Join-Path $store ('.factory-' + [guid]::NewGuid().ToString('N'))
    Copy-Item -LiteralPath $source -Destination $staging -Recurse
    Move-Item -LiteralPath $staging -Destination $destination
}
$marker = Join-Path $destination '.hil-only.json'
if ($PublicationMode -eq 'HIL_ONLY') {
    [IO.File]::WriteAllText($marker, '{"state":"HIL_ONLY","promote_exact_bytes_only":true}', (New-Object Text.UTF8Encoding($false)))
} elseif (Test-Path $marker) {
    Remove-Item -LiteralPath $marker -Force
}
Write-Host "Published immutable factory bundle $($manifest.bundle_id) as $PublicationMode"
