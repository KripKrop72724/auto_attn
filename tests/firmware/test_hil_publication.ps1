$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('hil-publication-' + [guid]::NewGuid().ToString('N'))
$source = Join-Path $root 'package'
$store = Join-Path $root 'store'
$legacyStore = Join-Path $root 'legacy-store'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$publish = Join-Path $repo 'deploy/add/publish-firmware.ps1'
try {
New-Item -ItemType Directory -Path $source -Force | Out-Null
. (Join-Path $repo 'deploy/add/firmware-storage-contract.ps1')
$contractImage = Join-Path $root 'contract.bin'
foreach ($version in @('2.5.4', '2.6.0', '2.6.1', '2.6.2', '2.6.3', '2.6.4', '2.6.5', '2.6.6', '2.6.7', '2.6.8')) {
    $mode = if ($version -eq '2.6.0') { 'SEGMENTED' } else { 'LEGACY' }
    $marker = if ($version -eq '2.6.8') {
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
    if ($version -in @('2.6.1', '2.6.2', '2.6.3', '2.6.4', '2.6.5', '2.6.6', '2.6.7', '2.6.8') -and
        ($contract.allowed_bootstrap_images['2.4.12'] -ne 'cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589' -or
         $contract.allowed_bootstrap_images['2.5.2'] -ne '4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b')) {
        throw 'Direct predecessor image identities changed'
    }
    if ($version -in @('2.6.7', '2.6.8') -and
        ($contract.allowed_bootstrap_versions.Count -ne $(if ($version -eq '2.6.8') { 4 } else { 3 }) -or
         $contract.allowed_bootstrap_images['2.6.6'] -ne '69ec4cf34204d84d76933c30510ed78d46ec11d294f7257697af19047ce6869e')) {
        throw 'Signed 2.6.6 HIL predecessor identity changed'
    }
    if ($version -eq '2.6.8' -and
        $contract.allowed_bootstrap_images['2.6.7'] -ne '3bed51d23d85fe50c03642e95f1d1d1e0b45960ccbf97d551645c0b268da1f1c') {
        throw 'Signed 2.6.7 HIL predecessor identity changed'
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
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
. (Join-Path $repo 'deploy/add/firmware-2-6-7-hil-scope.ps1')
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
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.8 -PublicationMode AVAILABLE } catch { $rejected = $true }
if (-not $rejected) { throw 'Direct 2.6.8 production publication accepted' }
$rejected = $false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.8 -PublicationMode HIL_ONLY -HilTargetsJson $targets } catch { $rejected = $true }
if (-not $rejected) { throw 'Partial 2.6.8 HIL scope accepted' }
& $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.8 -PublicationMode HIL_ONLY -HilTargetsJson $exact268
$zkt268Marker = Get-Content -LiteralPath (Join-Path $store '2.6.8/.hil-only.json') -Raw | ConvertFrom-Json
if ($zkt268Marker.targets.Count -ne 5 -or $zkt268Marker.application_sha256 -cne ('e'*64)) { throw '2.6.8 HIL marker is incomplete' }
# Family-labelled Hikvision bytes use a separate immutable package identity.
$hikImage = Join-Path $source 'zone-lite-hikvision-3.1.0.bin'
[IO.File]::WriteAllText($hikImage, 'Hikvision fixture, not deployable firmware')
$manifest = @{version='3.1.0';firmware_family='hikvision';project_name='zone_lite_hikvision';release_id='zone-lite-hikvision-3.1.0';image_name='zone-lite-hikvision-3.1.0.bin';image_sha256=(Get-FileHash $hikImage).Hash.ToLowerInvariant();image_size=(Get-Item $hikImage).Length;git_sha=('b'*40);application_sha256=('d'*64)}
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
& $publish -SourceDirectory $source -StoreDirectory $store -Version 3.1.0 -PublicationMode HIL_ONLY -HilTargetMac 'ac:27:6e:a4:e9:74'
if (-not (Test-Path (Join-Path $store '3.1.0/zone-lite-hikvision-3.1.0.bin'))) { throw 'Hikvision labelled image missing' }
$manifest.image_name='zone-lite-3.1.0.bin'
[IO.File]::WriteAllText((Join-Path $source 'manifest.json'), ($manifest | ConvertTo-Json))
$rejected=$false
try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 3.1.0 -PublicationMode HIL_ONLY -HilTargetMac 'ac:27:6e:a4:e9:74' } catch { $rejected=$true }
if (-not $rejected) { throw 'Mislabelled Hikvision image accepted' }
Write-Host 'Publication regression tests passed'

} finally {
    if (Test-Path $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
