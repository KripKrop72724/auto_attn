$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('firmware-admission-' + [guid]::NewGuid().ToString('N'))
$check = Join-Path $PSScriptRoot '../../deploy/add/check-deployed-firmware-contract.ps1'
$global:admissionTestCalls = New-Object System.Collections.ArrayList
$global:admissionTestScenario = ''
$global:admissionTestBytes = @{}
$previousExitCode = $global:LASTEXITCODE
$previousEncoding = $OutputEncoding
$OutputEncoding = New-Object System.Text.UTF8Encoding($true)
function docker {
    $arguments = @($args)
    [void]$global:admissionTestCalls.Add($arguments)
    $global:LASTEXITCODE = 0
    if ($arguments[0] -eq 'cp' -or $arguments[1] -eq '-i') { throw 'Admission transfer cannot rely on docker cp or transformed stdin' }
    if ($arguments[0] -ne 'exec' -or $arguments[2] -ne 'python') { throw 'Unexpected Docker operation' }
    if ($arguments[3] -eq '-c' -and $arguments[4] -match 'b64decode') {
        $path = $arguments[-3]
        $chunk = $arguments[-2]
        $offset = [int]$arguments[-1]
        if ($path -notmatch '^/tmp/add-firmware-admission-[a-f0-9]{32}/(manifest.json|manifest.sig|check.py)$' -or
            $chunk.Length -gt 3072) { throw 'Unsafe admission chunk identity or size' }
        $decoded = [Convert]::FromBase64String($chunk)
        $stored = $global:admissionTestBytes[$path]
        if ($null -eq $stored) { $stored = New-Object byte[] 0 }
        if ($stored.Length -ne $offset -or $offset + $decoded.Length -gt 65536) { throw 'Admission chunks lost their ordering or bound' }
        $global:admissionTestBytes[$path] = [byte[]]($stored + $decoded)
        if ($global:admissionTestScenario -eq 'copy-failure') { $global:LASTEXITCODE = 1 }
        return
    }
    if ($arguments[3] -eq '-c' -and $arguments[4] -match 'read_bytes') {
        $path = $arguments[-2]
        if (-not $global:admissionTestBytes.ContainsKey($path)) { throw 'Untransferred metadata cannot be verified' }
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $actual = ([BitConverter]::ToString($hasher.ComputeHash($global:admissionTestBytes[$path]))).Replace('-', '').ToLowerInvariant() }
        finally { $hasher.Dispose() }
        if ($actual -cne $arguments[-1]) { throw 'Transferred metadata digest changed' }
        if ($global:admissionTestScenario -eq 'hash-failure') { $global:LASTEXITCODE = 1 }
        return
    }
    if ($arguments[3] -eq '-c') {
        $path = $arguments[-1]
        if ($path -notmatch '^/tmp/add-firmware-admission-[a-f0-9]{32}$') { throw 'Unsafe container path' }
        if ($arguments[4] -match 'mkdir' -and $global:admissionTestScenario -eq 'prepare-failure') { $global:LASTEXITCODE = 1 }
        if ($arguments[4] -match 'rmtree' -and $global:admissionTestScenario -eq 'cleanup-failure') { $global:LASTEXITCODE = 1 }
        return
    }
    if ($arguments[3] -notmatch '/check.py$' -or $global:admissionTestBytes.Count -ne 3) { throw 'Checker must execute three fully transferred public files' }
    if ($global:admissionTestScenario -eq 'reject') { $global:LASTEXITCODE = 1; return 'ADD_FIRMWARE_CONTRACT_REJECTED' }
    if ($global:admissionTestScenario -eq 'empty') { return }
    if ($global:admissionTestScenario -eq 'wrong-artifact') { return ('ADD_FIRMWARE_CONTRACT_ACCEPTED:' + ('a'*64)) }
    if ($global:admissionTestScenario -eq 'extra-output') { Write-Output 'unexpected-output' }
    return ('ADD_FIRMWARE_CONTRACT_ACCEPTED:' + $arguments[-1])
}
try {
    New-Item -ItemType Directory -Path $root | Out-Null
    # Force several chunks and non-ASCII input; the Windows command arguments
    # contain only base64 and must reconstruct the original bytes exactly.
    [IO.File]::WriteAllBytes((Join-Path $root 'manifest.json'), [Text.Encoding]::UTF8.GetBytes(('x' * 12000) + [char]0x0627))
    [IO.File]::WriteAllText((Join-Path $root 'manifest.sig'), 'public-test-signature')
    foreach ($case in @('pass', 'reject', 'empty', 'wrong-artifact', 'extra-output', 'copy-failure', 'hash-failure', 'prepare-failure', 'cleanup-failure')) {
        $global:admissionTestScenario = $case
        $global:admissionTestCalls.Clear()
        $global:admissionTestBytes.Clear()
        $caught = $false
        $failure = ''
        try { & $check -SourceDirectory $root } catch { $caught = $true; $failure = $_.Exception.Message }
        if ($OutputEncoding.GetPreamble().Length -eq 0) { throw 'Transfer unexpectedly changed caller encoding' }
        if ($caught -ne ($case -ne 'pass')) { throw "Incorrect deployed admission result: $case ($failure)" }
        if ($case -eq 'prepare-failure') {
            if ($global:admissionTestCalls.Count -ne 1) { throw 'Uncreated temporary files cannot be cleaned' }
        } else {
            if ($global:admissionTestCalls[-1][4] -notmatch 'rmtree') { throw 'Missing temporary-file cleanup' }
            if ($global:admissionTestCalls[-1][-1] -cne $global:admissionTestCalls[0][-1]) { throw 'Cleanup scope mismatch' }
            if ($case -eq 'pass' -and $global:admissionTestCalls.Count -lt 12) { throw 'Multi-chunk transfer was not exercised' }
        }
    }
    Write-Host 'Deployed firmware admission regressions passed.'
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force
    Remove-Item Function:docker
    Remove-Variable -Name admissionTestCalls,admissionTestScenario,admissionTestBytes -Scope Global
    $global:LASTEXITCODE = $previousExitCode
    $OutputEncoding = $previousEncoding
}
