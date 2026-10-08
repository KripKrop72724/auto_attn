$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('storage-recovery-publication-' + [guid]::NewGuid().ToString('N'))
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$publish = Join-Path $repo 'deploy/add/publish-firmware.ps1'
$promote = Join-Path $repo 'deploy/add/promote-firmware.ps1'
$marker = 'ZONE_STORAGE_CONTRACT_V2:RECOVERY:READ=2:LANES=3F:BASE=2.5.2'
$baseline = '4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b'
function Assert-Rejected([scriptblock]$Action, [string]$Message) {
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw $Message }
}
try {
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    . (Join-Path $repo 'deploy/add/firmware-storage-contract.ps1')
    . (Join-Path $repo 'deploy/add/firmware-storage-recovery-hil-scope.ps1')
    $image = Join-Path $root 'contract.bin'
    # 2.6.24 is published; 2.6.25 is the current role. Both stay exact.
    $versions = @('2.6.24', '2.6.25')

    # Exact contract, including the single-element array under PowerShell 5.1.
    $expected = '{"allowed_bootstrap_images":{"2.5.2":"' + $baseline + '"},"allowed_bootstrap_versions":["2.5.2"],"read_format":2,"reader_mask":63,"schema_version":2,"write_format":1}'
    $legacy = 'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2'
    foreach ($version in $versions) {
        [IO.File]::WriteAllText($image, 'prefix' + [char]0 + $marker + [char]0 + 'suffix')
        $json = Get-FirmwareStorageContract -ImagePath $image -Version $version -ForSigning | ConvertTo-Json -Depth 10 -Compress
        if ($json -cne $expected) { throw "Recovery contract serialized differently for ${version}: $json" }
        foreach ($bad in @('', 'ZONE_STORAGE_CONTRACT_V1:LEGACY:READ=2:LANES=3F:COMPAT=2.5.4' + [char]0, $legacy + [char]0,
            ($marker + [char]0 + $marker + [char]0), ($marker + [char]0 + $legacy + [char]0), ($marker + 'X'),
            $marker.Replace('2.5.2', '2.4.12') + [char]0)) {
            [IO.File]::WriteAllText($image, $bad)
            Assert-Rejected { Get-FirmwareStorageContract -ImagePath $image -Version $version -ForSigning } "Unqualified recovery marker accepted for $version"
        }
    }
    [IO.File]::WriteAllText($image, 'prefix' + [char]0 + $marker + [char]0 + 'suffix')
    $contract = Get-FirmwareStorageContract -ImagePath $image -Version '2.6.25' -ForSigning
    foreach ($other in @('2.6.15', '2.6.23', '2.6.26', '2.7.0', '9.9.9')) {
        Assert-Rejected { Get-FirmwareStorageContract -ImagePath $image -Version $other -ForSigning } "Recovery marker signed as $other"
    }

    # Exact reviewed scope only.
    $scope = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-storage-recovery.json') -Raw
    $parsedScope = ConvertFrom-Json -InputObject $scope
    $targets = @($parsedScope)
    if ($targets.Count -ne 2) { throw 'Recovery scope file must hold two targets' }
    $exact = ConvertTo-Json -InputObject $targets -Depth 5 -Compress
    Assert-ZktStorageRecoveryHilScope -HilTargetsJson $exact
    $reordered = ConvertTo-Json -InputObject @($targets[1], $targets[0]) -Depth 5 -Compress
    $single = ConvertTo-Json -InputObject @($targets[0]) -Depth 5 -Compress
    $changed = $exact.Replace('CJH9211060002', 'CJH9211060003')
    $extra = $exact.Replace('"terminal_serial"', '"zone":"PESHAWAR","terminal_serial"')
    foreach ($bad in @('', '{}', $reordered, $single, $changed, $extra)) {
        Assert-Rejected { Assert-ZktStorageRecoveryHilScope -HilTargetsJson $bad } 'Recovery scope accepted a changed target list'
    }

    # Publication accepts only the exact experimental HIL package.
    $store = Join-Path $root 'store'
    function Write-Package($Values) {
        $path = Join-Path $source 'manifest.json'
        [IO.File]::WriteAllText($path, ($Values | ConvertTo-Json -Depth 10 -Compress))
        & python (Join-Path $repo 'scripts/canonicalize_firmware_manifest.py') $path
        if ($LASTEXITCODE -ne 0) { throw 'Test manifest canonicalization failed' }
        [IO.File]::WriteAllText((Join-Path $source 'manifest.sig'), 'test-signature')
        [IO.File]::WriteAllText((Join-Path $source 'SHA256SUMS'), 'test')
    }
    foreach ($version in $versions) {
        $source = Join-Path $root "package-$version"
        New-Item -ItemType Directory -Path $source -Force | Out-Null
        $imageName = "zone-lite-$version.bin"
        [IO.File]::WriteAllText((Join-Path $source $imageName), 'image' + [char]0 + $marker + [char]0)
        $imagePath = Join-Path $source $imageName
        $manifest = [ordered]@{
            application_sha256 = ('a' * 64)
            firmware_family = 'zkt'
            git_sha = ('b' * 40)
            hil_targets = $targets
            image_name = $imageName
            image_sha256 = (Get-FileHash -LiteralPath $imagePath -Algorithm SHA256).Hash.ToLowerInvariant()
            image_size = (Get-Item -LiteralPath $imagePath).Length
            minimum_bootstrap_version = '2.5.2'
            project_name = 'zone_lite'
            queue_storage = $contract
            release_channel = 'EXPERIMENTAL_HIL_ONLY'
            release_id = "zone-lite-$version"
            version = $version
        }
        Write-Package $manifest
        Assert-Rejected { & $publish -SourceDirectory $source -StoreDirectory $store -Version $version } "Recovery $version published as AVAILABLE"
        Assert-Rejected { & $publish -SourceDirectory $source -StoreDirectory $store -Version $version -PublicationMode HIL_ONLY -HilTargetMac 'e0:72:a1:d7:05:c4' } "Recovery $version published to a legacy MAC scope"
        Assert-Rejected { & $publish -SourceDirectory $source -StoreDirectory $store -Version $version -PublicationMode HIL_ONLY -HilTargetsJson $reordered } "Recovery $version published to a reordered scope"
        foreach ($change in @(@{minimum_bootstrap_version = '2.4.12'}, @{release_channel = $null}, @{hil_targets = @($targets[0])})) {
            $variant = [ordered]@{}
            foreach ($key in $manifest.Keys) { $variant[$key] = $manifest[$key] }
            foreach ($key in $change.Keys) { if ($null -eq $change[$key]) { $variant.Remove($key) } else { $variant[$key] = $change[$key] } }
            Write-Package $variant
            Assert-Rejected { & $publish -SourceDirectory $source -StoreDirectory $store -Version $version -PublicationMode HIL_ONLY -HilTargetsJson $exact } "Recovery $version published with an unqualified manifest"
        }
        Write-Package $manifest
        & $publish -SourceDirectory $source -StoreDirectory $store -Version $version -PublicationMode HIL_ONLY -HilTargetsJson $exact
        $published = Get-Content -LiteralPath (Join-Path $store "$version/.hil-only.json") -Raw | ConvertFrom-Json
        if ($published.schema_version -ne 2 -or (ConvertTo-Json -InputObject @($published.targets) -Depth 5 -Compress) -cne $exact) {
            throw "Recovery $version HIL marker does not bind the exact scope"
        }
        Assert-Rejected { & $promote -StoreDirectory $store -Version $version -GitSha ('b' * 40) -OutputDirectory (Join-Path $root 'out') } "Recovery $version image was promoted"
    }
    Write-Host 'Storage recovery publication tests passed'
} finally {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
