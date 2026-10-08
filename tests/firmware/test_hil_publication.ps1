param([ValidateSet("2.6.16", "2.6.17", "2.6.18", "2.6.19", "2.6.20", "2.6.21")][string]$BridgeVersion = "2.6.16")
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('hil-publication-' + [guid]::NewGuid().ToString('N'))
$source = Join-Path $root 'package'
$store = Join-Path $root 'store'
$legacyStore = Join-Path $root 'legacy-store'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$publish = Join-Path $repo 'deploy/add/publish-firmware.ps1'
function Write-TestManifest {
    $path = Join-Path $source 'manifest.json'
    [IO.File]::WriteAllText($path, ($manifest | ConvertTo-Json -Depth 10 -Compress))
    & python (Join-Path $repo 'scripts/canonicalize_firmware_manifest.py') $path
    if ($LASTEXITCODE -ne 0) { throw 'Test manifest canonicalization failed' }
}
try {
New-Item -ItemType Directory -Path $source -Force | Out-Null
. (Join-Path $repo 'deploy/add/firmware-storage-contract.ps1')
$contractImage = Join-Path $root 'contract.bin'
$bridgeMarker = 'ZONE_STORAGE_CONTRACT_V3:BRIDGE:LEGACY=2:JOURNAL=1:READERS=3F:CAPTURE=1:AUTHORITY=1'
if ($BridgeVersion -ne '2.6.16') { $bridgeMarker += ':VERSION=' + $BridgeVersion }
[IO.File]::WriteAllText($contractImage, $bridgeMarker + [char]0)
$bridgeContract = Get-FirmwareStorageContract -ImagePath $contractImage -Version $BridgeVersion
if ($bridgeContract.compatibility_version -cne $BridgeVersion -or $bridgeContract.schema_version -ne 3 -or $bridgeContract.journal_capture -ne $true -or
    $bridgeContract.journal_read_format -ne 1 -or $bridgeContract.journal_reader_mask -ne 63 -or
    $bridgeContract.journal_write_format -ne 1 -or $bridgeContract.write_format -ne 1 -or
    $bridgeContract.delivery_authority -ne 'LEGACY_UNTIL_PERSISTED_ADD_CUTOVER' -or
    $bridgeContract.allowed_bootstrap_versions.Count -ne 3 -or
    $bridgeContract.allowed_bootstrap_images['2.6.15'] -ne '832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6') {
    throw 'Bridge reader or predecessor contract changed'
}
foreach ($other in @('2.6.15', '2.7.0', '9.9.9')) {
    $rejected = $false
    try { Get-FirmwareStorageContract -ImagePath $contractImage -Version $other | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Bridge marker signed as another version' }
}
foreach ($bad in @('', $bridgeMarker.Replace('CAPTURE=1', 'CAPTURE=0'),
    ($bridgeMarker + [char]0 + $bridgeMarker),
    ($bridgeMarker + [char]0 + 'ZONE_STORAGE_CONTRACT_V2:LEGACY'))) {
    [IO.File]::WriteAllText($contractImage, $bad + [char]0)
    $rejected = $false
    try { Get-FirmwareStorageContract -ImagePath $contractImage -Version $BridgeVersion | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Unqualified bridge storage marker accepted' }
}
$writerMarker = 'ZONE_STORAGE_CONTRACT_V4:WRITER:LEGACY=2:JOURNAL=1:READERS=3F:AUTHORITY=ADD:BRIDGE=2.6.17'
[IO.File]::WriteAllText($contractImage, $writerMarker + [char]0)
$writerContract = Get-FirmwareStorageContract -ImagePath $contractImage -Version '2.7.0'
if ($writerContract.schema_version -ne 4 -or $writerContract.delivery_authority -cne 'ADD' -or
    $writerContract.compatibility_version -cne '2.6.17' -or $writerContract.journal_reader_mask -ne 63 -or
    $writerContract.allowed_bootstrap_versions.Count -ne 1 -or
    $writerContract.allowed_bootstrap_images['2.6.17'] -cne 'f803361f8c3e1ed03f57814793942ebda45bbbd10cd0c1e77239602f5e6e32a1') {
    throw 'Writer accepted a different storage or rollback reader contract'
}
foreach ($bad in @($bridgeMarker, $writerMarker.Replace('ADD', 'LEGACY'),
    $writerMarker.Replace('2.6.17', '2.6.15'), ($writerMarker + [char]0 + $writerMarker),
    ($writerMarker + [char]0 + $bridgeMarker))) {
    [IO.File]::WriteAllText($contractImage, $bad + [char]0)
    $rejected = $false
    try { Get-FirmwareStorageContract -ImagePath $contractImage -Version '2.7.0' | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Unqualified writer marker accepted' }
}
foreach ($version in @('2.5.4', '2.6.0', '2.6.1', '2.6.2', '2.6.3', '2.6.4', '2.6.5', '2.6.6', '2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15')) {
    $mode = if ($version -eq '2.6.0') { 'SEGMENTED' } else { 'LEGACY' }
    $marker = if ($version -eq '2.6.15') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12,2.6.13,2.6.14'
    } elseif ($version -eq '2.6.14') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12,2.6.13'
    } elseif ($version -eq '2.6.13') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12'
    } elseif ($version -in @('2.6.11', '2.6.12')) {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10'
    } elseif ($version -eq '2.6.10') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9'
    } elseif ($version -eq '2.6.9') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8'
    } elseif ($version -eq '2.6.8') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7'
    } elseif ($version -eq '2.6.7') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6'
    } elseif ($version -in @('2.6.1', '2.6.2', '2.6.3', '2.6.4', '2.6.5', '2.6.6')) {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2'
    } else {
        "ZONE_STORAGE_CONTRACT_V1:${mode}:READ=2:LANES=3F:COMPAT=2.5.4"
    }
    [IO.File]::WriteAllText($contractImage, $marker + [char]0)
    $contract = Get-FirmwareStorageContract -ImagePath $contractImage -Version $version
    if ($contract.read_format -ne 2 -or $contract.reader_mask -ne 63) { throw 'Wrong reader contract' }
    if ($version -in @('2.6.1', '2.6.2', '2.6.3', '2.6.4', '2.6.5', '2.6.6', '2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15') -and
        ($contract.allowed_bootstrap_images['2.4.12'] -ne 'cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589' -or
         $contract.allowed_bootstrap_images['2.5.2'] -ne '4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b')) {
        throw 'Direct predecessor image identities changed'
    }
    if ($version -in @('2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15') -and
        ($contract.allowed_bootstrap_versions.Count -ne $(if ($version -eq '2.6.15') { 10 } elseif ($version -eq '2.6.14') { 9 } elseif ($version -eq '2.6.13') { 8 } elseif ($version -in @('2.6.11', '2.6.12')) { 7 } elseif ($version -eq '2.6.10') { 6 } elseif ($version -eq '2.6.9') { 5 } elseif ($version -eq '2.6.8') { 4 } else { 3 }) -or
         $contract.allowed_bootstrap_images['2.6.6'] -ne '69ec4cf34204d84d76933c30510ed78d46ec11d294f7257697af19047ce6869e')) {
        throw 'Signed 2.6.6 HIL predecessor identity changed'
    }
    if ($version -in @('2.6.8', '2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15') -and
        $contract.allowed_bootstrap_images['2.6.7'] -ne '3bed51d23d85fe50c03642e95f1d1d1e0b45960ccbf97d551645c0b268da1f1c') {
        throw 'Signed 2.6.7 HIL predecessor identity changed'
    }
    if ($version -in @('2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15') -and
        $contract.allowed_bootstrap_images['2.6.8'] -ne 'fecc5df0223a3c7c8b019a445bcf829bc8d09dd93e920aecc8446908fadeadc6') {
        throw 'Signed 2.6.8 HIL predecessor identity changed'
    }
    if ($version -in @('2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15') -and
        $contract.allowed_bootstrap_images['2.6.9'] -ne 'ad71339fef6926b21a21a05c1e1c4e30a936e0df5be6160871c7283841ad91b8') {
        throw 'Signed 2.6.9 HIL predecessor identity changed'
    }
    if ($version -in @('2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15') -and
        $contract.allowed_bootstrap_images['2.6.10'] -ne 'a95370b1487d9c1454dfb932c41432451f24ef69d0abc1292a5c0262c17c243c') {
        throw 'Signed 2.6.10 HIL predecessor identity changed'
    }
    if ($version -in @('2.6.13', '2.6.14', '2.6.15') -and
        $contract.allowed_bootstrap_images['2.6.12'] -ne '3f9028126c8dde9816486783a27a9802ed41179cd9f8caf9935deb79a0057fc1') {
        throw 'Signed 2.6.12 HIL predecessor identity changed'
    }
    if ($version -in @('2.6.14', '2.6.15') -and
        $contract.allowed_bootstrap_images['2.6.13'] -ne 'c7be4171153333563f9ed6e43f91376d42dfb9d3ca2faefda2ca6d1ac87b2def') {
        throw 'Signed 2.6.13 HIL predecessor identity changed'
    }
    if ($version -eq '2.6.15' -and
        $contract.allowed_bootstrap_images['2.6.14'] -ne '7d0381fc68a01b34ab6989dfed2b5a1a93f39c293bfd0b7db1734b0871fa6469') {
        throw 'Signed 2.6.14 HIL predecessor identity changed'
    }
    $other = if ($version -eq '2.6.0') { '2.5.4' } else { '2.6.0' }
    $rejected = $false
    try { Get-FirmwareStorageContract -ImagePath $contractImage -Version $other | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Wrong writer mode accepted' }
    [IO.File]::WriteAllText($contractImage, $marker + [char]0 + $marker)
    $rejected = $false
    try { Get-FirmwareStorageContract -ImagePath $contractImage -Version $version | Out-Null } catch { $rejected = $true }
    if (-not $rejected) { throw 'Ambiguous contract accepted' }
}
[IO.File]::WriteAllText($contractImage, 'old-application')
if ($null -ne (Get-FirmwareStorageContract -ImagePath $contractImage -Version '2.5.3')) { throw 'Old signing changed' }
$rejected = $false
try { Get-FirmwareStorageContract -ImagePath $contractImage -Version '2.6.0' | Out-Null } catch { $rejected = $true }
if (-not $rejected) { throw 'Missing contract accepted' }
$image = Join-Path $source 'zone-lite-2.6.0.bin'
[IO.File]::WriteAllText($image, 'fixture, not deployable firmware')
$manifest = @{version='2.6.0';image_name='zone-lite-2.6.0.bin';image_sha256=(Get-FileHash $image).Hash.ToLowerInvariant();image_size=(Get-Item $image).Length;git_sha=('a'*40);application_sha256=('c'*64)}
Write-TestManifest
[IO.File]::WriteAllText((Join-Path $source 'manifest.sig'), 'test-signature')
[IO.File]::WriteAllText((Join-Path $source 'SHA256SUMS'), 'test')
$targets='[{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"},{"connector_id":"second","mac":"a4:cb:8f:d4:66:02","terminal_serial":"SERIAL2"}]'
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.0 -PublicationMode HIL_ONLY -HilTargetsJson $targets
$marker=Get-Content (Join-Path $store "2.6.0/.hil-only.json") -Raw | ConvertFrom-Json
if($marker.schema_version -ne 2 -or $marker.targets.Count -ne 2 -or $marker.targets[0].connector_id -cne 'first' -or $marker.application_sha256 -cne ('c'*64)) { throw 'Incorrect ordered marker' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.0 -PublicationMode HIL_ONLY -HilTargetsJson $targets
$bad=@(
    '[{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"},{"connector_id":"first","mac":"a4:cb:8f:d4:66:02","terminal_serial":"SERIAL2"}]',
    '[{"connector_id":"first","mac":"*","terminal_serial":"SERIAL1"}]',
    '[{"connector_id":"second","mac":"a4:cb:8f:d4:66:02","terminal_serial":"SERIAL2"},{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"}]'
)
foreach($scope in $bad) {
    $rejected=$false
    try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.0 -PublicationMode HIL_ONLY -HilTargetsJson $scope } catch { $rejected=$true }
    if(-not $rejected) { throw 'Invalid or changed scope accepted' }
}
$extend = Join-Path $repo 'deploy/add/extend-ordered-hil-scope.ps1'
$extended = '[{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"},{"connector_id":"second","mac":"a4:cb:8f:d4:66:02","terminal_serial":"SERIAL2"},{"connector_id":"third","mac":"a4:cb:8f:d4:66:03","terminal_serial":"SERIAL3"}]'
$extensionArgs = @{
    StoreDirectory = $store
    Version = '2.6.0'
    ExpectedGitSha = ('a'*40)
    ExpectedImageSha256 = [string]$manifest.image_sha256
    ExpectedApplicationSha256 = ('c'*64)
    ExistingTargetsJson = $targets
    ExtendedTargetsJson = $extended
}
& $extend @extensionArgs -PreviewOnly
if ((Get-Content (Join-Path $store '2.6.0/.hil-only.json') -Raw | ConvertFrom-Json).targets.Count -ne 2) { throw 'Preview mutated HIL scope' }
$imageBefore = (Get-FileHash (Join-Path $store '2.6.0/zone-lite-2.6.0.bin')).Hash
$signatureBefore = (Get-FileHash (Join-Path $store '2.6.0/manifest.sig')).Hash
& $extend @extensionArgs
& $extend @extensionArgs
$expanded = Get-Content (Join-Path $store '2.6.0/.hil-only.json') -Raw | ConvertFrom-Json
if ($expanded.targets.Count -ne 3 -or $expanded.targets[2].connector_id -cne 'third') { throw 'HIL scope did not extend in order' }
if ((Get-FileHash (Join-Path $store '2.6.0/zone-lite-2.6.0.bin')).Hash -cne $imageBefore -or
    (Get-FileHash (Join-Path $store '2.6.0/manifest.sig')).Hash -cne $signatureBefore) { throw 'Signed release bytes changed' }
if (@(Get-ChildItem -LiteralPath (Join-Path $store '2.6.0') -Filter '.hil-scope-before-*.json' -Force).Count -ne 1) { throw 'Missing or duplicate HIL scope backup' }
$extensionArgs.ExtendedTargetsJson = '[{"connector_id":"second","mac":"a4:cb:8f:d4:66:02","terminal_serial":"SERIAL2"},{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"},{"connector_id":"third","mac":"a4:cb:8f:d4:66:03","terminal_serial":"SERIAL3"}]'
$rejected = $false
try { & $extend @extensionArgs } catch { $rejected = $true }
if (-not $rejected) { throw 'Reordered HIL scope extension accepted' }
& $publish -SourceDirectory $source -StoreDirectory $legacyStore -Version 2.6.0 -PublicationMode HIL_ONLY -HilTargetMac 'ac:27:6e:a3:07:f8'
$legacy=Get-Content (Join-Path $legacyStore "2.6.0/.hil-only.json") -Raw | ConvertFrom-Json
if($legacy.schema_version -ne 1 -or $legacy.target_mac -cne 'ac:27:6e:a3:07:f8') { throw 'Legacy HIL broken' }
# ZKT 2.6.1 may be published only to the five reviewed, ordered HIL targets.
. (Join-Path $repo 'deploy/add/firmware-2-6-1-hil-scope.ps1')
$exact261 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.1.json') -Raw
Assert-Zkt261HilScope -HilTargetsJson $exact261
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt261HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.1 HIL scope accepted' }
}
$zkt261Image = Join-Path $source 'zone-lite-2.6.1.bin'
[IO.File]::WriteAllText($zkt261Image, 'ZKT 2.6.1 fixture, not deployable firmware')
$manifest = @{version='2.6.1';firmware_family='zkt';project_name='zone_lite';release_id='zone-lite-2.6.1';image_name='zone-lite-2.6.1.bin';image_sha256=(Get-FileHash $zkt261Image).Hash.ToLowerInvariant();image_size=(Get-Item $zkt261Image).Length;git_sha=('a'*40);application_sha256=('e'*64)}
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.1 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.1 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.1 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.1 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.1 -PublicationMode HIL_ONLY -HilTargetsJson $exact261
$zkt261Marker = Get-Content -LiteralPath (Join-Path $store '2.6.1/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt261Marker.targets.Count -ne 5 -or $zkt261Marker.application_sha256 -cne ('e'*64)) { throw '2.6.1 HIL marker is incomplete' }
# The corrected direct build keeps the same five exact terminals in order.
. (Join-Path $repo 'deploy/add/firmware-2-6-2-hil-scope.ps1')
$exact262 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.2.json') -Raw
Assert-Zkt262HilScope -HilTargetsJson $exact262
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt262HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.2 HIL scope accepted' }
}
$zkt262Image = Join-Path $source 'zone-lite-2.6.2.bin'
[IO.File]::WriteAllText($zkt262Image, 'ZKT 2.6.2 fixture, not deployable firmware')
$manifest.version='2.6.2'
$manifest.release_id='zone-lite-2.6.2'
$manifest.image_name='zone-lite-2.6.2.bin'
$manifest.image_sha256=(Get-FileHash $zkt262Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt262Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.2 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.2 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.2 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.2 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.2 -PublicationMode HIL_ONLY -HilTargetsJson $exact262
$zkt262Marker = Get-Content -LiteralPath (Join-Path $store '2.6.2/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt262Marker.targets.Count -ne 5 -or $zkt262Marker.application_sha256 -cne ('e'*64)) { throw '2.6.2 HIL marker is incomplete' }
# The cached-digest direct build keeps the same five exact terminals in order.
. (Join-Path $repo 'deploy/add/firmware-2-6-3-hil-scope.ps1')
$exact263 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.3.json') -Raw
Assert-Zkt263HilScope -HilTargetsJson $exact263
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt263HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.3 HIL scope accepted' }
}
$zkt263Image = Join-Path $source 'zone-lite-2.6.3.bin'
[IO.File]::WriteAllText($zkt263Image, 'ZKT 2.6.3 fixture, not deployable firmware')
$manifest.version='2.6.3'
$manifest.release_id='zone-lite-2.6.3'
$manifest.image_name='zone-lite-2.6.3.bin'
$manifest.image_sha256=(Get-FileHash $zkt263Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt263Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.3 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.3 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.3 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.3 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.3 -PublicationMode HIL_ONLY -HilTargetsJson $exact263
$zkt263Marker = Get-Content -LiteralPath (Join-Path $store '2.6.3/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt263Marker.targets.Count -ne 5 -or $zkt263Marker.application_sha256 -cne ('e'*64)) { throw '2.6.3 HIL marker is incomplete' }
# The reserved-worker direct build keeps the same five exact terminals in order.
. (Join-Path $repo 'deploy/add/firmware-2-6-4-hil-scope.ps1')
$exact264 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.4.json') -Raw
Assert-Zkt264HilScope -HilTargetsJson $exact264
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt264HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.4 HIL scope accepted' }
}
$zkt264Image = Join-Path $source 'zone-lite-2.6.4.bin'
[IO.File]::WriteAllText($zkt264Image, 'ZKT 2.6.4 fixture, not deployable firmware')
$manifest.version='2.6.4'
$manifest.release_id='zone-lite-2.6.4'
$manifest.image_name='zone-lite-2.6.4.bin'
$manifest.image_sha256=(Get-FileHash $zkt264Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt264Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.4 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.4 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.4 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.4 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.4 -PublicationMode HIL_ONLY -HilTargetsJson $exact264
$zkt264Marker = Get-Content -LiteralPath (Join-Path $store '2.6.4/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt264Marker.targets.Count -ne 5 -or $zkt264Marker.application_sha256 -cne ('e'*64)) { throw '2.6.4 HIL marker is incomplete' }
# The OTA confirmation direct build keeps the same five exact terminals in order.
. (Join-Path $repo 'deploy/add/firmware-2-6-5-hil-scope.ps1')
$exact265 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.5.json') -Raw
Assert-Zkt265HilScope -HilTargetsJson $exact265
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt265HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.5 HIL scope accepted' }
}
$zkt265Image = Join-Path $source 'zone-lite-2.6.5.bin'
[IO.File]::WriteAllText($zkt265Image, 'ZKT 2.6.5 fixture, not deployable firmware')
$manifest.version='2.6.5'
$manifest.release_id='zone-lite-2.6.5'
$manifest.image_name='zone-lite-2.6.5.bin'
$manifest.image_sha256=(Get-FileHash $zkt265Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt265Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.5 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.5 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.5 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.5 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.5 -PublicationMode HIL_ONLY -HilTargetsJson $exact265
$zkt265Marker = Get-Content -LiteralPath (Join-Path $store '2.6.5/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt265Marker.targets.Count -ne 5 -or $zkt265Marker.application_sha256 -cne ('e'*64)) { throw '2.6.5 HIL marker is incomplete' }
# The TLS memory direct build keeps the same five exact terminals in order.
. (Join-Path $repo 'deploy/add/firmware-2-6-6-hil-scope.ps1')
$exact266 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.6.json') -Raw
Assert-Zkt266HilScope -HilTargetsJson $exact266
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt266HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.6 HIL scope accepted' }
}
$zkt266Image = Join-Path $source 'zone-lite-2.6.6.bin'
[IO.File]::WriteAllText($zkt266Image, 'ZKT 2.6.6 fixture, not deployable firmware')
$manifest.version='2.6.6'
$manifest.release_id='zone-lite-2.6.6'
$manifest.image_name='zone-lite-2.6.6.bin'
$manifest.image_sha256=(Get-FileHash $zkt266Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt266Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.6 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.6 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.6 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.6 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.6 -PublicationMode HIL_ONLY -HilTargetsJson $exact266
$zkt266Marker = Get-Content -LiteralPath (Join-Path $store '2.6.6/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt266Marker.targets.Count -ne 5 -or $zkt266Marker.application_sha256 -cne ('e'*64)) { throw '2.6.6 HIL marker is incomplete' }
# Storage fault source diagnostics and the fail-closed boot gate retain the same five exact terminals.
. (Join-Path $repo 'deploy/add/firmware-2-6-7-hil-scope.ps1')
$exact267 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.7.json') -Raw
Assert-Zkt267HilScope -HilTargetsJson $exact267
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt267HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.7 HIL scope accepted' }
}
$zkt267Image = Join-Path $source 'zone-lite-2.6.7.bin'
[IO.File]::WriteAllText($zkt267Image, 'ZKT 2.6.7 fixture, not deployable firmware')
$manifest.version='2.6.7'
$manifest.release_id='zone-lite-2.6.7'
$manifest.image_name='zone-lite-2.6.7.bin'
$manifest.image_sha256=(Get-FileHash $zkt267Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt267Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.7 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.7 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.7 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.7 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.7 -PublicationMode HIL_ONLY -HilTargetsJson $exact267
$zkt267Marker = Get-Content -LiteralPath (Join-Path $store '2.6.7/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt267Marker.targets.Count -ne 5 -or $zkt267Marker.application_sha256 -cne ('e'*64)) { throw '2.6.7 HIL marker is incomplete' }
# Catalog admission diagnostics and the fail-closed boot gate retain the same five exact terminals.
. (Join-Path $repo 'deploy/add/firmware-2-6-8-hil-scope.ps1')
$exact268 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.8.json') -Raw
Assert-Zkt268HilScope -HilTargetsJson $exact268
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt268HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.8 HIL scope accepted' }
}
$zkt268Image = Join-Path $source 'zone-lite-2.6.8.bin'
[IO.File]::WriteAllText($zkt268Image, 'ZKT 2.6.8 fixture, not deployable firmware')
$manifest.version='2.6.8'
$manifest.release_id='zone-lite-2.6.8'
$manifest.image_name='zone-lite-2.6.8.bin'
$manifest.image_sha256=(Get-FileHash $zkt268Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt268Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.8 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.8 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.8 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.8 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.8 -PublicationMode HIL_ONLY -HilTargetsJson $exact268
$zkt268Marker = Get-Content -LiteralPath (Join-Path $store '2.6.8/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt268Marker.targets.Count -ne 5 -or $zkt268Marker.application_sha256 -cne ('e'*64)) { throw '2.6.8 HIL marker is incomplete' }
# Queue read contention remains retryable while the same five exact terminals stay quarantined.
. (Join-Path $repo 'deploy/add/firmware-2-6-9-hil-scope.ps1')
$exact269 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.9.json') -Raw
Assert-Zkt269HilScope -HilTargetsJson $exact269
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt269HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.9 HIL scope accepted' }
}
$zkt269Image = Join-Path $source 'zone-lite-2.6.9.bin'
[IO.File]::WriteAllText($zkt269Image, 'ZKT 2.6.9 fixture, not deployable firmware')
$manifest.version='2.6.9'
$manifest.release_id='zone-lite-2.6.9'
$manifest.image_name='zone-lite-2.6.9.bin'
$manifest.image_sha256=(Get-FileHash $zkt269Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt269Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.9 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.9 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.9 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.9 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.9 -PublicationMode HIL_ONLY -HilTargetsJson $exact269
$zkt269Marker = Get-Content -LiteralPath (Join-Path $store '2.6.9/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt269Marker.targets.Count -ne 5 -or $zkt269Marker.application_sha256 -cne ('e'*64)) { throw '2.6.9 HIL marker is incomplete' }
# Capacity-pressure patch keeps the exact five terminals quarantined.
. (Join-Path $repo 'deploy/add/firmware-2-6-10-hil-scope.ps1')
$exact2610 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.10.json') -Raw
Assert-Zkt2610HilScope -HilTargetsJson $exact2610
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt2610HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.10 HIL scope accepted' }
}
$zkt2610Image = Join-Path $source 'zone-lite-2.6.10.bin'
[IO.File]::WriteAllText($zkt2610Image, 'ZKT 2.6.10 fixture, not deployable firmware')
$manifest.version='2.6.10'
$manifest.release_id='zone-lite-2.6.10'
$manifest.image_name='zone-lite-2.6.10.bin'
$manifest.image_sha256=(Get-FileHash $zkt2610Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt2610Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.10 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.10 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.10 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.10 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.10 -PublicationMode HIL_ONLY -HilTargetsJson $exact2610
$zkt2610Marker = Get-Content -LiteralPath (Join-Path $store '2.6.10/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt2610Marker.targets.Count -ne 5 -or $zkt2610Marker.application_sha256 -cne ('e'*64)) { throw '2.6.10 HIL marker is incomplete' }
# Persistence diagnostic patch keeps the exact five terminals quarantined.
. (Join-Path $repo 'deploy/add/firmware-2-6-11-hil-scope.ps1')
$exact2611 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.11.json') -Raw
Assert-Zkt2611HilScope -HilTargetsJson $exact2611
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt2611HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.11 HIL scope accepted' }
}
$zkt2611Image = Join-Path $source 'zone-lite-2.6.11.bin'
[IO.File]::WriteAllText($zkt2611Image, 'ZKT 2.6.11 fixture, not deployable firmware')
$manifest.version='2.6.11'
$manifest.release_id='zone-lite-2.6.11'
$manifest.image_name='zone-lite-2.6.11.bin'
$manifest.image_sha256=(Get-FileHash $zkt2611Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt2611Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.11 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.11 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.11 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.11 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.11 -PublicationMode HIL_ONLY -HilTargetsJson $exact2611
$zkt2611Marker = Get-Content -LiteralPath (Join-Path $store '2.6.11/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt2611Marker.targets.Count -ne 5 -or $zkt2611Marker.application_sha256 -cne ('e'*64)) { throw '2.6.11 HIL marker is incomplete' }
# Canonical signing patch keeps the exact five terminals quarantined.
. (Join-Path $repo 'deploy/add/firmware-2-6-12-hil-scope.ps1')
$exact2612 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.12.json') -Raw
Assert-Zkt2612HilScope -HilTargetsJson $exact2612
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt2612HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.12 HIL scope accepted' }
}
$zkt2612Image = Join-Path $source 'zone-lite-2.6.12.bin'
[IO.File]::WriteAllText($zkt2612Image, 'ZKT 2.6.12 fixture, not deployable firmware')
$manifest.version='2.6.12'
$manifest.release_id='zone-lite-2.6.12'
$manifest.image_name='zone-lite-2.6.12.bin'
$manifest.image_sha256=(Get-FileHash $zkt2612Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt2612Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.12 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.12 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.12 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.12 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.12 -PublicationMode HIL_ONLY -HilTargetsJson $exact2612
$zkt2612Marker = Get-Content -LiteralPath (Join-Path $store '2.6.12/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt2612Marker.targets.Count -ne 5 -or $zkt2612Marker.application_sha256 -cne ('e'*64)) { throw '2.6.12 HIL marker is incomplete' }
# Live-frame patch remains scoped to the same five exact HIL devices.
. (Join-Path $repo 'deploy/add/firmware-2-6-13-hil-scope.ps1')
$exact2613 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.13.json') -Raw
Assert-Zkt2613HilScope -HilTargetsJson $exact2613
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt2613HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.13 HIL scope accepted' }
}
$zkt2613Image = Join-Path $source 'zone-lite-2.6.13.bin'
[IO.File]::WriteAllText($zkt2613Image, 'ZKT 2.6.13 fixture, not deployable firmware')
$manifest.version='2.6.13'
$manifest.release_id='zone-lite-2.6.13'
$manifest.image_name='zone-lite-2.6.13.bin'
$manifest.image_sha256=(Get-FileHash $zkt2613Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt2613Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.13 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.13 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.13 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.13 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.13 -PublicationMode HIL_ONLY -HilTargetsJson $exact2613
$zkt2613Marker = Get-Content -LiteralPath (Join-Path $store '2.6.13/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt2613Marker.targets.Count -ne 5 -or $zkt2613Marker.application_sha256 -cne ('e'*64)) { throw '2.6.13 HIL marker is incomplete' }
# Delivery-fault patch remains scoped to the same five exact HIL devices.
. (Join-Path $repo 'deploy/add/firmware-2-6-14-hil-scope.ps1')
$exact2614 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.14.json') -Raw
Assert-Zkt2614HilScope -HilTargetsJson $exact2614
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt2614HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.14 HIL scope accepted' }
}
$zkt2614Image = Join-Path $source 'zone-lite-2.6.14.bin'
[IO.File]::WriteAllText($zkt2614Image, 'ZKT 2.6.14 fixture, not deployable firmware')
$manifest.version='2.6.14'
$manifest.release_id='zone-lite-2.6.14'
$manifest.image_name='zone-lite-2.6.14.bin'
$manifest.image_sha256=(Get-FileHash $zkt2614Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt2614Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.14 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.14 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.14 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.14 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.14 -PublicationMode HIL_ONLY -HilTargetsJson $exact2614
$zkt2614Marker = Get-Content -LiteralPath (Join-Path $store '2.6.14/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt2614Marker.targets.Count -ne 5 -or $zkt2614Marker.application_sha256 -cne ('e'*64)) { throw '2.6.14 HIL marker is incomplete' }
# Lock-contention patch remains scoped to the same five exact HIL devices.
. (Join-Path $repo 'deploy/add/firmware-2-6-15-hil-scope.ps1')
$exact2615 = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.15.json') -Raw
Assert-Zkt2615HilScope -HilTargetsJson $exact2615
foreach ($scope in @('', '[]', '{', $targets)) {
    $rejected = $false
    try { Assert-Zkt2615HilScope -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected) { throw 'Invalid 2.6.15 HIL scope accepted' }
}
$zkt2615Image = Join-Path $source 'zone-lite-2.6.15.bin'
[IO.File]::WriteAllText($zkt2615Image, 'ZKT 2.6.15 fixture, not deployable firmware')
$manifest.version='2.6.15'
$manifest.release_id='zone-lite-2.6.15'
$manifest.image_name='zone-lite-2.6.15.bin'
$manifest.image_sha256=(Get-FileHash $zkt2615Image).Hash.ToLowerInvariant()
$manifest.image_size=(Get-Item $zkt2615Image).Length
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.15 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.15 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.15 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.15 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.15 -PublicationMode HIL_ONLY -HilTargetsJson $exact2615
$zkt2615Marker = Get-Content -LiteralPath (Join-Path $store '2.6.15/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt2615Marker.targets.Count -ne 5 -or $zkt2615Marker.application_sha256 -cne ('e'*64)) { throw '2.6.15 HIL marker is incomplete' }
# The later BLD5 extension changes only the quarantine marker of the exact
# already published image; it cannot authorize another image or device.
. (Join-Path $repo 'deploy/add/firmware-2-6-15-bld5-hil-scope.ps1')
$bld5Arguments = @{
    ExpectedGitSha = 'a88998346d5b1ce1ddf4d19e3963b7245e46633b'
    ExpectedImageSha256 = 'e2a2167fca307d73dbeb495bcc26baa535591794066b02a3de2848589c28887f'
    ExpectedApplicationSha256 = '832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6'
    ExistingTargetsJson = $exact2615
    ExtendedTargetsJson = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.15-bld5.json') -Raw
}
Assert-Zkt2615Bld5HilExtension @bld5Arguments
foreach ($field in @('ExpectedGitSha', 'ExpectedImageSha256', 'ExpectedApplicationSha256')) {
    $changed = $bld5Arguments.Clone()
    $changed[$field] = '0' * $changed[$field].Length
    $rejected = $false
    try { Assert-Zkt2615Bld5HilExtension @changed } catch { $rejected = $true }
    if (-not $rejected) { throw "BLD5 extension accepted changed $field" }
}
foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
    $changed = $bld5Arguments.Clone()
    $changedTargets = ConvertFrom-Json -InputObject $changed.ExtendedTargetsJson
    $changedTargets = @($changedTargets)
    $changedTargets[5].$field = 'replacement'
    $changed.ExtendedTargetsJson = ConvertTo-Json -InputObject $changedTargets -Depth 5 -Compress
    $rejected = $false
    try { Assert-Zkt2615Bld5HilExtension @changed } catch { $rejected = $true }
    if (-not $rejected) { throw "BLD5 extension accepted changed $field" }
}
# City extension retains the published six and appends only the reviewed eight.
. (Join-Path $repo 'deploy/add/firmware-2-6-15-city-hil-scope.ps1')
$cityArguments = $bld5Arguments.Clone()
$cityArguments.ExistingTargetsJson = $bld5Arguments.ExtendedTargetsJson
$cityArguments.ExtendedTargetsJson = Get-Content -LiteralPath (Join-Path $repo 'deploy/add/hil-targets-2.6.15-cities.json') -Raw
Assert-Zkt2615CityHilExtension @cityArguments
foreach ($field in @('ExpectedGitSha', 'ExpectedImageSha256', 'ExpectedApplicationSha256')) {
    $changed = $cityArguments.Clone()
    $changed[$field] = '0' * $changed[$field].Length
    $rejected = $false
    try { Assert-Zkt2615CityHilExtension @changed } catch { $rejected = $true }
    if (-not $rejected) { throw "City extension accepted changed $field" }
}
foreach ($mutation in @('prefix', 'connector', 'mac', 'serial', 'order', 'missing', 'extra', 'extra-key')) {
    $changed = $cityArguments.Clone()
    $changedTargets = ConvertFrom-Json -InputObject $changed.ExtendedTargetsJson
    $changedTargets = @($changedTargets)
    switch ($mutation) {
        'prefix' { $changedTargets[5].terminal_serial = 'replacement' }
        'connector' { $changedTargets[6].connector_id = 'replacement' }
        'mac' { $changedTargets[6].mac = '00:11:22:33:44:55' }
        'serial' { $changedTargets[6].terminal_serial = 'replacement' }
        'order' { $saved = $changedTargets[6]; $changedTargets[6] = $changedTargets[7]; $changedTargets[7] = $saved }
        'missing' { $changedTargets = @($changedTargets[0..12]) }
        'extra' { $changedTargets = @($changedTargets) + @($changedTargets[13]) }
        'extra-key' { $changedTargets[6] | Add-Member -MemberType NoteProperty -Name display_name -Value 'Faisalabad' }
    }
    $changed.ExtendedTargetsJson = ConvertTo-Json -InputObject $changedTargets -Depth 5 -Compress
    $rejected = $false
    try { Assert-Zkt2615CityHilExtension @changed } catch { $rejected = $true }
    if (-not $rejected) { throw "City extension accepted $mutation" }
}
# Family-labelled Hikvision bytes use a separate immutable package identity.
$hikImage = Join-Path $source 'zone-lite-hikvision-3.1.0.bin'
[IO.File]::WriteAllText($hikImage, 'Hikvision fixture, not deployable firmware')
$manifest = @{version='3.1.0';firmware_family='hikvision';project_name='zone_lite_hikvision';release_id='zone-lite-hikvision-3.1.0';image_name='zone-lite-hikvision-3.1.0.bin';image_sha256=(Get-FileHash $hikImage).Hash.ToLowerInvariant();image_size=(Get-Item $hikImage).Length;git_sha=('b'*40);application_sha256=('d'*64)}
Write-TestManifest
& $publish -SourceDirectory $source -StoreDirectory $store -Version 3.1.0 -PublicationMode HIL_ONLY -HilTargetMac 'ac:27:6e:a4:e9:74'
if (-not (Test-Path (Join-Path $store '3.1.0/zone-lite-hikvision-3.1.0.bin'))) { throw 'Hikvision labelled image missing' }
$manifest.image_name='zone-lite-3.1.0.bin'
Write-TestManifest
$rejected=$false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 3.1.0 -PublicationMode HIL_ONLY -HilTargetMac 'ac:27:6e:a4:e9:74' } catch { $rejected=$true }
if (-not $rejected) { throw 'Mislabelled Hikvision image accepted' }
# Experimental bridge publication requires connector, ESP and terminal identity;
# neither publication nor promotion may remove quarantine from these bytes.
$bridgeImage = Join-Path $source "zone-lite-$BridgeVersion.bin"
[IO.File]::WriteAllText($bridgeImage, 'Bridge fixture, not deployable firmware')
$manifest = @{version=$BridgeVersion;firmware_family='zkt';project_name='zone_lite';release_id="zone-lite-$BridgeVersion";image_name="zone-lite-$BridgeVersion.bin";image_sha256=(Get-FileHash $bridgeImage).Hash.ToLowerInvariant();image_size=(Get-Item $bridgeImage).Length;git_sha=('a'*40);application_sha256=('d'*64);release_channel='EXPERIMENTAL_HIL_ONLY';queue_storage=$bridgeContract}
. (Join-Path $repo 'deploy/add/journal-hil-scope.ps1')
$journalTargets = Get-JournalHilScope
$manifest.hil_targets = $journalTargets
$journalScope = ConvertTo-Json -InputObject @($journalTargets[0]) -Depth 5 -Compress
foreach ($count in 1..17) {
    Assert-JournalHilScope -TargetsJson (ConvertTo-Json -InputObject @($journalTargets[0..($count-1)]) -Depth 5 -Compress)
}
foreach ($bad in @('[]', $targets, (ConvertTo-Json -InputObject @($journalTargets[1], $journalTargets[0]) -Depth 5 -Compress))) {
    $rejected = $false
    try { Assert-JournalHilScope -TargetsJson $bad } catch { $rejected = $true }
    if (-not $rejected) { throw 'Journal scope accepted unreviewed target identities or order' }
}
Write-TestManifest
foreach ($mode in @('AVAILABLE', 'HIL_ONLY')) {
    $rejected = $false
    try {
        if ($mode -eq 'AVAILABLE') {
            & $publish -SourceDirectory $source -StoreDirectory $store -Version $BridgeVersion -PublicationMode $mode
        } else {
            & $publish -SourceDirectory $source -StoreDirectory $store -Version $BridgeVersion -PublicationMode $mode -HilTargetMac '00:11:22:33:44:55'
        }
    } catch { $rejected = $true }
    if (-not $rejected) { throw 'Experimental publication bypassed ordered HIL quarantine' }
}
if (Test-Path (Join-Path $store $BridgeVersion)) { throw 'Rejected bridge publication left a release' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version $BridgeVersion -PublicationMode HIL_ONLY -HilTargetsJson $journalScope
$bridgeScopePath = Join-Path $store "$BridgeVersion/.hil-only.json"
$bridgeScopeBefore = [IO.File]::ReadAllText($bridgeScopePath)
$bridgeOutput = Join-Path $root 'bridge-promotion-output'
$rejected = $false
try {
    & (Join-Path $repo 'deploy/add/promote-firmware.ps1') -StoreDirectory $store -Version $BridgeVersion -GitSha ('a'*40) -OutputDirectory $bridgeOutput
} catch { $rejected = $true }
if (-not $rejected -or (Test-Path $bridgeOutput) -or
    [IO.File]::ReadAllText($bridgeScopePath) -cne $bridgeScopeBefore) {
    throw 'Experimental promotion changed the quarantine or produced output'
}
$fullJournalScope = ConvertTo-Json -InputObject $journalTargets -Depth 5 -Compress
$bridgeArguments = @{
    StoreDirectory=$store; Version=$BridgeVersion; ExpectedGitSha=('a'*40)
    ExpectedImageSha256=[string]$manifest.image_sha256; ExpectedApplicationSha256=('d'*64)
    ExistingTargetsJson=$journalScope; ExtendedTargetsJson=$fullJournalScope
}
& $extend @bridgeArguments -PreviewOnly
if ([IO.File]::ReadAllText($bridgeScopePath) -cne $bridgeScopeBefore) { throw 'Journal preview changed quarantine' }
& $extend @bridgeArguments
$expandedBridge = Get-Content $bridgeScopePath -Raw | ConvertFrom-Json
if ($expandedBridge.targets.Count -ne 17) { throw 'Journal scope expansion lost a nationwide target' }
if ((Get-FileHash (Join-Path $store "$BridgeVersion/zone-lite-$BridgeVersion.bin")).Hash.ToLowerInvariant() -cne $manifest.image_sha256) {
    throw 'Journal expansion changed signed image bytes'
}
# The writer remains quarantined even if its experimental tag is removed.
$writerImage = Join-Path $source 'zone-lite-2.7.0.bin'
[IO.File]::WriteAllText($writerImage, 'Writer fixture, not deployable firmware')
$manifest = @{version='2.7.0';firmware_family='zkt';project_name='zone_lite';release_id='zone-lite-2.7.0';image_name='zone-lite-2.7.0.bin';image_sha256=(Get-FileHash $writerImage).Hash.ToLowerInvariant();image_size=(Get-Item $writerImage).Length;git_sha=('a'*40);application_sha256=('d'*64);queue_storage=$writerContract;runtime_profile='ZKT_JOURNAL_V1';minimum_bootstrap_version='2.6.17';hil_targets=$journalTargets}
foreach ($channel in @('', 'AVAILABLE', 'EXPERIMENTAL_HIL_ONLY')) {
    $manifest.release_channel = $channel
    Write-TestManifest
    $rejected = $false
    try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.7.0 -PublicationMode AVAILABLE } catch { $rejected = $true }
    if (-not $rejected) { throw 'Writer publication escaped quarantine' }
}
if (Test-Path (Join-Path $store '2.7.0')) { throw 'Rejected writer publication left release files' }
Write-TestManifest
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.7.0 -PublicationMode HIL_ONLY -HilTargetsJson $journalScope } catch { $rejected = $true }
if (-not $rejected) { throw 'Historical V4 writer was admitted for new publication' }
if (Test-Path (Join-Path $store '2.7.0')) { throw 'Rejected historical writer left release files' }
Write-Host 'Publication regression tests passed'

} finally {
    if (Test-Path $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
