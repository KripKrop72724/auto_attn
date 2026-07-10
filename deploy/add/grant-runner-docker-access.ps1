$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script once from an Administrator PowerShell on the production server."
}

$runnerServices = @(Get-CimInstance Win32_Service | Where-Object { $_.Name -like "actions.runner.*" })
if ($runnerServices.Count -eq 0) {
    throw "No GitHub Actions runner service was found."
}
if ($runnerServices.StartName -notcontains "NT AUTHORITY\NETWORK SERVICE") {
    $accounts = ($runnerServices | Select-Object -ExpandProperty StartName -Unique) -join ", "
    throw "This remediation is only for a runner using NETWORK SERVICE; found: $accounts"
}

# Docker Desktop authorizes its protected named pipes through docker-users.
# The GITHUB_ActionsRunner_* identity is a local group and Windows rejects
# nesting it inside another machine-local group with STATUS_INVALID_MEMBER.
# Add the actual service logon identity (well-known SID S-1-5-20) instead.
$networkServiceSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-20")
$networkServiceAccount = $networkServiceSid.Translate([Security.Principal.NTAccount]).Value
$dockerMembers = @(Get-LocalGroupMember -Group "docker-users")
if ($dockerMembers.SID.Value -notcontains $networkServiceSid.Value) {
    & net.exe localgroup "docker-users" $networkServiceAccount /add
    if ($LASTEXITCODE -ne 0) {
        throw "net localgroup failed with exit code $LASTEXITCODE"
    }
    Write-Warning "Docker access now applies to services running as NETWORK SERVICE on this host."
} else {
    Write-Host "$networkServiceAccount already has Docker access."
}

$dockerService = Get-Service -Name "com.docker.service" -ErrorAction SilentlyContinue
if ($dockerService -and $dockerService.Status -ne "Running") {
    Start-Service -Name $dockerService.Name
    Write-Host "Started $($dockerService.Name)."
}

foreach ($service in $runnerServices) {
    Restart-Service -Name $service.Name -Force
    Write-Host "Restarted $($service.Name) so its new group membership takes effect."
}

Write-Host "Runner Docker access is configured. Re-run the Deploy ADD workflow."
