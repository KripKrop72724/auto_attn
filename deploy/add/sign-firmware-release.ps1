param(
    [Parameter(Mandatory = $true)][string]$VaultDirectory,
    [Parameter(Mandatory = $true)][string]$UnsignedDirectory,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$GitSha
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security

function Invoke-Docker {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed with exit code $LASTEXITCODE" }
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

$vault = (Resolve-Path $VaultDirectory).Path
$unsigned = (Resolve-Path $UnsignedDirectory).Path
$vaultManifestPath = Join-Path $vault 'vault-manifest.json'
if (-not (Test-Path -LiteralPath $vaultManifestPath -PathType Leaf)) {
    throw 'The ADD firmware signing vault has not been bootstrapped'
}
$vaultManifest = Get-Content -LiteralPath $vaultManifestPath -Raw | ConvertFrom-Json
$active = @($vaultManifest.keys | Where-Object state -eq 'ACTIVE')
if ($active.Count -ne 1) { throw 'The firmware signing vault must contain exactly one ACTIVE key' }
$number = [int]$active[0].number
$keyId = [string]$active[0].key_id
$cipherPath = Join-Path $vault "key-$number.dpapi"
$entropyPath = Join-Path $vault "key-$number.entropy"
$publicPath = Join-Path $vault "key-$number-public.pem"
foreach ($path in @($cipherPath, $entropyPath, $publicPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Firmware vault is missing $path" }
}

$sourceImage = Join-Path $unsigned 'zone_lite.bin'
if (-not (Test-Path -LiteralPath $sourceImage -PathType Leaf)) { throw 'Unsigned Zone Lite image is missing' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$output = (Resolve-Path $OutputDirectory).Path
$work = Join-Path $env:TEMP ('zone-lite-release-sign-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
$tempKey = Join-Path $work 'active-key.pem'
try {
    $ciphertext = [IO.File]::ReadAllBytes($cipherPath)
    $entropy = [IO.File]::ReadAllBytes($entropyPath)
    $plaintext = [Security.Cryptography.ProtectedData]::Unprotect(
        $ciphertext,
        $entropy,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    [IO.File]::WriteAllBytes($tempKey, $plaintext)
    [Array]::Clear($plaintext, 0, $plaintext.Length)
    Copy-Item -LiteralPath $sourceImage -Destination (Join-Path $work 'zone_lite.bin')
    Invoke-Docker @(
        'run', '--rm', '-v', "${work}:/work", 'espressif/idf:v5.5.3',
        'espsecure.py', 'sign_data', '--version', '2', '--keyfile', '/work/active-key.pem',
        '--output', "/work/zone-lite-$Version.bin", '/work/zone_lite.bin'
    )
    $signedImage = Join-Path $work "zone-lite-$Version.bin"
    $size = (Get-Item -LiteralPath $signedImage).Length
    $guardedLimit = 0x280000 - (128 * 1024)
    if ($size -gt $guardedLimit) { throw "Signed image $size exceeds guarded slot limit $guardedLimit" }
    Copy-Item -LiteralPath $signedImage -Destination (Join-Path $output "zone-lite-$Version.bin")
    Copy-Item -LiteralPath $publicPath -Destination (Join-Path $output 'manifest-public-key.pem')
    $imageHash = (Get-FileHash -LiteralPath (Join-Path $output "zone-lite-$Version.bin") -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest = [ordered]@{
        created_at = [DateTime]::UtcNow.ToString('o')
        esp_idf_version = '5.5.3'
        git_sha = $GitSha
        hardware = 'esp32-s3-zone-lite'
        image_name = "zone-lite-$Version.bin"
        image_sha256 = $imageHash
        image_size = $size
        minimum_bootstrap_version = '2.2.0'
        partition_layout = 'zone-lite-ota-v1'
        release_id = "zone-lite-$Version"
        schema_version = 1
        secure_boot = 'v2'
        signing_key_id = $keyId
        version = $Version
    }
    $manifestJson = $manifest | ConvertTo-Json -Compress
    [IO.File]::WriteAllText(
        (Join-Path $output 'manifest.json'),
        $manifestJson,
        (New-Object Text.UTF8Encoding($false))
    )
    Copy-Item -LiteralPath (Join-Path $output 'manifest.json') -Destination (Join-Path $work 'manifest.json')
    Invoke-Docker @(
        'run', '--rm', '-v', "${work}:/work", 'espressif/idf:v5.5.3',
        'openssl', 'dgst', '-sha256', '-sign', '/work/active-key.pem',
        '-sigopt', 'rsa_padding_mode:pss', '-sigopt', 'rsa_pss_saltlen:32',
        '-out', '/work/manifest.sig', '/work/manifest.json'
    )
    $signature = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $work 'manifest.sig')))
    [IO.File]::WriteAllText(
        (Join-Path $output 'manifest.sig'),
        $signature + "`n",
        (New-Object Text.UTF8Encoding($false))
    )
    $sumLines = @()
    foreach ($name in @("zone-lite-$Version.bin", 'manifest.json', 'manifest.sig', 'manifest-public-key.pem')) {
        $hash = (Get-FileHash -LiteralPath (Join-Path $output $name) -Algorithm SHA256).Hash.ToLowerInvariant()
        $sumLines += "$hash  $name"
    }
    [IO.File]::WriteAllLines((Join-Path $output 'SHA256SUMS'), $sumLines, (New-Object Text.UTF8Encoding($false)))
}
finally {
    Clear-PlaintextFile $tempKey
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
}

Write-Host "Signed immutable Zone Lite $Version release with ADD vault key $keyId"
