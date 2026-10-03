$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot '../../deploy/add/backup-restore.ps1')
$compose = @('compose', '--env-file', '.env.test', '-f', 'test.yml')
$script:commands = New-Object System.Collections.ArrayList
$script:failRestore = $false
$script:revision = '20261003_0043'
function Invoke-Docker {
    param([string[]] $Arguments, [switch] $Capture)
    if (-not $Capture) { throw 'Restore diagnostics must be captured.' }
    [void]$script:commands.Add($Arguments)
    if ($script:failRestore -and $Arguments -contains 'pg_restore') { throw 'injected restore failure' }
}
function Get-DatabaseRevision {
    param([string] $DatabaseUser, [string] $DatabaseName)
    if ($DatabaseName -notmatch '^add_restore_verify_[a-f0-9]{32}$') { throw 'Unsafe verification database' }
    return $script:revision
}
foreach ($scenario in @('success', 'restore-fails', 'wrong-revision')) {
    $script:commands.Clear()
    $script:failRestore = $scenario -eq 'restore-fails'
    $script:revision = if ($scenario -eq 'wrong-revision') { 'wrong' } else { '20261003_0043' }
    $caught = $false
    try {
        $result = Assert-DatabaseBackupRestorable -DatabaseUser 'test' -ContainerBackup '/tmp/backup.dump' -ExpectedRevision '20261003_0043'
        if (-not $result) { throw 'Verification timestamp missing.' }
    } catch { $caught = $true }
    if ($caught -ne ($scenario -ne 'success')) { throw "Incorrect result: $scenario" }
    if ($script:commands.Count -ne 3) { throw "Expected create, restore, cleanup: $scenario" }
    if ($script:commands[0] -notcontains 'createdb' -or $script:commands[1] -notcontains 'pg_restore' -or $script:commands[2] -notcontains 'dropdb') {
        throw 'Backup verification order or cleanup is incorrect.'
    }
    $name = $script:commands[0][-1]
    if ($script:commands[2][-1] -cne $name -or $script:commands[1] -notcontains $name) { throw 'Database ownership mismatch.' }
}
Write-Host 'Backup restore gate regressions passed.'
