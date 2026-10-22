$ErrorActionPreference = 'Stop'
$config = Join-Path $env:ProgramData 'StateLife\AttendanceDeviceDashboard\config\add.env'
if (-not (Test-Path -LiteralPath $config -PathType Leaf)) { throw 'Protected ADD configuration is unavailable.' }
$map = @{}
foreach ($line in [IO.File]::ReadAllLines($config)) {
    if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
    $value = $Matches[2].Trim()
    if ($value.Length -ge 2 -and (($value.StartsWith("'") -and $value.EndsWith("'")) -or ($value.StartsWith('"') -and $value.EndsWith('"')))) { $value = $value.Substring(1, $value.Length - 2) }
    $map[$Matches[1]] = $value
}
$presence = [ordered]@{}
foreach ($key in @('ADD_PROVISIONING_ENABLED','ADD_FLEET_ROOT_SECRET','ADD_PII_LOOKUP_KEY','ADD_PROVISIONING_PAIRING_SECRET','ADD_PROVISIONING_INTERNAL_TOKEN','ADD_PROVISIONING_COMPANION_RELEASE_PUBLIC_KEY_B64','ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64','ADD_FACTORY_FIRMWARE_STORE_HOST_PATH','ADD_COMPANION_RELEASE_STORE_HOST_PATH')) {
    $presence[$key] = -not [string]::IsNullOrWhiteSpace([string]$map[$key])
}
$stores = @()
foreach ($key in @('ADD_FACTORY_FIRMWARE_STORE_HOST_PATH','ADD_COMPANION_RELEASE_STORE_HOST_PATH','ADD_FIRMWARE_STORE_HOST_PATH')) {
    $path = [string]$map[$key]
    $exists = $path -and (Test-Path -LiteralPath $path -PathType Container)
    $items = @()
    if ($exists) {
        foreach ($manifestPath in Get-ChildItem -LiteralPath $path -Filter 'manifest.json' -File -Recurse -ErrorAction Stop) {
            $manifest = Get-Content -LiteralPath $manifestPath.FullName -Raw | ConvertFrom-Json
            $items += [ordered]@{directory=$manifestPath.Directory.Name; version=$manifest.version; bundle_id=$manifest.bundle_id; setup_password_supplied=$manifest.setup_password_supplied; quarantined=(Test-Path -LiteralPath (Join-Path $manifestPath.Directory.FullName '.hil-only.json')); manifest_sha256=(Get-FileHash $manifestPath.FullName -Algorithm SHA256).Hash.ToLowerInvariant()}
        }
    }
    $stores += [ordered]@{setting=$key; configured=([bool]$path); exists=([bool]$exists); manifests=$items}
}
$images = @(& docker image ls --format '{{.Repository}}:{{.Tag}}' | Where-Object { $_ -eq 'state-life/add-provisioner:production' })
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect provisioner image availability.' }
$containers = @(& docker ps -a --filter 'name=add-provisioner' --format '{{.Names}} {{.Status}}')
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect provisioner container availability.' }
$vault = Join-Path $env:ProgramData 'StateLife\AttendanceDeviceDashboard\firmware-signing'
$vaultManifest = Join-Path $vault 'vault-manifest.json'
$publicMetadata = $null
if (Test-Path -LiteralPath $vaultManifest -PathType Leaf) {
    $v = Get-Content -LiteralPath $vaultManifest -Raw | ConvertFrom-Json
    $publicMetadata = [ordered]@{manifest_present=$true; active_keys=@($v.keys | Where-Object {$_.state -eq 'ACTIVE'}).Count; public_keys=@(Get-ChildItem -LiteralPath $vault -Filter '*public.pem' -File).Count}
}
[ordered]@{configuration_present=$true;provisioning_enabled=($map['ADD_PROVISIONING_ENABLED'] -eq 'true');presence=$presence;stores=$stores;provisioner_image_present=($images.Count -gt 0);provisioner_containers=$containers;signing_vault=$publicMetadata} | ConvertTo-Json -Depth 7
