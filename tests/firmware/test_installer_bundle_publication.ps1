# Installer bundles without Docker, a real vault or a board. The signer's
# installer mode must refuse every bad claim before it touches the vault, the
# output or a key, including a predecessor the installer app refuses and a
# release deployed ADD no longer offers. Synthetic signed images then become
# ZKT and Hikvision bundles signed by throwaway RSA-3072 keys (openssl, as the
# vault signs, when available, and .NET), and the publisher must verify them,
# pin the key whatever line endings its PEM has, publish them atomically and
# never overwrite a published bundle ID.
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$work = Join-Path ([IO.Path]::GetTempPath()) ('installer-bundle-test-' + [guid]::NewGuid().ToString('N'))
$signer = Join-Path $root 'deploy/add/bootstrap-firmware-signing.ps1'
$publisher = Join-Path $root 'deploy/add/publish-installer-bundle.ps1'
$bundleTool = Join-Path $root 'scripts/build_installer_bundle_manifest.py'
$images = Join-Path $work 'images'
$unsigned = Join-Path $work 'unsigned'
$vault = Join-Path $work 'vault'
$signerOutput = Join-Path $work 'signer-output'
$store = Join-Path $work 'installer-bundles'
$utf8 = New-Object Text.UTF8Encoding($false)
$originalStore = $env:ADD_INSTALLER_BUNDLE_STORE_HOST_PATH
$openssl = (Get-Command openssl -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source

$fixtures = @'
"""Synthetic signed ESP images and AVAILABLE release entries; no real key."""
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

work = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
import build_installer_bundle_manifest as bundles  # noqa: E402


def esp_image(segment):
    header = struct.pack("<BBBBI", 0xE9, 1, 2, 0x4F, 0x40380000)
    header += struct.pack("<B3sHBHH4sB", 0xEE, b"\0\0\0", 9, 0, 0, 0xFFFF, b"\0" * 4, 1)
    image = header + struct.pack("<II", 0x3C000020, len(segment)) + segment
    image += b"\0" * (15 - len(image) % 16)
    checksum = 0xEF
    for byte in segment:
        checksum ^= byte
    image += bytes([checksum])
    return image + hashlib.sha256(image).digest()


def signed(image, keys):
    # espsecure sign_data --version 2 layout with fake key material.
    image += b"\xff" * (-len(image) % 4096)
    digest = hashlib.sha256(image).digest()
    sector = b""
    for key in range(keys):
        block = struct.pack("<BBxx32s", 0xE7, 0x02, digest) + bytes([0x41 + key]) * 1160
        sector += block + struct.pack("<I", zlib.crc32(block)) + b"\0" * 16
    return image + sector + b"\xff" * (4096 - len(sector))


def application(project, version):
    descriptor = struct.pack("<II8x", 0xABCD5432, 0)
    descriptor += version.encode().ljust(32, b"\0") + project.encode().ljust(32, b"\0")
    return signed(esp_image(descriptor + hashlib.sha256(version.encode()).digest() * 8), 1)


def empty_storage():
    # spiffsgen.py's empty 8 MiB SPIFFS: one magic per 4 KiB block.
    blocks = bundles.STORAGE_SIZE // 4096
    return b"".join(
        b"\xff" * 252 + struct.pack("<H", (0x20140529 ^ 256 ^ (blocks - index)) & 0x1FFFF)
        + b"\xff" * 3842 for index in range(blocks))


files = {
    "bootloader-signed.bin": signed(esp_image(b"synthetic bootloader".ljust(64, b"\0")), 3),
    "partition-table.bin": bundles.production_partition_table(),
    "storage-empty.bin": empty_storage(),
    "zkt-2.7.8.bin": application("zone_lite", "2.7.8"),
    "zkt-2.7.7.bin": application("zone_lite", "2.7.7"),
    "zkt-2.5.2.bin": application("zone_lite", "2.5.2"),
    "hik-3.1.1.bin": application("zone_lite_hikvision", "3.1.1"),
}
assert hashlib.sha256(files["storage-empty.bin"]).hexdigest() == bundles.EMPTY_STORAGE_SHA256
(work / "images").mkdir()
for name, data in files.items():
    (work / "images" / name).write_bytes(data)
digests = {name: bundles.app_identity(data)[2] for name, data in files.items()
           if name.startswith(("zkt-", "hik-"))}


def release(store, version, image, qualifies, application_sha256=None):
    directory = work / store / version
    directory.mkdir(parents=True)
    name = f"zone-lite-{version}.bin"
    (directory / name).write_bytes(files[image])
    manifest = {
        "release_id": f"zone-lite-{version}", "version": version, "git_sha": "b" * 40,
        "firmware_family": "zkt", "partition_layout": "zone-lite-ota-v1",
        "release_channel": "STABLE", "image_name": name,
        "image_sha256": hashlib.sha256(files[image]).hexdigest(),
        "image_size": len(files[image]),
        "application_sha256": application_sha256 or digests[image],
        "queue_storage": {"allowed_bootstrap_images": qualifies},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    (directory / "manifest.sig").write_text("synthetic fixture; not a signature")


qualified = {"2.5.2": digests["zkt-2.5.2.bin"]}
release("store", "2.7.8", "zkt-2.7.8.bin", qualified)
release("store", "2.5.2", "zkt-2.5.2.bin", {})
release("store-unqualified", "2.7.8", "zkt-2.7.8.bin", {"2.4.12": "c" * 64})
release("store-other-digest", "2.7.8", "zkt-2.7.8.bin", {"2.5.2": "d" * 64})
release("store-mislabelled", "2.7.8", "zkt-2.7.7.bin", qualified)
release("store-wrong-digest", "2.7.8", "zkt-2.7.8.bin", qualified, "e" * 64)
print(json.dumps(digests))
'@

function Invoke-Native {
    param([string]$Command, [string[]]$Arguments)
    $preference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 turns native stderr into ErrorRecords.
        $ErrorActionPreference = 'Continue'
        $lines = @(& $Command @Arguments 2>&1 | ForEach-Object { "$_" })
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $preference }
    if ($exitCode -ne 0) { throw "$([IO.Path]::GetFileName($Command)) failed: $($lines -join ' ')" }
    return $lines
}

function Get-TreeState {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return 'absent' }
    # Children carry the root's own path form (8.3 names on Windows runners).
    $base = (Get-Item -LiteralPath $Path).FullName.TrimEnd('\', '/')
    return (@(Get-ChildItem -LiteralPath $Path -Recurse -Force | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($base.Length)
        if ($_.PSIsContainer) { $relative } else { $relative + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    }) -join ';')
}

function Merge-Arguments {
    param([hashtable]$Base, [hashtable]$Change)
    $merged = @{}
    foreach ($key in $Base.Keys) { $merged[$key] = $Base[$key] }
    foreach ($key in $Change.Keys) { $merged[$key] = $Change[$key] }
    return $merged
}

function New-VaultFixture {
    param([string]$Directory, [scriptblock]$Change = $null)
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    foreach ($number in 1..3) {
        foreach ($suffix in @('dpapi', 'entropy')) {
            [IO.File]::WriteAllText((Join-Path $Directory "key-$number.$suffix"), 'synthetic fixture; not a key', $utf8)
        }
        [IO.File]::WriteAllText((Join-Path $Directory "key-$number-public.pem"), "synthetic public key $number`n", $utf8)
    }
    $keys = @(foreach ($number in 1..3) {
        [ordered]@{
            number = $number
            key_id = (Get-FileHash -LiteralPath (Join-Path $Directory "key-$number-public.pem") -Algorithm SHA256).Hash.ToLowerInvariant()
            state = $(if ($number -eq 1) { 'ACTIVE' } else { 'RESERVE' })
        }
    })
    if ($Change) { & $Change $keys $Directory }
    $manifest = [ordered]@{ schema_version = 1; algorithm = 'RSA-3072'; keys = $keys }
    [IO.File]::WriteAllText((Join-Path $Directory 'vault-manifest.json'), ($manifest | ConvertTo-Json -Depth 5), $utf8)
    return $Directory
}

function Invoke-InstallerSigner {
    param([hashtable]$Arguments, [string]$Vault)
    $parameters = @{
        VaultDirectory = $Vault
        UnsignedDirectory = $unsigned
        OutputDirectory = $signerOutput
        InstallerBundle = $true
        GitSha = ('a' * 40)
    }
    foreach ($key in $Arguments.Keys) { $parameters[$key] = $Arguments[$key] }
    $message = ''
    try { & $signer @parameters | Out-Null } catch { $message = $_.Exception.Message }
    return $message
}

function Assert-SignerRefusedBeforeKeyAccess {
    param([string]$Case, [hashtable]$Arguments, [string]$Pattern, [string]$Vault = $vault)
    $vaultBefore = Get-TreeState $Vault
    $outputBefore = Get-TreeState $signerOutput
    $message = Invoke-InstallerSigner $Arguments $Vault
    if ($message -notmatch $Pattern) { throw "$Case was not refused as expected; got '$message'" }
    if ((Get-TreeState $Vault) -ne $vaultBefore -or (Get-TreeState $signerOutput) -ne $outputBefore) {
        throw "$Case touched the vault or the bundle output before refusing"
    }
}

function Set-UnsignedStorage {
    param([byte[]]$Bytes)
    [IO.File]::WriteAllBytes((Join-Path $unsigned 'storage-empty.bin'), $Bytes)
}

function ConvertTo-DerElement {
    param([int]$Tag, [byte[]]$Content)
    $length = $Content.Length
    $header = if ($length -lt 0x80) { @($Tag, $length) } elseif ($length -lt 0x100) { @($Tag, 0x81, $length) } else { @($Tag, 0x82, ($length -shr 8), ($length -band 0xFF)) }
    return [byte[]]($header + $Content)
}

function New-SigningKey {
    param([string]$Directory, [switch]$DotNet)
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    # Spki: SHA-256 of the DER SubjectPublicKeyInfo, the pin installers use.
    $key = @{ Public = (Join-Path $Directory 'public.pem'); Private = (Join-Path $Directory 'private.pem'); Rsa = $null; Spki = '' }
    if ($openssl -and -not $DotNet) {
        Invoke-Native $openssl @('genrsa', '-out', $key.Private, '3072') | Out-Null
        Invoke-Native $openssl @('pkey', '-in', $key.Private, '-pubout', '-out', $key.Public) | Out-Null
        $der = Join-Path $Directory 'public.der'
        Invoke-Native $openssl @('pkey', '-pubin', '-in', $key.Public, '-outform', 'DER', '-out', $der) | Out-Null
        $key.Spki = (Get-FileHash -LiteralPath $der -Algorithm SHA256).Hash.ToLowerInvariant()
        return $key
    }
    $key.Rsa = if ($PSVersionTable.PSEdition -eq 'Core') { [Security.Cryptography.RSA]::Create(3072) } else { New-Object Security.Cryptography.RSACng 3072 }
    $parameters = $key.Rsa.ExportParameters($false)
    $modulus = [byte[]]$parameters.Modulus
    if ($modulus[0] -band 0x80) { $modulus = [byte[]](@([byte]0) + $modulus) }
    $rsaKey = ConvertTo-DerElement 0x30 ([byte[]]((ConvertTo-DerElement 0x02 $modulus) + (ConvertTo-DerElement 0x02 ([byte[]]$parameters.Exponent))))
    $algorithm = [byte[]](0x30, 0x0D, 0x06, 0x09, 0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x01, 0x01, 0x05, 0x00)
    $info = ConvertTo-DerElement 0x30 ([byte[]]($algorithm + (ConvertTo-DerElement 0x03 ([byte[]](@([byte]0) + $rsaKey)))))
    $pem = "-----BEGIN PUBLIC KEY-----`n" + ([Convert]::ToBase64String($info) -replace '(.{64})', "`$1`n").TrimEnd("`n") + "`n-----END PUBLIC KEY-----`n"
    [IO.File]::WriteAllText($key.Public, $pem, $utf8)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { $key.Spki = [BitConverter]::ToString($sha256.ComputeHash($info)).Replace('-', '').ToLowerInvariant() }
    finally { $sha256.Dispose() }
    return $key
}

function Set-BundleSignature {
    param([string]$Bundle, [hashtable]$Key)
    $manifest = Join-Path $Bundle 'manifest.json'
    if ($Key.Rsa) {
        $signature = $Key.Rsa.SignData(
            [IO.File]::ReadAllBytes($manifest),
            [Security.Cryptography.HashAlgorithmName]::SHA256,
            [Security.Cryptography.RSASignaturePadding]::Pss
        )
    } else {
        # The vault's exact command (bootstrap-firmware-signing.ps1).
        $binary = Join-Path $work ('manifest-' + [guid]::NewGuid().ToString('N') + '.sig.bin')
        Invoke-Native $openssl @('dgst', '-sha256', '-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32',
            '-sign', $Key.Private, '-out', $binary, $manifest) | Out-Null
        $signature = [IO.File]::ReadAllBytes($binary)
    }
    [IO.File]::WriteAllText((Join-Path $Bundle 'manifest.sig'), [Convert]::ToBase64String($signature), $utf8)
}

function Invoke-Publisher {
    param([hashtable]$Arguments)
    $result = @{ Id = $null; Message = '' }
    try { $result.Id = & $publisher @Arguments } catch { $result.Message = $_.Exception.Message }
    return $result
}

function Copy-Bundle {
    param([string]$Source, [string]$Name)
    $copy = Join-Path $work $Name
    Copy-Item -LiteralPath $Source -Destination $copy -Recurse
    return $copy
}

function Assert-PublisherRefused {
    param([string]$Case, [hashtable]$Arguments, [string]$Pattern)
    $before = Get-TreeState $store
    $result = Invoke-Publisher $Arguments
    if ($result.Message -notmatch $Pattern) { throw "$Case was not refused as expected; got '$($result.Message)'" }
    if ((Get-TreeState $store) -ne $before) { throw "$Case changed the installer bundle store" }
}

New-Item -ItemType Directory -Path $work -Force | Out-Null
Push-Location $root
try {
    $fixtureScript = Join-Path $work 'fixtures.py'
    [IO.File]::WriteAllText($fixtureScript, $fixtures, $utf8)
    $digests = (Invoke-Native 'python' @($fixtureScript, $work, (Join-Path $root 'scripts')) | Select-Object -Last 1) | ConvertFrom-Json

    # The signer's flat package; the synthetic zone_lite.bin fails the
    # existing identity check, so a claim that passes every installer check
    # still stops before the vault or any key is touched.
    New-Item -ItemType Directory -Path $unsigned -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $images 'bootloader-signed.bin') -Destination (Join-Path $unsigned 'bootloader.bin')
    Copy-Item -LiteralPath (Join-Path $images 'partition-table.bin') -Destination (Join-Path $unsigned 'partition-table.bin')
    Copy-Item -LiteralPath (Join-Path $images 'storage-empty.bin') -Destination (Join-Path $unsigned 'storage-empty.bin')
    [IO.File]::WriteAllBytes((Join-Path $unsigned 'ota_data_initial.bin'), [byte[]](@(0xFF) * 0x2000))
    [IO.File]::WriteAllText((Join-Path $unsigned 'flasher_args.json'), '{}', $utf8)
    [IO.File]::WriteAllBytes((Join-Path $unsigned 'zone_lite.bin'), [byte[]]@(0, 1, 2, 3))
    $emptyStorage = [IO.File]::ReadAllBytes((Join-Path $unsigned 'storage-empty.bin'))
    $production = Join-Path $work 'store/2.7.8'
    $predecessor = Join-Path $work 'store/2.5.2'
    $zktClaim = @{ ProductionReleaseDirectory = $production; RollbackPredecessorDirectory = $predecessor }
    $hikClaim = @{ FirmwareFamily = 'hikvision'; GitSha = 'dc7d9c3b0de1aa5e1f1b7f279f16e317bf7afe65' }

    # 1. Installer mode never creates, replaces or guesses at signing keys.
    $missingVault = Join-Path $work 'no-vault'
    Assert-SignerRefusedBeforeKeyAccess 'Installer bundle without a vault' $zktClaim 'cannot create or replace signing keys' $missingVault
    New-VaultFixture $vault | Out-Null
    $incomplete = New-VaultFixture (Join-Path $work 'vault-incomplete') { param($keys, $directory) Remove-Item -LiteralPath (Join-Path $directory 'key-3.entropy') }
    Assert-SignerRefusedBeforeKeyAccess 'Incomplete vault' $zktClaim 'signing vault is incomplete' $incomplete
    $swapped = New-VaultFixture (Join-Path $work 'vault-swapped') { param($keys, $directory) $keys[0].key_id = ('e' * 64) }
    Assert-SignerRefusedBeforeKeyAccess 'Vault key 1 public key swapped' $zktClaim 'does not match its recorded key ID' $swapped
    $twoActive = New-VaultFixture (Join-Path $work 'vault-two-active') { param($keys, $directory) $keys[1].state = 'ACTIVE' }
    Assert-SignerRefusedBeforeKeyAccess 'Vault with two active keys' $zktClaim 'active bootstrap signer is unavailable' $twoActive
    $reserveActive = New-VaultFixture (Join-Path $work 'vault-reserve-active') { param($keys, $directory) $keys[0].state = 'RESERVE'; $keys[1].state = 'ACTIVE' }
    Assert-SignerRefusedBeforeKeyAccess 'Vault whose active key is not key 1' $zktClaim 'active bootstrap signer is unavailable' $reserveActive

    # 2. The claim must be a MAC-independent factory bundle of one family.
    Assert-SignerRefusedBeforeKeyAccess 'Installer bundle with a device MAC' (Merge-Arguments $zktClaim @{ DeviceMac = 'ac:27:6e:a4:4a:d8' }) 'Parameter set cannot be resolved'
    Assert-SignerRefusedBeforeKeyAccess 'Installer bundle for a rescue role' (Merge-Arguments $zktClaim @{ ImageRole = 'usb-wifi-rescue' }) 'carries only the factory role'
    Assert-SignerRefusedBeforeKeyAccess 'ZKT installer bundle without a production release' @{} 'boots an exact AVAILABLE production release'
    Assert-SignerRefusedBeforeKeyAccess 'ZKT installer bundle without a predecessor' @{ ProductionReleaseDirectory = $production } 'must carry a rollback predecessor for ota_1'
    Assert-SignerRefusedBeforeKeyAccess 'Hikvision installer bundle with a production release' (Merge-Arguments $hikClaim @{ ProductionReleaseDirectory = $production }) 'carries no production release or rollback predecessor'
    New-Item -ItemType Directory -Path $signerOutput | Out-Null
    [IO.File]::WriteAllText((Join-Path $signerOutput 'left-over.txt'), 'earlier run', $utf8)
    Assert-SignerRefusedBeforeKeyAccess 'Installer bundle into a used output directory' $zktClaim 'output directory must be new or empty'
    Remove-Item -LiteralPath $signerOutput -Recurse -Force

    # 3. Only the bench-verified empty SPIFFS, and only qualified predecessors.
    Set-UnsignedStorage (New-Object byte[] 0x800000)
    Assert-SignerRefusedBeforeKeyAccess 'ZKT bundle with other storage bytes' $zktClaim 'not the bench-verified empty SPIFFS'
    Assert-SignerRefusedBeforeKeyAccess 'Hikvision bundle with other storage bytes' $hikClaim 'not the bench-verified empty SPIFFS'
    Remove-Item -LiteralPath (Join-Path $unsigned 'storage-empty.bin') -Force
    Assert-SignerRefusedBeforeKeyAccess 'Hikvision bundle without storage' $hikClaim 'missing storage-empty\.bin'
    Set-UnsignedStorage $emptyStorage
    foreach ($case in @('store-unqualified', 'store-other-digest')) {
        Assert-SignerRefusedBeforeKeyAccess "Production release from $case" @{
            ProductionReleaseDirectory = (Join-Path $work "$case/2.7.8"); RollbackPredecessorDirectory = $predecessor
        } 'does not qualify 2\.5\.2 as its ota_1 rollback image'
    }
    Assert-SignerRefusedBeforeKeyAccess 'Production release whose image is another version' @{
        ProductionReleaseDirectory = (Join-Path $work 'store-mislabelled/2.7.8'); RollbackPredecessorDirectory = $predecessor
    } 'Release application identity does not match its signed manifest: Application version does not match'
    Assert-SignerRefusedBeforeKeyAccess 'Production release whose manifest names another application digest' @{
        ProductionReleaseDirectory = (Join-Path $work 'store-wrong-digest/2.7.8'); RollbackPredecessorDirectory = $predecessor
    } 'Release application identity does not match its signed manifest: Application digest does not match'
    # Every installed app refuses an ota_1 image outside its own
    # QUALIFIED_PREDECESSORS, whatever the release qualifies. The real gate
    # reads that list, and no synthetic image's digest is in it.
    Assert-SignerRefusedBeforeKeyAccess 'Predecessor the installer app does not accept' $zktClaim 'Every installed app would refuse this bundle: Rollback predecessor 2\.5\.2 is not one the State Life Device Installer accepts'

    # The signer's last two gates need what CI does not have: deployed ADD's
    # release catalogue, and the shipped app's predecessors (no synthetic
    # image carries one of their digests). tests/unit runs both against real
    # data. Until step 5 a stand-in answers a gate that $gateAnswers names,
    # and every other python call runs the real tool.
    $realPython = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $gateAnswers = @{ predecessor = @{ Exit = 0; Lines = @('2.5.2') } }
    $gateCalls = New-Object System.Collections.ArrayList
    function python {
        $gate = ''
        if ($args.Count -ge 2 -and [IO.Path]::GetFileName([string]$args[0]) -eq 'build_installer_bundle_manifest.py') {
            $gate = [string]$args[1]
        }
        if ($gate -eq 'add-available' -or $gateAnswers.ContainsKey($gate)) {
            [void]$gateCalls.Add(($args -join '|'))
            if (-not $gateAnswers.ContainsKey($gate)) { throw 'The test reached deployed ADD without a stand-in' }
            $global:LASTEXITCODE = $gateAnswers[$gate].Exit
            return $gateAnswers[$gate].Lines
        }
        & $realPython @args
    }

    # 4. Complete claims pass every installer check and stop only at the
    # existing identity check of the synthetic build, still before key access.
    foreach ($claim in @($zktClaim, $hikClaim)) {
        $vaultBefore = Get-TreeState $vault
        $message = Invoke-InstallerSigner $claim $vault
        if ($message -match 'installer bundle|signing keys|vault|storage|qualify|production release|predecessor|Release application|revoked') {
            throw "A complete installer claim was refused by its own validation: '$message'"
        }
        if ((Get-TreeState $vault) -ne $vaultBefore -or (Test-Path -LiteralPath $signerOutput)) {
            throw 'A complete installer claim touched the vault or output before the identity check'
        }
    }
    if (@($gateCalls).Count -ne 1 -or $gateCalls[0] -notlike ('*|predecessor|--version|2.5.2|--app-digest|' + $digests.'zkt-2.5.2.bin')) {
        throw "The ZKT claim did not ask the installer app about its exact predecessor: $($gateCalls -join '; ')"
    }

    # 4b. With a build that passes its identity check, deployed ADD has the
    # last word before the vault: ADD records a revocation only in its
    # database. A release ADD no longer offers and an unreachable ADD are
    # refused; a release ADD offers goes on to the next check (a missing file).
    $syntheticBuild = [IO.File]::ReadAllBytes((Join-Path $unsigned 'zone_lite.bin'))
    Copy-Item -LiteralPath (Join-Path $images 'zkt-2.7.8.bin') -Destination (Join-Path $unsigned 'zone_lite.bin') -Force
    foreach ($case in @(
            @{ Name = 'Production release ADD revoked'; Line = 'Release 2.7.8 is not AVAILABLE in deployed ADD (revoked, never published, or other bytes)' },
            @{ Name = 'Deployed ADD unreachable'; Line = "Deployed ADD's release catalogue could not be read" })) {
        $gateAnswers['add-available'] = @{ Exit = 1; Lines = @($case.Line) }
        Assert-SignerRefusedBeforeKeyAccess $case.Name $zktClaim ('A revoked or unpublished release is never signed: ' + [regex]::Escape($case.Line))
    }
    $gateAnswers['add-available'] = @{ Exit = 0; Lines = @('2.7.8', '2.5.2') }
    $gateCalls.Clear()
    $otadata = Join-Path $unsigned 'ota_data_initial.bin'
    Move-Item -LiteralPath $otadata -Destination (Join-Path $work 'ota_data_initial.bin')
    Assert-SignerRefusedBeforeKeyAccess 'Release deployed ADD offers' $zktClaim 'Unsigned firmware package is missing ota_data_initial\.bin'
    $asked = @($gateCalls | Where-Object { $_.Split('|')[1] -ceq 'add-available' } | ForEach-Object { ($_.Split('|') | Select-Object -Skip 2) -join '|' })
    $expected = @('--require-all', (Resolve-Path -LiteralPath $production).Path, (Resolve-Path -LiteralPath $predecessor).Path) -join '|'
    if ($asked.Count -ne 1 -or $asked[0] -cne $expected) {
        throw "The signer did not ask deployed ADD about exactly its two releases: $($asked -join '; ')"
    }
    Move-Item -LiteralPath (Join-Path $work 'ota_data_initial.bin') -Destination $otadata
    [IO.File]::WriteAllBytes((Join-Path $unsigned 'zone_lite.bin'), $syntheticBuild)
    Remove-Item -LiteralPath Function:\python

    # 5. Bundles from synthetic signed images, as the signer builds them.
    $zktBundle = Join-Path $work 'zkt-bundle'
    $zktId = Invoke-Native 'python' @($bundleTool, 'build', '--output', $zktBundle, '--family', 'zkt', '--git-sha', ('a' * 40),
        '--bootloader', (Join-Path $images 'bootloader-signed.bin'), '--partition-table', (Join-Path $images 'partition-table.bin'),
        '--factory', (Join-Path $production 'zone-lite-2.7.8.bin'), '--factory-version', '2.7.8', '--factory-app-digest', $digests.'zkt-2.7.8.bin',
        '--predecessor', (Join-Path $predecessor 'zone-lite-2.5.2.bin'), '--predecessor-version', '2.5.2', '--predecessor-app-digest', $digests.'zkt-2.5.2.bin',
        '--storage', (Join-Path $images 'storage-empty.bin')) | Select-Object -Last 1
    if ($zktId -cne ('zkt-2.7.8-' + $digests.'zkt-2.7.8.bin'.Substring(0, 8))) { throw "Unexpected ZKT bundle ID $zktId" }
    $hikBundle = Join-Path $work 'hik-bundle'
    $hikId = Invoke-Native 'python' @($bundleTool, 'build', '--output', $hikBundle, '--family', 'hikvision', '--git-sha', $hikClaim.GitSha,
        '--bootloader', (Join-Path $images 'bootloader-signed.bin'), '--partition-table', (Join-Path $images 'partition-table.bin'),
        '--factory', (Join-Path $images 'hik-3.1.1.bin'), '--storage', (Join-Path $images 'storage-empty.bin')) | Select-Object -Last 1
    if ($hikId -cne ('hikvision-3.1.1-dc7d9c3-' + $digests.'hik-3.1.1.bin'.Substring(0, 8))) { throw "Unexpected Hikvision bundle ID $hikId" }
    $zktManifest = Get-Content -LiteralPath (Join-Path $zktBundle 'manifest.json') -Raw | ConvertFrom-Json
    $layout = @($zktManifest.images | ForEach-Object { '{0}@{1}' -f $_.role, $_.offset }) -join ','
    if ($layout -cne 'bootloader@0,partition-table@65536,otadata@94208,factory@131072,ota_0@2752512,ota_1@5373952,storage@7995392' -or
        $zktManifest.release_state -cne 'AVAILABLE' -or $zktManifest.recommended -ne $true) {
        throw "ZKT bundle layout or state changed: $layout"
    }
    $hikManifest = Get-Content -LiteralPath (Join-Path $hikBundle 'manifest.json') -Raw | ConvertFrom-Json
    $layout = @($hikManifest.images | ForEach-Object { '{0}@{1}' -f $_.role, $_.offset }) -join ','
    if ($layout -cne 'bootloader@0,partition-table@65536,otadata@94208,factory@131072,storage@7995392' -or
        $hikManifest.release_state -cne 'FIELD_BASELINE' -or $hikManifest.git_sha -cne $hikClaim.GitSha) {
        throw "Hikvision bundle layout or state changed: $layout"
    }

    # 6. Throwaway keys: the vault's openssl command when available, and .NET.
    $key = New-SigningKey (Join-Path $work 'key')
    $dotNetKey = New-SigningKey (Join-Path $work 'dotnet-key') -DotNet
    $otherKey = New-SigningKey (Join-Path $work 'other-key') -DotNet
    Set-BundleSignature $zktBundle $key
    Set-BundleSignature $hikBundle $dotNetKey

    # 7. Every damaged or foreign bundle is refused before the store changes.
    New-Item -ItemType Directory -Path $store -Force | Out-Null
    $publish = @{ SourceDirectory = $zktBundle; StoreDirectory = $store; PublicKeyPath = $key.Public; ExpectedPublicKeySpkiSha256 = $key.Spki }
    Assert-PublisherRefused 'Bundle signed by another key' (Merge-Arguments $publish @{ PublicKeyPath = $otherKey.Public; ExpectedPublicKeySpkiSha256 = '' }) 'signature is invalid'
    Assert-PublisherRefused 'Key that installers do not pin' (Merge-Arguments $publish @{ ExpectedPublicKeySpkiSha256 = ('0' * 64) }) 'not the key every installer pins'
    Assert-PublisherRefused 'Another key under the pin' (Merge-Arguments $publish @{ PublicKeyPath = $otherKey.Public }) 'not the key every installer pins'
    $tampered = Copy-Bundle $zktBundle 'tampered-image'
    $bytes = [IO.File]::ReadAllBytes((Join-Path $tampered 'rollback-predecessor.bin'))
    $bytes[100] = $bytes[100] -bxor 0xFF
    [IO.File]::WriteAllBytes((Join-Path $tampered 'rollback-predecessor.bin'), $bytes)
    Assert-PublisherRefused 'Bundle with a changed image' (Merge-Arguments $publish @{ SourceDirectory = $tampered }) 'rollback-predecessor\.bin does not match its size and SHA-256'
    $edited = Copy-Bundle $zktBundle 'edited-manifest'
    $raw = [IO.File]::ReadAllText((Join-Path $edited 'manifest.json'))
    [IO.File]::WriteAllText((Join-Path $edited 'manifest.json'), $raw.Replace('ZKTeco terminals.', 'ZKTeco terminals!'), $utf8)
    Assert-PublisherRefused 'Manifest changed after signing' (Merge-Arguments $publish @{ SourceDirectory = $edited }) 'signature is invalid'
    $pretty = Copy-Bundle $zktBundle 'pretty-manifest'
    [IO.File]::WriteAllText((Join-Path $pretty 'manifest.json'), $raw.Replace('","', '", "'), $utf8)
    Assert-PublisherRefused 'Manifest not in canonical form' (Merge-Arguments $publish @{ SourceDirectory = $pretty }) 'not in canonical signed form'
    $extra = Copy-Bundle $zktBundle 'extra-file'
    [IO.File]::WriteAllText((Join-Path $extra 'notes.txt'), 'not listed', $utf8)
    Assert-PublisherRefused 'Bundle with an unlisted file' (Merge-Arguments $publish @{ SourceDirectory = $extra }) 'holds files its manifest does not list'
    $short = Copy-Bundle $zktBundle 'short-signature'
    [IO.File]::WriteAllText((Join-Path $short 'manifest.sig'), [Convert]::ToBase64String((New-Object byte[] 100)), $utf8)
    Assert-PublisherRefused 'Truncated signature' (Merge-Arguments $publish @{ SourceDirectory = $short }) 'not an RSA-3072 signature'
    # A validly signed bundle must still carry the production layout.
    $foreignTable = Copy-Bundle $zktBundle 'foreign-partition-table'
    $tablePath = Join-Path $foreignTable 'partition-table.bin'
    $tableHash = (Get-FileHash -LiteralPath $tablePath -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllBytes($tablePath, [byte[]](@(0xFF) * 3072))
    $blankHash = (Get-FileHash -LiteralPath $tablePath -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText((Join-Path $foreignTable 'manifest.json'), $raw.Replace($tableHash, $blankHash), $utf8)
    Set-BundleSignature $foreignTable $key
    Assert-PublisherRefused 'Signed bundle with another partition table' (Merge-Arguments $publish @{ SourceDirectory = $foreignTable }) 'Partition table does not match the production OTA layout'
    $unsignedCopy = Copy-Bundle $zktBundle 'no-signature'
    Remove-Item -LiteralPath (Join-Path $unsignedCopy 'manifest.sig') -Force
    Assert-PublisherRefused 'Bundle without a signature' (Merge-Arguments $publish @{ SourceDirectory = $unsignedCopy }) 'signature is missing'

    # 8. Publication: atomic, byte-identical, verifiable where ADD reads it.
    # The key is the pinned PEM with its line endings swapped, LF for CRLF or
    # CRLF for LF, as a Windows checkout does: the pin names the key, not the
    # file's bytes.
    $pemText = [IO.File]::ReadAllText($key.Public)
    $swapped = if ($pemText.Contains("`r`n")) { $pemText.Replace("`r`n", "`n") } else { $pemText.Replace("`n", "`r`n") }
    $otherEndings = Join-Path $work 'key-public-other-line-endings.pem'
    [IO.File]::WriteAllText($otherEndings, $swapped, $utf8)
    if ((Get-FileHash -LiteralPath $otherEndings).Hash -eq (Get-FileHash -LiteralPath $key.Public).Hash) {
        throw 'The copy of the key with other line endings kept the PEM bytes'
    }
    $result = Invoke-Publisher (Merge-Arguments $publish @{ PublicKeyPath = $otherEndings })
    if ($result.Message -or [string]$result.Id -cne $zktId) { throw "ZKT bundle was not published: '$($result.Message)'" }
    $published = Join-Path $store $zktId
    $expectedFiles = 'bootloader-signed.bin,manifest.json,manifest.sig,ota-data.bin,partition-table.bin,rollback-predecessor.bin,storage-empty.bin,zone-lite-signed.bin'
    if ((@(Get-ChildItem -LiteralPath $published -Force | ForEach-Object Name | Sort-Object) -join ',') -cne $expectedFiles) {
        throw 'Published ZKT bundle does not hold exactly its listed files'
    }
    if ((Get-TreeState $published) -cne (Get-TreeState $zktBundle)) { throw 'Published ZKT bundle is not byte-identical to the signed bundle' }
    if (@(Get-ChildItem -LiteralPath $store -Force) | Where-Object { $_.Name -ne $zktId }) { throw 'Publication left a staging directory behind' }
    if ((Invoke-Native 'python' @($bundleTool, 'verify', '--bundle', $published) | Select-Object -Last 1) -cne $zktId) {
        throw 'Published ZKT bundle does not verify'
    }

    # 9. Published bundle IDs are immutable, even for a newly signed bundle.
    Assert-PublisherRefused 'Same bundle published again' $publish 'already published; published bundles are never overwritten'
    $resigned = Copy-Bundle $zktBundle 'resigned'
    Set-BundleSignature $resigned $key
    Assert-PublisherRefused 'Same bundle ID signed again' (Merge-Arguments $publish @{ SourceDirectory = $resigned }) 'already published'

    # 10. The store defaults to ADD_INSTALLER_BUNDLE_STORE_HOST_PATH.
    $environmentStore = Join-Path $work 'environment-store'
    $env:ADD_INSTALLER_BUNDLE_STORE_HOST_PATH = $environmentStore
    $result = Invoke-Publisher @{ SourceDirectory = $hikBundle; PublicKeyPath = $dotNetKey.Public }
    if ($result.Message -or [string]$result.Id -cne $hikId) { throw "Hikvision bundle was not published: '$($result.Message)'" }
    $expectedFiles = 'bootloader-signed.bin,manifest.json,manifest.sig,ota-data.bin,partition-table.bin,storage-empty.bin,zone-lite-signed.bin'
    if ((@(Get-ChildItem -LiteralPath (Join-Path $environmentStore $hikId) -Force | ForEach-Object Name | Sort-Object) -join ',') -cne $expectedFiles) {
        throw 'Published Hikvision bundle does not hold exactly its listed files'
    }
    if (Test-Path -LiteralPath (Join-Path $store $hikId)) { throw 'Hikvision bundle ignored ADD_INSTALLER_BUNDLE_STORE_HOST_PATH' }

    $signerKind = $(if ($openssl) { 'openssl and .NET' } else { '.NET only; openssl not found' })
    Write-Output "PASS: installer bundles refuse bad claims, app-refused predecessors and releases ADD does not offer before key access, verify $signerKind RSA-PSS signatures, pin the key by its SubjectPublicKeyInfo, publish atomically and never overwrite"
    # Refusal cases make native tools exit non-zero on purpose; clear only
    # that expected exit code.
    $global:LASTEXITCODE = 0
} finally {
    Pop-Location
    $env:ADD_INSTALLER_BUNDLE_STORE_HOST_PATH = $originalStore
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
