param(
    [Parameter(Mandatory = $true)][string]$VaultDirectory,
    [Parameter(Mandatory = $true)][string]$UnsignedDirectory,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$')][string]$DeviceMac
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security

function Invoke-DockerText {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $output = @(& docker @Arguments)
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed with exit code $LASTEXITCODE" }
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
    $staging = Join-Path $VaultDirectory ('.new-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        $keys = @()
        foreach ($number in 1..3) {
            $pem = Invoke-DockerText @('run', '--rm', 'espressif/idf:v5.5.3', 'openssl', 'genrsa', '3072')
            $plain = [Text.Encoding]::ASCII.GetBytes($pem)
            $protected = Protect-Key $plain
            [IO.File]::WriteAllBytes((Join-Path $staging "key-$number.dpapi"), $protected.Ciphertext)
            [IO.File]::WriteAllBytes((Join-Path $staging "key-$number.entropy"), $protected.Entropy)

            $tempKey = Join-Path $staging "key-$number.pem"
            [IO.File]::WriteAllBytes($tempKey, $plain)
            try {
                $public = Invoke-DockerText @(
                    'run', '--rm', '-v', "${staging}:/keys", 'espressif/idf:v5.5.3',
                    'openssl', 'pkey', '-in', "/keys/key-$number.pem", '-pubout'
                )
                $publicPath = Join-Path $staging "key-$number-public.pem"
                [IO.File]::WriteAllText($publicPath, $public, (New-Object Text.UTF8Encoding($false)))
                $keyId = (Get-FileHash -LiteralPath $publicPath -Algorithm SHA256).Hash.ToLowerInvariant()
                $keys += @{ number = $number; key_id = $keyId; state = $(if ($number -eq 1) { 'ACTIVE' } else { 'RESERVE' }) }
            }
            finally {
                Clear-PlaintextFile $tempKey
                [Array]::Clear($plain, 0, $plain.Length)
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
        [IO.File]::WriteAllText((Join-Path $staging 'vault-manifest.json'), $manifest, (New-Object Text.UTF8Encoding($false)))
        foreach ($item in Get-ChildItem -LiteralPath $staging) {
            Move-Item -LiteralPath $item.FullName -Destination (Join-Path $VaultDirectory $item.Name)
        }
    }
    finally {
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
    Invoke-DockerText @(
        'run', '--rm', '-v', "${work}:/work", 'espressif/idf:v5.5.3',
        'espsecure.py', 'sign_data', '--version', '2', '--keyfile', '/work/key-1.pem',
        '--output', '/work/bootloader-signed-1.bin', '/work/bootloader.bin'
    ) | Out-Null
    Invoke-DockerText @(
        'run', '--rm', '-v', "${work}:/work", 'espressif/idf:v5.5.3',
        'espsecure.py', 'sign_data', '--version', '2', '--keyfile', '/work/key-2.pem',
        '--append-signatures', '--output', '/work/bootloader-signed-2.bin', '/work/bootloader-signed-1.bin'
    ) | Out-Null
    Invoke-DockerText @(
        'run', '--rm', '-v', "${work}:/work", 'espressif/idf:v5.5.3',
        'espsecure.py', 'sign_data', '--version', '2', '--keyfile', '/work/key-3.pem',
        '--append-signatures', '--output', '/work/bootloader-signed.bin', '/work/bootloader-signed-2.bin'
    ) | Out-Null
    Invoke-DockerText @(
        'run', '--rm', '-v', "${work}:/work", 'espressif/idf:v5.5.3',
        'espsecure.py', 'sign_data', '--version', '2', '--keyfile', '/work/key-1.pem',
        '--output', '/work/zone-lite-signed.bin', '/work/zone_lite.bin'
    ) | Out-Null
    Invoke-DockerText @(
        'run', '--rm', '-v', "${work}:/work", 'espressif/idf:v5.5.3',
        'espsecure.py', 'signature-info-v2', '/work/bootloader-signed.bin'
    ) | Out-Null
    Invoke-DockerText @(
        'run', '--rm', '-v', "${work}:/work", 'espressif/idf:v5.5.3',
        'espsecure.py', 'signature-info-v2', '/work/zone-lite-signed.bin'
    ) | Out-Null

    Copy-Item -LiteralPath (Join-Path $work 'bootloader-signed.bin') -Destination (Join-Path $output 'bootloader-signed.bin')
    Copy-Item -LiteralPath (Join-Path $work 'zone-lite-signed.bin') -Destination (Join-Path $output 'zone-lite-signed.bin')
    foreach ($name in @('partition-table.bin', 'ota_data_initial.bin', 'flasher_args.json')) {
        Copy-Item -LiteralPath (Join-Path $unsigned $name) -Destination (Join-Path $output $name)
    }
    foreach ($number in 1..3) {
        Copy-Item -LiteralPath (Join-Path $VaultDirectory "key-$number-public.pem") -Destination (Join-Path $output "key-$number-public.pem")
    }
    Copy-Item -LiteralPath $vaultManifest -Destination (Join-Path $output 'vault-manifest.json')
    $package = @{
        schema_version = 1
        target_mac = $DeviceMac.ToLowerInvariant()
        git_sha = $env:GITHUB_SHA
        created_at = [DateTime]::UtcNow.ToString('o')
        bootloader_sha256 = (Get-FileHash (Join-Path $output 'bootloader-signed.bin') -Algorithm SHA256).Hash.ToLowerInvariant()
        application_sha256 = (Get-FileHash (Join-Path $output 'zone-lite-signed.bin') -Algorithm SHA256).Hash.ToLowerInvariant()
    } | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $output 'bootstrap-manifest.json'), $package, (New-Object Text.UTF8Encoding($false)))
}
finally {
    foreach ($number in 1..3) { Clear-PlaintextFile (Join-Path $work "key-$number.pem") }
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
}

Write-Host "Created signed bootstrap package for $DeviceMac; private keys remain DPAPI-protected in $VaultDirectory"
