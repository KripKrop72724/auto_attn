$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'repair.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Get-ExactUsbPort','Assert-SecurityProfile')) {
    $function = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true)
    if (-not $function) { throw "Missing guard $name" }
    Invoke-Expression $function.Extent.Text
}
$target = 'ac:27:6e:a3:16:d4'
$script:devices = @([pscustomobject]@{Name='USB Serial Device (COM6)';DeviceID='USB\VID_303A&PID_1001&MI_00\6&105C2414&0&0000'})
$script:parent = 'USB\VID_303A&PID_1001\AC:27:6E:A3:16:D4'
function Get-CimInstance { param($ClassName,$Filter) return $script:devices }
function Get-PnpDeviceProperty { param($InstanceId,$KeyName,$ErrorAction) return [pscustomobject]@{Data=$script:parent} }
if ((Get-ExactUsbPort).Port -ne 'COM6') { throw 'Native USB parent identity was not recognized' }
foreach ($parent in @('USB\VID_303A&PID_1001\14:C1:9F:46:AE:1C', 'USB\VID_303A&PID_1001\123456', 'ROOT\UNKNOWN')) {
    $script:parent = $parent
    $refused = $false
    try { $null = Get-ExactUsbPort } catch { $refused = $true }
    if (-not $refused) { throw 'Wrong/unknown physical device accepted' }
}
$script:parent = 'USB\VID_303A&PID_1001\AC:27:6E:A3:16:D4'
$script:devices += $script:devices[0]
$refused = $false
try { $null = Get-ExactUsbPort } catch { $refused = $true }
if (-not $refused) { throw 'Ambiguous USB device accepted' }
$report = @'
ESP32-S3 in Secure Download Mode
Flags: 0x000000c5 (0b11000101)
Key Purposes: (8, 9, 10, 11, 0, 0, 12)
Secure Boot: Enabled
Flash Encryption: Disabled
'@
Assert-SecurityProfile $report
foreach ($bad in @($report.Replace('c5','c1'), $report.Replace('(8,','(0,'), $report.Replace('Secure Boot: Enabled','Secure Boot: Disabled'), $report.Replace('Flash Encryption: Disabled','Flash Encryption: Enabled'), 'incomplete output')) {
    $refused = $false
    try { Assert-SecurityProfile $bad } catch { $refused = $true }
    if (-not $refused) { throw 'Changed or incomplete security profile accepted' }
}
Write-Output 'PASS: exact USB parent MAC and security profile; wrong, unknown and ambiguous devices refused'
