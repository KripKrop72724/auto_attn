# ZONE-BLD9-01 K50 (19 October 2026): append one reviewed device to the
# quarantine marker of the immutable, already published signed 2.7.4. The
# signed nationwide 17 stay first, in their order, and remain the denominator;
# the signed manifest, signature and image are never rewritten.
# See docs/zone-lite-2.7.4-hil.md.

function Get-Zkt274Bld9K50Release {
    return @{
        GitSha = '87e14fe6e9756cd65a27e64ac1a7420fe35aa816'
        ImageSha256 = '9a3e2226b7abff11cc264472356c186d3b9cce87a0df059aeb0a9965f1f095c5'
        ApplicationSha256 = '3e4bb613ab046df5ad29e4a1ab37fd801e1778e6b0d2c41c6604fe75bbea2fac'
    }
}

function Assert-Zkt274Bld9K50HilScope {
    param(
        [Parameter(Mandatory = $true)][string]$ExistingTargetsJson,
        [Parameter(Mandatory = $true)][string]$ExtendedTargetsJson
    )
    . (Join-Path $PSScriptRoot 'journal-hil-scope.ps1')
    Assert-JournalHilScope -TargetsJson $ExistingTargetsJson -Complete
    if (-not $ExtendedTargetsJson.Trim().StartsWith('[')) { throw 'ZONE-BLD9-01 K50 extension must be an ordered array' }
    $extended = ConvertFrom-Json -InputObject $ExtendedTargetsJson
    $extended = @($extended)
    if ($extended.Count -ne 18) { throw 'ZONE-BLD9-01 K50 extension requires exactly 18 ordered targets' }
    Assert-JournalHilScope -TargetsJson (ConvertTo-Json -InputObject @($extended[0..16]) -Depth 5 -Compress) -Complete
    $last = $extended[17]
    $keys = @($last.PSObject.Properties | ForEach-Object { $_.Name })
    if ($keys.Count -ne 3 -or
        $keys -cnotcontains 'connector_id' -or $keys -cnotcontains 'mac' -or
        $keys -cnotcontains 'terminal_serial' -or
        $last.connector_id -isnot [string] -or $last.mac -isnot [string] -or $last.terminal_serial -isnot [string] -or
        $last.connector_id -cne '9d8c589f-9735-4b58-9277-a385a60d0c9b' -or
        $last.mac -cne 'ac:27:6e:a3:33:28' -or
        $last.terminal_serial -cne 'ANM4261301077') {
        throw 'ZONE-BLD9-01 K50 extension differs from the reviewed exact ZKT identity'
    }
}

function Assert-Zkt274Bld9K50HilExtension {
    param(
        [string]$ExpectedGitSha,
        [string]$ExpectedImageSha256,
        [string]$ExpectedApplicationSha256,
        [string]$ExistingTargetsJson,
        [string]$ExtendedTargetsJson
    )
    $release = Get-Zkt274Bld9K50Release
    if ($ExpectedGitSha -cne $release.GitSha -or
        $ExpectedImageSha256 -cne $release.ImageSha256 -or
        $ExpectedApplicationSha256 -cne $release.ApplicationSha256) {
        throw 'ZONE-BLD9-01 K50 HIL extension requires the exact published 2.7.4 release identity'
    }
    Assert-Zkt274Bld9K50HilScope -ExistingTargetsJson $ExistingTargetsJson -ExtendedTargetsJson $ExtendedTargetsJson
}

# Preview proves, without changing anything, that the published 2.7.4 still
# holds the 17 and that the running ADD admits the 18-target marker; ADD fails
# every catalog read on a marker it cannot parse. Apply then replaces only the
# marker (with a backup) and re-checks the deployed catalog and package bytes.
function Invoke-Zkt274Bld9K50HilExtension {
    param(
        [Parameter(Mandatory = $true)][string]$StoreDirectory,
        [Parameter(Mandatory = $true)][ValidateSet('preview', 'apply')][string]$Mode
    )
    $ErrorActionPreference = 'Stop'
    $pins = Get-Zkt274Bld9K50Release
    $extendedJson = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'hil-targets-2.7.4-bld9-k50.json'))
    $arguments = @{
        StoreDirectory = $StoreDirectory
        Version = '2.7.4'
        ExpectedGitSha = $pins.GitSha
        ExpectedImageSha256 = $pins.ImageSha256
        ExpectedApplicationSha256 = $pins.ApplicationSha256
        ExistingTargetsJson = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'hil-targets-zkt-270.json'))
        ExtendedTargetsJson = $extendedJson
    }
    $check = Join-Path $PSScriptRoot 'check-deployed-firmware-contract.ps1'
    $extend = Join-Path $PSScriptRoot 'extend-ordered-hil-scope.ps1'
    $directory = Join-Path (Resolve-Path -LiteralPath $StoreDirectory).Path '2.7.4'
    $markerPath = Join-Path $directory '.hil-only.json'
    $package = @('manifest.json', 'manifest.sig', 'zone-lite-2.7.4.bin')
    $hash = { param($Path) (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
    $before = @{}
    foreach ($name in $package) { $before[$name] = & $hash (Join-Path $directory $name) }
    $markerBefore = & $hash $markerPath
    $assertPackage = {
        foreach ($name in $package) {
            if ((& $hash (Join-Path $directory $name)) -cne $before[$name]) { throw 'The signed 2.7.4 package changed during the K50 extension.' }
        }
    }
    $published = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($markerPath))
    if (@($published.targets).Count -ne 17) {
        throw 'The 2.7.4 marker is not the published 17-target scope; inspect prior attempts before another run.'
    }
    # 1. The running ADD holds the published 17-target scope as its current catalog.
    $state = @(& $check -SourceDirectory $directory -RequirePublishedHilRelease)
    if ($state.Count -ne 1 -or $state[0] -cne 'CATALOG_CURRENT') { throw 'The deployed ADD does not hold the published 2.7.4 HIL scope as current.' }
    # 2. Exact signed identity, image bytes, unrevoked marker and the 17 prefix.
    & $extend @arguments -PreviewOnly
    # 3. The running ADD must parse the 18-target marker before it is written.
    $candidate = Join-Path ([IO.Path]::GetTempPath()) ('zkt-274-bld9-k50-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $candidate | Out-Null
    try {
        foreach ($name in @('manifest.json', 'manifest.sig')) {
            Copy-Item -LiteralPath (Join-Path $directory $name) -Destination (Join-Path $candidate $name)
        }
        $marker = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($markerPath))
        $targets = ConvertFrom-Json -InputObject $extendedJson
        $marker.targets = @($targets)
        [IO.File]::WriteAllText((Join-Path $candidate '.hil-only.json'), (($marker | ConvertTo-Json -Depth 6 -Compress) + "`n"), (New-Object Text.UTF8Encoding($false)))
        $state = @(& $check -SourceDirectory $candidate -RequirePublishedHilRelease -AllowPreviousPrefixCount 17)
        if ($state.Count -ne 1 -or $state[0] -cne 'CATALOG_REFRESH_PENDING') {
            throw 'The deployed ADD does not admit the reviewed 18-target 2.7.4 scope; deploy its backend support first.'
        }
    } finally {
        Remove-Item -LiteralPath $candidate -Recurse -Force
    }
    & $assertPackage
    if ((& $hash $markerPath) -cne $markerBefore) { throw 'The 2.7.4 HIL marker changed during the preview.' }
    if ($Mode -eq 'preview') {
        Write-Host 'ZONE-BLD9-01 K50 preview passed: the deployed ADD admits the 18-target 2.7.4 scope; nothing was changed.'
        return
    }
    $backupsBefore = @(Get-ChildItem -LiteralPath $directory -Filter '.hil-scope-before-*.json' -File -Force | ForEach-Object { $_.Name })
    & $extend @arguments
    $backups = @(Get-ChildItem -LiteralPath $directory -Filter '.hil-scope-before-*.json' -File -Force | Where-Object { $backupsBefore -cnotcontains $_.Name })
    if ($backups.Count -ne 1 -or (& $hash $backups[0].FullName) -cne $markerBefore) { throw 'The exact prior 2.7.4 marker backup was not verified.' }
    # 4. The running ADD admits the replaced marker; its next catalog read refreshes it.
    $state = @(& $check -SourceDirectory $directory -RequirePublishedHilRelease -AllowPreviousPrefixCount 17)
    if ($state.Count -ne 1 -or @('CATALOG_CURRENT', 'CATALOG_REFRESH_PENDING') -cnotcontains $state[0]) {
        throw 'The deployed ADD did not verify the extended 2.7.4 HIL scope; inspect the marker and its backup.'
    }
    & $assertPackage
    $after = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($markerPath))
    $expected = ConvertFrom-Json -InputObject $extendedJson
    $expected = @($expected)
    if (@($after.targets).Count -ne $expected.Count) { throw 'The extended 2.7.4 marker has a different target count.' }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if (@($after.targets)[$index].$field -cne $expected[$index].$field) { throw 'The extended 2.7.4 marker has a different target identity or order.' }
        }
    }
    Write-Host "Extended the immutable 2.7.4 HIL quarantine to the ZONE-BLD9-01 K50 ($($state[0])); previous marker preserved at $($backups[0].Name)."
}
