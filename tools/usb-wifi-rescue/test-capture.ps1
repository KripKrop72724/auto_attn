$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    Add-Type -AssemblyName System.Windows.Forms
} else {
    Add-Type 'namespace Windows.Forms { public static class Application { public static void DoEvents() {} } }'
}
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'repair.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Wait-ForEspPowerOff','Write-CaptureNote','New-CapturePort','Capture-Repair')) {
    $definition = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    if (-not $definition) { throw "Missing capture function $name" }
    Invoke-Expression $definition.Extent.Text
}
if ($PSVersionTable.PSVersion.Major -eq 5) {
    $native = New-CapturePort 'COM6'
    if ($native.PortName -ne 'COM6' -or $native.BaudRate -ne 115200 -or $native.DtrEnable -or $native.RtsEnable) { throw 'Native SerialPort constructor/settings failed' }
    $native.Dispose()
    Write-Output 'PASS: actual Windows SerialPort constructor and safe line settings'
}
function Say([string]$Text) {}
$label = [pscustomobject]@{Text=''}
$script:folder = Join-Path ([IO.Path]::GetTempPath()) ('SLIC capture test ' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $script:folder | Out-Null
try {
    $script:probes = 0; $script:prompts = 0; $script:cancel = $false
    function Get-ExactUsbPort { param([switch]$AllowMissing)
        $script:probes++
        if ($script:probes -le 2 -or $script:cancel) { return [pscustomobject]@{Port='COM6';Identity='TEST-ESP'} }
        return $null
    }
    function Confirm-EspPowerOff { $script:prompts++; return -not $script:cancel }
    Wait-ForEspPowerOff
    if ($script:prompts -ne 2 -or $script:probes -ne 3) { throw 'Power-off step continued without observing USB removal' }
    $script:cancel = $true; $refused = $false
    try { Wait-ForEspPowerOff } catch { $refused = $_.Exception.Message -match 'cancelled' }
    if (-not $refused) { throw 'Cancelled power-off step continued' }
    Write-Output 'PASS: USB removal required; cancellation stops before capture'

    # Invoke the actual resume branch, with a tripwire for every host command.
    $resumeBranch = $ast.Find({param($n) $n -is [Management.Automation.Language.IfStatementAst] -and $n.Extent.Text.Contains('No USB flash writes, erases, security reset')}, $true)
    if (-not $resumeBranch) { throw 'Resume branch is missing' }
    function Run-Tool { throw 'Resume mode invoked a host command' }
    $ResumeOnly = $true
    Invoke-Expression $resumeBranch.Extent.Text
    Write-Output 'PASS: resume branch invokes no USB host commands'

    function Wait-ForEspPowerOff {}
    function Get-ExactUsbPort { param([switch]$AllowMissing) return [pscustomobject]@{Port='COM6';Identity='TEST-ESP'} }
    function New-CapturePort { param($PortName) throw 'TEST PORT OPEN BLOCKED' }
    $text = Capture-Repair -WaitSeconds 1 -CaptureSeconds 1 -PowerOffPauseSeconds 0
    $notes = Get-Content (Join-Path $script:folder 'serial-detection.txt') -Raw
    if ($notes -notmatch 'TEST PORT OPEN BLOCKED' -or (Get-Item (Join-Path $script:folder 'serial.bin')).Length -ne 0 -or $text.Length -ne 0) { throw 'Silent port failure or false serial success' }
    Write-Output 'PASS: actual capture logs port-open errors and preserves empty raw evidence'

    $script:payload = [Text.Encoding]::UTF8.GetBytes("TEST SERIAL STREAM`r`n")
    function New-CapturePort { param($PortName)
        $p = [pscustomobject]@{BytesToRead=$script:payload.Length}
        $p | Add-Member ScriptMethod Open {}
        $p | Add-Member ScriptMethod Dispose {}
        $p | Add-Member ScriptMethod Read {param($buffer,$offset,$count)
            [Array]::Copy($script:payload,0,$buffer,$offset,$count)
            $this.BytesToRead = 0
            return $count
        }
        return $p
    }
    $text = Capture-Repair -WaitSeconds 1 -CaptureSeconds 1 -PowerOffPauseSeconds 0
    $raw = [IO.File]::ReadAllBytes((Join-Path $script:folder 'serial.bin'))
    if ($text -ne "TEST SERIAL STREAM`r`n" -or [Convert]::ToBase64String($raw) -ne [Convert]::ToBase64String($script:payload)) { throw 'Serial bytes were lost or changed' }
    Write-Output 'PASS: actual capture preserves streamed raw bytes and decoded text'

    # Setup recording must retain the save/reboot evidence and respond to the
    # operator's Finish button without waiting for its fifteen-minute timeout.
    $WatchSetup = $true
    $finishButton = [pscustomobject]@{Enabled=$false}
    $script:finishCapture = $false
    $script:payload = [Text.Encoding]::UTF8.GetBytes("Wi-Fi setup access point enabled`r`nValidated and atomically saved a new Wi-Fi network`r`nConnecting to Wi-Fi SSID `r`n")
    function Say([string]$Text) {
        if ($Text.StartsWith('Setup Wi-Fi is ready.')) { $script:finishCapture = $true }
    }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $text = Capture-Repair -WaitSeconds 1 -CaptureSeconds 900 -PowerOffPauseSeconds 0
    if ($timer.Elapsed.TotalSeconds -gt 5 -or -not $finishButton.Enabled -or $text -notmatch 'Validated and atomically saved' -or $label.Text -notmatch 'SETUP WI-FI READY') { throw 'Setup recording lost save evidence, instructions, or Finish action' }
    Write-Output 'PASS: setup-session capture retains save/reboot evidence and finishes on operator request'
} finally { Remove-Item -LiteralPath $script:folder -Recurse -Force }
