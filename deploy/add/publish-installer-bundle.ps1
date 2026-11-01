param(
    # A signed bundle from bootstrap-firmware-signing.ps1 -InstallerBundle.
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    # Vault key 1, the key the manifest is signed with.
    [Parameter(Mandatory = $true)][string]$PublicKeyPath,
    # ADD serves this store read-only as /installer-bundles.
    [string]$StoreDirectory = '',
    # SHA-256 of the DER SubjectPublicKeyInfo of the key every State Life
    # Device Installer pins; a bundle signed by any other key would be refused
    # by every installed app. It names the key, whatever line endings or
    # wrapping its PEM file has.
    [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$ExpectedPublicKeySpkiSha256 = ''
)

# Publishes one installer bundle (docs/esp-installer-bundles.md) atomically and
# never over an existing bundle ID. The signature and every image hash are
# checked before anything is written and again where ADD reads them.
$ErrorActionPreference = 'Stop'
if (-not $StoreDirectory) { $StoreDirectory = $env:ADD_INSTALLER_BUNDLE_STORE_HOST_PATH }
if (-not $StoreDirectory) {
    $StoreDirectory = 'C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-bundles'
}
if ($PSVersionTable.PSEdition -ne 'Core') { Add-Type -AssemblyName System.Core }
$bundleTool = Join-Path $PSScriptRoot '../../scripts/build_installer_bundle_manifest.py'

function Read-DerElement {
    param([byte[]]$Data, [int]$Offset, [int]$Tag)
    if ($Offset + 2 -gt $Data.Length -or $Data[$Offset] -ne $Tag) {
        throw 'Installer bundle signing key is not an RSA public key'
    }
    $length = [int]$Data[$Offset + 1]
    $start = $Offset + 2
    if ($length -band 0x80) {
        $count = $length -band 0x7F
        if ($count -lt 1 -or $count -gt 3 -or $start + $count -gt $Data.Length) {
            throw 'Installer bundle signing key is not an RSA public key'
        }
        $length = 0
        foreach ($index in 0..($count - 1)) { $length = ($length -shl 8) -bor $Data[$start + $index] }
        $start += $count
    }
    if ($start + $length -gt $Data.Length) { throw 'Installer bundle signing key is not an RSA public key' }
    return @{ Start = $start; End = $start + $length }
}

function Get-PublicKeyDer {
    param([Parameter(Mandatory = $true)][string]$Path)
    # The DER SubjectPublicKeyInfo inside the PEM, the same whatever line
    # endings the file has.
    $pem = [regex]::Match([IO.File]::ReadAllText($Path), '-----BEGIN PUBLIC KEY-----([A-Za-z0-9+/=\s]+)-----END PUBLIC KEY-----')
    if (-not $pem.Success) { throw 'Installer bundle signing key is not a PEM public key' }
    return ,[Convert]::FromBase64String(($pem.Groups[1].Value -replace '\s', ''))
}

function Get-RsaPublicParameters {
    param([Parameter(Mandatory = $true)][byte[]]$Der)
    # SubjectPublicKeyInfo: SEQUENCE { SEQUENCE { rsaEncryption, NULL },
    # BIT STRING { SEQUENCE { INTEGER n, INTEGER e } } }. Windows PowerShell
    # 5.1 has no PEM import, so read the few DER fields directly.
    $info = Read-DerElement $der 0 0x30
    $algorithm = Read-DerElement $der $info.Start 0x30
    $oid = Read-DerElement $der $algorithm.Start 0x06
    if ([BitConverter]::ToString($der, $oid.Start, $oid.End - $oid.Start) -ne '2A-86-48-86-F7-0D-01-01-01') {
        throw 'Installer bundle signing key is not an RSA public key'
    }
    $bits = Read-DerElement $der $algorithm.End 0x03
    if ($der[$bits.Start] -ne 0) { throw 'Installer bundle signing key is not an RSA public key' }
    $key = Read-DerElement $der ($bits.Start + 1) 0x30
    $modulus = Read-DerElement $der $key.Start 0x02
    $exponent = Read-DerElement $der $modulus.End 0x02
    $modulusStart = $modulus.Start
    while ($modulusStart -lt $modulus.End - 1 -and $der[$modulusStart] -eq 0) { $modulusStart++ }
    if ($modulus.End - $modulusStart -ne 384) { throw 'Installer bundle signing key is not an RSA-3072 key' }
    $parameters = New-Object Security.Cryptography.RSAParameters
    $parameters.Modulus = [byte[]]$der[$modulusStart..($modulus.End - 1)]
    $parameters.Exponent = [byte[]]$der[$exponent.Start..($exponent.End - 1)]
    return $parameters
}

function Test-ManifestSignature {
    param([Parameter(Mandatory = $true)][string]$Directory)
    # RSA-PSS SHA-256; .NET's PSS salt is the 32-byte hash length, exactly the
    # rsa_pss_saltlen:32 the vault signs with.
    $signature = [Convert]::FromBase64String(([IO.File]::ReadAllText((Join-Path $Directory 'manifest.sig'))).Trim())
    $manifest = [IO.File]::ReadAllBytes((Join-Path $Directory 'manifest.json'))
    $rsa = if ($PSVersionTable.PSEdition -eq 'Core') { [Security.Cryptography.RSA]::Create() } else { New-Object Security.Cryptography.RSACng }
    try {
        $rsa.ImportParameters($script:signingKey)
        $valid = $rsa.VerifyData(
            $manifest,
            $signature,
            [Security.Cryptography.HashAlgorithmName]::SHA256,
            [Security.Cryptography.RSASignaturePadding]::Pss
        )
    } finally { $rsa.Dispose() }
    if (-not $valid) { throw 'Installer bundle manifest signature is invalid' }
}

function Assert-InstallerBundle {
    param([Parameter(Mandatory = $true)][string]$Directory)
    # The tool checks canonical JSON, layout, sizes, SHA-256s and images; it
    # prints the bundle ID. Windows PowerShell 5.1 turns native stderr into
    # ErrorRecords, so keep the tool's own refusal as the reason.
    $preference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& python $bundleTool verify --bundle $Directory 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ })
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $preference }
    if ($exitCode -ne 0) { throw "Installer bundle verification failed: $($lines | Select-Object -Last 1)" }
    $verifiedId = [string]($lines | Select-Object -Last 1)
    if ($verifiedId -notmatch '^(zkt-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{8}|hikvision-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{7}-[0-9a-f]{8})$') {
        throw 'Installer bundle verification returned no bundle ID'
    }
    Test-ManifestSignature $Directory
    return $verifiedId
}

$source = (Resolve-Path -LiteralPath $SourceDirectory).Path
$publicKey = (Resolve-Path -LiteralPath $PublicKeyPath).Path
$publicKeyDer = Get-PublicKeyDer $publicKey
if ($ExpectedPublicKeySpkiSha256) {
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { $spki = [BitConverter]::ToString($sha256.ComputeHash($publicKeyDer)).Replace('-', '').ToLowerInvariant() }
    finally { $sha256.Dispose() }
    if ($spki -cne $ExpectedPublicKeySpkiSha256) { throw 'Installer bundle signing key is not the key every installer pins' }
}
$script:signingKey = Get-RsaPublicParameters $publicKeyDer
$bundleId = Assert-InstallerBundle $source
$names = @((Get-Content -LiteralPath (Join-Path $source 'manifest.json') -Raw | ConvertFrom-Json).images |
    ForEach-Object { [string]$_.name } | Select-Object -Unique)

New-Item -ItemType Directory -Path $StoreDirectory -Force | Out-Null
$store = (Resolve-Path -LiteralPath $StoreDirectory).Path
$final = Join-Path $store $bundleId
if (Test-Path -LiteralPath $final) {
    throw "Installer bundle $bundleId is already published; published bundles are never overwritten (retire it first)"
}
# Staged one level down, where no reader of <store>\<id>\manifest.json looks,
# then renamed into place on the same volume.
$stagingRoot = Join-Path $store '.staging'
$staging = Join-Path $stagingRoot ($bundleId + '-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    # Images first and the signed manifest last.
    foreach ($name in @($names + @('manifest.json', 'manifest.sig'))) {
        Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $staging $name)
    }
    Assert-InstallerBundle $staging | Out-Null
    # One rename; it fails rather than merge if the bundle ID appeared meanwhile.
    [IO.Directory]::Move($staging, $final)
}
finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    if ((Test-Path -LiteralPath $stagingRoot) -and -not (Get-ChildItem -LiteralPath $stagingRoot -Force)) {
        Remove-Item -LiteralPath $stagingRoot -Force
    }
}
try {
    $publishedId = Assert-InstallerBundle $final
    if ($publishedId -cne $bundleId) { throw 'Published installer bundle changed identity' }
}
catch {
    # Only this run created $final; never leave a bundle that failed its check.
    Remove-Item -LiteralPath $final -Recurse -Force
    throw
}
Write-Host "Published installer bundle $bundleId to $final"
Write-Output $bundleId
