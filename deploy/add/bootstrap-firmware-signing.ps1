[CmdletBinding(DefaultParameterSetName = 'DeviceBootstrap')]
param(
    [Parameter(Mandatory = $true)][string]$VaultDirectory,
    [Parameter(Mandatory = $true)][string]$UnsignedDirectory,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true, ParameterSetName = 'DeviceBootstrap')][ValidatePattern('^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$')][string]$DeviceMac,
    # One MAC-independent installer bundle for every new board of a family
    # (docs/esp-installer-bundles.md): signed with the existing vault only,
    # written to an empty OutputDirectory with its own signed manifest.
    [Parameter(Mandatory = $true, ParameterSetName = 'InstallerBundle')][switch]$InstallerBundle,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-fA-F]{40}$')][string]$GitSha,
    [ValidateSet('zkt', 'hikvision')][string]$FirmwareFamily = 'zkt',
    [ValidateSet('factory', 'usb-wifi-rescue', 'usb-wifi-rescue-seed')][string]$ImageRole = 'factory',
    # A published AVAILABLE release directory in the ADD firmware store. Its
    # exact signed application replaces this build's, so a new ESP boots the
    # production bytes ADD already verifies (flashed to factory and ota_0).
    [string]$ProductionReleaseDirectory = '',
    # An older published AVAILABLE release, flashed to ota_1. 2.7.x and 2.8.0
    # direct upgrade builds start storage only when the other OTA slot holds a
    # qualified predecessor (exact application digest) as their rollback image.
    [string]$RollbackPredecessorDirectory = ''
)

$ErrorActionPreference = 'Stop'
$GitSha = $GitSha.ToLowerInvariant()
# The empty SPIFFS image of the 8 MiB storage partition. Zone Lite never
# formats storage, so a blank partition would never mount.
$storagePartitionSize = 0x800000
# The exact empty SPIFFS bytes bench-verified on new boards on 29 Oct 2026
# (add-ci.yml pins them too); installer bundles carry no other storage image.
$emptyStorageSha256 = 'f39db7c1c7626ee6156dbab5da7912968040a489d1d927c1fb288d1f42c5ec70'

function Get-AvailableReleaseImage {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    $releaseDirectory = (Resolve-Path -LiteralPath $Directory).Path
    foreach ($marker in @('.hil-only.json', '.diagnostic-only.json')) {
        if (Test-Path -LiteralPath (Join-Path $releaseDirectory $marker)) {
            throw "$Purpose $releaseDirectory is not AVAILABLE ($marker)"
        }
    }
    $releaseManifest = Get-Content -LiteralPath (Join-Path $releaseDirectory 'manifest.json') -Raw | ConvertFrom-Json
    if (-not (Test-Path -LiteralPath (Join-Path $releaseDirectory 'manifest.sig') -PathType Leaf)) {
        throw "$Purpose is missing its signed manifest"
    }
    $releaseFamily = if ($releaseManifest.firmware_family) { [string]$releaseManifest.firmware_family } else { 'zkt' }
    if ($releaseFamily -ne $FirmwareFamily -or [string]$releaseManifest.partition_layout -ne 'zone-lite-ota-v1' -or
        [string]$releaseManifest.release_channel -eq 'EXPERIMENTAL_HIL_ONLY') {
        throw "$Purpose family, layout or channel is not a factory production image"
    }
    if ([string]$releaseManifest.version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
        throw "$Purpose version is not an exact release version"
    }
    $image = Join-Path $releaseDirectory ([string]$releaseManifest.image_name)
    $hash = (Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne [string]$releaseManifest.image_sha256 -or
        (Get-Item -LiteralPath $image).Length -ne [int64]$releaseManifest.image_size) {
        throw "$Purpose image does not match its signed manifest"
    }
    return @{
        Directory = $releaseDirectory
        Image = $image
        Version = [version][string]$releaseManifest.version
        Manifest = $releaseManifest
        Record = [ordered]@{
            release_id = [string]$releaseManifest.release_id
            version = [string]$releaseManifest.version
            git_sha = [string]$releaseManifest.git_sha
            image_sha256 = $hash
            application_sha256 = [string]$releaseManifest.application_sha256
        }
    }
}

function Invoke-InstallerPython {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Failure
    )
    $preference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 turns native stderr into ErrorRecords; keep
        # the tool's own refusal as the reason.
        $ErrorActionPreference = 'Continue'
        $lines = @(& python @Arguments 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ })
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $preference }
    if ($exitCode -ne 0) { throw "$Failure`: $($lines | Select-Object -Last 1)" }
    return [string]($lines | Select-Object -Last 1)
}

function Assert-InstallerSigningVault {
    # Installer bundles reuse the fleet keys and never make new ones: a board
    # whose bootloader trusts other keys could never boot ADD's releases.
    $existingVault = Join-Path $VaultDirectory 'vault-manifest.json'
    if (-not (Test-Path -LiteralPath $existingVault -PathType Leaf)) { throw 'An installer bundle cannot create or replace signing keys' }
    $existingManifest = Get-Content -LiteralPath $existingVault -Raw | ConvertFrom-Json
    $activeKey = @($existingManifest.keys | Where-Object { $_.state -eq 'ACTIVE' })
    if (@($existingManifest.keys).Count -ne 3 -or $activeKey.Count -ne 1 -or $activeKey[0].number -ne 1) {
        throw 'Expected existing active bootstrap signer is unavailable'
    }
    foreach ($number in 1..3) {
        foreach ($name in @("key-$number.dpapi", "key-$number.entropy", "key-$number-public.pem")) {
            if (-not (Test-Path -LiteralPath (Join-Path $VaultDirectory $name) -PathType Leaf)) {
                throw 'Existing installer bundle signing vault is incomplete'
            }
        }
    }
    $activePublicKey = (Get-FileHash -LiteralPath (Join-Path $VaultDirectory 'key-1-public.pem') -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($activePublicKey -ne [string]$activeKey[0].key_id) {
        throw 'Vault key 1 public key does not match its recorded key ID'
    }
}

if ($InstallerBundle) {
    # Every installer refusal happens before the vault, the output or any key
    # is touched.
    if ($ImageRole -ne 'factory') { throw 'An installer bundle carries only the factory role' }
    if ($FirmwareFamily -eq 'zkt' -and -not $ProductionReleaseDirectory) {
        throw 'A ZKT installer bundle boots an exact AVAILABLE production release from ota_0'
    }
    if ($FirmwareFamily -eq 'hikvision' -and ($ProductionReleaseDirectory -or $RollbackPredecessorDirectory)) {
        throw 'A Hikvision installer bundle boots its exact-source build from factory and carries no production release or rollback predecessor'
    }
    if ((Test-Path -LiteralPath $OutputDirectory) -and @(Get-ChildItem -LiteralPath $OutputDirectory -Force).Count) {
        throw 'Installer bundle output directory must be new or empty'
    }
    Assert-InstallerSigningVault
}

$productionRelease = $null
$rollbackPredecessor = $null
if ($RollbackPredecessorDirectory -and -not $ProductionReleaseDirectory) {
    throw 'A rollback predecessor is carried only together with a production release'
}
if ($ProductionReleaseDirectory) {
    if ($ImageRole -ne 'factory') { throw 'Only a factory bootstrap can carry a production release' }
    if (-not $RollbackPredecessorDirectory) {
        throw 'A production release bootstrap must carry a rollback predecessor for ota_1; 2.7.x and 2.8.0 refuse storage without one'
    }
    $production = Get-AvailableReleaseImage -Directory $ProductionReleaseDirectory -Purpose 'Production release'
    $predecessor = Get-AvailableReleaseImage -Directory $RollbackPredecessorDirectory -Purpose 'Rollback predecessor'
    if ($predecessor.Version -ge $production.Version) {
        throw "Rollback predecessor $($predecessor.Version) must be older than production release $($production.Version)"
    }
    $productionImage = $production.Image
    $productionRelease = $production.Record
    $productionRelease.boot_slot = 'ota_0'
    $rollbackPredecessorImage = $predecessor.Image
    $rollbackPredecessor = $predecessor.Record
    $rollbackPredecessor.slot = 'ota_1'
    $storageSource = Join-Path $UnsignedDirectory 'storage-empty.bin'
    if (-not (Test-Path -LiteralPath $storageSource -PathType Leaf)) {
        throw 'Unsigned firmware package is missing storage-empty.bin, the empty SPIFFS image a production bootstrap writes to storage'
    }
    if ((Get-Item -LiteralPath $storageSource).Length -ne $storagePartitionSize) {
        throw 'Empty storage image does not cover the 8 MiB storage partition'
    }
}
if ($InstallerBundle) {
    $storageSource = Join-Path $UnsignedDirectory 'storage-empty.bin'
    if (-not (Test-Path -LiteralPath $storageSource -PathType Leaf)) {
        throw 'Unsigned firmware package is missing storage-empty.bin, the empty SPIFFS image a new board writes to storage'
    }
    if ((Get-FileHash -LiteralPath $storageSource -Algorithm SHA256).Hash.ToLowerInvariant() -ne $emptyStorageSha256) {
        throw 'Empty storage image is not the bench-verified empty SPIFFS of the 8 MiB storage partition'
    }
    if ($productionRelease) {
        # A production release starts storage only beside a predecessor it
        # qualifies; its signed manifest lists those exact application digests.
        $qualified = $production.Manifest.queue_storage.allowed_bootstrap_images
        $qualifiedDigest = if ($qualified) { $qualified.PSObject.Properties[$rollbackPredecessor.version] } else { $null }
        if (-not $qualifiedDigest -or [string]$qualifiedDigest.Value -ne $rollbackPredecessor.application_sha256) {
            throw "Production release $($productionRelease.version) does not qualify $($rollbackPredecessor.version) as its ota_1 rollback image"
        }
        foreach ($release in @($production, $predecessor)) {
            Invoke-InstallerPython -Failure 'Release application identity does not match its signed manifest' -Arguments @(
                (Join-Path $PSScriptRoot '../../scripts/build_installer_bundle_manifest.py'), 'identity',
                '--image', $release.Image, '--family', 'zkt', '--version', $release.Record.version,
                '--app-digest', $release.Record.application_sha256
            ) | Out-Null
        }
        # Every installed app refuses an ota_1 image outside its own
        # QUALIFIED_PREDECESSORS, whatever this release qualifies.
        Invoke-InstallerPython -Failure 'Every installed app would refuse this bundle' -Arguments @(
            (Join-Path $PSScriptRoot '../../scripts/build_installer_bundle_manifest.py'), 'predecessor',
            '--version', $rollbackPredecessor.version, '--app-digest', $rollbackPredecessor.application_sha256
        ) | Out-Null
    }
}
Add-Type -AssemblyName System.Security

function Invoke-DockerText {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = @(& docker @Arguments)
    if ($LASTEXITCODE -ne 0) { throw "Docker operation $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
    return ($output -join "`n") + "`n"
}

function Protect-Key {
    param([Parameter(Mandatory = $true)][byte[]]$Plaintext)
    $entropy = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($entropy)
    $ciphertext = [Security.Cryptography.ProtectedData]::Protect(
        $Plaintext,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    return @{ Ciphertext = $ciphertext; Entropy = $entropy }
}

function Unprotect-Key {
    param([Parameter(Mandatory = $true)][int]$Number)
    $ciphertext = [IO.File]::ReadAllBytes((Join-Path $VaultDirectory "key-$Number.dpapi"))
    $entropy = [IO.File]::ReadAllBytes((Join-Path $VaultDirectory "key-$Number.entropy"))
    return [Security.Cryptography.ProtectedData]::Unprotect(
        $ciphertext,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
}

function Clear-PlaintextFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $length = (Get-Item -LiteralPath $Path).Length
    if ($length -gt 0) {
        $random = New-Object byte[] $length
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($random)
        [IO.File]::WriteAllBytes($Path, $random)
    }
    Remove-Item -LiteralPath $Path -Force
}

$unsigned = (Resolve-Path $UnsignedDirectory).Path
if ($ImageRole -eq 'factory') {
    & python scripts/check_firmware_identity.py (Join-Path $unsigned 'zone_lite.bin') --family $FirmwareFamily
    if ($LASTEXITCODE -ne 0) { throw 'Factory application identity does not match firmware family' }
    if ($productionRelease) {
        # ADD records a revocation only in its database; the store stays
        # unmarked. Before the vault is touched, deployed ADD must still offer
        # both releases as AVAILABLE and unrevoked, with these exact bytes.
        Invoke-InstallerPython -Failure 'A revoked or unpublished release is never signed' -Arguments @(
            (Join-Path $PSScriptRoot '../../scripts/build_installer_bundle_manifest.py'), 'add-available',
            '--require-all', $production.Directory, $predecessor.Directory
        ) | Out-Null
    }
} else {
    if ($FirmwareFamily -ne 'zkt' -or $DeviceMac.ToLowerInvariant() -ne '14:c1:9f:46:ae:1c' -or $env:ADD_FACTORY_VERSION) {
        throw 'USB rescue signing is HIL-only for the exact spare and cannot publish a factory bundle'
    }
    $existingVault = Join-Path $VaultDirectory 'vault-manifest.json'
    if (-not (Test-Path -LiteralPath $existingVault -PathType Leaf)) { throw 'USB rescue cannot create or replace signing keys' }
    $existingManifest = Get-Content -LiteralPath $existingVault -Raw | ConvertFrom-Json
    if (@($existingManifest.keys).Count -ne 3 -or
        @($existingManifest.keys | Where-Object { $_.number -eq 1 -and $_.state -eq 'ACTIVE' }).Count -ne 1) {
        throw 'Expected existing active bootstrap signer is unavailable'
    }
    foreach ($number in 1..3) {
        foreach ($name in @("key-$number.dpapi", "key-$number.entropy", "key-$number-public.pem")) {
            if (-not (Test-Path -LiteralPath (Join-Path $VaultDirectory $name) -PathType Leaf)) {
                throw 'Existing USB rescue signing vault is incomplete'
            }
        }
    }
    $validatorPython = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $validationPreference = $ErrorActionPreference
    $validationExit = -1
    try {
        # Windows PowerShell 5.1 turns native stderr into ErrorRecords. An
        # invalid artifact is an expected refusal, not an incidental traceback
        # that should skip the explicit before-key-access failure below.
        $ErrorActionPreference = 'Continue'
        & $validatorPython scripts/usb_wifi_rescue_artifacts.py $unsigned --role $ImageRole 2>&1 | Out-Null
        $validationExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $validationPreference }
    if ($validationExit -ne 0) { throw 'USB rescue artifact validation failed before key access' }
}
foreach ($required in @('bootloader.bin', 'zone_lite.bin', 'partition-table.bin', 'ota_data_initial.bin', 'flasher_args.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $unsigned $required) -PathType Leaf)) {
        throw "Unsigned firmware package is missing $required"
    }
}

New-Item -ItemType Directory -Path $VaultDirectory -Force | Out-Null
$VaultDirectory = (Resolve-Path $VaultDirectory).Path
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $VaultDirectory /inheritance:r | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not disable inherited firmware-vault ACLs' }
& icacls $VaultDirectory /grant:r "${identity}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' 'BUILTIN\Administrators:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not restrict firmware-vault ACLs' }

$vaultManifest = Join-Path $VaultDirectory 'vault-manifest.json'
if (-not (Test-Path -LiteralPath $vaultManifest -PathType Leaf)) {
    if ($ImageRole -ne 'factory') { throw 'USB rescue must never generate a replacement vault' }
    if ($InstallerBundle) { throw 'An installer bundle must never generate a replacement vault' }
    $staging = Join-Path $env:TEMP ('zone-lite-vault-staging-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging | Out-Null
    $container = 'zone-lite-keygen-' + [guid]::NewGuid().ToString('N')
    try {
        foreach ($number in 1..3) {
            $pem = Invoke-DockerText @('run', '--rm', 'espressif/idf:v5.5.3', 'openssl', 'genrsa', '3072')
            $plain = [Text.Encoding]::ASCII.GetBytes($pem)
            try {
                $protected = Protect-Key $plain
                [IO.File]::WriteAllBytes((Join-Path $staging "key-$number.dpapi"), $protected.Ciphertext)
                [IO.File]::WriteAllBytes((Join-Path $staging "key-$number.entropy"), $protected.Entropy)
                [IO.File]::WriteAllBytes((Join-Path $staging "key-$number.pem"), $plain)
            }
            finally {
                [Array]::Clear($plain, 0, $plain.Length)
            }
        }

        Invoke-DockerText @(
            'create', '--name', $container, 'espressif/idf:v5.5.3',
            'tail', '-f', '/dev/null'
        ) | Out-Null
        Invoke-DockerText @('start', $container) | Out-Null
        Invoke-DockerText @('exec', $container, '/bin/mkdir', '-p', '/keys') | Out-Null
        Invoke-DockerText @('cp', (Join-Path $staging '.'), "${container}:/keys") | Out-Null
        foreach ($number in 1..3) {
            Invoke-DockerText @(
                'exec', $container, '/usr/bin/openssl', 'pkey',
                '-in', "/keys/key-$number.pem",
                '-pubout', '-out', "/keys/key-$number-public.pem"
            ) | Out-Null
            Invoke-DockerText @(
                'cp', "${container}:/keys/key-$number-public.pem",
                (Join-Path $staging "key-$number-public.pem")
            ) | Out-Null
        }

        $keys = @()
        foreach ($number in 1..3) {
            $publicPath = Join-Path $staging "key-$number-public.pem"
            $keyId = (Get-FileHash -LiteralPath $publicPath -Algorithm SHA256).Hash.ToLowerInvariant()
            $keys += @{
                number = $number
                key_id = $keyId
                state = $(if ($number -eq 1) { 'ACTIVE' } else { 'RESERVE' })
            }
        }
        $manifest = @{
            schema_version = 1
            algorithm = 'RSA-3072'
            protection = 'Windows DPAPI CurrentUser'
            runner_identity = $identity
            created_at = [DateTime]::UtcNow.ToString('o')
            keys = $keys
        } | ConvertTo-Json -Depth 5

        foreach ($number in 1..3) {
            foreach ($suffix in @('dpapi', 'entropy')) {
                $source = Join-Path $staging "key-$number.$suffix"
                $destination = Join-Path $VaultDirectory "key-$number.$suffix"
                [IO.File]::WriteAllBytes($destination, [IO.File]::ReadAllBytes($source))
            }
            $publicSource = Join-Path $staging "key-$number-public.pem"
            $publicDestination = Join-Path $VaultDirectory "key-$number-public.pem"
            [IO.File]::WriteAllText(
                $publicDestination,
                [IO.File]::ReadAllText($publicSource),
                (New-Object Text.UTF8Encoding($false))
            )
        }
        [IO.File]::WriteAllText($vaultManifest, $manifest, (New-Object Text.UTF8Encoding($false)))
    }
    finally {
        & docker rm -f $container 2>$null | Out-Null
        foreach ($number in 1..3) {
            Clear-PlaintextFile (Join-Path $staging "key-$number.pem")
        }
        if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    }
}
foreach ($number in 1..3) {
    foreach ($suffix in @('dpapi', 'entropy')) {
        if (-not (Test-Path -LiteralPath (Join-Path $VaultDirectory "key-$number.$suffix") -PathType Leaf)) {
            throw "Firmware signing vault is incomplete for key $number"
        }
    }
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$output = (Resolve-Path $OutputDirectory).Path
$work = Join-Path $env:TEMP ('zone-lite-sign-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $unsigned 'bootloader.bin') -Destination (Join-Path $work 'bootloader.bin')
    Copy-Item -LiteralPath (Join-Path $unsigned 'zone_lite.bin') -Destination (Join-Path $work 'zone_lite.bin')
    foreach ($number in 1..3) {
        [IO.File]::WriteAllBytes((Join-Path $work "key-$number.pem"), (Unprotect-Key $number))
    }

    $container = 'zone-lite-sign-' + [guid]::NewGuid().ToString('N')
    try {
        Invoke-DockerText @(
            'create', '--name', $container, 'espressif/idf:v5.5.3',
            'tail', '-f', '/dev/null'
        ) | Out-Null
        Invoke-DockerText @('start', $container) | Out-Null
        Invoke-DockerText @('exec', $container, '/bin/mkdir', '-p', '/work') | Out-Null
        Invoke-DockerText @('cp', (Join-Path $work '.'), "${container}:/work") | Out-Null
        Invoke-DockerText @(
            'exec', $container, '/bin/bash', '-lc',
            '. /opt/esp/idf/export.sh >/dev/null 2>&1 && espsecure.py sign_data --version 2 --keyfile /work/key-1.pem /work/key-2.pem /work/key-3.pem --output /work/bootloader-signed.bin /work/bootloader.bin'
        ) | Out-Null
        Invoke-DockerText @(
            'exec', $container, '/bin/bash', '-lc',
            '. /opt/esp/idf/export.sh >/dev/null 2>&1 && espsecure.py sign_data --version 2 --keyfile /work/key-1.pem --output /work/zone-lite-signed.bin /work/zone_lite.bin'
        ) | Out-Null
        Invoke-DockerText @(
            'exec', $container, '/bin/bash', '-lc',
            '. /opt/esp/idf/export.sh >/dev/null 2>&1 && espsecure.py signature_info_v2 /work/bootloader-signed.bin'
        ) | Out-Null
        Invoke-DockerText @(
            'exec', $container, '/bin/bash', '-lc',
            '. /opt/esp/idf/export.sh >/dev/null 2>&1 && espsecure.py signature_info_v2 /work/zone-lite-signed.bin'
        ) | Out-Null
        Invoke-DockerText @(
            'cp', "${container}:/work/bootloader-signed.bin",
            (Join-Path $work 'bootloader-signed.bin')
        ) | Out-Null
        Invoke-DockerText @(
            'cp', "${container}:/work/zone-lite-signed.bin",
            (Join-Path $work 'zone-lite-signed.bin')
        ) | Out-Null
    }
    finally {
        & docker rm -f $container 2>$null | Out-Null
    }

    if ($InstallerBundle) {
        # The bundle carries no MAC and none of the per-device package files
        # below: its images and a canonical manifest signed by vault key 1.
        $bundleArguments = @(
            (Join-Path $PSScriptRoot '../../scripts/build_installer_bundle_manifest.py'), 'build',
            '--output', $output, '--family', $FirmwareFamily, '--git-sha', $GitSha,
            '--bootloader', (Join-Path $work 'bootloader-signed.bin'),
            '--partition-table', (Join-Path $unsigned 'partition-table.bin'),
            '--storage', (Join-Path $unsigned 'storage-empty.bin')
        )
        if ($productionRelease) {
            # The exact published bytes, as ADD verifies them, not this build's.
            $bundleArguments += @(
                '--factory', $productionImage,
                '--factory-version', $productionRelease.version,
                '--factory-app-digest', $productionRelease.application_sha256,
                '--predecessor', $rollbackPredecessorImage,
                '--predecessor-version', $rollbackPredecessor.version,
                '--predecessor-app-digest', $rollbackPredecessor.application_sha256
            )
        }
        else {
            $bundleArguments += @('--factory', (Join-Path $work 'zone-lite-signed.bin'))
        }
        $bundleId = Invoke-InstallerPython -Arguments $bundleArguments -Failure 'Installer bundle manifest generation failed'
        $signatureBinary = Join-Path $work 'manifest.sig.bin'
        $manifestContainer = 'zone-lite-installer-sign-' + [guid]::NewGuid().ToString('N')
        try {
            # Same isolated-container signing as the factory manifest below,
            # then an independent check against the vault's public key.
            Invoke-DockerText @(
                'create', '--name', $manifestContainer, 'espressif/idf:v5.5.3',
                'tail', '-f', '/dev/null'
            ) | Out-Null
            Invoke-DockerText @('start', $manifestContainer) | Out-Null
            Invoke-DockerText @('exec', $manifestContainer, '/bin/mkdir', '-p', '/bundle', '/work') | Out-Null
            Invoke-DockerText @(
                'cp', (Join-Path $output 'manifest.json'),
                "${manifestContainer}:/bundle/manifest.json"
            ) | Out-Null
            Invoke-DockerText @(
                'cp', (Join-Path $work 'key-1.pem'),
                "${manifestContainer}:/work/key-1.pem"
            ) | Out-Null
            Invoke-DockerText @(
                'cp', (Join-Path $VaultDirectory 'key-1-public.pem'),
                "${manifestContainer}:/work/key-1-public.pem"
            ) | Out-Null
            Invoke-DockerText @(
                'exec', $manifestContainer,
                'openssl', 'dgst', '-sha256',
                '-sigopt', 'rsa_padding_mode:pss',
                '-sigopt', 'rsa_pss_saltlen:32',
                '-sign', '/work/key-1.pem',
                '-out', '/bundle/manifest.sig.bin',
                '/bundle/manifest.json'
            ) | Out-Null
            Invoke-DockerText @(
                'exec', $manifestContainer,
                'openssl', 'dgst', '-sha256',
                '-sigopt', 'rsa_padding_mode:pss',
                '-sigopt', 'rsa_pss_saltlen:32',
                '-verify', '/work/key-1-public.pem',
                '-signature', '/bundle/manifest.sig.bin',
                '/bundle/manifest.json'
            ) | Out-Null
            Invoke-DockerText @(
                'cp', "${manifestContainer}:/bundle/manifest.sig.bin",
                $signatureBinary
            ) | Out-Null
        }
        finally {
            & docker rm -f $manifestContainer 2>$null | Out-Null
        }
        [IO.File]::WriteAllText(
            (Join-Path $output 'manifest.sig'),
            [Convert]::ToBase64String([IO.File]::ReadAllBytes($signatureBinary)),
            (New-Object Text.UTF8Encoding($false))
        )
        Write-Host "Created signed installer bundle $bundleId; private keys remain DPAPI-protected in $VaultDirectory"
        # The finally block below still clears the plaintext keys.
        return
    }

    Copy-Item -LiteralPath (Join-Path $work 'bootloader-signed.bin') -Destination (Join-Path $output 'bootloader-signed.bin')
    if ($productionRelease) {
        Copy-Item -LiteralPath $productionImage -Destination (Join-Path $output 'zone-lite-signed.bin')
        Copy-Item -LiteralPath $rollbackPredecessorImage -Destination (Join-Path $output 'rollback-predecessor.bin')
        Copy-Item -LiteralPath (Join-Path $unsigned 'storage-empty.bin') -Destination (Join-Path $output 'storage-empty.bin')
    }
    else {
        Copy-Item -LiteralPath (Join-Path $work 'zone-lite-signed.bin') -Destination (Join-Path $output 'zone-lite-signed.bin')
    }
    foreach ($name in @('partition-table.bin', 'ota_data_initial.bin', 'flasher_args.json')) {
        Copy-Item -LiteralPath (Join-Path $unsigned $name) -Destination (Join-Path $output $name)
    }
    foreach ($number in 1..3) {
        Copy-Item -LiteralPath (Join-Path $VaultDirectory "key-$number-public.pem") -Destination (Join-Path $output "key-$number-public.pem")
    }
    Copy-Item -LiteralPath $vaultManifest -Destination (Join-Path $output 'vault-manifest.json')
    $packageFields = @{
        schema_version = 1
        firmware_family = $FirmwareFamily
        target_mac = $DeviceMac.ToLowerInvariant()
        git_sha = $GitSha
        created_at = [DateTime]::UtcNow.ToString('o')
        bootloader_sha256 = (Get-FileHash (Join-Path $output 'bootloader-signed.bin') -Algorithm SHA256).Hash.ToLowerInvariant()
        application_sha256 = (Get-FileHash (Join-Path $output 'zone-lite-signed.bin') -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    if ($productionRelease) {
        if ($packageFields.application_sha256 -ne $productionRelease.image_sha256) {
            throw 'Bootstrap application is not the exact production release image'
        }
        $predecessorHash = (Get-FileHash (Join-Path $output 'rollback-predecessor.bin') -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($predecessorHash -ne $rollbackPredecessor.image_sha256) {
            throw 'Bootstrap ota_1 image is not the exact rollback predecessor image'
        }
        $storageImage = Get-Item -LiteralPath (Join-Path $output 'storage-empty.bin')
        $packageFields.production_release = $productionRelease
        $packageFields.rollback_predecessor = $rollbackPredecessor
        $packageFields.storage_image_sha256 = (Get-FileHash $storageImage.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $packageFields.storage_image_size = $storageImage.Length
    }
    if ($ImageRole -ne 'factory') {
        $packageFields.image_role = $ImageRole
        $packageFields.hil_only = $true
        $packageFields.factory_release_eligible = $false
        [IO.File]::WriteAllText((Join-Path $output '.hil-only.json'), '{"state":"HIL_ONLY","purpose":"USB_WIFI_RESCUE"}', (New-Object Text.UTF8Encoding($false)))
    }
    $package = $packageFields | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText((Join-Path $output 'bootstrap-manifest.json'), $package, (New-Object Text.UTF8Encoding($false)))
    if ($env:ADD_FACTORY_VERSION) {
        & python scripts/build_factory_manifest.py `
            --bundle $output `
            --version $env:ADD_FACTORY_VERSION `
            --git-sha $GitSha `
            --firmware-family $FirmwareFamily `
            --vault-manifest (Join-Path $output 'vault-manifest.json')
        if ($LASTEXITCODE -ne 0) { throw 'Factory manifest generation failed' }
        $signatureBinary = Join-Path $output 'manifest.sig.bin'
        $manifestContainer = 'zone-lite-manifest-sign-' + [guid]::NewGuid().ToString('N')
        try {
            # The self-hosted runner executes as NETWORK SERVICE. Docker Desktop's
            # daemon cannot bind-mount that identity's private TEMP directory, so
            # copy the two inputs into an isolated container just as the image
            # signing path above does. This keeps the plaintext key out of a
            # daemon-readable host mount and works with the runner's protected ACL.
            Invoke-DockerText @(
                'create', '--name', $manifestContainer, 'espressif/idf:v5.5.3',
                'tail', '-f', '/dev/null'
            ) | Out-Null
            Invoke-DockerText @('start', $manifestContainer) | Out-Null
            Invoke-DockerText @('exec', $manifestContainer, '/bin/mkdir', '-p', '/bundle', '/work') | Out-Null
            Invoke-DockerText @(
                'cp', (Join-Path $output 'manifest.json'),
                "${manifestContainer}:/bundle/manifest.json"
            ) | Out-Null
            Invoke-DockerText @(
                'cp', (Join-Path $work 'key-1.pem'),
                "${manifestContainer}:/work/key-1.pem"
            ) | Out-Null
            Invoke-DockerText @(
                'exec', $manifestContainer,
                'openssl', 'dgst', '-sha256',
                '-sigopt', 'rsa_padding_mode:pss',
                '-sigopt', 'rsa_pss_saltlen:32',
                '-sign', '/work/key-1.pem',
                '-out', '/bundle/manifest.sig.bin',
                '/bundle/manifest.json'
            ) | Out-Null
            Invoke-DockerText @(
                'cp', "${manifestContainer}:/bundle/manifest.sig.bin",
                $signatureBinary
            ) | Out-Null
        }
        finally {
            & docker rm -f $manifestContainer 2>$null | Out-Null
        }
        $encodedSignature = [Convert]::ToBase64String([IO.File]::ReadAllBytes($signatureBinary))
        [IO.File]::WriteAllText(
            (Join-Path $output 'manifest.sig'),
            $encodedSignature,
            (New-Object Text.UTF8Encoding($false))
        )
        Remove-Item -LiteralPath $signatureBinary -Force
    }
}
finally {
    foreach ($number in 1..3) { Clear-PlaintextFile (Join-Path $work "key-$number.pem") }
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
}

Write-Host "Created signed bootstrap package for $DeviceMac; private keys remain DPAPI-protected in $VaultDirectory"
