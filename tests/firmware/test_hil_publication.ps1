$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('hil-publication-' + [guid]::NewGuid().ToString('N'))
$source = Join-Path $root 'package'
$store = Join-Path $root 'store'
$legacyStore = Join-Path $root 'legacy-store'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$publish = Join-Path $repo 'deploy/add/publish-firmware.ps1'
try {
New-Item -ItemType Directory -Path $source -Force | Out-Null
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
& $publish -SourceDirectory $source -StoreDirectory $legacyStore -Version 2.6.0 -PublicationMode HIL_ONLY -HilTargetMac 'ac:27:6e:a3:07:f8'
$legacy=Get-Content (Join-Path $legacyStore "2.6.0/.hil-only.json") -Raw | ConvertFrom-Json
if($legacy.schema_version -ne 1 -or $legacy.target_mac -cne 'ac:27:6e:a3:07:f8') { throw 'Legacy HIL broken' }
Write-Host 'Publication regression tests passed'

} finally {
    if (Test-Path $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
