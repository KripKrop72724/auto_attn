param(
    [string] $ExpectedSha = $env:GITHUB_SHA
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$compose = @("compose", "--env-file", ".env.add", "-f", "docker-compose.add.yml")
$apiImage = "state-life/add-api:production"
$webImage = "state-life/add-web:production"
$provisionerImage = "state-life/add-provisioner:production"
$watchdogImage = "state-life/add-watchdog:production"
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

function Write-DockerFailureDiagnostics {
    param([Parameter(Mandatory = $true)][hashtable] $Environment)

    $previousPreference = $ErrorActionPreference
    try {
        # Emit only bounded container state and application logs while failed
        # containers still exist. Never render Compose config or environment.
        $ErrorActionPreference = "Continue"
        Write-Warning "Capturing bounded pre-rollback container diagnostics."
        $psArguments = $compose + @("ps", "-a")
        $logArguments = $compose + @("logs", "--no-color", "--tail", "250", "add-api")
        $diagnostics = @(& docker @psArguments 2>&1) + @(& docker @logArguments 2>&1)
        $material = (($diagnostics | ForEach-Object { "$_" }) -join "`n")
        foreach ($name in @(
            "ADD_POSTGRES_PASSWORD", "ADD_ADMIN_PASSWORD_HASH", "ADD_PII_FERNET_KEY",
            "ADD_PII_LOOKUP_KEY", "ADD_FLEET_ROOT_SECRET", "ADD_ORDS_PASSWORD",
            "ADD_PROVISIONING_PAIRING_SECRET", "ADD_PROVISIONING_INTERNAL_TOKEN"
        )) {
            if ($Environment.ContainsKey($name) -and $Environment[$name]) {
                $material = $material.Replace([string]$Environment[$name], "***")
            }
        }
        $material = [regex]::Replace(
            $material,
            '(?i)(postgres(?:ql)?(?:\+psycopg)?://[^:\s/]+:)[^@\s]+(@)',
            '$1***$2'
        )
        # Keyed lookup values, event fingerprints, and image digests are not
        # useful in a public deployment log. Redact every standalone SHA-256
        # shaped value before the repository enters its guarded public window.
        $material = [regex]::Replace(
            $material,
            '(?i)\b[a-f0-9]{64}\b',
            '<redacted-hash>'
        )
        Write-Host $material
    } finally {
        $ErrorActionPreference = $previousPreference
    }
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

function Assert-DockerCapacity {
    $rawInfo = Invoke-Docker -Arguments @("info", "--format", "{{json .}}") -Capture
    $jsonLine = @($rawInfo -split "`r?`n" | Where-Object {
        $_.TrimStart().StartsWith("{")
    }) | Select-Object -Last 1
    if (-not $jsonLine) {
        throw "Docker capacity could not be read."
    }
    try {
        $dockerInfo = $jsonLine | ConvertFrom-Json
    } catch {
        throw "Docker capacity returned invalid data."
    }

    $minimumMemoryBytes = [int64](48GB)
    $minimumLogicalCpus = 24
    $actualMemoryBytes = [int64]$dockerInfo.MemTotal
    $actualLogicalCpus = [int]$dockerInfo.NCPU
    if ($actualMemoryBytes -lt $minimumMemoryBytes -or
        $actualLogicalCpus -lt $minimumLogicalCpus) {
        $actualMemoryGb = [math]::Round($actualMemoryBytes / 1GB, 1)
        throw (
            "Docker exposes only $actualMemoryGb GB RAM and $actualLogicalCpus logical CPUs. " +
            "Allocate at least 48 GB RAM and 24 logical CPUs to Docker before deploying ADD."
        )
    }
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

function Assert-OrdsAuthentication {
    param(
        [Parameter(Mandatory = $true)][string] $BaseUrl,
        [Parameter(Mandatory = $true)][string] $Username,
        [Parameter(Mandatory = $true)][string] $Password
    )

    $probeEventUid = "7f19a5f6c2d038b37cd20d91d18df7282d27b673a90170ce21c3e44a9bf4be21"
    $headers = @{
        "X-API-Username" = $Username
        "X-API-Password" = $Password
    }
    $body = @{
        event_uids = @($probeEventUid)
    } | ConvertTo-Json -Compress
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Method Post `
            -Uri ($BaseUrl.TrimEnd("/") + "/raw-captures/check") `
            -Headers $headers `
            -ContentType "application/json" `
            -Body $body `
            -TimeoutSec 15
    } catch {
        $status = $null
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $status = [int]$_.Exception.Response.StatusCode
        }
        if ($status) {
            throw "Oracle membership authentication failed with HTTP $status."
        }
        throw "Oracle membership authentication failed before an HTTP response ($($_.Exception.GetType().Name))."
    }

    if ($response.StatusCode -ne 200) {
        throw "Oracle membership authentication returned unexpected HTTP $($response.StatusCode)."
    }
    try {
        $result = $response.Content | ConvertFrom-Json
    } catch {
        throw "Oracle membership authentication returned an invalid JSON response."
    }
    if (
        $result.success -ne $true -or
        [int]$result.received_count -ne 1 -or
        [int]$result.existing_count + [int]$result.missing_count -ne 1
    ) {
        throw "Oracle membership authentication returned an invalid response contract."
    }
    Write-Host "Authenticated Oracle membership probe passed from the ADD host."
}

function Assert-OrdsRepairAuthentication {
    param(
        [Parameter(Mandatory = $true)][string] $BaseUrl,
        [Parameter(Mandatory = $true)][string] $Username,
        [Parameter(Mandatory = $true)][string] $Password
    )

    $headers = @{
        "X-API-Username" = $Username
        "X-API-Password" = $Password
    }
    try {
        [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Method Get `
            -Uri ($BaseUrl.TrimEnd("/") + "/raw-captures/identity-repairs/capabilities") `
            -Headers $headers `
            -TimeoutSec 15
    } catch {
        $status = $null
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $status = [int]$_.Exception.Response.StatusCode
        }
        if ($status) {
            throw "Oracle repair authentication failed with HTTP $status."
        }
        throw "Oracle repair authentication failed before an HTTP response ($($_.Exception.GetType().Name))."
    }
    try {
        $result = $response.Content | ConvertFrom-Json
    } catch {
        throw "Oracle repair authentication returned an invalid JSON response."
    }
    if (
        $response.StatusCode -ne 200 -or
        [string]$result.contract_version -ne "1" -or
        $result.add_only_auth -ne $true -or
        $result.execution_ready -ne $true -or
        [int]$result.batch_limit -ne 100
    ) {
        throw "Oracle repair authentication returned an invalid or unready capability contract."
    }
    Write-Host "Authenticated Oracle repair capability probe passed from the ADD host."
}

function Assert-OrdsContainerAuthentication {
    $probe = @'
import json
import os
import sys
import urllib.error
import urllib.request

probe_uid = "7f19a5f6c2d038b37cd20d91d18df7282d27b673a90170ce21c3e44a9bf4be21"
url = os.environ["ADD_ORDS_BASE_URL"].rstrip("/") + "/raw-captures/check"
request = urllib.request.Request(
    url,
    data=json.dumps({"event_uids": [probe_uid]}).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "X-API-Username": os.environ["ADD_ORDS_USERNAME"],
        "X-API-Password": os.environ["ADD_ORDS_PASSWORD"],
    },
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        status = response.status
        payload = json.load(response)
except urllib.error.HTTPError as exc:
    print(f"ORDS_AUTH_HTTP_{exc.code}")
    sys.exit(1)
except Exception as exc:
    print(f"ORDS_AUTH_ERROR_{type(exc).__name__}")
    sys.exit(1)

valid = (
    status == 200
    and payload.get("success") is True
    and payload.get("received_count") == 1
    and payload.get("existing_count", -1) + payload.get("missing_count", -1) == 1
)
if not valid:
    print("ORDS_AUTH_INVALID_RESPONSE")
    sys.exit(1)
if os.environ.get("ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED", "false").lower() == "true":
    repair_request = urllib.request.Request(
        os.environ["ADD_ORDS_BASE_URL"].rstrip("/")
        + "/raw-captures/identity-repairs/capabilities",
        headers={
            "X-API-Username": os.environ["ADD_ATTENDANCE_REPAIR_ORDS_USERNAME"],
            "X-API-Password": os.environ["ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD"],
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(repair_request, timeout=15) as response:
            repair_status = response.status
            repair_payload = json.load(response)
    except urllib.error.HTTPError as exc:
        print(f"ORDS_REPAIR_AUTH_HTTP_{exc.code}")
        sys.exit(1)
    except Exception as exc:
        print(f"ORDS_REPAIR_AUTH_ERROR_{type(exc).__name__}")
        sys.exit(1)
    repair_valid = (
        repair_status == 200
        and str(repair_payload.get("contract_version")) == "1"
        and repair_payload.get("add_only_auth") is True
        and repair_payload.get("execution_ready") is True
        and repair_payload.get("batch_limit") == 100
    )
    if not repair_valid:
        print("ORDS_REPAIR_AUTH_INVALID_RESPONSE")
        sys.exit(1)
print("ORDS_AUTH_OK")
'@
    $probeEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($probe))
    $probeLauncher = "import base64;exec(base64.b64decode('$probeEncoded'))"
    $result = Invoke-Docker -Arguments ($compose + @(
        "exec", "-T", "add-api", "python", "-c", $probeLauncher
    )) -Capture
    if (@($result -split "\r?\n") -notcontains "ORDS_AUTH_OK") {
        throw "Authenticated Oracle membership probe failed inside the ADD container."
    }
    Write-Host "Authenticated Oracle membership probe passed inside the ADD container."
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
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_ORDS_USERNAME)) {
    $environment["ADD_ORDS_USERNAME"] = $env:ADD_DEPLOY_ORDS_USERNAME
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_ORDS_PASSWORD)) {
    $environment["ADD_ORDS_PASSWORD"] = $env:ADD_DEPLOY_ORDS_PASSWORD
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_ATTENDANCE_REPAIR_ORDS_USERNAME)) {
    $environment["ADD_ATTENDANCE_REPAIR_ORDS_USERNAME"] = $env:ADD_DEPLOY_ATTENDANCE_REPAIR_ORDS_USERNAME
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_ATTENDANCE_REPAIR_ORDS_PASSWORD)) {
    $environment["ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD"] = $env:ADD_DEPLOY_ATTENDANCE_REPAIR_ORDS_PASSWORD
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_FIRMWARE_OTA_ENABLED)) {
    $environment["ADD_FIRMWARE_OTA_ENABLED"] = $env:ADD_DEPLOY_FIRMWARE_OTA_ENABLED
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_FIRMWARE_HIL_ENABLED)) {
    $environment["ADD_FIRMWARE_HIL_ENABLED"] = $env:ADD_DEPLOY_FIRMWARE_HIL_ENABLED
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_FIRMWARE_HIL_TARGET_MAC)) {
    $environment["ADD_FIRMWARE_HIL_TARGET_MAC"] = $env:ADD_DEPLOY_FIRMWARE_HIL_TARGET_MAC
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_COMM_KEY_MANAGEMENT_ENABLED)) {
    $environment["ADD_COMM_KEY_MANAGEMENT_ENABLED"] = $env:ADD_DEPLOY_COMM_KEY_MANAGEMENT_ENABLED
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_COMM_KEY_REVEAL_ENABLED)) {
    $environment["ADD_COMM_KEY_REVEAL_ENABLED"] = $env:ADD_DEPLOY_COMM_KEY_REVEAL_ENABLED
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_COMM_KEY_SECRET_FERNET_KEY)) {
    $environment["ADD_COMM_KEY_SECRET_FERNET_KEY"] = $env:ADD_DEPLOY_COMM_KEY_SECRET_FERNET_KEY
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_RECONCILIATION_ENABLED)) {
    $environment["ADD_RECONCILIATION_ENABLED"] = $env:ADD_DEPLOY_RECONCILIATION_ENABLED
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_RECONCILIATION_SELF_HEALING_ENABLED)) {
    $environment["ADD_RECONCILIATION_SELF_HEALING_ENABLED"] = $env:ADD_DEPLOY_RECONCILIATION_SELF_HEALING_ENABLED
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_RECONCILIATION_DEVICE_CONCURRENCY)) {
    $environment["ADD_RECONCILIATION_DEVICE_CONCURRENCY"] = $env:ADD_DEPLOY_RECONCILIATION_DEVICE_CONCURRENCY
}
$environment["ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED"] = if (
    $env:ADD_DEPLOY_ATTENDANCE_REPAIR_PREVIEW_ENABLED -eq "true"
) { "true" } else { "false" }
$environment["ADD_ATTENDANCE_REPAIR_EXECUTION_ENABLED"] = if (
    $env:ADD_DEPLOY_ATTENDANCE_REPAIR_EXECUTION_ENABLED -eq "true"
) { "true" } else { "false" }
if (
    $environment["ADD_ATTENDANCE_REPAIR_EXECUTION_ENABLED"] -eq "true" -and
    $environment["ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED"] -ne "true"
) {
    throw "Attendance repair execution requires attendance repair preview to be enabled."
}
$environment["ADD_PROVISIONING_ENABLED"] = if ($env:ADD_DEPLOY_PROVISIONING_ENABLED -eq "true") { "true" } else { "false" }
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_PROVISIONING_PAIRING_SECRET)) {
    $environment["ADD_PROVISIONING_PAIRING_SECRET"] = $env:ADD_DEPLOY_PROVISIONING_PAIRING_SECRET
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_PROVISIONING_INTERNAL_TOKEN)) {
    $environment["ADD_PROVISIONING_INTERNAL_TOKEN"] = $env:ADD_DEPLOY_PROVISIONING_INTERNAL_TOKEN
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_DEPLOY_PROVISIONING_COMPANION_RELEASE_PUBLIC_KEY_B64)) {
    $environment["ADD_PROVISIONING_COMPANION_RELEASE_PUBLIC_KEY_B64"] = $env:ADD_DEPLOY_PROVISIONING_COMPANION_RELEASE_PUBLIC_KEY_B64
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_FACTORY_FIRMWARE_STORE_HOST_PATH)) {
    $environment["ADD_FACTORY_FIRMWARE_STORE_HOST_PATH"] = $env:ADD_FACTORY_FIRMWARE_STORE_HOST_PATH
}
if (-not [string]::IsNullOrWhiteSpace($env:ADD_COMPANION_RELEASE_STORE_HOST_PATH)) {
    $environment["ADD_COMPANION_RELEASE_STORE_HOST_PATH"] = $env:ADD_COMPANION_RELEASE_STORE_HOST_PATH
}
# These deployment coordinates are locked product requirements, not operator
# secrets. Canonicalizing them migrates older protected environments safely.
$environment["ADD_ADMIN_COOKIE_SECURE"] = "true"
$environment["ADD_PUBLIC_DEVICE_WS_URL"] = "wss://autoattn.slichealth.com/device/v2/stream"
$environment["ADD_FIRMWARE_PUBLIC_BASE_URL"] = "https://autoattn.slichealth.com"
$environment["ADD_PROVISIONING_PUBLIC_WS_URL"] = "wss://autoattn.slichealth.com/companion/v1/stream"
$environment["ADD_PROVISIONING_WORKER_URL"] = "http://add-provisioner:8097"
$environment["ADD_ORDS_BASE_URL"] = "https://local.slichealth.com/ords/slic_hrm/raw_attn_capture_event"
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
    "ADD_PUBLIC_DEVICE_WS_URL", "ADD_FIRMWARE_PUBLIC_BASE_URL", "ADD_ORDS_BASE_URL", "ADD_ORDS_USERNAME",
    "ADD_ORDS_PASSWORD"
)
foreach ($name in $required) { [void](Require-EnvironmentValue -Map $environment -Name $name) }
if ($environment["ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED"] -eq "true") {
    foreach ($name in @(
        "ADD_ATTENDANCE_REPAIR_ORDS_USERNAME",
        "ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD"
    )) {
        [void](Require-EnvironmentValue -Map $environment -Name $name)
    }
    if (
        $environment["ADD_ATTENDANCE_REPAIR_ORDS_USERNAME"] -eq $environment["ADD_ORDS_USERNAME"] -and
        $environment["ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD"] -eq $environment["ADD_ORDS_PASSWORD"]
    ) {
        throw "Employee attendance repair must not reuse the connector/fleet Oracle credential."
    }
}

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
if (
    $environment["ADD_COMM_KEY_MANAGEMENT_ENABLED"] -eq "true" -or
    $environment["ADD_COMM_KEY_REVEAL_ENABLED"] -eq "true"
) {
    [void](Require-EnvironmentValue -Map $environment -Name "ADD_COMM_KEY_SECRET_FERNET_KEY")
    if ($environment["ADD_COMM_KEY_SECRET_FERNET_KEY"] -notmatch '^[A-Za-z0-9_-]{43}=$') {
        throw "ADD_COMM_KEY_SECRET_FERNET_KEY is not a valid dedicated Fernet key."
    }
    if ($environment["ADD_COMM_KEY_SECRET_FERNET_KEY"] -eq $environment["ADD_PII_FERNET_KEY"]) {
        throw "ADD_COMM_KEY_SECRET_FERNET_KEY must not reuse ADD_PII_FERNET_KEY."
    }
}
if ($environment["ADD_POSTGRES_PASSWORD"].Length -lt 24) {
    throw "ADD_POSTGRES_PASSWORD must contain at least 24 characters."
}
if ($environment["ADD_PROVISIONING_ENABLED"] -eq "true") {
    foreach ($name in @(
        "ADD_PROVISIONING_PAIRING_SECRET",
        "ADD_PROVISIONING_INTERNAL_TOKEN",
        "ADD_PROVISIONING_COMPANION_RELEASE_PUBLIC_KEY_B64",
        "ADD_FIRMWARE_SIGNING_PUBLIC_KEY_PEM_B64",
        "ADD_FACTORY_FIRMWARE_STORE_HOST_PATH",
        "ADD_COMPANION_RELEASE_STORE_HOST_PATH"
    )) {
        [void](Require-EnvironmentValue -Map $environment -Name $name)
    }
    if ($environment["ADD_PROVISIONING_PAIRING_SECRET"].Length -lt 32 -or
        $environment["ADD_PROVISIONING_INTERNAL_TOKEN"].Length -lt 32) {
        throw "Provisioning pairing and internal secrets must contain at least 32 characters."
    }
    if ($environment["ADD_PROVISIONING_COMPANION_RELEASE_PUBLIC_KEY_B64"] -notmatch '^[A-Za-z0-9+/]{43}=$') {
        throw "The companion release Ed25519 public key must be a raw 32-byte base64 value."
    }
    foreach ($pathName in @(
        "ADD_FACTORY_FIRMWARE_STORE_HOST_PATH",
        "ADD_COMPANION_RELEASE_STORE_HOST_PATH"
    )) {
        $path = $environment[$pathName]
        if (-not [IO.Path]::IsPathRooted($path) -or -not (Test-Path -LiteralPath $path -PathType Container)) {
            throw "Production provisioning store $pathName must be an existing absolute directory."
        }
    }
}
if ($environment["ADD_PUBLIC_DEVICE_WS_URL"] -ne "wss://autoattn.slichealth.com/device/v2/stream") {
    throw "ADD_PUBLIC_DEVICE_WS_URL must use the production TLS device stream."
}
if ($environment["ADD_FIRMWARE_PUBLIC_BASE_URL"] -ne "https://autoattn.slichealth.com") {
    throw "ADD_FIRMWARE_PUBLIC_BASE_URL must use the production TLS device gateway."
}
if ($environment["ADD_ORDS_BASE_URL"] -ne "https://local.slichealth.com/ords/slic_hrm/raw_attn_capture_event") {
    throw "ADD_ORDS_BASE_URL must use the validated internal production raw attendance capture endpoint."
}
Assert-OrdsAuthentication `
    -BaseUrl $environment["ADD_ORDS_BASE_URL"] `
    -Username $environment["ADD_ORDS_USERNAME"] `
    -Password $environment["ADD_ORDS_PASSWORD"]
if ($environment["ADD_ATTENDANCE_REPAIR_PREVIEW_ENABLED"] -eq "true") {
    Assert-OrdsRepairAuthentication `
        -BaseUrl $environment["ADD_ORDS_BASE_URL"] `
        -Username $environment["ADD_ATTENDANCE_REPAIR_ORDS_USERNAME"] `
        -Password $environment["ADD_ATTENDANCE_REPAIR_ORDS_PASSWORD"]
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
    Assert-DockerCapacity
    Invoke-Docker -Arguments ($compose + @("config", "--quiet"))

    # Image tags are rollback candidates only after a completed release wrote
    # immutable metadata. A failed first build can leave production-named tags
    # behind, but those images have never passed health checks.
    $previousRelease = Get-ChildItem -LiteralPath $releaseRoot -Filter "*.json" -File |
        Sort-Object LastWriteTimeUtc |
        Select-Object -Last 1
    $preApiImage = if ($previousRelease) { Get-ImageId -Image $apiImage } else { $null }
    $preWebImage = if ($previousRelease) { Get-ImageId -Image $webImage } else { $null }
    $preProvisionerImage = if ($previousRelease) { Get-ImageId -Image $provisionerImage } else { $null }
    $preWatchdogImage = if ($previousRelease) { Get-ImageId -Image $watchdogImage } else { $null }
    $previousProvisioningEnabled = $false
    if ($previousRelease -and (Test-Path -LiteralPath $protectedEnvironment -PathType Leaf)) {
        $previousSettings = Get-EnvironmentMap -Path $protectedEnvironment
        $previousProvisioningEnabled = $previousSettings.ContainsKey("ADD_PROVISIONING_ENABLED") -and
            $previousSettings["ADD_PROVISIONING_ENABLED"] -eq "true"
    }
    if ($previousRelease -and (-not $preApiImage -or -not $preWebImage)) {
        throw "A completed release marker exists, but its rollback image set is incomplete."
    }
    if ($previousProvisioningEnabled -and -not $preProvisionerImage) {
        throw "Provisioning was enabled in the prior release, but its rollback image is missing."
    }
    $rollbackApiTag = "state-life/add-api:rollback-$stamp"
    $rollbackWebTag = "state-life/add-web:rollback-$stamp"
    $rollbackProvisionerTag = "state-life/add-provisioner:rollback-$stamp"
    $rollbackWatchdogTag = "state-life/add-watchdog:rollback-$stamp"
    if ($preApiImage) { Invoke-Docker -Arguments @("tag", $preApiImage, $rollbackApiTag) }
    if ($preWebImage) { Invoke-Docker -Arguments @("tag", $preWebImage, $rollbackWebTag) }
    if ($preProvisionerImage) { Invoke-Docker -Arguments @("tag", $preProvisionerImage, $rollbackProvisionerTag) }
    if ($preWatchdogImage) { Invoke-Docker -Arguments @("tag", $preWatchdogImage, $rollbackWatchdogTag) }

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
        Assert-OrdsContainerAuthentication
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
            previous_provisioner_image = $preProvisionerImage
            previous_watchdog_image = $preWatchdogImage
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
        Write-DockerFailureDiagnostics -Environment $environment
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
            Invoke-Docker -Arguments ($compose + @(
                "stop", "add-watchdog", "add-api", "add-web", "add-provisioner"
            ))
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
                [void](Invoke-DockerProbe -Arguments @("image", "rm", $apiImage))
                [void](Invoke-DockerProbe -Arguments @("image", "rm", $webImage))
                [void](Invoke-DockerProbe -Arguments @("image", "rm", $provisionerImage))
                [void](Invoke-DockerProbe -Arguments @("image", "rm", $watchdogImage))
                throw "Deployment failed; the first release was stopped cleanly: $deploymentError"
            }
            Invoke-Docker -Arguments @("tag", $rollbackApiTag, $apiImage)
            Invoke-Docker -Arguments @("tag", $rollbackWebTag, $webImage)
            if ($preProvisionerImage) {
                Invoke-Docker -Arguments @("tag", $rollbackProvisionerTag, $provisionerImage)
            }
            if ($preWatchdogImage) {
                Invoke-Docker -Arguments @("tag", $rollbackWatchdogTag, $watchdogImage)
            }
            $rollbackServices = @("postgres", "redis", "add-api", "add-web")
            if ($previousProvisioningEnabled) { $rollbackServices += "add-provisioner" }
            if ($preWatchdogImage) { $rollbackServices += "add-watchdog" }
            Invoke-Docker -Arguments ($compose + @("up", "-d", "--no-build", "--remove-orphans") + $rollbackServices)
            Wait-Endpoint -Uri "http://127.0.0.1:8096/health/ready"
            Wait-Endpoint -Uri "http://127.0.0.1:8095/"
            throw "Deployment failed and the previous release was restored: $deploymentError"
        } catch {
            if ($_.Exception.Message -like "Deployment failed and the previous release was restored:*" -or
                $_.Exception.Message -like "Deployment failed; the first release was stopped cleanly:*") {
                throw
            }
            throw "Deployment failed ($deploymentError); rollback also failed: $($_.Exception.Message)"
        }
    }
} finally {
    Remove-Item -LiteralPath $pendingEnvironment -Force -ErrorAction SilentlyContinue
}
