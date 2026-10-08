param(
    [Parameter(Mandatory = $true)][string]$StoreDirectory,
    [Parameter(Mandatory = $true)][ValidateSet('2.6.23', '2.6.22', '2.7.0')][string]$Version,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedGitSha,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ExpectedImageSha256,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{64}$')][string]$ExpectedApplicationSha256,
    [Parameter(Mandatory = $true)][ValidateRange(1, 16)][int]$ExistingPrefixCount,
    [Parameter(Mandatory = $true)][ValidateRange(2, 17)][int]$ExtendedPrefixCount,
    [Parameter(Mandatory = $true)][ValidateSet('preview', 'apply')][string]$Mode,
    [Parameter(Mandatory = $true)][string]$Confirmation,
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedMainSha,
    [Parameter(Mandatory = $true)][string]$EvidencePath
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$helper = Join-Path $repo 'scripts/hil_scope_extension.py'
$containerName = 'attendance-device-dashboard-add-api-1'
$report = [ordered]@{schema_version=1;mode=$Mode;version=$Version;status='REFUSED';marker_mutation='NOT_ATTEMPTED';eligibility='NOT_GRANTED; EXISTING_ADD_SCHEDULER_REQUIRED';main_sha=$ExpectedMainSha}
$lock = $null
$evidence = [IO.Path]::GetFullPath($EvidencePath)
$evidenceReservation = New-Object IO.FileStream($evidence, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
$evidenceReservation.Dispose()
function Get-Hash([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Read-BoundedJson([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $item.Length -le 0 -or $item.Length -gt 65536) { throw 'Scope metadata is missing, indirect or oversized.' }
    return ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($Path))
}
function Assert-Main {
    $lines = @(& python $helper --verify-main $ExpectedMainSha --source $ExpectedGitSha)
    if ($LASTEXITCODE -ne 0 -or $lines.Count -ne 1) { throw 'Unchanged green main was not verified.' }
    $value = ConvertFrom-Json -InputObject $lines[0]
    if ($value.main_sha -cne $ExpectedMainSha -or $value.source_sha -cne $ExpectedGitSha) { throw 'Main verification identity changed.' }
    $report.main_verification = $value
}
function Get-BackendIdentity {
    # The allowlist format never reads or logs the container environment.
    $lines = @(& docker inspect --format '{{.Id}}|{{.Image}}|{{.State.StartedAt}}|{{.State.Running}}|{{.RestartCount}}' $containerName)
    if ($LASTEXITCODE -ne 0 -or $lines.Count -ne 1 -or
        $lines[0] -cnotmatch '^([a-f0-9]{64})\|(sha256:[a-f0-9]{64})\|([0-9TZ:.+-]+)\|true\|([0-9]+)$') {
        throw 'Running ADD container identity is unavailable.'
    }
    return [ordered]@{id=$Matches[1];image=$Matches[2];started_at=$Matches[3];restart_count=$Matches[4]}
}
function Assert-BackendUnchanged {
    $now = Get-BackendIdentity
    foreach ($field in @('id','image','started_at','restart_count')) {
        if ($now[$field] -cne $backend[$field]) { throw 'ADD container changed during scope extension.' }
    }
}
function Assert-Published([int]$PreviousPrefixCount = 0) {
    Assert-BackendUnchanged
    # Pin the checked instance; a later container-name replacement cannot silently
    # supply a different validator between this read and the following identity check.
    $states = @(& (Join-Path $PSScriptRoot 'check-deployed-firmware-contract.ps1') -SourceDirectory $release -AddContainer $backend.id -RequirePublishedHilRelease -AllowPreviousPrefixCount $PreviousPrefixCount)
    if ($states.Count -ne 1 -or @('CATALOG_CURRENT','CATALOG_REFRESH_PENDING') -cnotcontains $states[0] -or
        ($states[0] -ceq 'CATALOG_REFRESH_PENDING' -and -not $PreviousPrefixCount)) { throw 'Published catalog state was not verified.' }
    $report.catalog_state = $states[0]
    Assert-BackendUnchanged
}
function Assert-UnchangedPackage {
    foreach ($name in @('manifest.json','manifest.sig',"zone-lite-$Version.bin")) {
        if ((Get-Hash (Join-Path $release $name)) -cne $before[$name]) { throw 'Published signed package changed during extension.' }
    }
}
try {
    if ($env:GITHUB_REF -cne 'refs/heads/main' -or $env:GITHUB_SHA -cne $ExpectedMainSha -or
        $env:GITHUB_REPOSITORY -cne 'KripKrop72724/auto_attn') { throw 'Exact main workflow context is required.' }
    $head = @(& git -C $repo rev-parse HEAD)
    if ($LASTEXITCODE -ne 0 -or $head.Count -ne 1 -or $head[0] -cne $ExpectedMainSha) { throw 'Checked-out main identity changed.' }
    & git -C $repo diff --quiet HEAD -- .
    if ($LASTEXITCODE -ne 0) { throw 'Tracked checkout changes are not permitted.' }
    $lines = @(& python $helper --version $Version --source $ExpectedGitSha --application $ExpectedApplicationSha256 --artifact $ExpectedImageSha256 --old-count $ExistingPrefixCount --new-count $ExtendedPrefixCount --mode $Mode --confirmation $Confirmation)
    if ($LASTEXITCODE -ne 0 -or $lines.Count -ne 1) { throw 'Exact ordered scope or typed confirmation was refused.' }
    $plan = ConvertFrom-Json -InputObject $lines[0]
    $report.plan = $plan
    Assert-Main
    $store = (Resolve-Path -LiteralPath $StoreDirectory).Path
    $release = Join-Path $store $Version
    foreach ($directory in @($store, $release)) {
        $item = Get-Item -LiteralPath $directory
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Direct published release directory required.' }
    }
    # Serializes cooperating calls to this wrapper only. This is not a global
    # Docker/DB lock. ADD's revocation and campaign guards remain authoritative.
    $lock = New-Object IO.FileStream((Join-Path $store '.journal-hil-scope.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $markerPath = Join-Path $release '.hil-only.json'
    $manifest = Read-BoundedJson (Join-Path $release 'manifest.json')
    $marker = Read-BoundedJson $markerPath
    if ($manifest.version -cne $Version -or $manifest.release_id -cne "zone-lite-$Version" -or
        $manifest.release_channel -cne 'EXPERIMENTAL_HIL_ONLY' -or $manifest.firmware_family -cne 'zkt' -or
        $manifest.git_sha -cne $ExpectedGitSha -or $manifest.image_sha256 -cne $ExpectedImageSha256 -or
        $manifest.application_sha256 -cne $ExpectedApplicationSha256 -or $manifest.image_name -cne "zone-lite-$Version.bin") { throw 'Published release identity differs from exact inputs.' }
    foreach ($name in @('.revoked', '.revoked.json')) {
        if (Test-Path -LiteralPath (Join-Path $release $name)) { throw 'Revoked release marker present.' }
    }
    if ($marker.revoked_at -or $marker.revoked -or ($marker.state -and $marker.state -cne 'HIL_ONLY')) { throw 'Revoked or unsupported HIL marker state.' }
    $actual = @($marker.targets)
    if ($actual.Count -ne $ExistingPrefixCount) { throw 'Published old prefix count changed; inspect prior attempts.' }
    $before = @{}
    foreach ($name in @('manifest.json','manifest.sig',"zone-lite-$Version.bin")) {
        $item = Get-Item -LiteralPath (Join-Path $release $name)
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Indirect published file refused.' }
        $before[$name] = Get-Hash $item.FullName
    }
    if ($before["zone-lite-$Version.bin"] -cne $ExpectedImageSha256) { throw 'Signed image digest changed.' }
    $report.package_sha256 = $before
    $markerBefore = Get-Hash $markerPath
    $report.marker_before_sha256 = $markerBefore
    $backend = Get-BackendIdentity
    $report.backend_identity = $backend
    Assert-Published
    $arguments = @{StoreDirectory=$store;Version=$Version;ExpectedGitSha=$ExpectedGitSha;
        ExpectedImageSha256=$ExpectedImageSha256;ExpectedApplicationSha256=$ExpectedApplicationSha256;
        ExistingTargetsJson=$plan.old_targets_json;ExtendedTargetsJson=$plan.new_targets_json}
    & (Join-Path $PSScriptRoot 'extend-ordered-hil-scope.ps1') @arguments -PreviewOnly
    Assert-Main
    Assert-Published
    Assert-UnchangedPackage
    if ((Get-Hash $markerPath) -cne $markerBefore) { throw 'HIL marker changed after preview.' }
    if ($Mode -eq 'apply') {
        $backupsBefore = @(Get-ChildItem -LiteralPath $release -Filter '.hil-scope-before-*.json' -File -Force | ForEach-Object { $_.Name })
        $report.marker_mutation = 'ATTEMPTED_OUTCOME_UNCERTAIN'
        # Persist intent before the only marker mutation. A transport/process
        # interruption requires inspecting this evidence and marker; never retry blindly.
        [IO.File]::WriteAllText($evidence, ($report | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
        & (Join-Path $PSScriptRoot 'extend-ordered-hil-scope.ps1') @arguments
        $backups = @(Get-ChildItem -LiteralPath $release -Filter '.hil-scope-before-*.json' -File -Force | Where-Object { $backupsBefore -cnotcontains $_.Name })
        if ($backups.Count -ne 1 -or (Get-Hash $backups[0].FullName) -cne $markerBefore) { throw 'Exact prior marker backup was not verified.' }
        $report.backup_sha256 = Get-Hash $backups[0].FullName
        $report.marker_mutation = 'APPLIED'
    }
    $previousCount = if ($Mode -eq 'apply') { $ExistingPrefixCount } else { 0 }
    Assert-Published -PreviousPrefixCount $previousCount
    Assert-UnchangedPackage
    Assert-Main
    $report.marker_after_sha256 = Get-Hash $markerPath
    $after = Read-BoundedJson $markerPath
    $expectedJson = if ($Mode -eq 'apply') { $plan.new_targets_json } else { $plan.old_targets_json }
    $expected = ConvertFrom-Json -InputObject $expectedJson
    if (@($after.targets).Count -ne @($expected).Count) { throw 'Final prefix count differs.' }
    for ($index=0; $index -lt @($expected).Count; $index++) {
        foreach ($field in @('connector_id','mac','terminal_serial')) {
            if ($after.targets[$index].$field -cne $expected[$index].$field) { throw 'Final prefix identity differs.' }
        }
    }
    $report.status = if ($Mode -eq 'apply') { 'EXPOSURE_EXTENDED' } else { 'PREVIEW_PASSED' }
} catch {
    # Arbitrary paths, configuration, native stderr and credentials never enter
    # the retained machine-readable report. A failed postcheck can follow an
    # applied marker; preserve that fact and never restore a revoked DB row.
    $report.failure = 'HIL_SCOPE_EXTENSION_REFUSED_OR_POSTCHECK_FAILED'
    throw
} finally {
    if ($lock) { $lock.Dispose() }
    [IO.File]::WriteAllText($evidence, ($report | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
}
Write-Host "HIL scope result: $($report.status); campaign eligibility not granted."
