$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('firmware-admission-' + [guid]::NewGuid().ToString('N'))
$check = Join-Path $PSScriptRoot '../../deploy/add/check-deployed-firmware-contract.ps1'
$global:admissionTestCalls = New-Object System.Collections.ArrayList
$global:admissionTestScenario = ''
$previousExitCode = $global:LASTEXITCODE
$previousEncoding = $OutputEncoding
$OutputEncoding = New-Object System.Text.UTF8Encoding($true)
function docker {
    $arguments = @($args)
    [void]$global:admissionTestCalls.Add($arguments)
    $global:LASTEXITCODE = 0
    if ($arguments[0] -eq 'cp') { throw 'docker cp cannot transfer into the production tmpfs mount' }
    if ($arguments[0] -eq 'exec' -and $arguments[1] -eq '-i') {
        if ($OutputEncoding.GetPreamble().Length -ne 0) { throw 'Native transfer encoding must not inject a BOM' }
        if ($arguments[3] -ne 'python' -or $arguments[4] -ne '-c' -or $arguments[5] -notmatch 'b64decode') {
            throw 'Admission transfer must decode bounded public bytes inside the container'
        }
        $encoded = @($input)
        if ($encoded.Count -ne 1) { throw 'Admission transfer requires one bounded encoded payload' }
        $decoded = [Convert]::FromBase64String($encoded[0])
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $actual = ([BitConverter]::ToString($hasher.ComputeHash($decoded))).Replace('-', '').ToLowerInvariant() }
        finally { $hasher.Dispose() }
        if ($actual -cne $arguments[-1] -or $arguments[-2] -notmatch '^/tmp/add-firmware-admission-[a-f0-9]{32}/(manifest.json|manifest.sig|check.py)$') {
            throw 'Transferred metadata identity or destination changed'
        }
        if ($global:admissionTestScenario -eq 'copy-failure') { $global:LASTEXITCODE = 1 }
        return
    }
    if ($arguments[0] -ne 'exec' -or $arguments[2] -ne 'python') { throw 'Unexpected Docker operation' }
    if ($arguments[3] -eq '-c') {
        $path = $arguments[-1]
        if ($path -notmatch '^/tmp/add-firmware-admission-[a-f0-9]{32}$') { throw 'Unsafe container path' }
        if ($arguments[4] -match 'mkdir' -and $global:admissionTestScenario -eq 'prepare-failure') { $global:LASTEXITCODE = 1 }
        if ($arguments[4] -match 'rmtree' -and $global:admissionTestScenario -eq 'cleanup-failure') { $global:LASTEXITCODE = 1 }
        return
    }
    if ($arguments[3] -notmatch '/check.py$') { throw 'Checker must execute a saved file' }
    if ($global:admissionTestScenario -eq 'reject') { $global:LASTEXITCODE = 1; return 'ADD_FIRMWARE_CONTRACT_REJECTED' }
    if ($global:admissionTestScenario -eq 'empty') { return }
    if ($global:admissionTestScenario -eq 'wrong-artifact') { return ('ADD_FIRMWARE_CONTRACT_ACCEPTED:' + ('a'*64)) }
    if ($global:admissionTestScenario -eq 'extra-output') { Write-Output 'unexpected-output' }
    return ('ADD_FIRMWARE_CONTRACT_ACCEPTED:' + $arguments[-1])
}
try {
    New-Item -ItemType Directory -Path $root | Out-Null
    [IO.File]::WriteAllText((Join-Path $root 'manifest.json'), '{}')
    [IO.File]::WriteAllText((Join-Path $root 'manifest.sig'), 'public-test-signature')
    foreach ($case in @('pass', 'reject', 'empty', 'wrong-artifact', 'extra-output', 'copy-failure', 'prepare-failure', 'cleanup-failure')) {
        $global:admissionTestScenario = $case
        $global:admissionTestCalls.Clear()
        $caught = $false
        $failure = ''
        try { & $check -SourceDirectory $root } catch { $caught = $true; $failure = $_.Exception.Message }
        if ($OutputEncoding.GetPreamble().Length -eq 0) { throw 'Caller encoding was not restored' }
        if ($caught -ne ($case -ne 'pass')) { throw "Incorrect deployed admission result: $case ($failure)" }
        if ($case -eq 'prepare-failure') {
            if ($global:admissionTestCalls.Count -ne 1) { throw 'Uncreated temporary files cannot be cleaned' }
        } else {
            if ($global:admissionTestCalls[-1][4] -notmatch 'rmtree') { throw 'Missing temporary-file cleanup' }
            if ($global:admissionTestCalls[-1][-1] -cne $global:admissionTestCalls[0][-1]) { throw 'Cleanup scope mismatch' }
            if ($case -ne 'copy-failure' -and $global:admissionTestCalls.Count -ne 6) { throw 'Wrong admission command count' }
        }
    }
    Write-Host 'Deployed firmware admission regressions passed.'
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force
    Remove-Item Function:docker
    Remove-Variable -Name admissionTestCalls,admissionTestScenario -Scope Global
    # Failure scenarios deliberately set the native-command exit status. Do
    # not leak a mocked failure into GitHub's shell wrapper after tests pass.
    $global:LASTEXITCODE = $previousExitCode
    $OutputEncoding = $previousEncoding
}
