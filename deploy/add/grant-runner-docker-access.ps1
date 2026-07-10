param(
    [string] $RunnerGroupName = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script once from an Administrator PowerShell on the production server."
}

if (-not $RunnerGroupName) {
    $runnerGroups = @(Get-LocalGroup | Where-Object { $_.Name -like "GITHUB_ActionsRunner_*" })
    if ($runnerGroups.Count -ne 1) {
        $names = ($runnerGroups | Select-Object -ExpandProperty Name) -join ", "
        throw "Expected one GitHub runner group, found $($runnerGroups.Count): $names. Pass -RunnerGroupName explicitly."
    }
    $RunnerGroupName = $runnerGroups[0].Name
}

$runnerGroup = Get-LocalGroup -Name $RunnerGroupName
$qualifiedRunnerGroup = "$env:COMPUTERNAME\$($runnerGroup.Name)"
$dockerMembers = @(Get-LocalGroupMember -Group "docker-users")
if ($dockerMembers.Name -notcontains $qualifiedRunnerGroup) {
    Add-LocalGroupMember -Group "docker-users" -Member $qualifiedRunnerGroup
    Write-Host "Granted Docker access to $qualifiedRunnerGroup."
} else {
    Write-Host "$qualifiedRunnerGroup already has Docker access."
}

$dockerService = Get-Service -Name "com.docker.service" -ErrorAction SilentlyContinue
if ($dockerService -and $dockerService.Status -ne "Running") {
    Start-Service -Name $dockerService.Name
    Write-Host "Started $($dockerService.Name)."
}

$runnerServices = @(Get-Service | Where-Object { $_.Name -like "actions.runner.*" })
if ($runnerServices.Count -eq 0) {
    throw "No GitHub Actions runner service was found."
}
foreach ($service in $runnerServices) {
    Restart-Service -Name $service.Name -Force
    Write-Host "Restarted $($service.Name) so its new group membership takes effect."
}

Write-Host "Runner Docker access is configured. Re-run the Deploy ADD workflow."
