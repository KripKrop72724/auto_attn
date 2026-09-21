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
foreach ($version in @('2.5.4', '2.6.0')) {
    $mode = if ($version -eq '2.6.0') { 'SEGMENTED' } else { 'LEGACY' }
    $marker = "ZONE_STORAGE_CONTRACT_V1:${mode}:READ=2:LANES=3F:COMPAT=2.5.4"
    [IO.File]::WriteAllText($contractImage, $marker + [char]0)
    $contract = Get-FirmwareStorageContract -ImagePath $contractImage -Version $version
    if ($contract.read_format -ne 2 -or $contract.reader_mask -ne 63) { throw 'Wrong reader contract' }
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
# Use a fresh store for invalid inputs so an immutable existing release cannot
# hide a parser that accepts a malformed target list.
$invalidTargets = @(
    '[]',
    '[null]',
    '[[{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"}]]',
    '{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"}',
    '[{"connector_id":"first","mac":"a4:cb:8f:d4:66:01"}]',
    '[{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1","extra":true}]',
    '[{"CONNECTOR_ID":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"}]',
    '[{"connector_id":["first"],"mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"}]',
    '[{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"},{"connector_id":"first","mac":"a4:cb:8f:d4:66:02","terminal_serial":"SERIAL2"}]'
)
foreach ($scope in $invalidTargets) {
    $rejected = $false
    try { & $publish -SourceDirectory $source -StoreDirectory $store -Version 2.6.0 -PublicationMode HIL_ONLY -HilTargetsJson $scope } catch { $rejected = $true }
    if (-not $rejected -or (Test-Path (Join-Path $store '2.6.0'))) { throw 'Invalid target scope was published' }
}
$singleTargets='[{"connector_id":"first","mac":"a4:cb:8f:d4:66:01","terminal_serial":"SERIAL1"}]'
$singleStore=Join-Path $root 'single-ordered-store'
& $publish -SourceDirectory $source -StoreDirectory $singleStore -Version 2.6.0 -PublicationMode HIL_ONLY -HilTargetsJson $singleTargets
$singleMarker=Get-Content (Join-Path $singleStore '2.6.0/.hil-only.json') -Raw | ConvertFrom-Json
if ($singleMarker.targets.Count -ne 1 -or $singleMarker.targets[0].connector_id -cne 'first') { throw 'Single ordered target changed shape' }
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
& $publish -SourceDirectory $source -StoreDirectory $legacyStore -Version 2.6.0 -PublicationMode HIL_ONLY -HilTargetMac 'ac:27:6e:a3:07:f8'
$legacy=Get-Content (Join-Path $legacyStore "2.6.0/.hil-only.json") -Raw | ConvertFrom-Json
if($legacy.schema_version -ne 1 -or $legacy.target_mac -cne 'ac:27:6e:a3:07:f8') { throw 'Legacy HIL broken' }
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
