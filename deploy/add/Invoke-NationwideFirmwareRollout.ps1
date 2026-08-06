param(
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$GitSha,
    [Parameter(Mandatory = $true)][string]$CanaryCampaignId,
    [Parameter(Mandatory = $true)][string]$AdminPassword,
    [string]$BaseUrl = 'http://127.0.0.1:8096',
    [string]$AdminUsername = 'StateHealthAdmin',
    [ValidateRange(1, 5)][int]$BatchSize = 5,
    [ValidateRange(15, 240)][int]$BatchTimeoutMinutes = 90
)

$ErrorActionPreference = 'Stop'

function Invoke-AddApi {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        [object]$Body = $null
    )
    $parameters = @{
        Method = $Method
        Uri = "$BaseUrl$Path"
        WebSession = $script:Session
        TimeoutSec = 30
    }
    if ($Method -eq 'POST') {
        $parameters.Headers = @{ 'X-CSRF-Token' = $script:CsrfToken }
        $parameters.ContentType = 'application/json'
        $parameters.Body = ($Body | ConvertTo-Json -Depth 8 -Compress)
    }
    return Invoke-RestMethod @parameters
}

function Stop-CampaignsFailClosed {
    param([object[]]$Campaigns, [string]$Reason)
    foreach ($campaign in $Campaigns) {
        if ($campaign.status -in @('ACTIVE', 'PAUSED')) {
            try {
                Invoke-AddApi -Method POST -Path "/api/v1/firmware/campaigns/$($campaign.campaign_id)/cancel" -Body @{
                    reason = $Reason.Substring(0, [Math]::Min(200, $Reason.Length))
                    password = $AdminPassword
                } | Out-Null
            } catch {
                Write-Warning "Could not cancel campaign $($campaign.campaign_id): $($_.Exception.Message)"
            }
        }
    }
}

function Test-ReportedFirmwareVersion {
    param(
        [AllowEmptyString()][string]$ReportedVersion,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )
    $normalized = $ReportedVersion.Trim()
    if ($normalized.StartsWith('zone-lite-')) {
        $normalized = $normalized.Substring(10)
    }
    return $normalized -eq $ExpectedVersion
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must be SemVer without a v prefix.' }
if ($GitSha -notmatch '^[0-9a-f]{40}$') { throw 'GitSha must be an exact lowercase commit SHA.' }
if ($CanaryCampaignId -notmatch '^[0-9a-f]{32}$') { throw 'CanaryCampaignId is invalid.' }
if ([string]::IsNullOrWhiteSpace($AdminPassword)) { throw 'AdminPassword is required.' }

$loginBody = @{ username = $AdminUsername; password = $AdminPassword } | ConvertTo-Json -Compress
$loginResponse = Invoke-WebRequest -UseBasicParsing -Method Post `
    -Uri "$BaseUrl/api/v1/auth/login" -ContentType 'application/json' `
    -Body $loginBody -TimeoutSec 30
$login = $loginResponse.Content | ConvertFrom-Json
if (-not $login.ok -or [string]::IsNullOrWhiteSpace([string]$login.csrf_token)) {
    throw 'Could not establish an authenticated ADD rollout session.'
}
$setCookie = @($loginResponse.Headers['Set-Cookie']) -join ','
$cookieMatch = [regex]::Match($setCookie, '(?:^|[,\s])add_admin=([^;,\s]+)')
if (-not $cookieMatch.Success) {
    throw 'ADD rollout login did not return the administrator cookie.'
}
$script:Session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$adminCookie = New-Object System.Net.Cookie
$adminCookie.Name = 'add_admin'
$adminCookie.Value = $cookieMatch.Groups[1].Value
$adminCookie.Path = '/'
$adminCookie.Secure = $false
$script:Session.Cookies.Add([Uri]$BaseUrl, $adminCookie)
$script:CsrfToken = [string]$login.csrf_token

try {
    $releases = Invoke-AddApi -Method GET -Path '/api/v1/firmware/releases'
    $releaseMatches = @($releases.rows | Where-Object {
        $_.version -eq $Version -and $_.git_sha -eq $GitSha -and $_.state -eq 'AVAILABLE'
    })
    if ($releaseMatches.Count -ne 1) { throw 'Exact AVAILABLE firmware release was not found.' }
    $release = $releaseMatches[0]

    $campaignResponse = Invoke-AddApi -Method GET -Path '/api/v1/firmware/campaigns'
    $canaries = @($campaignResponse.rows | Where-Object {
        $_.campaign_id -eq $CanaryCampaignId -and $_.version -eq $Version -and
        $_.status -eq 'CANCELLED' -and [int]$_.counts.SUCCEEDED -ge 1 -and
        ([string]$_.pause_reason).StartsWith('CANARY_ACCEPTED:')
    })
    if ($canaries.Count -ne 1) { throw 'Accepted production canary evidence was not found.' }
    $active = @($campaignResponse.rows | Where-Object { $_.status -in @('ACTIVE', 'PAUSED') })
    if ($active.Count -ne 0) { throw 'An active or paused firmware campaign already exists; rollout is fail-closed.' }

    $devicesResponse = Invoke-AddApi -Method GET -Path '/api/v1/devices'
    $eligibleDevices = @($devicesResponse.rows | Where-Object {
        [bool]$_.ota_capable -and -not [string]::IsNullOrWhiteSpace([string]$_.zone_id)
    })
    if ($eligibleDevices.Count -eq 0) { throw 'No OTA-capable devices were found.' }
    $allZones = @($eligibleDevices | ForEach-Object { [string]$_.zone_id } | Sort-Object -Unique)
    $pendingZones = @($allZones | Where-Object {
        $zone = $_
        @($eligibleDevices | Where-Object {
            $_.zone_id -eq $zone -and
            -not (Test-ReportedFirmwareVersion -ReportedVersion ([string]$_.firmware_version) -ExpectedVersion $Version)
        }).Count -gt 0
    })

    Write-Host "Nationwide rollout inventory: devices=$($eligibleDevices.Count), zones=$($allZones.Count), pending_zones=$($pendingZones.Count)."
    for ($offset = 0; $offset -lt $pendingZones.Count; $offset += $BatchSize) {
        $last = [Math]::Min($offset + $BatchSize - 1, $pendingZones.Count - 1)
        $batch = @($pendingZones[$offset..$last])
        $created = @()
        $batchAccepted = $false
        try {
            foreach ($zoneId in $batch) {
                $createdCampaign = Invoke-AddApi -Method POST -Path '/api/v1/firmware/campaigns' -Body @{
                    release_id = $release.release_id
                    zone_id = $zoneId
                    reason = "Nationwide Wi-Fi setup portal rollout $Version; batch $([int]($offset / $BatchSize) + 1)."
                    typed_confirmation = $Version
                    password = $AdminPassword
                }
                if ([int]$createdCampaign.eligible -lt 1) { throw "Zone $zoneId has no eligible deployment." }
                $created += [pscustomobject]@{ campaign_id = $createdCampaign.campaign_id; zone_id = $zoneId; status = 'ACTIVE' }
            }

            $deadline = (Get-Date).AddMinutes($BatchTimeoutMinutes)
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 15
                $latest = Invoke-AddApi -Method GET -Path '/api/v1/firmware/campaigns'
                $rows = @($latest.rows | Where-Object { $_.campaign_id -in @($created.campaign_id) })
                if ($rows.Count -ne $created.Count) { throw 'ADD omitted a campaign during batch monitoring.' }
                $failed = @($rows | Where-Object {
                    $_.status -eq 'PAUSED' -or [int]$_.counts.FAILED -gt 0 -or [int]$_.counts.ROLLED_BACK -gt 0
                })
                if ($failed.Count -gt 0) {
                    $detail = ($failed | ForEach-Object { "$($_.zone_id):$($_.pause_reason)" }) -join '; '
                    throw "Batch failed: $detail"
                }
                $complete = @($rows | Where-Object {
                    [int]$_.eligible -gt 0 -and [int]$_.counts.SUCCEEDED -eq [int]$_.eligible
                })
                if ($complete.Count -eq $created.Count) {
                    foreach ($row in $rows) {
                        Invoke-AddApi -Method POST -Path "/api/v1/firmware/campaigns/$($row.campaign_id)/cancel" -Body @{
                            reason = "NATIONWIDE_ACCEPTED: version $Version stable in zone $($row.zone_id)."
                            password = $AdminPassword
                        } | Out-Null
                    }
                    Write-Host "Accepted nationwide batch $([int]($offset / $BatchSize) + 1): $($batch -join ', ')."
                    $batchAccepted = $true
                    break
                }
            }
            if (-not $batchAccepted) { throw 'Batch timed out before every eligible deployment succeeded.' }
        } catch {
            $currentRows = @((Invoke-AddApi -Method GET -Path '/api/v1/firmware/campaigns').rows | Where-Object {
                $_.campaign_id -in @($created.campaign_id)
            })
            Stop-CampaignsFailClosed -Campaigns $currentRows -Reason "NATIONWIDE_HALTED: $($_.Exception.Message)"
            throw
        }
    }

    $finalDevices = @((Invoke-AddApi -Method GET -Path '/api/v1/devices').rows | Where-Object {
        [bool]$_.ota_capable -and $_.zone_id -in $allZones
    })
    $unstable = @($finalDevices | Where-Object {
        -not (Test-ReportedFirmwareVersion -ReportedVersion ([string]$_.firmware_version) -ExpectedVersion $Version) -or
        -not [bool]$_.connected -or
        $_.state -ne 'ONLINE' -or $_.ota_state -ne 'OTA_READY' -or
        -not [bool]$_.zkt.online -or $_.zkt.certification_state -ne 'CERTIFIED' -or
        -not [bool]$_.zkt.snapshot_complete -or
        @('ONLINE', 'LIVE_CAPTURE') -notcontains [string]$_.current_activity
    })
    if ($unstable.Count -gt 0) {
        $identities = ($unstable | ForEach-Object { "$($_.zone_id)/$($_.hardware_id)" }) -join ', '
        throw "Post-rollout stability verification failed for: $identities"
    }
    Write-Host "Nationwide OTA succeeded: version=$Version devices=$($finalDevices.Count) zones=$($allZones.Count)."
} finally {
    try {
        Invoke-AddApi -Method POST -Path '/api/v1/auth/logout' -Body @{} | Out-Null
    } catch {
        Write-Warning 'The temporary ADD rollout session could not be revoked cleanly.'
    }
}
