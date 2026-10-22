param([string]$ReadyFile, [string]$ToolArchive, [switch]$GuiSmokeTest)
$ErrorActionPreference = 'Stop'
try {
Write-Output 'SLIC USB Diagnostics v1.3 USB security check: loading Windows Forms.'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object Windows.Forms.Form
$form.Text = 'SLIC Attendance - Lahore 2 USB Diagnostics v1.3 USB security check'
$form.Size = New-Object Drawing.Size(760,560)
$form.StartPosition = 'CenterScreen'
$label = New-Object Windows.Forms.Label
$label.Location = New-Object Drawing.Point(15,15)
$label.Size = New-Object Drawing.Size(710,65)
$label.Text = 'Connect the ESP using a USB DATA cable, then click Start. USB ports are detected automatically. Allow about 4 minutes. The security check reboots the ESP; unplug and reconnect USB when prompted. Live serial logs and network checks are saved in a Desktop ZIP. No installation or password needed.'
$form.Controls.Add($label)
$button = New-Object Windows.Forms.Button
$button.Text = 'Start diagnostics'
$button.Location = New-Object Drawing.Point(15,85)
$button.Size = New-Object Drawing.Size(180,35)
$form.Controls.Add($button)
$box = New-Object Windows.Forms.TextBox
$box.Multiline = $true
$box.ScrollBars = 'Both'
$box.ReadOnly = $true
$box.WordWrap = $false
$box.Location = New-Object Drawing.Point(15,135)
$box.Size = New-Object Drawing.Size(710,370)
$box.Anchor = 'Top,Bottom,Left,Right'
$form.Controls.Add($box)
$ErrorActionPreference = 'Continue'
$form.Add_Shown({
    Write-Output 'Diagnostic GUI is visible.'
    if ($ReadyFile) { [IO.File]::WriteAllText($ReadyFile, 'ready') }
    $form.Activate()
    $form.BringToFront()
    if ($GuiSmokeTest) {
        try {
            $tool = Get-BundledSecurityTool
            $version = & $tool version 2>&1 | Out-String
            [Console]::Out.WriteLine($version)
            if ($LASTEXITCODE -ne 0 -or $version -notmatch '5\.1\.0') { throw 'Bundled offline esptool smoke test failed.' }
        } catch { [Console]::Out.WriteLine(($_ | Out-String)); $script:smokeFailed = $true }
        $script:smokeTimer = New-Object Windows.Forms.Timer
        $script:smokeTimer.Interval = 500
        $script:smokeTimer.Add_Tick({ $script:smokeTimer.Stop(); $form.Close() })
        $script:smokeTimer.Start()
    }
})
function Show-Line($message) {
    if ($box.TextLength -gt 100000) { $box.Text = $box.Text.Substring($box.TextLength - 60000) }
    $box.AppendText("$message`r`n")
    [Windows.Forms.Application]::DoEvents()
}
function Start-SerialCapture {
    param([string]$Destination)
    Start-Job -ArgumentList $Destination -ScriptBlock {
        param($Destination)
        $ErrorActionPreference = 'Stop'
        $ports = @{}
        $retry = @{}
        $totals = @{}
        $identities = @{}
        $started = Get-Date
        $discovery = [DateTime]::MinValue
        $statusPath = Join-Path $Destination 'serial-detection.txt'
        $timingPath = Join-Path $Destination 'serial-chunks.csv'
        'host_time,port,raw_byte_offset,byte_count' | Set-Content $timingPath -Encoding UTF8
        'USB serial capture v1.1: 115200 baud, 8N1, no handshake, DTR/RTS false. No commands, reset or flash requested. USB bridge identity alone does not prove this is an ESP.' | Set-Content $statusPath -Encoding UTF8
        function Note($text) {
            $line = '[' + (Get-Date -Format o) + '] ' + $text
            Add-Content -LiteralPath $statusPath -Value $line -Encoding UTF8
            Write-Output $line
        }
        function Close-Port($name) {
            $entry = $ports[$name]
            if ($entry) {
                try { $entry.Port.Dispose() } catch {}
                try { $entry.Raw.Dispose() } catch {}
                try { $entry.Text.Dispose() } catch {}
                $ports.Remove($name)
            }
        }
        try {
            Note 'Looking for USB serial devices. Connect the ESP now if it is not already connected.'
            while (((Get-Date) - $started).TotalSeconds -lt 210) {
                if (((Get-Date) - $discovery).TotalSeconds -ge 3) {
                    $discovery = Get-Date
                    try {
                        $devices = @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Ports'" | Where-Object {
                            $_.Name -match '\(COM\d+\)' -and $_.DeviceID -match '^(USB|FTDIBUS)\\' -and $_.Name -notmatch 'modem|bluetooth'
                        })
                        foreach ($device in $devices) {
                            $name = [regex]::Match($device.Name, '\((COM\d+)\)').Groups[1].Value
                            $identity = $device.DeviceID
                            if (!$identities.ContainsKey($name) -or $identities[$name] -ne $identity) {
                                if ($ports.ContainsKey($name)) { Close-Port $name }
                                $identities[$name] = $identity
                                Note "Detected $name : $($device.Name); PNP=$identity; Windows error=$($device.ConfigManagerErrorCode)"
                            }
                            if ($ports.ContainsKey($name)) { continue }
                            if ($retry.ContainsKey($name) -and (Get-Date) -lt $retry[$name]) { continue }
                            $retry[$name] = (Get-Date).AddSeconds(10)
                            $port = $null; $raw = $null; $text = $null
                            try {
                                $port = New-Object IO.Ports.SerialPort $name,115200,([IO.Ports.Parity]::None),8,([IO.Ports.StopBits]::One)
                                $port.Handshake = [IO.Ports.Handshake]::None
                                $port.DtrEnable = $false
                                $port.RtsEnable = $false
                                $port.ReadTimeout = 200
                                $port.WriteTimeout = 200
                                $port.ReadBufferSize = 65536
                                $port.Open()
                                $raw = [IO.File]::Open((Join-Path $Destination "serial-$name.raw.bin"), [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::Read)
                                $text = New-Object IO.StreamWriter (Join-Path $Destination "serial-$name.txt"),$true,([Text.Encoding]::UTF8)
                                $text.AutoFlush = $true
                                $text.WriteLine("`r`n--- OPEN $(Get-Date -Format o) $name 115200 8N1; $identity ---")
                                $ports[$name] = @{Port=$port;Raw=$raw;Text=$text;Decoder=([Text.Encoding]::UTF8.GetDecoder())}
                                if (!$totals.ContainsKey($name)) { $totals[$name] = [long]0 }
                                Note "Listening on $name at 115200 baud."
                            } catch {
                                if ($port) { $port.Dispose() }; if ($raw) { $raw.Dispose() }; if ($text) { $text.Dispose() }
                                Note "Cannot open $name : $($_.Exception.Message). Close other serial programs if the port is busy; retrying automatically."
                            }
                        }
                    } catch { Note "USB discovery failed: $($_.Exception.Message)" }
                }
                foreach ($name in @($ports.Keys)) {
                    $entry = $ports[$name]
                    try {
                        $count = [Math]::Min(65536, $entry.Port.BytesToRead)
                        if ($count -gt 0) {
                            $buffer = New-Object byte[] $count
                            $received = $entry.Port.Read($buffer, 0, $count)
                            $entry.Raw.Write($buffer, 0, $received)
                            $entry.Raw.Flush()
                            $totals[$name] += $received
                            Add-Content $timingPath -Value ((Get-Date -Format o) + ',' + $name + ',' + ($totals[$name] - $received) + ',' + $received) -Encoding UTF8
                            $chars = New-Object char[] ([Text.Encoding]::UTF8.GetMaxCharCount($received))
                            $length = $entry.Decoder.GetChars($buffer, 0, $received, $chars, 0, $false)
                            if ($length -gt 0) {
                                $decoded = -join $chars[0..($length-1)]
                                $entry.Text.Write($decoded)
                                Write-Output ("[$name] " + $decoded)
                            }
                        }
                    } catch [TimeoutException] {
                        # A timeout is normal if the device stops transmitting between polling and reading.
                    } catch {
                        Note "Disconnected/read error on $name : $($_.Exception.Message). Waiting to reconnect."
                        Close-Port $name
                    }
                }
                Start-Sleep -Milliseconds 50
            }
        } catch { Note "Serial capture error: $($_.Exception.Message)" }
        finally {
            foreach ($name in @($ports.Keys)) { Close-Port $name }
            $report = @('SERIAL CAPTURE RESULT', "Started: $($started.ToString('o'))", "Finished: $(Get-Date -Format o)")
            if ($identities.Count -eq 0) {
                $report += 'NO USB COM PORT DETECTED. Possible causes: charge-only cable, missing USB bridge driver, wrong connector, disconnected/unpowered device. See USB inventory for Windows device errors. No serial logs were captured.'
            }
            foreach ($name in $identities.Keys) {
                $bytes = 0; if ($totals.ContainsKey($name)) { $bytes = $totals[$name] }
                $report += "$name : $bytes bytes received; $($identities[$name])"
                if ($bytes -eq 0) { $report += "$name : No serial output captured. Check serial-detection.txt for open failures. A silent port can also mean firmware logging is disabled, wrong console connector/baud, or native USB requires DTR. This tool does not assert DTR or reset the ESP." }
            }
            $report += 'Raw .bin files contain the exact received bytes. Text files decode UTF-8 continuously with connection headers; serial-chunks.csv records host timestamps and raw byte offsets. All bytes received by this collector are retained; bytes before connection or during disconnects/driver loss cannot be recovered. The capture does not download stored historical logs. Boot messages appear only if the ESP boots while connected and the port is open. Native USB may disappear during a reboot; early boot bytes may be missed. USB connection/opening can itself affect power or reset lines on some boards.'
            $report += 'Serial logs are unfiltered and may contain device identifiers or firmware-emitted secrets. Share the ZIP only with your support contact.'
            $report | Set-Content (Join-Path $Destination 'serial-result.txt') -Encoding UTF8
            $report | Write-Output
        }
    }
}
function Show-SerialProgress {
    if ($script:serialJob) { Receive-Job -Job $script:serialJob -ErrorAction Continue | ForEach-Object { Show-Line ([string]$_) } }
}
function Save-Check($name, [scriptblock]$action) {
    Show-SerialProgress
    Show-Line "Checking: $name"
    try { $value = & $action 2>&1 | Out-String -Width 240; $value | Set-Content -LiteralPath (Join-Path $script:folder "$name.txt") -Encoding UTF8; return $value }
    catch { "FAILED: $_" | Set-Content -LiteralPath (Join-Path $script:folder "$name.txt"); return "FAILED: $_" }
}
function Run-Command($exe, $arguments) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $exe
    $info.Arguments = $arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $p = New-Object Diagnostics.Process
    $p.StartInfo = $info
    if (!$p.Start()) { throw 'Could not start command' }
    $out = $p.StandardOutput.ReadToEndAsync()
    $err = $p.StandardError.ReadToEndAsync()
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (!$p.HasExited -and $timer.Elapsed.TotalSeconds -lt 20) { Show-SerialProgress; [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }
    if (!$p.HasExited) { $p.Kill(); $p.WaitForExit(); 'COMMAND TIMED OUT after 20 seconds' }
    $out.GetAwaiter().GetResult()
    $err.GetAwaiter().GetResult()
    "Exit code: $($p.ExitCode)"
    $p.Dispose()
}
function Get-BundledSecurityTool {
    if (!$ToolArchive -or !(Test-Path -LiteralPath $ToolArchive)) { throw 'Embedded security tool archive is missing.' }
    if ((Get-FileHash -LiteralPath $ToolArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne '21c65676806ed100a1be5d79aff7128e5afe39d58b24b8b25074aa59b0f10ae3') { throw 'Embedded tool archive failed its integrity check.' }
    $script:toolDir = Join-Path $env:TEMP ('SLIC-USB-Tools-' + [Guid]::NewGuid().ToString('N'))
    Expand-Archive -LiteralPath $ToolArchive -DestinationPath $script:toolDir -ErrorAction Stop
    $tool = Join-Path $script:toolDir 'esptool.exe'
    if ((Get-FileHash -LiteralPath $tool -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'a5aeedac428eee8b45e55dd5ad8a0f4f62adf8bee74f9d02c81e3e088219eee9') { throw 'Embedded esptool failed its integrity check.' }
    return $tool
}
$button.Add_Click({
    $button.Enabled = $false
    $form.ControlBox = $false
    $script:serialJob = $null
    $script:toolDir = $null
    try {
        $desktop = [Environment]::GetFolderPath('Desktop')
        if (!$desktop -or !(Test-Path $desktop)) { $desktop = $env:TEMP }
        $script:folder = Join-Path $desktop ('SLIC-Lahore2-Diagnostics-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,6))
        New-Item -ItemType Directory -Path $script:folder -ErrorAction Stop | Out-Null
        Show-Line "Saving to: $script:folder"
        Show-Line 'Preparing the bundled Espressif 5.1 security check. No Python or downloads needed.'
        $tool = Get-BundledSecurityTool
        Copy-Item -LiteralPath (Join-Path $script:toolDir 'LICENSE') -Destination (Join-Path $script:folder 'esptool-LICENSE.txt')
        'Espressif esptool 5.1.0 source: https://github.com/espressif/esptool/tree/v5.1.0' | Set-Content (Join-Path $script:folder 'esptool-source.txt')
        $candidates = @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Ports'" | Where-Object { $_.Name -match '\(COM\d+\)' -and $_.DeviceID -match 'VID_303A&PID_1001' })
        if ($candidates.Count -eq 1) {
            $com = [regex]::Match($candidates[0].Name, '\((COM\d+)\)').Groups[1].Value
            $null = Save-Check 'usb-security' {
                'Espressif esptool 5.1.0; get-security-info only; no stub, flash writes, erases, key reads or eFuse burns.'
                'Source: https://github.com/espressif/esptool/releases/tag/v5.1.0'
                "Detected: $($candidates[0].Name); $($candidates[0].DeviceID)"
                Run-Command $tool "--chip esp32s3 --port $com --no-stub --before usb-reset --after hard-reset get-security-info"
            }
            Show-Line 'SECURITY CHECK FINISHED: Unplug the ESP USB cable and reconnect it now to return to normal boot.'
            $label.Text = 'Unplug the ESP USB cable, then reconnect it now. Leave connected while logs are collected automatically. No flash or attendance data is written or erased.'
        } else {
            "Security probe skipped: expected exactly one Espressif native USB COM port; found $($candidates.Count). No ROM reset requested by the probe." | Set-Content (Join-Path $script:folder 'usb-security.txt')
            Show-Line 'Security probe skipped: connect only the ESP native USB port, then run again if needed.'
        }
        Show-Line 'Starting serial log capture (210 seconds). USB reconnects are detected automatically.'
        $script:serialJob = Start-SerialCapture -Destination $script:folder
        $null = Save-Check 'computer' { Get-Date -Format o; Get-TimeZone; Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,LastBootUpTime; 'Diagnostic version: 1.3 USB security check; target label: Lahore 2 (device identity not independently verified)'; 'No saved Wi-Fi keys or administrator credentials requested. Raw firmware serial output is unfiltered.' }
        $null = Save-Check 'wifi-service' { Get-Service WlanSvc; Get-NetAdapter | Format-List Name,InterfaceDescription,Status,MacAddress,LinkSpeed,DriverInformation }
        $drivers = Save-Check 'wifi-drivers' { Run-Command netsh.exe 'wlan show drivers' }
        $interfaces = Save-Check 'wifi-interfaces' { Run-Command netsh.exe 'wlan show interfaces' }
        $scans = ''
        for ($i=1; $i -le 3; $i++) {
            $scans += Save-Check "wifi-networks-$i" { Run-Command netsh.exe 'wlan show networks mode=bssid' }
            if ($i -lt 3) { Show-Line 'Waiting for the next Wi-Fi observation...'; for ($j=0;$j -lt 60;$j++) { Show-SerialProgress; Start-Sleep -Milliseconds 100; [Windows.Forms.Application]::DoEvents() } }
        }
        $null = Save-Check 'ip-config' { Run-Command ipconfig.exe '/all' }
        $null = Save-Check 'routes' { Run-Command route.exe 'print' }
        $null = Save-Check 'neighbors' { Run-Command arp.exe '-a' }
        $null = Save-Check 'network-details' { Get-NetIPConfiguration | Format-List *; Get-NetConnectionProfile | Format-List *; Get-DnsClientServerAddress | Format-Table -AutoSize }
        $null = Save-Check 'usb-and-serial-devices' { Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -in @('Ports','USB','Net') -or $_.ConfigManagerErrorCode -ne 0 } | Select-Object Name,PNPClass,Status,ConfigManagerErrorCode,DeviceID | Format-List; 'USB COM ports are captured at 115200 8N1. No serial commands, reset or flash operations are requested.' }
        $null = Save-Check 'wifi-events-7days' { Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-WLAN-AutoConfig/Operational';StartTime=(Get-Date).AddDays(-7)} -MaxEvents 400 -ErrorAction Stop | Select-Object TimeCreated,Id,LevelDisplayName,Message | Format-List }
        $null = Save-Check 'system-network-events' { Get-WinEvent -FilterHashtable @{LogName='System';StartTime=(Get-Date).AddDays(-2)} -MaxEvents 1500 -ErrorAction Stop | Where-Object { $_.ProviderName -match 'WLAN|NDIS|Tcpip|Dhcp|DNS|Kernel-PnP|Netwtw|rtwl' } | Select-Object TimeCreated,Id,ProviderName,Message | Format-List }
        $portal = 'Not checked: this computer has no address on the setup network (192.168.254.x).'
        $localSetup = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -match '^192\.168\.254\.' })
        if ($localSetup.Count -gt 0) {
            $portal = Save-Check 'setup-portal' {
                $request = [Net.HttpWebRequest]::Create('http://192.168.254.1/api/status')
                $request.Proxy = $null; $request.Timeout = 5000; $request.ReadWriteTimeout = 5000; $request.AllowAutoRedirect = $false
                $response = $request.GetResponse()
                try {
                    "HTTP status: $([int]$response.StatusCode)"
                    $reader = New-Object IO.StreamReader($response.GetResponseStream())
                    try {
                        $status = $reader.ReadToEnd() | ConvertFrom-Json
                        # Only capture known diagnostic fields; never include tokens or credentials.
                        $status | Select-Object state,result,message,connected,ssid,ip,recovery,active,busy | ConvertTo-Json -Depth 4
                    } finally { $reader.Dispose() }
                } finally { $response.Dispose() }
            }
        } else { $portal | Set-Content (Join-Path $script:folder 'setup-portal.txt') }
        Show-Line 'Network checks finished. Continuing USB serial capture; keep the ESP connected...'
        $lastProgress = [DateTime]::MinValue
        while ($script:serialJob.State -in @('NotStarted','Running')) {
            Show-SerialProgress
            if (((Get-Date) - $lastProgress).TotalSeconds -ge 10) {
                $lastProgress = Get-Date
                Show-Line 'USB capture is still running. Keep connected; results will open automatically.'
            }
            [Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 200
        }
        Show-SerialProgress
        if ($script:serialJob.State -ne 'Completed') {
            "Serial background job ended in state $($script:serialJob.State). $($script:serialJob.ChildJobs[0].JobStateInfo.Reason)" | Set-Content (Join-Path $script:folder 'serial-job-error.txt')
        }
        $signals = @(Get-ChildItem -LiteralPath $script:folder -Filter 'serial-COM*.txt' | Select-String -Pattern 'wifi.*(fail|disconnect|reason|auth)|no ap found|auth.*fail|brownout|Guru Meditation|panic|abort\(|reset reason|setup.*(disabled|fail)|access point enabled|invalid.*(state|crc)' | Select-Object -First 60)
        if ($signals.Count -gt 0) {
            $signals | ForEach-Object { "$($_.Filename):$($_.LineNumber): $($_.Line)" } | Set-Content (Join-Path $script:folder 'serial-notable-messages.txt') -Encoding UTF8
        }
        $scans += Save-Check 'wifi-networks-after-recovery' { Run-Command netsh.exe 'wlan show networks mode=bssid' }
        $visible = $scans -match 'SLIC\s+ATTENDANCE'
        $summary = New-Object Collections.Generic.List[string]
        $summary.Add('SLIC ATTENDANCE - LAHORE 2 DIAGNOSTIC RESULT')
        $summary.Add("Time: $(Get-Date -Format o)")
        $summary.Add('')
        if (Test-Path (Join-Path $script:folder 'serial-result.txt')) { $summary.Add((Get-Content (Join-Path $script:folder 'serial-result.txt') -Raw)) }
        else { $summary.Add('Serial collection did not finish successfully. See serial-job-error.txt and any partial serial files. USB logs are incomplete.') }
        if ($signals.Count -gt 0) {
            $summary.Add('NOTABLE SERIAL MESSAGES (observations, not a confirmed root cause):')
            $summary.Add((($signals | Select-Object -First 12 | ForEach-Object { $_.Line }) -join "`r`n"))
            $summary.Add('See serial-notable-messages.txt and the complete per-port serial files.')
        }
        if ($visible) {
            $summary.Add('SLIC ATTENDANCE network was observed. See wifi-networks files for names, signal, channel and BSSID. A matching name alone does not prove it is Lahore 2.')
        } else {
            $summary.Add('No SLIC ATTENDANCE network was reported in the early and final Windows observations. This does not prove a hardware fault: scan permissions, radio state, range, firmware and setup mode can all affect visibility.')
        }
        $service = Get-Service WlanSvc -ErrorAction SilentlyContinue
        if (!$service -or $service.Status -ne 'Running') { $summary.Add('ACTION: Windows WLAN service is not running or is unavailable. This computer cannot perform normal Wi-Fi discovery.') }
        if (($scans + $interfaces) -match 'location|permission|access is denied|Access denied') { $summary.Add('ACTION: Windows reported a permission/location restriction. Enable Location services and allow desktop apps to access location in Windows Settings, then run again.') }
        if (($scans + $interfaces) -match 'no wireless interface|no wireless adapter') { $summary.Add('ACTION: Windows reports no usable Wi-Fi adapter. A computer with Wi-Fi is required.') }
        $summary.Add('')
        $summary.Add('Setup portal result: ' + $portal)
        $summary.Add('')
        $summary.Add('Firmware reference: blinking blue means Wi-Fi connection activity. Current firmware normally enables a recovery access point after two minutes continuously disconnected. Installed firmware version cannot be established from the LED or a missing network.')
        $summary.Add('Serial files contain output actually received over USB during this run. Earlier internal logs cannot be recovered by this tool. Port detection is automatic; a USB bridge alone does not establish ESP identity. Windows logs and command failures are included.')
        $summary.Add('No flash, firmware, Wi-Fi configuration, attendance storage or eFuses were written or erased. The security probe requests a USB ROM reboot; unplug and reconnect after that probe to restore normal boot. No automatic upload was performed.')
        $summary.Add('Logs contain network names, IP/MAC addresses, computer/USB identifiers and unfiltered firmware output, which may contain secrets; share only with your support contact.')
        $summary.Add('')
        $summary.Add('Send the ZIP next to this folder to your support contact. Each text file records one check; failed checks are preserved.')
        $summary | Set-Content -LiteralPath (Join-Path $script:folder 'READ-ME-FIRST.txt') -Encoding UTF8
        $zip = $script:folder + '.zip'
        Compress-Archive -LiteralPath $script:folder -DestinationPath $zip -ErrorAction Stop
        Show-Line ''
        Show-Line ($summary -join "`r`n")
        Show-Line "`r`nCOMPLETE - ZIP: $zip"
        Start-Process explorer.exe -ArgumentList ('/select,"' + $zip + '"')
    } catch {
        Show-Line "Collection stopped: $_"
        Show-Line "Any completed logs remain at: $script:folder"
        [Windows.Forms.MessageBox]::Show("Could not finish: $_`r`nCompleted logs remain at: $script:folder", 'SLIC Diagnostics') | Out-Null
    } finally {
        if ($script:serialJob) { Stop-Job $script:serialJob -ErrorAction SilentlyContinue; Remove-Job $script:serialJob -Force -ErrorAction SilentlyContinue; $script:serialJob = $null }
        if ($script:toolDir) { Remove-Item -LiteralPath $script:toolDir -Recurse -Force -ErrorAction SilentlyContinue }
        $form.ControlBox = $true; $button.Enabled = $true; $button.Text = 'Run again'
    }
})
[void]$form.ShowDialog()

Write-Output 'Diagnostic window closed.'
if ($GuiSmokeTest) {
    if ($script:toolDir) { Remove-Item -LiteralPath $script:toolDir -Recurse -Force -ErrorAction SilentlyContinue }
    if ($script:smokeFailed) { exit 1 }
    exit 0
}
} catch {
    Write-Output ('STARTUP FAILURE: ' + ($_ | Out-String))
    try { [Windows.Forms.MessageBox]::Show(('Diagnostics could not start: ' + $_.Exception.Message + [Environment]::NewLine + 'Return to the SLIC launcher and click Open startup log.'), 'SLIC Diagnostics startup failure') | Out-Null } catch {}
    exit 1
}
