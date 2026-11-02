$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('add-inspection-' + [guid]::NewGuid().ToString('N'))
$savedProgramData = $env:ProgramData
$savedReceipts = $env:ADD_FACTORY_HIL_RECEIPT_PATH
New-Item -ItemType Directory -Path $root | Out-Null
try {
    $env:ProgramData = $root
    $state = Join-Path $root 'StateLife/AttendanceDeviceDashboard'
    $config = Join-Path $state 'config'
    $factory = Join-Path $root 'factory/test-bundle'
    $companion = Join-Path $root 'companion/1.0.0'
    $receipts = Join-Path $root 'receipts'
    foreach ($path in @($config,$factory,$companion,$receipts)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
    $sentinel = 'DO-NOT-EXPORT-' + [guid]::NewGuid().ToString('N')
    @(
        "ADD_PROVISIONING_ENABLED='false'",
        "ADD_FLEET_ROOT_SECRET='$sentinel'",
        "ADD_PROVISIONING_INTERNAL_TOKEN='$sentinel'",
        "ADD_FACTORY_FIRMWARE_STORE_HOST_PATH='$(Split-Path $factory)'",
        "ADD_COMPANION_RELEASE_STORE_HOST_PATH='$(Split-Path $companion)'"
    ) | Set-Content (Join-Path $config 'add.env')
    @{version='2.4.12';bundle_id='test-bundle';setup_password_supplied=$true;private_field=$sentinel} | ConvertTo-Json | Set-Content (Join-Path $factory 'manifest.json')
    '{}' | Set-Content (Join-Path $factory '.hil-only.json')
    @{version='1.0.0';private_field=$sentinel} | ConvertTo-Json | Set-Content (Join-Path $companion 'manifest.json')
    @{bundle_id='test-bundle';status='PASSED';manifest_sha256='public-digest';checks=@{storage_outbox_preserved=$true}} | ConvertTo-Json | Set-Content (Join-Path $receipts 'incomplete.json')
    $env:ADD_FACTORY_HIL_RECEIPT_PATH = $receipts
    function docker {
        $global:LASTEXITCODE = 0
        if ($args[0] -eq 'image') { return 'state-life/add-provisioner:production' }
        return 'add-provisioner Exited (143)'
    }
    $before = @{}; Get-ChildItem $root -Recurse -File -Force | ForEach-Object { $before[$_.FullName] = (Get-FileHash $_.FullName).Hash }
    $output = (& (Join-Path $PSScriptRoot '../../deploy/add/inspect-provisioning.ps1')) -join "`n"
    if ($output.Contains($sentinel)) { throw 'Inspection exported a protected value.' }
    $report = $output | ConvertFrom-Json
    if ($report.provisioning_enabled -ne $false -or $report.presence.ADD_FLEET_ROOT_SECRET -ne $true -or $report.presence.ADD_PROVISIONING_PAIRING_SECRET -ne $false) { throw 'Presence flags are incorrect.' }
    if ($report.stores[0].manifests[0].quarantined -ne $true) { throw 'Quarantine state is incorrect.' }
    if ($report.factory_receipts[0].missing_checks -notcontains 'all_ranges_readback_verified') { throw 'An incomplete acceptance receipt was not detected.' }
    if ($report.factory_receipts[0].missing_checks -contains 'storage_outbox_preserved') { throw 'A passing check was classified as missing.' }
    if ($report.provisioner_image_present -ne $true) { throw 'Provisioner image presence is incorrect.' }
    foreach ($path in $before.Keys) { if ((Get-FileHash $path).Hash -ne $before[$path]) { throw 'Read-only inspection modified a file.' } }
    'PASS: protected values omitted; presence, quarantine and incomplete receipts reported; files unchanged.'
} finally {
    $env:ProgramData = $savedProgramData
    $env:ADD_FACTORY_HIL_RECEIPT_PATH = $savedReceipts
    Remove-Item $root -Recurse -Force
    Remove-Item Function:docker -ErrorAction SilentlyContinue
}
