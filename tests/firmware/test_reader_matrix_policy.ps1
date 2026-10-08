param([ValidateSet('Legacy', 'Standard')][string]$NativeArgumentMode = 'Legacy')
$ErrorActionPreference = 'Stop'
# Exercise the production Windows 5.1 native argument boundary on PowerShell 7
# too. Native Windows already uses these semantics and has no such preference.
if (Test-Path variable:PSNativeCommandArgumentPassing) { $PSNativeCommandArgumentPassing = $NativeArgumentMode }
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$root = Join-Path ([IO.Path]::GetTempPath()) ('reader-matrix-' + [guid]::NewGuid().ToString('N'))
function Assert-Refused([scriptblock]$Action) {
    $refused = $false
    try { & $Action | Out-Null } catch { $refused = $true }
    if (-not $refused) { throw 'Unsafe policy was accepted' }
}
try {
    foreach ($folder in @('deploy/add', 'scripts', 'apps/add_backend/zk_add', 'package')) {
        New-Item -ItemType Directory -Path (Join-Path $root $folder) -Force | Out-Null
    }
    foreach ($file in @('deploy/add/firmware-storage-contract.ps1', 'deploy/add/publish-firmware.ps1',
        'deploy/add/extend-ordered-hil-scope.ps1', 'deploy/add/journal-hil-scope.ps1',
        'deploy/add/check-deployed-reader-packages.ps1', 'deploy/add/sign-firmware-release.ps1',
        'scripts/check_deployed_reader_packages.py',
        'deploy/add/hil-targets-zkt-270.json', 'scripts/build_zkt_reader_matrix.py',
        'scripts/build_zkt_factory_contract.py', 'scripts/canonicalize_firmware_manifest.py',
        'apps/add_backend/zk_add/zkt_reader_matrix.py', 'apps/add_backend/zk_add/zkt_reader_matrix.json',
        'apps/add_backend/zk_add/zkt_factory_contract.py')) {
        Copy-Item -LiteralPath (Join-Path $repo $file) -Destination (Join-Path $root $file)
    }
    # Keep the blocked-policy refusal independent of the checked-in release state.
    [IO.File]::WriteAllText((Join-Path $root 'apps/add_backend/zk_add/zkt_reader_matrix.json'), '{"schema_version":1,"matrix_id":"zkt-2.7.0-readers-v1","state":"BLOCKED","readers":[]}')
    . (Join-Path $root 'deploy/add/firmware-storage-contract.ps1')
    $image = Join-Path $root 'contract.bin'
    [IO.File]::WriteAllText($image, 'ZONE_STORAGE_CONTRACT_V4:WRITER:LEGACY=2:JOURNAL=1:READERS=3F:AUTHORITY=ADD:BRIDGE=2.6.17' + [char]0)
    if ((Get-FirmwareStorageContract -ImagePath $image -Version 2.7.0).schema_version -ne 4) { throw 'Historical audit reader changed' }
    Assert-Refused { Get-FirmwareStorageContract -ImagePath $image -Version 2.7.0 -ForSigning }
    [IO.File]::WriteAllText($image, 'ZONE_STORAGE_CONTRACT_V5:WRITER:LEGACY=2:JOURNAL=1:READERS=3F:AUTHORITY=ADD:MATRIX=' + ('a' * 64) + [char]0)
    Assert-Refused { Get-FirmwareStorageContract -ImagePath $image -Version 2.7.0 -ForSigning }
    # Synthetic pins exist only in this disposable test copy. No release policy is changed.
    $entries = @()
    foreach ($item in @(@('2.6.23', '1'), @('2.6.22', '2'))) {
        $entries += @{version=$item[0];release_id=('zone-lite-' + $item[0]);application_sha256=($item[1] + ('a'*63));
            artifact_sha256=($item[1] + ('b'*63));source_sha=($item[1] + ('c'*39));signing_key_id='isolated-test'}
    }
    $matrix = @{schema_version=1;matrix_id='zkt-2.7.0-readers-v1';state='PINNED';readers=$entries}
    [IO.File]::WriteAllText((Join-Path $root 'apps/add_backend/zk_add/zkt_reader_matrix.json'), ($matrix | ConvertTo-Json -Depth 10 -Compress))
    $contractJson = & python (Join-Path $root 'scripts/build_zkt_reader_matrix.py') --signing-contract
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic matrix was refused' }
    $contract = ($contractJson -join [Environment]::NewLine) | ConvertFrom-Json
    if ((Get-WriterBootstrapMinimum -StorageContract @{schema_version=5;allowed_bootstrap_versions=@('2.6.23','2.6.22')}) -cne '2.6.22') {
        throw 'Writer bootstrap minimum incorrectly follows preferred reader order'
    }
    $marker = 'ZONE_STORAGE_CONTRACT_V5:WRITER:LEGACY=2:JOURNAL=1:READERS=3F:AUTHORITY=ADD:MATRIX=' + $contract.reader_matrix_sha256
    [IO.File]::WriteAllText($image, $marker + [char]0)
    $parsed = Get-FirmwareStorageContract -ImagePath $image -Version 2.7.0 -ForSigning
    if ($parsed.schema_version -ne 5 -or $parsed.allowed_bootstrap_versions.Count -ne 2 -or
        $parsed.reader_matrix_sha256 -cne $contract.reader_matrix_sha256) { throw 'Reader identity was lost' }
    [IO.File]::WriteAllText($image, $marker + '#' + [char]0)
    Assert-Refused { Get-FirmwareStorageContract -ImagePath $image -Version 2.7.0 -ForSigning }
    [IO.File]::WriteAllText($image, $marker + [char]0 + $marker + [char]0)
    Assert-Refused { Get-FirmwareStorageContract -ImagePath $image -Version 2.7.0 -ForSigning }
    $source = Join-Path $root 'package'
    $image = Join-Path $source 'zone-lite-2.7.0.bin'
    [IO.File]::WriteAllText($image, $marker + [char]0)
    # Preserve the array through the shared loader, as in the Windows 5.1
    # publication tests; do not depend on pipeline JSON conversion semantics.
    . (Join-Path $root 'deploy/add/journal-hil-scope.ps1')
    $all = Get-JournalHilScope
    $targets = ConvertTo-Json -InputObject @($all[0]) -Depth 5 -Compress
    $fullTargets = ConvertTo-Json -InputObject @($all) -Depth 5 -Compress
    if ($all.Count -ne 17) { throw 'Synthetic writer manifest lost the nationwide denominator' }
    Assert-JournalHilScope -TargetsJson $targets
    Assert-JournalHilScope -TargetsJson $fullTargets -Complete
    Assert-Refused { Assert-JournalHilScope -TargetsJson (ConvertTo-Json -InputObject $all[0] -Depth 5 -Compress) }
    $manifest = @{version='2.7.0';release_id='zone-lite-2.7.0';firmware_family='zkt';project_name='zone_lite';
        release_channel='EXPERIMENTAL_HIL_ONLY';image_name='zone-lite-2.7.0.bin';image_size=(Get-Item $image).Length;
        image_sha256=(Get-FileHash $image).Hash.ToLowerInvariant();application_sha256=('d'*64);git_sha=('a'*40);
        minimum_bootstrap_version='2.6.22';runtime_profile='ZKT_JOURNAL_V1';queue_storage=$contract;hil_targets=$all}
    $manifestPath = Join-Path $source 'manifest.json'
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress))
    & python (Join-Path $root 'scripts/canonicalize_firmware_manifest.py') $manifestPath
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic manifest canonicalization failed' }
    [IO.File]::WriteAllText((Join-Path $source 'manifest.sig'), 'synthetic-noncryptographic-test')
    [IO.File]::WriteAllText((Join-Path $source 'SHA256SUMS'), 'synthetic-test')
    $store = Join-Path $root 'store'
    & (Join-Path $root 'deploy/add/publish-firmware.ps1') -SourceDirectory $source -StoreDirectory $store -Version 2.7.0 -PublicationMode HIL_ONLY -HilTargetsJson $targets
    & (Join-Path $root 'deploy/add/extend-ordered-hil-scope.ps1') -StoreDirectory $store -Version 2.7.0 -ExpectedGitSha $manifest.git_sha -ExpectedImageSha256 $manifest.image_sha256 -ExpectedApplicationSha256 $manifest.application_sha256 -ExistingTargetsJson $targets -ExtendedTargetsJson $fullTargets
    if ((Get-Content (Join-Path $store '2.7.0/.hil-only.json') -Raw | ConvertFrom-Json).targets.Count -ne 17) { throw 'Writer denominator changed' }
    $manifest.queue_storage.reader_matrix.readers[0].signing_key_id = 'changed'
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Depth 12 -Compress))
    & python (Join-Path $root 'scripts/canonicalize_firmware_manifest.py') $manifestPath
    Assert-Refused { & (Join-Path $root 'deploy/add/publish-firmware.ps1') -SourceDirectory $source -StoreDirectory (Join-Path $root 'bad-store') -Version 2.7.0 -PublicationMode HIL_ONLY -HilTargetsJson $targets }
    # A factory bridge carries no general legacy predecessor allowlist.
    [IO.File]::WriteAllText($image, 'ZONE_STORAGE_CONTRACT_V3:BRIDGE:LEGACY=2:JOURNAL=1:READERS=3F:CAPTURE=1:AUTHORITY=1:VERSION=2.6.22:FACTORY_TRIAL=1' + [char]0)
    $factory = Get-FirmwareStorageContract -ImagePath $image -Version 2.6.22 -ForSigning
    if ($factory.allowed_bootstrap_versions.Count -ne 0 -or $factory.allowed_bootstrap_images.Count -ne 0) { throw 'Factory bridge allowed an unbound predecessor' }
    $trialJson = & python (Join-Path $root 'scripts/build_zkt_factory_contract.py')
    if ($LASTEXITCODE -ne 0) { throw 'Factory contract unavailable' }
    $trial = ($trialJson -join [Environment]::NewLine) | ConvertFrom-Json
    $factoryTargets = @($trial.targets | ForEach-Object { @{connector_id=$_.connector_id;mac=$_.mac;terminal_serial=$_.terminal_serial} })
    $factoryScope = ConvertTo-Json -InputObject $factoryTargets -Depth 5 -Compress
    # Diagnose the native byte/JSON boundary without logging any input identity.
    $factoryScope | & python (Join-Path $repo 'tests/firmware/tools/check_factory_scope_input.py')
    if ($LASTEXITCODE -ne 0) { throw 'Factory input diagnostic failed' }
    $previousEncoding = $OutputEncoding
    try {
        foreach ($encoding in @([Text.Encoding]::ASCII, (New-Object Text.UTF8Encoding($false)), (New-Object Text.UTF8Encoding($true)))) {
            $OutputEncoding = $encoding
            $factoryScope | & python (Join-Path $root 'scripts/build_zkt_factory_contract.py') --exposure-stdin | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Exact factory targets refused' }
        }
    } finally { $OutputEncoding = $previousEncoding }
    Assert-Refused {
        $targets | & python (Join-Path $root 'scripts/build_zkt_factory_contract.py') --exposure-stdin 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Factory trial refused the unrelated 3FL target' }
    }
    # Exercise real factory publication/extension, including native JSON input.
    $factoryImage = Join-Path $source 'zone-lite-2.6.22.bin'
    Copy-Item -LiteralPath $image -Destination $factoryImage
    $factoryManifest = @{version='2.6.22';release_id='zone-lite-2.6.22';firmware_family='zkt';project_name='zone_lite';
        release_channel='EXPERIMENTAL_HIL_ONLY';image_name='zone-lite-2.6.22.bin';image_size=(Get-Item $factoryImage).Length;
        image_sha256=(Get-FileHash $factoryImage).Hash.ToLowerInvariant();application_sha256=('f'*64);git_sha=('a'*40);
        minimum_bootstrap_version='2.5.2';queue_storage=$factory;factory_trial=$trial;hil_targets=$all}
    [IO.File]::WriteAllText($manifestPath, ($factoryManifest | ConvertTo-Json -Depth 12 -Compress))
    & python (Join-Path $root 'scripts/canonicalize_firmware_manifest.py') $manifestPath
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic factory manifest canonicalization failed' }
    $firstFactory = ConvertTo-Json -InputObject @($factoryTargets[0]) -Depth 5 -Compress
    & (Join-Path $root 'deploy/add/publish-firmware.ps1') -SourceDirectory $source -StoreDirectory $store -Version 2.6.22 -PublicationMode HIL_ONLY -HilTargetsJson $firstFactory
    & (Join-Path $root 'deploy/add/extend-ordered-hil-scope.ps1') -StoreDirectory $store -Version 2.6.22 -ExpectedGitSha $factoryManifest.git_sha -ExpectedImageSha256 $factoryManifest.image_sha256 -ExpectedApplicationSha256 $factoryManifest.application_sha256 -ExistingTargetsJson $firstFactory -ExtendedTargetsJson $factoryScope
    if ((Get-Content (Join-Path $store '2.6.22/.hil-only.json') -Raw | ConvertFrom-Json).targets.Count -ne 3) { throw 'Factory exposure changed' }
    # Execute the actual pre-decryption signer branch with an isolated vault.
    # A rejected deployed proof must stop before output or key work is created.
    $vault = Join-Path $root 'vault'
    $unsigned = Join-Path $root 'unsigned'
    New-Item -ItemType Directory -Path $vault, $unsigned | Out-Null
    [IO.File]::WriteAllText((Join-Path $vault 'vault-manifest.json'), '{"keys":[{"number":1,"key_id":"synthetic","state":"ACTIVE"}]}')
    foreach ($name in @('key-1.dpapi', 'key-1.entropy', 'key-1-public.pem')) {
        [IO.File]::WriteAllText((Join-Path $vault $name), 'synthetic-invalid-vault-material')
    }
    $descriptor = New-Object byte[] (256 + $marker.Length + 1)
    $descriptor[0] = 0xE9
    [Array]::Copy([BitConverter]::GetBytes([uint32]2882360370), 0, $descriptor, 32, 4)
    [Array]::Copy([Text.Encoding]::ASCII.GetBytes('2.7.0'), 0, $descriptor, 48, 5)
    [Array]::Copy([Text.Encoding]::ASCII.GetBytes('zone_lite'), 0, $descriptor, 80, 9)
    [Array]::Copy([Text.Encoding]::ASCII.GetBytes($marker), 0, $descriptor, 256, $marker.Length)
    [IO.File]::WriteAllBytes((Join-Path $unsigned 'zone_lite.bin'), $descriptor)
    $global:readerProofMode = 'reject'
    $global:readerProofCalls = 0
    function global:docker {
        $global:LASTEXITCODE = 0
        if ($args[3] -like '*/check.py') {
            $global:readerProofCalls++
            if ($global:readerProofMode -eq 'accept') { Write-Output ('ADD_READER_PACKAGES_ACCEPTED:' + $args[4]) }
            elseif ($global:readerProofMode -eq 'wrong-hash') { Write-Output ('ADD_READER_PACKAGES_ACCEPTED:' + ('e' * 64)) }
            else { $global:LASTEXITCODE = 1; Write-Output 'ADD_READER_PACKAGES_REJECTED' }
        }
    }
    try {
        $blocked = $false
        try {
            & (Join-Path $root 'deploy/add/sign-firmware-release.ps1') -VaultDirectory $vault -UnsignedDirectory $unsigned -OutputDirectory (Join-Path $root 'must-not-exist') -Version 2.7.0 -GitSha ('a' * 40) -HilTargetsJson $targets
        } catch {
            $blocked = $_.Exception.Message -like 'Exact reader packages are missing*'
        }
        if (-not $blocked -or $global:readerProofCalls -ne 1 -or (Test-Path (Join-Path $root 'must-not-exist'))) {
            throw 'Writer signer did not stop at deployed reader proof before vault work'
        }
        $global:readerProofMode = 'accept'
        & (Join-Path $root 'deploy/add/check-deployed-reader-packages.ps1') -MatrixSha256 $contract.reader_matrix_sha256
        $global:readerProofMode = 'wrong-hash'
        Assert-Refused { & (Join-Path $root 'deploy/add/check-deployed-reader-packages.ps1') -MatrixSha256 $contract.reader_matrix_sha256 }
    } finally { Remove-Item Function:global:docker }
    Write-Host 'Reader matrix and factory publication contracts passed'
} finally {
    if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force }
}
