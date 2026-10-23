param([string]$ReadyFile, [string]$ToolArchive, [switch]$GuiSmokeTest, [switch]$WatchSetup, [switch]$ResumeOnly)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$script:active = $false
$script:folder = $null
$target = '__TARGET_MAC__'
$archiveDigest = '__ARCHIVE_SHA256__'
$imageDigest = '__IMAGE_SHA256__'
$form = New-Object Windows.Forms.Form
$form.Text = 'SLIC Attendance - USB Wi-Fi repair'
$form.Size = New-Object Drawing.Size(760,600)
$form.StartPosition = 'CenterScreen'
$label = New-Object Windows.Forms.Label
$label.Text = "Connect the Lahore ESP by USB, then click Start repair.`r`nFollow the USB instructions shown here. Attendance storage is preserved."
$label.Location = New-Object Drawing.Point(15,15)
$label.Size = New-Object Drawing.Size(710,55)
$form.Controls.Add($label)
$button = New-Object Windows.Forms.Button
$button.Text = 'Start repair'
if ($ResumeOnly) {
    $form.Text = 'SLIC Attendance - Resume installed USB repair'
    $label.Text = "The repair was installed by v1.1. Click Resume and verify.`r`nFollow the power-off and reconnect instructions."
    $button.Text = 'Resume and verify'
}
$button.Location = New-Object Drawing.Point(15,80)
$button.Size = New-Object Drawing.Size(180,35)
$form.Controls.Add($button)
$finishButton = New-Object Windows.Forms.Button
$finishButton.Text = 'Finish and save logs'
$finishButton.Location = New-Object Drawing.Point(210,80)
$finishButton.Size = New-Object Drawing.Size(220,35)
$finishButton.Enabled = $false
$finishButton.Visible = [bool]$WatchSetup
$finishButton.Add_Click({ $script:finishCapture = $true; $finishButton.Enabled = $false })
$form.Controls.Add($finishButton)
$script:finishCapture = $false
$box = New-Object Windows.Forms.TextBox
$box.Multiline = $true
$box.ReadOnly = $true
$box.ScrollBars = 'Both'
$box.Location = New-Object Drawing.Point(15,130)
$box.Size = New-Object Drawing.Size(710,400)
$box.Anchor = 'Top,Bottom,Left,Right'
$form.Controls.Add($box)
function Say([string]$Text) {
    $line = '[' + (Get-Date -Format HH:mm:ss) + '] ' + $Text
    $box.AppendText($line + "`r`n")
    if ($script:folder) { Add-Content -LiteralPath (Join-Path $script:folder 'progress.txt') -Value $line -Encoding UTF8 }
    [Windows.Forms.Application]::DoEvents()
}
function Get-ExactUsbPort([switch]$AllowMissing) {
    $devicesFound = @()
    foreach ($device in @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Ports'")) {
        # Windows can retain disconnected COM entries. Do not let a stale
        # device hide removal or make the reconnected target look ambiguous.
        if ($device.PSObject.Properties['Present'] -and $device.Present -eq $false) { continue }
        if ($device.PSObject.Properties['ConfigManagerErrorCode'] -and $null -ne $device.ConfigManagerErrorCode -and $device.ConfigManagerErrorCode -ne 0) { continue }
        if ($device.Name -notmatch '\((COM\d+)\)' -or $device.DeviceID -notmatch '^USB\\VID_303A&PID_1001') { continue }
        $portName = [regex]::Match($device.Name, '\((COM\d+)\)').Groups[1].Value
        $ancestor = $device.DeviceID
        $identity = $null
        for ($depth = 0; $depth -lt 5 -and $ancestor; $depth++) {
            if ($ancestor -match '^USB\\VID_303A&PID_1001\\([0-9A-F:]{17}|[0-9A-F]{12})$') {
                $identity = $Matches[1].Replace(':','').ToLowerInvariant()
                break
            }
            $ancestor = (Get-PnpDeviceProperty -InstanceId $ancestor -KeyName 'DEVPKEY_Device_Parent' -ErrorAction Stop).Data
        }
        if ($identity -eq $target.Replace(':','')) {
            $devicesFound += [pscustomobject]@{ Port=$portName; Identity=$device.DeviceID }
        }
    }
    if ($devicesFound.Count -eq 0 -and $AllowMissing) { return $null }
    if ($devicesFound.Count -ne 1) { throw 'The expected ESP was not uniquely identified. Check its USB cable and close other serial programs, then run this tool again.' }
    return $devicesFound[0]
}
function Run-Tool([string]$Stage, [string[]]$Arguments, [int]$TimeoutSeconds = 240) {
    $stdout = Join-Path $script:folder ($Stage + '.stdout.txt')
    $stderr = Join-Path $script:folder ($Stage + '.stderr.txt')
    # All arguments are generated from fixed choices, a COM-number match and
    # filenames in the private extraction directory. Never accept arbitrary CLI.
    $quoted = @($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    # Own the process handle from Start through WaitForExit. Start-Process's
    # detached Process plus Refresh can lose ExitCode on Windows PowerShell,
    # causing a successful security query to be mistaken for failure.
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $script:tool
    $info.Arguments = $quoted
    $info.WorkingDirectory = $script:extract
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    $outTask = $null; $errTask = $null
    $outText = ''; $errText = ''; $exitCode = $null
    $timedOut = $false; $started = $false
    try {
        if (-not $process.Start()) { throw "Could not start USB operation during $Stage." }
        $started = $true
        # Drain both pipes concurrently, including large flash progress output.
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $next = Get-Date
        while (-not $process.HasExited) {
            if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                $timedOut = $true
                $process.Kill()
                break
            }
            if ((Get-Date) -ge $next) { Say "$Stage is running. Keep USB connected..."; $next = (Get-Date).AddSeconds(10) }
            [Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 100
        }
        $process.WaitForExit()
        $exitCode = $process.ExitCode
    } finally {
        if ($started -and -not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
        if ($outTask) { $outText = $outTask.GetAwaiter().GetResult() }
        if ($errTask) { $errText = $errTask.GetAwaiter().GetResult() }
        [IO.File]::WriteAllText($stdout, $outText, (New-Object Text.UTF8Encoding($false)))
        [IO.File]::WriteAllText($stderr, $errText, (New-Object Text.UTF8Encoding($false)))
        @{ exit_code=$exitCode; timed_out=$timedOut } | ConvertTo-Json | Set-Content (Join-Path $script:folder ($Stage + '.process.json')) -Encoding UTF8
        $process.Dispose()
    }
    if ($timedOut) { throw "USB operation timed out during $Stage. Keep the logs for support." }
    if ($null -eq $exitCode -or $exitCode -ne 0) { throw "USB operation failed during $Stage (exit code $exitCode). Saved logs contain the details." }
    return $outText + $errText
}
function Assert-SecurityProfile([string]$Report) {
    if ($Report -notmatch 'Secure Download Mode' -or $Report -notmatch 'Flags:\s+0x000000c5\b' -or
        $Report -notmatch 'Key Purposes:\s*\(8, 9, 10, 11, 0, 0, 12\)' -or
        $Report -notmatch 'Secure Boot:\s+Enabled' -or $Report -notmatch 'Flash Encryption:\s+Disabled') {
        throw 'USB security differs from the tested device profile. No flash writes were made.'
    }
}
function Write-CaptureNote([string]$Text) {
    Add-Content -LiteralPath (Join-Path $script:folder 'serial-detection.txt') -Value ((Get-Date -Format o) + ' ' + $Text) -Encoding UTF8
}
function Confirm-EspPowerOff {
    $answer = [Windows.Forms.MessageBox]::Show(
        "Unplug the ESP USB cable NOW. If the ESP has another power lead, remove that ESP power lead too.`r`n`r`nKeep the ESP unplugged, then click OK. Reconnect only when the next screen asks. Do not hold BOOT.",
        'Step 1 of 2 - Unplug the ESP and keep it unplugged',
        [Windows.Forms.MessageBoxButtons]::OKCancel,
        [Windows.Forms.MessageBoxIcon]::Information)
    return $answer -eq [Windows.Forms.DialogResult]::OK
}
function Wait-ForEspPowerOff {
    $label.Text = "STEP 1: Unplug the ESP USB and any other ESP power.`r`nKeep it unplugged until this window asks you to reconnect."
    while ($null -ne (Get-ExactUsbPort -AllowMissing)) {
        Say 'ACTION REQUIRED: unplug the ESP. Keep it unplugged, then click OK in the instructions.'
        if (-not (Confirm-EspPowerOff)) { throw 'Power-cycle step cancelled. The installed repair was left in place; run Resume and verify again when ready.' }
        [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 500
    }
    Write-CaptureNote 'Expected ESP USB removal detected. Waiting five seconds before reconnect instruction.'
}
function New-CapturePort([string]$PortName) {
    $port = New-Object System.IO.Ports.SerialPort $PortName,115200,([IO.Ports.Parity]::None),8,([IO.Ports.StopBits]::One)
    $port.DtrEnable = $false; $port.RtsEnable = $false
    $port.Handshake = [IO.Ports.Handshake]::None
    $port.ReadTimeout = 200
    return $port
}
function Capture-Repair([int]$WaitSeconds = 90, [int]$CaptureSeconds = 240, [int]$PowerOffPauseSeconds = 5) {
    Wait-ForEspPowerOff
    $pause = [Diagnostics.Stopwatch]::StartNew()
    while ($pause.Elapsed.TotalSeconds -lt $PowerOffPauseSeconds) { [Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }
    $label.Text = "STEP 2: Reconnect the ESP USB NOW, WITHOUT holding BOOT.`r`nThen leave it connected while data and setup Wi-Fi are checked."
    Say 'Reconnect the ESP USB now, WITHOUT BOOT. Leave any second ESP power lead disconnected for this test.'
    Say 'Waiting for the ESP to check its data and restart the original firmware...'
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    $raw = [IO.File]::Open((Join-Path $script:folder 'serial.bin'), [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $text = New-Object Text.StringBuilder
    $serial = $null
    $next = Get-Date
    $firstOpen = $false
    $setupAnnounced = $false
    $lastError = ''; $nextErrorLog = Get-Date
    try {
        while ((Get-Date) -lt $deadline -and -not $script:finishCapture) {
            try {
                if (-not $serial) {
                    $device = Get-ExactUsbPort -AllowMissing
                    if ($null -eq $device) { throw 'Expected ESP USB is not connected yet. Reconnect its USB cable WITHOUT BOOT.' }
                    $serial = New-CapturePort $device.Port
                    $serial.Open()
                    Write-CaptureNote ("Opened $($device.Port); 115200 baud; DTR=False; RTS=False; identity=$($device.Identity)")
                    if (-not $firstOpen) {
                        $firstOpen = $true
                        $deadline = (Get-Date).AddSeconds($CaptureSeconds)
                        $label.Text = "ESP USB detected. Keep it connected.`r`nChecking the repair, saved data and setup Wi-Fi (up to 4 minutes)."
                        Say 'ESP reconnected. Serial recorder is open. Keep USB connected.'
                        if ($WatchSetup) {
                            $finishButton.Enabled = $true
                            $label.Text = "Keep USB connected. Setup Wi-Fi should appear in about 2 minutes.`r`nUse a phone or this PC to save site Wi-Fi. Keep this recorder running."
                            Say 'Setup recording: use a phone or this PC for the setup page; keep USB connected. Recording can run for 15 minutes. After saving Wi-Fi and waiting two minutes, click Finish and save logs.'
                        }
                    }
                }
                $count = $serial.BytesToRead
                if ($count -gt 0) {
                    $bytes = New-Object byte[] ([Math]::Min($count,65536))
                    $count = $serial.Read($bytes,0,$bytes.Length)
                    $raw.Write($bytes,0,$count); $raw.Flush()
                    [void]$text.Append([Text.Encoding]::UTF8.GetString($bytes,0,$count))
                    if ($WatchSetup -and $text.ToString().Contains('access point enabled') -and -not $setupAnnounced) {
                        $setupAnnounced = $true
                        $label.Text = "SETUP WI-FI READY: connect a phone or this PC to SLIC ATTENDANCE-16D5.`r`nOpen 192.168.254.1 and save the 2.4 GHz site Wi-Fi. Keep USB connected."
                        Say 'Setup Wi-Fi is ready. On a phone or this PC open http://192.168.254.1 and save the correct 2.4 GHz network. Do not close this EXE or unplug USB. After saving, wait two minutes, then click Finish and save logs.'
                    }
                }
            } catch {
                $detail = $_.Exception.GetType().FullName + ': ' + $_.Exception.Message
                if ($detail -ne $lastError -or (Get-Date) -ge $nextErrorLog) {
                    Write-CaptureNote $detail
                    Say ('USB recorder: ' + $_.Exception.Message)
                    $lastError = $detail; $nextErrorLog = (Get-Date).AddSeconds(5)
                }
                if ($serial) { $serial.Dispose(); $serial = $null }
            }
            if ((Get-Date) -ge $next) { Say 'Waiting for the repair result. Keep the ESP connected.'; $next = (Get-Date).AddSeconds(10) }
            [Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 100
        }
    } finally {
        if ($serial) { $serial.Dispose() }
        $raw.Dispose()
        $completeText = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes((Join-Path $script:folder 'serial.bin')))
        [IO.File]::WriteAllText((Join-Path $script:folder 'serial.txt'), $completeText, (New-Object Text.UTF8Encoding($false)))
        Write-CaptureNote ("Capture finished. Received $($completeText.Length) decoded characters; raw bytes saved in serial.bin.")
    }
    return $completeText
}
$form.Add_FormClosing({ param($sender,$event) if ($script:active) { $event.Cancel = $true; Say 'Please wait for the repair and log collection to finish.' } })
$form.Add_Shown({
    if ($ReadyFile) { [IO.File]::WriteAllText($ReadyFile,'ready') }
    if ($GuiSmokeTest) {
        Write-Output 'Repair GUI is visible. Smoke mode performs no USB operation.'
        $script:timer = New-Object Windows.Forms.Timer
        $script:timer.Interval = 500
        $script:timer.Add_Tick({ $script:timer.Stop(); $form.Close() })
        $script:timer.Start()
    }
})
$button.Add_Click({
    $script:active = $true
    $button.Enabled = $false
    $label.Text = "Repair in progress. Keep the USB cable connected.`r`nThe window will tell you when to reconnect it."
    $script:extract = Join-Path ([IO.Path]::GetTempPath()) ('SLIC-Wifi-' + [guid]::NewGuid().ToString('N'))
    try {
        $script:folder = Join-Path ([Environment]::GetFolderPath('Desktop')) ('SLIC-WiFi-Repair-' + (Get-Date -Format yyyyMMdd-HHmmss) + '-' + [guid]::NewGuid().ToString('N').Substring(0,6))
        New-Item -ItemType Directory -Path $script:folder | Out-Null
        Say 'Checking the bundled offline repair files...'
        if ((Get-FileHash -LiteralPath $ToolArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archiveDigest) { throw 'Bundled archive verification failed. No repair was started.' }
        Expand-Archive -LiteralPath $ToolArchive -DestinationPath $script:extract
        $script:tool = Join-Path $script:extract 'esptool.exe'
        $image = Join-Path $script:extract 'rescue.bin'
        if ((Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant() -ne $imageDigest) { throw 'Repair image verification failed. No repair was started.' }
        $device = Get-ExactUsbPort
        Say "Correct ESP detected on $($device.Port)."
        $device | ConvertTo-Json | Set-Content (Join-Path $script:folder 'usb-identity.json') -Encoding UTF8
        if ($ResumeOnly) {
            Say 'Resume mode: using the repair already installed by v1.1. No USB flash writes, erases, security reset or re-provisioning will be performed by this EXE.'
        } else {
        Say 'Checking USB security...'
        $common = @('--chip','esp32s3','--port',$device.Port,'--no-stub','--after','no-reset')
        $security = Run-Tool 'security-check' ($common + @('get-security-info'))
        Assert-SecurityProfile $security
        $deviceAgain = Get-ExactUsbPort
        if ($deviceAgain.Port -ne $device.Port -or $deviceAgain.Identity -ne $device.Identity) { throw 'USB identity changed. No flash writes were made.' }
        $common += @('--before','no-reset')
        Say 'Installing the bounded Wi-Fi repair. Do not unplug USB during this step.'
        $null = Run-Tool 'install-rescue' ($common + @('write-flash','--flash-size','16MB','--flash-mode','keep','--flash-freq','keep','--no-compress','0x20000','rescue.bin','0x2a0000','rescue.bin'))
        [IO.File]::WriteAllBytes((Join-Path $script:extract 'empty-sector.bin'), [byte[]](@(255)*4096))
        foreach ($offset in @('0x17000','0x18000')) {
            $null = Run-Tool ('select-rescue-' + $offset) ($common + @('write-flash','--flash-size','16MB','--flash-mode','keep','--flash-freq','keep','--no-compress',$offset,'empty-sector.bin'))
        }
        }
        if ($WatchSetup) { $serialText = Capture-Repair -CaptureSeconds 900 } else { $serialText = Capture-Repair }
        $scan = (& netsh.exe wlan show networks mode=bssid 2>&1 | Out-String)
        $scan | Set-Content (Join-Path $script:folder 'wifi-networks.txt') -Encoding UTF8
        $success = $serialText -match 'SLIC_WIFI_RESCUE_RESULT status=SUCCESS changed=[01] other_keys_unchanged=1 storage_unchanged=1' -and $serialText -match 'SLIC_WIFI_RESCUE_RETURNING_ORIGINAL' -and $serialText -match 'Loaded app from partition at offset 0x520000'
        # A nearby attendance ESP must not count as this device's setup AP.
        # Keep the complete Wi-Fi scan in the logs, and require this USB
        # device's own portal-enable message for the positive result.
        $ap = $serialText -match 'access point enabled'
        if ($success -and $ap) {
            $result = 'Wi-Fi repair verified. Attendance storage and other settings checked unchanged. The ESP reported its setup access point enabled. Connect to SLIC ATTENDANCE and open http://192.168.254.1 to enter the correct Wi-Fi details.'
        } elseif ($success) {
            $result = 'Wi-Fi settings repaired and attendance storage checked unchanged. Setup Wi-Fi was not confirmed by this computer. Send the saved ZIP to support.'
        } else {
            $result = 'Repair could not be verified. Do not erase or re-provision the ESP. Send the saved ZIP to support.'
            if ($serialText.Length -eq 0) { $result += ' No serial bytes were received. See serial-detection.txt for USB/open errors and make sure all ESP power was removed before reconnecting WITHOUT BOOT.' }
        }
        if ($WatchSetup) {
            $result = 'Setup-session recording complete. Send this ZIP to support to check the saved network and any red LED fault.'
            if ($success) { $result += ' The one-shot repair verified attendance storage and other settings unchanged before returning to the original firmware.' }
            if ($serialText -match 'Connecting to Wi-Fi SSID[ \t]*\r?\n') { $result += ' An empty Wi-Fi name was observed during this recording; router connectivity is not verified.' }
            if ($serialText -match 'Validated and atomically saved a new Wi-Fi network') { $result += ' A setup-page save was captured; support must check the following reboot for successful connection.' }
        }
        $result | Set-Content (Join-Path $script:folder 'RESULT.txt') -Encoding UTF8
        Say $result
    } catch {
        $result = 'Repair stopped: ' + $_.Exception.Message
        Say $result
        if ($script:folder) { $result | Set-Content (Join-Path $script:folder 'RESULT.txt') -Encoding UTF8 }
    } finally {
        if (Test-Path -LiteralPath $script:extract) { Remove-Item -LiteralPath $script:extract -Recurse -Force }
        if ($script:folder) {
            try { Compress-Archive -Path (Join-Path $script:folder '*') -DestinationPath ($script:folder + '.zip'); Say ('Logs saved: ' + $script:folder + '.zip') } catch { Say 'The results folder is available even though ZIP creation failed.' }
            Start-Process explorer.exe -ArgumentList ('"' + $script:folder + '"')
        }
        $script:active = $false
        $label.Text = 'Finished. Read the result below. Results and logs are on the Desktop.'
    }
})
[void]$form.ShowDialog()
