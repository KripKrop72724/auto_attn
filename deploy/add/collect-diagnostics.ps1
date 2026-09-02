param(
    [string] $EnvironmentFile,
    [int] $Hours = 24,
    [string] $OutputDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Hours -lt 1 -or $Hours -gt 168) {
    throw "Hours must be between 1 and 168."
}

function Get-EnvironmentMap {
    param([Parameter(Mandatory = $true)][string] $Path)
    $map = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { continue }
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

$candidateFiles = @(
    $EnvironmentFile,
    (Join-Path $env:ProgramData 'StateLife\AttendanceDeviceDashboard\config\add.env'),
    (Join-Path $HOME '.config\auto-attn\add.env'),
    (Join-Path $PWD '.env.add')
)
$resolvedEnvironmentFile = $null
foreach ($candidate in $candidateFiles) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $resolvedEnvironmentFile = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}
if (-not $resolvedEnvironmentFile) {
    throw "ADD environment file was not found. Pass -EnvironmentFile with its protected path."
}

$environment = Get-EnvironmentMap -Path $resolvedEnvironmentFile
$dbUser = if ($environment.ContainsKey('ADD_POSTGRES_USER')) {
    $environment['ADD_POSTGRES_USER']
} else { 'add_service' }
$dbName = if ($environment.ContainsKey('ADD_POSTGRES_DB')) {
    $environment['ADD_POSTGRES_DB']
} else { 'attendance_devices' }

$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $PWD "add-diagnostics-$stamp"
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$secretValues = @()
foreach ($entry in $environment.GetEnumerator()) {
    if (
        $entry.Key -match '(?i)(PASSWORD|SECRET|TOKEN|HASH|FERNET|PRIVATE|CREDENTIAL)' -and
        -not [string]::IsNullOrWhiteSpace([string]$entry.Value) -and
        ([string]$entry.Value).Length -ge 6
    ) {
        $secretValues += [string]$entry.Value
    }
}

function Protect-DiagnosticText {
    param([AllowEmptyString()][string] $Text)
    $protected = $Text
    foreach ($value in $secretValues) {
        $protected = $protected.Replace($value, '***')
    }
    $protected = [regex]::Replace(
        $protected,
        '(?i)(postgres(?:ql)?(?:\+psycopg)?://[^:\s/]+:)[^@\s]+(@)',
        '$1***$2'
    )
    $protected = [regex]::Replace(
        $protected,
        '(?i)(authorization|password|secret|token)(\s*[:=]\s*)[^\s,;]+',
        '$1$2***'
    )
    return $protected
}

function Save-Text {
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [AllowEmptyString()][string] $Content
    )
    $path = Join-Path $OutputDirectory $Name
    Protect-DiagnosticText -Text $Content |
        Set-Content -LiteralPath $path -Encoding UTF8
}

function Save-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)][string] $FilePath,
        [Parameter(Mandatory = $true)][string[]] $Arguments
    )
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        $material = (($output | ForEach-Object { "$_" }) -join "`r`n")
        Save-Text -Name $Name -Content ("exit_code=$exitCode`r`n$material")
    } catch {
        Save-Text -Name $Name -Content ("capture_error=$($_.Exception.GetType().Name)")
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

$compose = @(
    'compose', '--env-file', $resolvedEnvironmentFile,
    '-f', (Join-Path $PWD 'docker-compose.add.yml')
)

$summary = [ordered]@{
    collected_at_utc = [DateTime]::UtcNow.ToString('o')
    hours = $Hours
    machine = $env:COMPUTERNAME
    repository_path = "$PWD"
}
Save-Text -Name '00-summary.json' -Content ($summary | ConvertTo-Json)

Save-NativeCommand -Name '01-docker-version.txt' -FilePath 'docker' -Arguments @('version')
Save-NativeCommand -Name '02-docker-info.txt' -FilePath 'docker' -Arguments @('info')
Save-NativeCommand -Name '03-compose-ps.txt' -FilePath 'docker' -Arguments ($compose + @('ps', '-a'))
Save-NativeCommand -Name '04-docker-stats.txt' -FilePath 'docker' -Arguments @(
    'stats', '--no-stream', '--format',
    'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}\t{{.BlockIO}}\t{{.NetIO}}'
)
Save-NativeCommand -Name '05-docker-disk-usage.txt' -FilePath 'docker' -Arguments @('system', 'df', '-v')

$inspectFormat = @"
service={{index .Config.Labels "com.docker.compose.service"}}
name={{.Name}}
status={{.State.Status}}
health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}
oom_killed={{.State.OOMKilled}}
restart_count={{.RestartCount}}
exit_code={{.State.ExitCode}}
started_at={{.State.StartedAt}}
finished_at={{.State.FinishedAt}}
memory_limit={{.HostConfig.Memory}}
pids_limit={{.HostConfig.PidsLimit}}
log_path={{.LogPath}}
---
"@
Save-NativeCommand -Name '06-container-state.txt' -FilePath 'docker' -Arguments @(
    'inspect', '--format', $inspectFormat,
    'attendance-device-dashboard-postgres-1',
    'attendance-device-dashboard-redis-1',
    'attendance-device-dashboard-add-api-1',
    'attendance-device-dashboard-add-provisioner-1',
    'attendance-device-dashboard-add-web-1',
    'attendance-device-dashboard-add-watchdog-1'
)

$since = [DateTime]::UtcNow.AddHours(-$Hours).ToString('o')
$until = [DateTime]::UtcNow.ToString('o')
Save-NativeCommand -Name '07-docker-events.txt' -FilePath 'docker' -Arguments @(
    'events', '--since', $since, '--until', $until,
    '--filter', 'label=com.docker.compose.project=attendance-device-dashboard'
)
Save-NativeCommand -Name '08-compose-logs.txt' -FilePath 'docker' -Arguments (
    $compose + @('logs', '--no-color', '--timestamps', '--since', "${Hours}h", '--tail', '5000')
)

$postgresSql = @'
SET statement_timeout = '10s';
SET lock_timeout = '2s';
SELECT 'database_size' AS diagnostic_section;
SELECT current_database() AS database, pg_size_pretty(pg_database_size(current_database())) AS size;
SELECT 'database_activity' AS diagnostic_section;
SELECT state, wait_event_type, wait_event, count(*) AS connections,
       max(extract(epoch FROM (clock_timestamp() - xact_start)))::bigint AS longest_xact_seconds
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY state, wait_event_type, wait_event
ORDER BY connections DESC;
SELECT 'blocked_sessions' AS diagnostic_section;
SELECT count(*) AS blocked_sessions
FROM pg_stat_activity
WHERE datname = current_database() AND cardinality(pg_blocking_pids(pid)) > 0;
SELECT 'database_counters' AS diagnostic_section;
SELECT numbackends, xact_commit, xact_rollback, blks_read, blks_hit,
       temp_files, pg_size_pretty(temp_bytes) AS temp_bytes, deadlocks
FROM pg_stat_database WHERE datname = current_database();
SELECT 'largest_tables' AS diagnostic_section;
SELECT relname, n_live_tup, n_dead_tup,
       pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 30;
SELECT 'outbox_status' AS diagnostic_section;
SELECT status, count(*) AS rows, min(created_at) AS oldest_created_at
FROM add_ords_outbox GROUP BY status ORDER BY rows DESC;
SELECT 'operational_row_counts' AS diagnostic_section;
SELECT relname AS relation, n_live_tup AS estimated_rows
FROM pg_stat_user_tables
WHERE relname IN (
  'add_attendance_events', 'add_device_logs', 'add_device_telemetry',
  'add_connector_nonces', 'add_admin_sessions'
)
ORDER BY relname;
SELECT 'postgres_runtime_settings' AS diagnostic_section;
SELECT name, setting, reset_val, unit, source FROM pg_settings
WHERE name IN (
  'max_connections', 'shared_buffers', 'effective_cache_size', 'work_mem',
  'maintenance_work_mem', 'statement_timeout', 'lock_timeout',
  'idle_in_transaction_session_timeout'
) ORDER BY name;
'@
Save-NativeCommand -Name '09-postgres-diagnostics.txt' -FilePath 'docker' -Arguments (
    $compose + @(
        'exec', '-T', 'postgres', 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
        '-P', 'pager=off', '-U', $dbUser, '-d', $dbName, '-c', $postgresSql
    )
)

$probeTargets = @(
    'http://127.0.0.1:8096/health/live',
    'http://127.0.0.1:8096/health/serve',
    'http://127.0.0.1:8096/health/ready',
    'http://127.0.0.1:8095/health/ui',
    'https://autoattn.slichealth.com/health/ready',
    'https://attendancedevices.slichealth.com/health/ui'
)
$probeLines = @()
foreach ($target in $probeTargets) {
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $timing = & curl.exe `
                --output NUL --silent --show-error --max-time 10 `
                --write-out 'status=%{http_code} dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} first_byte=%{time_starttransfer} total=%{time_total}' `
                $target 2>&1
            $probeLines += "$target attempt=$attempt exit_code=$LASTEXITCODE $timing"
        } finally {
            $ErrorActionPreference = $previousPreference
        }
    }
}
Save-Text -Name '10-http-timings.txt' -Content ($probeLines -join "`r`n")

$hostInfo = [ordered]@{
    operating_system = @(
        Get-CimInstance Win32_OperatingSystem |
            Select-Object Caption, Version, LastBootUpTime, TotalVisibleMemorySize, FreePhysicalMemory
    )
    processors = @(
        Get-CimInstance Win32_Processor |
            Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, LoadPercentage
    )
    disks = @(
        Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' |
            Select-Object DeviceID, VolumeName, Size, FreeSpace
    )
    docker_services = @(
        Get-Service -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '(?i)docker|com.docker' } |
            Select-Object Name, Status, StartType
    )
    github_runner_services = @(
        Get-CimInstance Win32_Service |
            Where-Object { $_.Name -like 'actions.runner.*' } |
            Select-Object Name, State, StartMode, StartName
    )
    caddy_services = @(
        Get-CimInstance Win32_Service |
            Where-Object { $_.Name -match '(?i)caddy' } |
            Select-Object Name, State, StartMode, StartName
    )
    relevant_processes = @(
        Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -match '(?i)docker|caddy|runner' } |
            Select-Object ProcessName, Id, CPU, WorkingSet64, PrivateMemorySize64
    )
}
Save-Text -Name '11-host-info.json' -Content ($hostInfo | ConvertTo-Json -Depth 5)

$archive = "$OutputDirectory.zip"
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
Compress-Archive -Path (Join-Path $OutputDirectory '*') -DestinationPath $archive -Force
Write-Host "ADD diagnostics created: $archive"
Write-Host "The protected environment file was used only to address Compose and redact secrets; it was not copied."
