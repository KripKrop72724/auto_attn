# Hardware-free integration checks: actual background job and file outputs,
# with USB discovery and the serial endpoint simulated. Run with pwsh.
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'diagnose.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$function = $ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-SerialCapture'}, $true)
$definition = $function.Extent.Text.Replace('-lt 210', '-lt 1.5')
$temp = Join-Path ([IO.Path]::GetTempPath()) ('slic-serial-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $temp | Out-Null
try {
    foreach ($scenario in @('absent', 'stream', 'busy')) {
        $output = Join-Path $temp $scenario
        New-Item -ItemType Directory $output | Out-Null
        $mock = @'
function Get-CimInstance {
    param($ClassName, $Filter)
    if ('SCENARIO' -eq 'absent') { return }
    [pscustomobject]@{Name='USB Serial ESP (COM7)';DeviceID='USB\VID_10C4&PID_EA60\TEST';ConfigManagerErrorCode=0}
}
function New-FakeSerialPort {
    $fake = [pscustomobject]@{Handshake=$null;DtrEnable=$null;RtsEnable=$null;ReadTimeout=0;WriteTimeout=0;ReadBufferSize=0;Offset=0;Data=([Text.Encoding]::UTF8.GetBytes("ESP WiFi disconnect reason=201`nUnicode: café`n"))}
    $fake | Add-Member ScriptMethod Open { if ('SCENARIO' -eq 'busy') { throw 'Access denied: serial port busy' } }
    $fake | Add-Member ScriptMethod Dispose {}
    $fake | Add-Member ScriptProperty BytesToRead { [Math]::Min(3, $this.Data.Length - $this.Offset) }
    $fake | Add-Member ScriptMethod Read {
        param($buffer,$offset,$count)
        [Array]::Copy($this.Data,$this.Offset,$buffer,$offset,$count)
        $this.Offset += $count
        return $count
    }
    return $fake
}
'@
        $testDefinition = $definition.Replace("`$ErrorActionPreference = 'Stop'", ("`$ErrorActionPreference = 'Stop'`n" + $mock.Replace('SCENARIO', $scenario)))
        $testDefinition = $testDefinition.Replace('New-Object IO.Ports.SerialPort $name,115200,([IO.Ports.Parity]::None),8,([IO.Ports.StopBits]::One)', 'New-FakeSerialPort')
        Invoke-Expression $testDefinition
        $job = Start-SerialCapture $output
        try {
            $null = Wait-Job $job -Timeout 20
            $messages = Receive-Job $job
            if ($job.State -ne 'Completed') { throw "Job failed/hung: $($job.State)" }
            $report = Get-Content (Join-Path $output 'serial-result.txt') -Raw
            switch ($scenario) {
                absent { if ($report -notmatch 'NO USB COM PORT DETECTED') { throw 'Missing no-device result' } }
                busy {
                    $detection = Get-Content (Join-Path $output 'serial-detection.txt') -Raw
                    if ($detection -notmatch 'Cannot open COM7' -or $report -notmatch '0 bytes received') { throw 'Busy port not reported correctly' }
                }
                stream {
                    $expected = [Text.Encoding]::UTF8.GetBytes("ESP WiFi disconnect reason=201`nUnicode: café`n")
                    $actual = [IO.File]::ReadAllBytes((Join-Path $output 'serial-COM7.raw.bin'))
                    if ([Convert]::ToBase64String($actual) -ne [Convert]::ToBase64String($expected)) { throw 'Raw bytes were lost or altered' }
                    $text = Get-Content (Join-Path $output 'serial-COM7.txt') -Raw
                    # Remove host chunk timestamps and opening header to inspect decoder output.
                    $decoded = $text -replace '(?m)^--- OPEN .*---\r?\n','' -replace '\r?\n\[[^\]]+\]\r?\n','' -replace '^\r?\n',''
                    if ($decoded -notmatch 'Unicode: café') { throw 'Split UTF-8 decoding failed' }
                    if ($report -notmatch "$($expected.Length) bytes received") { throw 'Byte count incorrect' }
                }
            }
            "PASS: $scenario"
        } finally { Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force }
    }
    'PASS: PowerShell parse, background capture, absent/busy device reports, exact raw bytes and split UTF-8 decoding.'
} finally { Remove-Item $temp -Recurse -Force }
