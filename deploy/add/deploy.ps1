param(
    [string] $ExpectedSha = $env:GITHUB_SHA
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$compose = @("compose", "--env-file", ".env.add", "-f", "docker-compose.add.yml")
$apiImage = "state-life/add-api:production"
$webImage = "state-life/add-web:production"
$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$applicationStarted = $false
$environmentPromoted = $false

function Invoke-Docker {
    param(
        [Parameter(Mandatory = $true)][string[]] $Arguments,
        [switch] $Capture
    )
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 promotes redirected native stderr to a
        # NativeCommandError. The process exit code is authoritative here.
        $ErrorActionPreference = "Continue"
        if ($Capture) {
            $result = & docker @Arguments 2>&1
        } else {
            & docker @Arguments
        }
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $exitCode"
    }
    if ($Capture) {
        return (($result | ForEach-Object { "$_" }) -join "`n").Trim()
    }
}

function Invoke-DockerProbe {
    param([Parameter(Mandatory = $true)][string[]] $Arguments)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        $result = & docker @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) { return $null }
    return (($result | ForEach-Object { "$_" }) -join "`n").Trim()
}

function Get-EnvironmentMap {
    param([Parameter(Mandatory = $true)][string] $Path)
    $map = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            throw "Invalid environment-file line; expected NAME=value."
        }
        $key = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $map[$key] = $value
    }
    return $map
}

function Require-EnvironmentValue {
    param(
        [Parameter(Mandatory = $true)][hashtable] $Map,
        [Parameter(Mandatory = $true)][string] $Name
    )
    if (-not $Map.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace($Map[$Name])) {
        throw "Missing required production setting $Name."
    }
    $value = [string]$Map[$Name]
    if ($value -match '(?i)replace-me|replace-with|example\.invalid') {
        throw "Production setting $Name still contains a placeholder."
    }
    return $value
}

function Get-ImageId {
    param([Parameter(Mandatory = $true)][string] $Image)
    return Invoke-DockerProbe -Arguments @("image", "inspect", $Image, "--format", "{{.Id}}")
}

function Get-PostgresContainer {
    return Invoke-DockerProbe -Arguments ($compose + @("ps", "-q", "postgres"))
}

function Get-DatabaseRevision {
    param([string] $DatabaseUser, [string] $DatabaseName)
    return Invoke-DockerProbe -Arguments ($compose + @(
        "exec", "-T", "postgres", "psql", "-tA", "-U", $DatabaseUser,
        "-d", $DatabaseName, "-c", "SELECT version_num FROM alembic_version LIMIT 1"
    ))
}

function Wait-Endpoint {
    param(
        [Parameter(Mandatory = $true)][string] $Uri,
        [int] $Attempts = 36
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 4
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { return }
        } catch {
            if ($attempt -eq $Attempts) { throw }
        }
        Start-Sleep -Seconds 5
    }
    throw "Health check did not become ready: $Uri"
}

function Protect-StateDirectory {
    param([Parameter(Mandatory = $true)][string] $Path)

    $alreadyExists = Test-Path -LiteralPath $Path -PathType Container
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    if ($alreadyExists) {
        # The runner deliberately has deployment-data access but may not have
        # WRITE_DAC on a directory created by an administrator. Reapplying the
        # ACL on every release can therefore leave a valid directory unusable.
        # Existing directories are checked for effective write access below.
        return
    }

    & icacls.exe $Path /inheritance:r `
        /grant:r '*S-1-5-18:(OI)(CI)F' `
        '*S-1-5-32-544:(OI)(CI)F' `
        '*S-1-5-20:(OI)(CI)F' /T | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not protect deployment state directory $Path."
    }
}

function Assert-StateDirectoryWritable {
    param([Parameter(Mandatory = $true)][string] $Path)

    $probe = Join-Path $Path ".add-write-probe-$PID-$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText($probe, "")
    } catch {
        throw "The runner cannot write deployment state directory $Path. Repair its ACL for NETWORK SERVICE before retrying."
    } finally {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath ".env.add" -PathType Leaf)) {
    throw "Missing .env.add"
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is not installed or not available to the runner service."
}

$actualSha = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw "Unable to read the checked-out Git commit." }
if ($ExpectedSha -and $actualSha -ne $ExpectedSha) {
    throw "Refusing deployment: expected commit $ExpectedSha but checkout is $actualSha."
}

$environment = Get-EnvironmentMap -Path ".env.add"
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_FLEET_ROOT_SECRET)) {
    $environment["ADD_FLEET_ROOT_SECRET"] = $env:ADD_DEPLOY_FLEET_ROOT_SECRET
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_ADMIN_PASSWORD_HASH)) {
    $environment["ADD_ADMIN_PASSWORD_HASH"] = $env:ADD_DEPLOY_ADMIN_PASSWORD_HASH
}
# These deployment coordinates are locked product requirements, not operator
# secrets. Canonicalizing them migrates older protected environments safely.
$environment["ADD_ADMIN_COOKIE_SECURE"] = "true"
$environment["ADD_PUBLIC_DEVICE_WS_URL"] = "wss://autoattn.slichealth.com/device/v2/stream"
$environment["ADD_ORDS_BASE_URL"] = "https://eclaim2.slichealth.com/ords/slic_hrm/raw_attn_capture_event"
if ($environment.ContainsKey("ADD_ADMIN_PASSWORD_HASH") -and
    $environment["ADD_ADMIN_PASSWORD_HASH"].StartsWith('$$argon2id$$')) {
    $environment["ADD_ADMIN_PASSWORD_HASH"] = $environment["ADD_ADMIN_PASSWORD_HASH"].Replace('$$', '$')
}
# Canonical single-quoted values prevent Compose from expanding dollar signs in
# Argon2 verifiers or operator-generated secrets. URL-safe production secrets
# must not contain a literal single quote.
$canonicalLines = New-Object System.Collections.Generic.List[string]
foreach ($key in ($environment.Keys | Sort-Object)) {
    $value = [string]$environment[$key]
    if ($value.Contains("'")) {
        throw "Production setting $key contains a single quote; regenerate it using URL-safe characters."
    }
    $canonicalLines.Add("$key='$value'")
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines((Join-Path $PWD ".env.add"), $canonicalLines, $utf8NoBom)
$environment = Get-EnvironmentMap -Path ".env.add"
$required = @(
    "ADD_POSTGRES_PASSWORD", "ADD_ADMIN_USERNAME", "ADD_ADMIN_PASSWORD_HASH",
    "ADD_PII_FERNET_KEY", "ADD_PII_LOOKUP_KEY", "ADD_FLEET_ROOT_SECRET",
    "ADD_PUBLIC_DEVICE_WS_URL", "ADD_ORDS_BASE_URL", "ADD_ORDS_USERNAME",
    "ADD_ORDS_PASSWORD"
)
foreach ($name in $required) { [void](Require-EnvironmentValue -Map $environment -Name $name) }

$dbUser = if ($environment.ContainsKey("ADD_POSTGRES_USER")) { $environment["ADD_POSTGRES_USER"] } else { "add_service" }
$dbName = if ($environment.ContainsKey("ADD_POSTGRES_DB")) { $environment["ADD_POSTGRES_DB"] } else { "attendance_devices" }
if ($dbUser -notmatch '^[A-Za-z_][A-Za-z0-9_]*$' -or $dbName -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    throw "PostgreSQL user and database names may contain only identifier characters."
}
if ($environment["ADD_ADMIN_USERNAME"] -ne "StateHealthAdmin") {
    throw "ADD_ADMIN_USERNAME must be StateHealthAdmin."
}
if ($environment["ADD_ADMIN_PASSWORD_HASH"] -notmatch '^\$argon2id\$') {
    throw "ADD_ADMIN_PASSWORD_HASH must be an Argon2id hash, never a plaintext password."
}
if ($environment["ADD_PII_FERNET_KEY"] -notmatch '^[A-Za-z0-9_-]{43}=$') {
    throw "ADD_PII_FERNET_KEY is not a valid Fernet key."
}
if ($environment["ADD_FLEET_ROOT_SECRET"].Length -lt 32) {
    throw "ADD_FLEET_ROOT_SECRET must contain at least 32 characters."
}
if ($environment["ADD_POSTGRES_PASSWORD"].Length -lt 24) {
    throw "ADD_POSTGRES_PASSWORD must contain at least 24 characters."
}
if ($environment["ADD_PUBLIC_DEVICE_WS_URL"] -ne "wss://autoattn.slichealth.com/device/v2/stream") {
    throw "ADD_PUBLIC_DEVICE_WS_URL must use the production TLS device stream."
}
if ($environment["ADD_ORDS_BASE_URL"] -ne "https://eclaim2.slichealth.com/ords/slic_hrm/raw_attn_capture_event") {
    throw "ADD_ORDS_BASE_URL must use the production raw attendance capture endpoint."
}

$stateRoot = if ($env:ADD_DEPLOY_STATE_DIR) {
    $env:ADD_DEPLOY_STATE_DIR
} else {
    Join-Path $env:ProgramData "StateLife\AttendanceDeviceDashboard"
}
$backupRoot = Join-Path $stateRoot "backups"
$configRoot = Join-Path $stateRoot "config"
$releaseRoot = Join-Path $stateRoot "releases"
Protect-StateDirectory -Path $stateRoot
foreach ($directory in @($backupRoot, $configRoot, $releaseRoot)) {
    Protect-StateDirectory -Path $directory
    Assert-StateDirectoryWritable -Path $directory
}

$protectedEnvironment = Join-Path $configRoot "add.env"
$legacyPendingEnvironment = "$protectedEnvironment.new"
if (Test-Path -LiteralPath $legacyPendingEnvironment -PathType Leaf) {
    try {
        Remove-Item -LiteralPath $legacyPendingEnvironment -Force -ErrorAction Stop
    } catch {
        Write-Warning "A legacy pending environment could not be removed; it remains inside the protected config directory."
    }
}
$previousEnvironmentBackup = $null
if (Test-Path -LiteralPath $protectedEnvironment -PathType Leaf) {
    $previousEnvironmentBackup = Join-Path $backupRoot "add-env-$stamp.bak"
    Copy-Item -LiteralPath $protectedEnvironment -Destination $previousEnvironmentBackup -Force
}
$pendingEnvironment = Join-Path $configRoot "add-env-$stamp.new"

try {
    Copy-Item -LiteralPath ".env.add" -Destination $pendingEnvironment

    Invoke-Docker -Arguments @("info") | Out-Null
    Invoke-Docker -Arguments ($compose + @("config", "--quiet"))

    $preApiImage = Get-ImageId -Image $apiImage
    $preWebImage = Get-ImageId -Image $webImage
    $rollbackApiTag = "state-life/add-api:rollback-$stamp"
    $rollbackWebTag = "state-life/add-web:rollback-$stamp"
    if ($preApiImage) { Invoke-Docker -Arguments @("tag", $preApiImage, $rollbackApiTag) }
    if ($preWebImage) { Invoke-Docker -Arguments @("tag", $preWebImage, $rollbackWebTag) }

    # Bring only durable dependencies online first, then take a binary-safe logical backup.
    Invoke-Docker -Arguments ($compose + @("up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis"))
    $postgresContainer = Get-PostgresContainer
    if (-not $postgresContainer) { throw "PostgreSQL container was not created." }
    $preRevision = Get-DatabaseRevision -DatabaseUser $dbUser -DatabaseName $dbName
    $databaseBackup = Join-Path $backupRoot "attendance-devices-$stamp.dump"
    $containerBackup = "/tmp/attendance-devices-$stamp.dump"
    Invoke-Docker -Arguments ($compose + @(
        "exec", "-T", "postgres", "pg_dump", "-Fc", "-U", $dbUser,
        "-d", $dbName, "-f", $containerBackup
    ))
    Invoke-Docker -Arguments @("cp", "${postgresContainer}:$containerBackup", $databaseBackup)
    Invoke-Docker -Arguments ($compose + @("exec", "-T", "postgres", "rm", "-f", $containerBackup))
    if (-not (Test-Path -LiteralPath $databaseBackup) -or (Get-Item $databaseBackup).Length -lt 100) {
        throw "The pre-deployment PostgreSQL backup is missing or invalid."
    }

    try {
        Invoke-Docker -Arguments ($compose + @("build", "--pull"))
        $applicationStarted = $true
        Invoke-Docker -Arguments ($compose + @("up", "-d", "--remove-orphans", "--wait", "--wait-timeout", "240"))
        Wait-Endpoint -Uri "http://127.0.0.1:8096/health/ready"
        Wait-Endpoint -Uri "http://127.0.0.1:8095/health/ui"
        $postRevision = Get-DatabaseRevision -DatabaseUser $dbUser -DatabaseName $dbName
        if (-not $postRevision) { throw "Alembic schema revision is unavailable after deployment." }

        Move-Item -LiteralPath $pendingEnvironment -Destination $protectedEnvironment -Force
        $environmentPromoted = $true

        $metadata = [ordered]@{
            deployed_at_utc = [DateTime]::UtcNow.ToString("o")
            commit_sha = $actualSha
            pre_revision = $preRevision
            post_revision = $postRevision
            database_backup = $databaseBackup
            previous_api_image = $preApiImage
            previous_web_image = $preWebImage
            environment_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $protectedEnvironment).Hash
        }
        $metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $releaseRoot "$stamp.json") -Encoding UTF8
        Invoke-Docker -Arguments ($compose + @("ps"))

        $cutoff = [DateTime]::UtcNow.AddDays(-14)
        Get-ChildItem -LiteralPath $backupRoot -File | Where-Object {
            $_.LastWriteTimeUtc -lt $cutoff
        } | Remove-Item -Force
        Write-Host "ADD commit $actualSha is healthy on 0.0.0.0:8095 and 0.0.0.0:8096."
    } catch {
        $deploymentError = $_.Exception.Message
        Write-Warning "Deployment failed; beginning bounded rollback."
        try {
            Remove-Item -LiteralPath $pendingEnvironment -Force -ErrorAction SilentlyContinue
            if ($environmentPromoted) {
                if ($previousEnvironmentBackup) {
                    Copy-Item -LiteralPath $previousEnvironmentBackup -Destination $protectedEnvironment -Force
                } else {
                    Remove-Item -LiteralPath $protectedEnvironment -Force -ErrorAction SilentlyContinue
                }
            }
            if (Test-Path -LiteralPath $protectedEnvironment -PathType Leaf) {
                Copy-Item -LiteralPath $protectedEnvironment -Destination ".env.add" -Force
                $rollbackEnvironment = Get-EnvironmentMap -Path ".env.add"
                $dbUser = if ($rollbackEnvironment.ContainsKey("ADD_POSTGRES_USER")) {
                    $rollbackEnvironment["ADD_POSTGRES_USER"]
                } else { "add_service" }
                $dbName = if ($rollbackEnvironment.ContainsKey("ADD_POSTGRES_DB")) {
                    $rollbackEnvironment["ADD_POSTGRES_DB"]
                } else { "attendance_devices" }
            }
            Invoke-Docker -Arguments ($compose + @("stop", "add-api", "add-web"))
            $postFailureRevision = Get-DatabaseRevision -DatabaseUser $dbUser -DatabaseName $dbName
            $schemaMayHaveChanged = $applicationStarted -and (
                -not $preRevision -or -not $postFailureRevision -or $postFailureRevision -ne $preRevision
            )
            if ($schemaMayHaveChanged) {
                $postgresContainer = Get-PostgresContainer
                if (-not $postgresContainer) { throw "Cannot restore PostgreSQL: container is unavailable." }
                $rollbackDump = "/tmp/rollback-$stamp.dump"
                Invoke-Docker -Arguments @("cp", $databaseBackup, "${postgresContainer}:$rollbackDump")
                $terminateSql = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$dbName' AND pid <> pg_backend_pid();"
                Invoke-Docker -Arguments ($compose + @(
                    "exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1",
                    "-U", $dbUser, "-d", "postgres", "-c", $terminateSql
                ))
                Invoke-Docker -Arguments ($compose + @("exec", "-T", "postgres", "dropdb", "-U", $dbUser, "--if-exists", $dbName))
                Invoke-Docker -Arguments ($compose + @("exec", "-T", "postgres", "createdb", "-U", $dbUser, "-O", $dbUser, $dbName))
                Invoke-Docker -Arguments ($compose + @(
                    "exec", "-T", "postgres", "pg_restore", "--exit-on-error",
                    "-U", $dbUser, "-d", $dbName, $rollbackDump
                ))
                Invoke-Docker -Arguments ($compose + @("exec", "-T", "postgres", "rm", "-f", $rollbackDump))
            }
            if (-not $preApiImage -or -not $preWebImage) {
                Invoke-Docker -Arguments ($compose + @("down", "--remove-orphans"))
                throw "No complete previous application image set exists; first deployment was stopped."
            }
            Invoke-Docker -Arguments @("tag", $rollbackApiTag, $apiImage)
            Invoke-Docker -Arguments @("tag", $rollbackWebTag, $webImage)
            Invoke-Docker -Arguments ($compose + @("up", "-d", "--no-build", "--remove-orphans"))
            Wait-Endpoint -Uri "http://127.0.0.1:8096/health/ready"
            Wait-Endpoint -Uri "http://127.0.0.1:8095/"
            throw "Deployment failed and the previous release was restored: $deploymentError"
        } catch {
            if ($_.Exception.Message -like "Deployment failed and the previous release was restored:*") {
                throw
            }
            throw "Deployment failed ($deploymentError); rollback also failed: $($_.Exception.Message)"
        }
    }
} finally {
    Remove-Item -LiteralPath $pendingEnvironment -Force -ErrorAction SilentlyContinue
}
