param([ValidateSet('Legacy','Standard')][string]$NativeArgumentMode = 'Legacy')
$ErrorActionPreference = 'Stop'
if (Test-Path variable:PSNativeCommandArgumentPassing) { $PSNativeCommandArgumentPassing = $NativeArgumentMode }
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$root = Join-Path ([IO.Path]::GetTempPath()) ('scope-extension-' + [guid]::NewGuid().ToString('N'))
$global:scopePython = (Get-Command python -CommandType Application).Source
$previous = @{}
foreach ($name in @('GITHUB_REF','GITHUB_SHA','GITHUB_REPOSITORY')) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
$encoding = $OutputEncoding
# Exercise the native5.1 BOM boundary deliberately; only bounded metadata flows.
$OutputEncoding = New-Object Text.UTF8Encoding($true)
function global:git { $global:LASTEXITCODE = 0; if ($args -contains 'rev-parse') { return ('a' * 40) } }
function global:python {
    if ($args -contains '--verify-main') {
        $global:scopeMainChecks++
        $global:LASTEXITCODE = 0
        if ($global:scopeCase -eq 'main-changed' -and $global:scopeMainChecks -eq 2) { $global:LASTEXITCODE=1; return }
        if ($global:scopeMainChecks -eq 2) {
            if ($global:scopeCase -eq 'marker-race') { [IO.File]::AppendAllText($global:scopeMarker, ' ') }
            if ($global:scopeCase -eq 'package-race') { [IO.File]::AppendAllText($global:scopeManifest, ' ') }
        }
        return ('{"schema_version":1,"main_sha":"' + ('a'*40) + '","source_sha":"' + ('b'*40) + '","check_ids":{}}')
    }
    $piped=@($input)
    if ($piped.Count) { $piped | & $global:scopePython @args } else { & $global:scopePython @args }
}
function global:docker {
    if ($args[0] -ne 'inspect' -or $args[-1] -cne 'attendance-device-dashboard-add-api-1' -or
        $args[2] -cne '{{.Id}}|{{.Image}}|{{.State.StartedAt}}|{{.State.Running}}|{{.RestartCount}}') { throw 'Unexpected Docker access in synthetic test' }
    $global:scopeInspectCalls++
    $global:LASTEXITCODE=0
    $id = 'd'*64
    if (($global:scopeCase -eq 'backend-replaced' -and $global:scopeInspectCalls -ge 4) -or
        ($global:scopeCase -eq 'backend-replaced-after' -and $global:scopeInspectCalls -ge 6)) { $id='e'*64 }
    return ($id + '|sha256:' + ('f'*64) + '|2026-01-01T00:00:00Z|true|0')
}
try {
    foreach ($folder in @('deploy/add','scripts','apps/add_backend/zk_add')) {
        New-Item -ItemType Directory -Path (Join-Path $root $folder) -Force | Out-Null
    }
    foreach ($file in @('deploy/add/extend-journal-hil-scope.ps1','deploy/add/extend-ordered-hil-scope.ps1',
        'deploy/add/journal-hil-scope.ps1','deploy/add/hil-targets-zkt-270.json','scripts/hil_scope_extension.py',
        'scripts/build_zkt_factory_contract.py','scripts/build_zkt_reader_matrix.py',
        'apps/add_backend/zk_add/zkt_factory_contract.py','apps/add_backend/zk_add/zkt_reader_matrix.py',
        'apps/add_backend/zk_add/zkt_reader_matrix.json')) {
        Copy-Item -LiteralPath (Join-Path $repo $file) -Destination (Join-Path $root $file)
    }
    # Network/production checker is the sole stub. Separate Python tests execute
    # actual manifest cryptography and read-only DB revocation/identity guards.
    [IO.File]::WriteAllText((Join-Path $root 'deploy/add/check-deployed-firmware-contract.ps1'), @'
param($SourceDirectory,$AddContainer,[switch]$RequirePublishedHilRelease,[int]$AllowPreviousPrefixCount)
if (-not $RequirePublishedHilRelease -or $AddContainer -cne ('d'*64)) { throw 'Missing exact deployed published-state check' }
$global:scopeContractChecks++
if ($global:scopeCase -eq 'revoked-db' -or ($global:scopeCase -eq 'revoked-after' -and $global:scopeContractChecks -ge 3)) { throw 'Synthetic revoked release refusal' }
if ($AllowPreviousPrefixCount) { Write-Output 'CATALOG_REFRESH_PENDING' } else { Write-Output 'CATALOG_CURRENT' }
'@)
    $entries=@()
    foreach ($item in @(@('2.6.23','1'),@('2.6.22','2'))) {
        $entries+=@{version=$item[0];release_id=('zone-lite-'+$item[0]);application_sha256=($item[1]+('a'*63));artifact_sha256=($item[1]+('b'*63));source_sha=($item[1]+('c'*39));signing_key_id='synthetic'}
    }
    [IO.File]::WriteAllText((Join-Path $root 'apps/add_backend/zk_add/zkt_reader_matrix.json'), (@{schema_version=1;matrix_id='zkt-2.7.0-readers-v1';state='PINNED';readers=$entries} | ConvertTo-Json -Depth 10 -Compress))
    $env:GITHUB_REF='refs/heads/main';$env:GITHUB_SHA='a'*40;$env:GITHUB_REPOSITORY='KripKrop72724/auto_attn'
    . (Join-Path $root 'deploy/add/journal-hil-scope.ps1')
    $all=Get-JournalHilScope
    $cases=@('preview','apply','wrong-identity','wrong-prefix','old-count','revoked-marker','revoked-db','main-changed','marker-race','package-race','backend-replaced','backend-replaced-after','revoked-after')
    foreach ($version in @('2.6.23','2.6.22','2.7.0')) {
        foreach ($case in $cases) {
            $global:scopeCase=$case;$global:scopeMainChecks=0;$global:scopeInspectCalls=0;$global:scopeContractChecks=0
            $store=Join-Path $root ($version+'-'+$case)
            $release=Join-Path $store $version
            New-Item -ItemType Directory -Path $release -Force | Out-Null
            $image=Join-Path $release "zone-lite-$version.bin"
            [IO.File]::WriteAllText($image,'Synthetic test image; not a firmware or signature.')
            [IO.File]::WriteAllText((Join-Path $release 'manifest.sig'),'synthetic-test-signature')
            $imageHash=(Get-FileHash -LiteralPath $image).Hash.ToLowerInvariant()
            $manifest=@{version=$version;release_id="zone-lite-$version";firmware_family='zkt';git_sha=('b'*40);application_sha256=('c'*64);image_sha256=$imageHash;image_name="zone-lite-$version.bin";image_size=(Get-Item $image).Length;release_channel='EXPERIMENTAL_HIL_ONLY';hil_targets=$all}
            $targets=$all
            if ($version -eq '2.6.22') {
                $lines=@(& python (Join-Path $root 'scripts/build_zkt_factory_contract.py'))
                if ($LASTEXITCODE -ne 0) { throw 'Synthetic factory fixture unavailable' }
                $trial=ConvertFrom-Json -InputObject ($lines -join [Environment]::NewLine)
                $manifest.factory_trial=$trial;$manifest.minimum_bootstrap_version='2.5.2'
                $manifest.queue_storage=@{allowed_bootstrap_versions=@();allowed_bootstrap_images=@{}}
                $targets=@($trial.targets | ForEach-Object { [ordered]@{connector_id=$_.connector_id;mac=$_.mac;terminal_serial=$_.terminal_serial} })
            } elseif ($version -eq '2.7.0') {
                $lines=@(& python (Join-Path $root 'scripts/build_zkt_reader_matrix.py') --signing-contract)
                if ($LASTEXITCODE -ne 0) { throw 'Synthetic writer fixture unavailable' }
                $manifest.queue_storage=ConvertFrom-Json -InputObject ($lines -join [Environment]::NewLine)
                $manifest.minimum_bootstrap_version='2.6.22';$manifest.runtime_profile='ZKT_JOURNAL_V1'
            }
            $global:scopeManifest=Join-Path $release 'manifest.json'
            [IO.File]::WriteAllText($global:scopeManifest,($manifest | ConvertTo-Json -Depth 12 -Compress))
            $first=@($targets[0])
            if ($case -eq 'wrong-prefix') { $first=@($targets[1]) }
            $marker=@{schema_version=2;version=$version;git_sha=('b'*40);image_sha256=$imageHash;application_sha256=('c'*64);targets=$first}
            $global:scopeMarker=Join-Path $release '.hil-only.json'
            [IO.File]::WriteAllText($global:scopeMarker,($marker | ConvertTo-Json -Depth 6 -Compress))
            if ($case -eq 'revoked-marker') { [IO.File]::WriteAllText((Join-Path $release '.revoked.json'),'{}') }
            $markerHash=(Get-FileHash $global:scopeMarker).Hash
            $manifestHash=(Get-FileHash $global:scopeManifest).Hash
            $signatureHash=(Get-FileHash (Join-Path $release 'manifest.sig')).Hash
            $evidence=Join-Path $store 'evidence.json'
            $mode=if($case -eq 'preview'){'preview'}else{'apply'}
            $old=if($case -eq 'old-count'){2}else{1}
            $new=if($case -eq 'old-count'){3}else{2}
            $app=if($case -eq 'wrong-identity'){'e'*64}else{'c'*64}
            $caught=$false;$failure=""
            try {
                & (Join-Path $root 'deploy/add/extend-journal-hil-scope.ps1') -StoreDirectory $store -Version $version -ExpectedGitSha ('b'*40) -ExpectedImageSha256 $imageHash -ExpectedApplicationSha256 $app -ExistingPrefixCount $old -ExtendedPrefixCount $new -Mode $mode -Confirmation ($mode.ToUpperInvariant()+"-$version-HIL-$old-TO-$new") -ExpectedMainSha ('a'*40) -EvidencePath $evidence
            } catch { $caught=$true;$failure=$_.Exception.Message }
            if ($caught -ne ($case -notin @('apply','preview'))) { throw "Unexpected extension result $version/$case : $failure" }
            $result=ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($evidence))
            $applied=$case -in @('apply','backend-replaced-after','revoked-after')
            if ($applied) {
                if ($result.marker_mutation -cne 'APPLIED' -or @((ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($global:scopeMarker))).targets).Count -ne 2) { throw 'Applied or partial outcome was lost' }
                $backups=@(Get-ChildItem -LiteralPath $release -Filter '.hil-scope-before-*.json' -Force)
                if ($backups.Count -ne 1 -or (Get-FileHash $backups[0].FullName).Hash -cne $markerHash) { throw 'Prior marker backup changed' }
            } elseif ($case -ne 'marker-race' -and (Get-FileHash $global:scopeMarker).Hash -cne $markerHash) { throw 'Refused/preview operation changed the marker' }
            if ($case -ne 'package-race' -and (Get-FileHash $global:scopeManifest).Hash -cne $manifestHash) { throw 'Manifest changed' }
            if ((Get-FileHash $image).Hash.ToLowerInvariant() -cne $imageHash -or (Get-FileHash (Join-Path $release 'manifest.sig')).Hash -cne $signatureHash) { throw 'Immutable image or signature changed' }
            if ($caught -and $result.status -cne 'REFUSED') { throw 'Failed postcheck became success' }
        }
    }
    Write-Host 'Journal HIL extension:39 preview/apply/fault cases passed.'
} finally {
    foreach ($name in $previous.Keys) { [Environment]::SetEnvironmentVariable($name,$previous[$name]) }
    $OutputEncoding=$encoding
    Remove-Item Function:git,Function:python,Function:docker
    Remove-Variable -Scope Global -Name scopePython,scopeMainChecks,scopeInspectCalls,scopeContractChecks,scopeCase,scopeManifest,scopeMarker -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $root -Recurse -Force
}
