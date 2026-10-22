param([string]$ReadyFile, [string]$ToolArchive, [switch]$GuiSmokeTest)
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
$button.Location = New-Object Drawing.Point(15,80)
$button.Size = New-Object Drawing.Size(180,35)
$form.Controls.Add($button)
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
function Get-ExactUsbPort {
    $devicesFound = @()
    foreach ($device in @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Ports'")) {
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
    if ($devicesFound.Count -ne 1) { throw 'The expected ESP was not uniquely identified. Check its USB cable and close other serial programs, then run this tool again. No repair was started.' }
    return $devicesFound[0]
}
function Run-Tool([string]$Stage, [string[]]$Arguments) {
    $stdout = Join-Path $script:folder ($Stage + '.stdout.txt')
    $stderr = Join-Path $script:folder ($Stage + '.stderr.txt')
    # All arguments are generated from fixed choices, a COM-number match and
    # filenames in the private extraction directory. Never accept arbitrary CLI.
    $quoted = @($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $process = Start-Process -FilePath $script:tool -ArgumentList $quoted -WorkingDirectory $script:extract -NoNewWindow -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $deadline = (Get-Date).AddMinutes(4)
    $next = Get-Date
    while (-not $process.HasExited) {
        if ((Get-Date) -gt $deadline) { $process.Kill(); throw "USB operation timed out during $Stage. Keep the logs for support." }
        if ((Get-Date) -ge $next) { Say "$Stage is running. Keep USB connected..."; $next = (Get-Date).AddSeconds(10) }
        [Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 100
        $process.Refresh()
    }
    $process.WaitForExit()
    $process.Refresh()
    $text = (Get-Content -LiteralPath $stdout -Raw) + (Get-Content -LiteralPath $stderr -Raw)
    if ($process.ExitCode -ne 0) { throw "USB operation failed during $Stage. Saved logs contain the details." }
    return $text
}
function Assert-SecurityProfile([string]$Report) {
    if ($Report -notmatch 'Secure Download Mode' -or $Report -notmatch 'Flags:\s+0x000000c5\b' -or
        $Report -notmatch 'Key Purposes:\s*\(8, 9, 10, 11, 0, 0, 12\)' -or
        $Report -notmatch 'Secure Boot:\s+Enabled' -or $Report -notmatch 'Flash Encryption:\s+Disabled') {
        throw 'USB security differs from the tested device profile. No flash writes were made.'
    }
}
function Capture-Repair {
    $label.Text = "Unplug and reconnect the ESP USB cable now.`r`nThen leave it connected and wait for the result."
    Say 'The repair is installed. Unplug and reconnect the ESP USB cable now.'
    Say 'Waiting for the ESP to check its data and restart the original firmware...'
    $deadline = (Get-Date).AddSeconds(210)
    $raw = [IO.File]::Open((Join-Path $script:folder 'serial.bin'), [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::Read)
    $text = New-Object Text.StringBuilder
    $serial = $null
    $next = Get-Date
    try {
        while ((Get-Date) -lt $deadline) {
            try {
                if (-not $serial) {
                    $device = Get-ExactUsbPort
                    $serial = New-Object IO.Ports.SerialPort $device.Port,115200,([IO.Ports.Parity]::None),8,([IO.Ports.StopBits]::One)
                    $serial.DtrEnable = $false; $serial.RtsEnable = $false
                    $serial.Handshake = [IO.Ports.Handshake]::None
                    $serial.ReadTimeout = 200
                    $serial.Open()
                }
                $count = $serial.BytesToRead
                if ($count -gt 0) {
                    $bytes = New-Object byte[] ([Math]::Min($count,65536))
                    $count = $serial.Read($bytes,0,$bytes.Length)
                    $raw.Write($bytes,0,$count); $raw.Flush()
                    [void]$text.Append([Text.Encoding]::UTF8.GetString($bytes,0,$count))
                }
            } catch {
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
        Say "Correct ESP detected on $($device.Port). Checking USB security..."
        $device | ConvertTo-Json | Set-Content (Join-Path $script:folder 'usb-identity.json') -Encoding UTF8
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
        $serialText = Capture-Repair
        $scan = (& netsh.exe wlan show networks mode=bssid 2>&1 | Out-String)
        $scan | Set-Content (Join-Path $script:folder 'wifi-networks.txt') -Encoding UTF8
        $success = $serialText -match 'SLIC_WIFI_RESCUE_RESULT status=SUCCESS changed=[01] other_keys_unchanged=1 storage_unchanged=1' -and $serialText -match 'SLIC_WIFI_RESCUE_RETURNING_ORIGINAL' -and $serialText -match 'Loaded app from partition at offset 0x520000'
        $ap = $serialText -match 'access point enabled' -or $scan -match 'SLIC\s+ATTENDANCE'
        if ($success -and $ap) {
            $result = 'Wi-Fi repair verified. Attendance storage and other settings checked unchanged. The setup access point was observed. Connect to SLIC ATTENDANCE and open http://192.168.254.1 to enter the correct Wi-Fi details.'
        } elseif ($success) {
            $result = 'Wi-Fi settings repaired and attendance storage checked unchanged. Setup Wi-Fi was not confirmed by this computer. Send the saved ZIP to support.'
        } else {
            $result = 'Repair could not be verified. Do not erase or re-provision the ESP. Send the saved ZIP to support.'
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
