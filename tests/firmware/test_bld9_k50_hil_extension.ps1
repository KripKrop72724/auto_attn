$ErrorActionPreference = 'Stop'
# Exercises the ZONE-BLD9-01 K50 preview/apply sequence and the real ordered
# extension writer against synthetic stores. Synthetic bytes cannot carry the
# real signed 2.7.4 digests, so a copy of the scope file pins these instead;
# test_hil_publication.ps1 checks the real pins. The deployed ADD is a stub that
# behaves like its catalog: the 17 stay current until a catalog read refreshes.
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$root = Join-Path ([IO.Path]::GetTempPath()) ('bld9-k50-extension-' + [guid]::NewGuid().ToString('N'))
$deploy = Join-Path $root 'deploy/add'
$realGit = '87e14fe6e9756cd65a27e64ac1a7420fe35aa816'
$realImage = '9a3e2226b7abff11cc264472356c186d3b9cce87a0df059aeb0a9965f1f095c5'
$realApplication = '3e4bb613ab046df5ad29e4a1ab37fd801e1778e6b0d2c41c6604fe75bbea2fac'
$global:k50Checks = New-Object System.Collections.ArrayList
$global:k50Case = ''
function New-K50Store([string]$Case, [string]$Mode) {
    $store = Join-Path $root ('store-' + $Case + '-' + $Mode)
    $release = Join-Path $store '2.7.4'
    New-Item -ItemType Directory -Path $release -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $release 'zone-lite-2.7.4.bin'), 'Synthetic 2.7.4 test image; not firmware.')
    [IO.File]::WriteAllText((Join-Path $release 'manifest.sig'), 'synthetic-test-signature')
    $releaseId = if ($Case -eq 'wrong-release-id') { 'zone-lite-2.7.4-other' } else { 'zone-lite-2.7.4' }
    $manifest = [ordered]@{application_sha256=$script:application;firmware_family='zkt';git_sha=$script:git;image_name='zone-lite-2.7.4.bin';image_sha256=$script:image;image_size=(Get-Item (Join-Path $release 'zone-lite-2.7.4.bin')).Length;release_id=$releaseId;version='2.7.4'}
    [IO.File]::WriteAllText((Join-Path $release 'manifest.json'), ($manifest | ConvertTo-Json -Compress))
    $targets = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText((Join-Path $deploy 'hil-targets-zkt-270.json')))
    $targets = @($targets)
    if ($Case -eq 'partial-scope') { $targets = @($targets[0..15]) }
    $marker = [ordered]@{schema_version=2;version='2.7.4';git_sha=$script:git;image_sha256=$script:image;application_sha256=$script:application;targets=$targets}
    if ($Case -eq 'revoked-marker') { $marker.revoked_at = '2026-10-19T00:00:00Z' }
    [IO.File]::WriteAllText((Join-Path $release '.hil-only.json'), (($marker | ConvertTo-Json -Depth 6 -Compress) + "`n"))
    if ($Case -eq 'revoked') { [IO.File]::WriteAllText((Join-Path $release '.revoked.json'), '{}') }
    if ($Case -eq 'wrong-image') { [IO.File]::AppendAllText((Join-Path $release 'zone-lite-2.7.4.bin'), ' changed') }
    return $store
}
function Get-Hashes([string]$Release) {
    $hashes = @{}
    foreach ($name in @('manifest.json', 'manifest.sig', 'zone-lite-2.7.4.bin', '.hil-only.json')) {
        $hashes[$name] = (Get-FileHash -LiteralPath (Join-Path $Release $name)).Hash
    }
    return $hashes
}
try {
    New-Item -ItemType Directory -Path $deploy -Force | Out-Null
    foreach ($file in @('extend-ordered-hil-scope.ps1', 'journal-hil-scope.ps1', 'hil-targets-zkt-270.json', 'hil-targets-2.7.4-bld9-k50.json')) {
        Copy-Item -LiteralPath (Join-Path $repo "deploy/add/$file") -Destination (Join-Path $deploy $file)
    }
    $imageProbe = Join-Path $root 'probe.bin'
    [IO.File]::WriteAllText($imageProbe, 'Synthetic 2.7.4 test image; not firmware.')
    $script:git = 'b' * 40
    $script:application = 'c' * 64
    $script:image = (Get-FileHash -LiteralPath $imageProbe).Hash.ToLowerInvariant()
    $scope = [IO.File]::ReadAllText((Join-Path $repo 'deploy/add/firmware-2-7-4-bld9-k50-hil-scope.ps1'))
    foreach ($pin in @($realGit, $realImage, $realApplication)) {
        if (([regex]::Matches($scope, $pin)).Count -ne 1) { throw 'The K50 scope file no longer pins the 2.7.4 identity once' }
    }
    $scope = $scope.Replace($realGit, $script:git).Replace($realImage, $script:image).Replace($realApplication, $script:application)
    [IO.File]::WriteAllText((Join-Path $deploy 'firmware-2-7-4-bld9-k50-hil-scope.ps1'), $scope)
    [IO.File]::WriteAllText((Join-Path $deploy 'check-deployed-firmware-contract.ps1'), @'
param([string]$SourceDirectory, [switch]$RequirePublishedHilRelease, [ValidateRange(0,17)][int]$AllowPreviousPrefixCount = 0)
if (-not $RequirePublishedHilRelease) { throw 'The published-state check is required' }
foreach ($name in @('manifest.json', 'manifest.sig')) {
    if ((Get-FileHash -LiteralPath (Join-Path $SourceDirectory $name)).Hash -cne $global:k50Package[$name]) { throw 'The checked signed metadata differs' }
}
$marker = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText((Join-Path $SourceDirectory '.hil-only.json')))
$count = @($marker.targets).Count
[void]$global:k50Checks.Add("$count/$AllowPreviousPrefixCount")
if ($global:k50Case -eq 'deployed-refuses' -and $count -eq 18) { throw 'ADD_FIRMWARE_CONTRACT_REJECTED' }
if ($global:k50Case -eq 'catalog-stale' -and -not $AllowPreviousPrefixCount) { Write-Output 'CATALOG_REFRESH_PENDING'; return }
if ($count -eq 17 -and -not $AllowPreviousPrefixCount) { Write-Output 'CATALOG_CURRENT'; return }
if ($count -eq 18 -and $AllowPreviousPrefixCount -eq 17) { Write-Output 'CATALOG_REFRESH_PENDING'; return }
throw 'Synthetic ADD refused this published state'
'@)
    . (Join-Path $deploy 'firmware-2-7-4-bld9-k50-hil-scope.ps1')
    $extended = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText((Join-Path $deploy 'hil-targets-2.7.4-bld9-k50.json')))
    $extended = @($extended)
    $cases = @('preview', 'apply', 'deployed-refuses', 'catalog-stale', 'revoked', 'revoked-marker', 'wrong-release-id', 'wrong-image', 'partial-scope')
    $refusals = @{
        'deployed-refuses' = 'ADD_FIRMWARE_CONTRACT_REJECTED'
        'catalog-stale' = 'does not hold the published 2.7.4 HIL scope as current'
        'revoked' = 'Revoked release marker present'
        'revoked-marker' = 'Revoked or unsupported HIL marker state'
        'wrong-release-id' = 'Published 2.7.4 release identity differs'
        'wrong-image' = 'Published signed image differs'
        'partial-scope' = 'not the published 17-target scope'
    }
    foreach ($case in $cases) {
        foreach ($mode in @('preview', 'apply')) {
            if ($case -in @('preview', 'apply') -and $case -ne $mode) { continue }
            $global:k50Case = $case
            $global:k50Checks.Clear()
            $store = New-K50Store $case $mode
            $release = Join-Path $store '2.7.4'
            $before = Get-Hashes $release
            $global:k50Package = $before
            $failure = ''
            try { Invoke-Zkt274Bld9K50HilExtension -StoreDirectory $store -Mode $mode } catch { $failure = $_.Exception.Message }
            $after = Get-Hashes $release
            foreach ($name in @('manifest.json', 'manifest.sig', 'zone-lite-2.7.4.bin')) {
                if ($after[$name] -cne $before[$name]) { throw "Signed package changed: $case/$mode" }
            }
            $backups = @(Get-ChildItem -LiteralPath $release -Filter '.hil-scope-before-*.json' -File -Force)
            $candidates = @(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) -Filter 'zkt-274-bld9-k50-*' -Directory -ErrorAction SilentlyContinue)
            if ($candidates.Count) { throw "Candidate marker directory was left behind: $case/$mode" }
            if ($case -eq 'apply') {
                if ($failure) { throw "K50 apply failed: $failure" }
                $marker = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText((Join-Path $release '.hil-only.json')))
                $targets = @($marker.targets)
                if ($targets.Count -ne 18 -or $marker.schema_version -ne 2 -or $marker.git_sha -cne $script:git -or
                    $marker.image_sha256 -cne $script:image -or $marker.application_sha256 -cne $script:application) {
                    throw 'Applied K50 marker lost its identity or count'
                }
                for ($index = 0; $index -lt 18; $index++) {
                    foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
                        if ($targets[$index].$field -cne $extended[$index].$field) { throw 'Applied K50 marker changed a target identity or order' }
                    }
                }
                if ($backups.Count -ne 1 -or (Get-FileHash -LiteralPath $backups[0].FullName).Hash -cne $before['.hil-only.json']) { throw 'Exact prior marker backup missing' }
                if (($global:k50Checks -join ',') -cne '17/0,18/17,18/17') { throw "Unexpected deployed checks: $($global:k50Checks -join ',')" }
                # A second run is refused instead of treated as a retry.
                $failure = ''
                try { Invoke-Zkt274Bld9K50HilExtension -StoreDirectory $store -Mode preview } catch { $failure = $_.Exception.Message }
                if ($failure -notmatch 'not the published 17-target scope') { throw "Re-run was not refused: $failure" }
                continue
            }
            if ($after['.hil-only.json'] -cne $before['.hil-only.json'] -or $backups.Count) { throw "Marker changed: $case/$mode" }
            if ($case -eq 'preview') {
                if ($failure) { throw "K50 preview failed: $failure" }
                if (($global:k50Checks -join ',') -cne '17/0,18/17') { throw "Unexpected deployed checks: $($global:k50Checks -join ',')" }
                continue
            }
            if ($failure -notmatch [regex]::Escape($refusals[$case])) { throw "K50 extension did not refuse $case/$mode as expected: $failure" }
        }
    }
    Write-Host 'ZONE-BLD9-01 K50 HIL extension: preview, apply and refusal cases passed.'
} finally {
    Remove-Variable -Scope Global -Name k50Checks, k50Case, k50Package -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
