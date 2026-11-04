# ZKT 2.8.2 (4 November 2026): 2.8.1 with the 49-byte attendance records of
# the MB40-VL/ID (ZAM180_TFT) terminals at LHRCENTRAL. Like every 2.7 platform
# image it may reach an exact prefix of the signed nationwide 17. It may
# instead reach an exact ordered prefix of the three LHRCENTRAL ESPs, none of
# them one of the 17, pinned by ESP hardware ID and terminal serial in
# hil-pins-2.8.2-lhrcentral.json. Each connector ID is the one ADD shows for
# that ESP; ADD matches it to the ESP's own connector or offers nothing.
# ADD applies the same rule (apps/add_backend/zk_add/zkt282_lhrcentral.py).
# See docs/zone-lite-2.8.2-release.md.

function Get-Zkt282LhrcentralPins {
    $path = Join-Path $PSScriptRoot 'hil-pins-2.8.2-lhrcentral.json'
    $pins = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($path))
    $pins = @($pins)
    if ($pins.Count -ne 3) { throw 'The 2.8.2 LHRCENTRAL pins must list exactly three ESPs' }
    return ,$pins
}

function Assert-Zkt282HilScope {
    param([string]$HilTargetsJson)

    if ([string]::IsNullOrWhiteSpace($HilTargetsJson) -or -not $HilTargetsJson.Trim().StartsWith('[')) {
        throw 'ZKT 2.8.2 HIL scope must be an ordered array'
    }
    # Windows PowerShell 5.1 emits a JSON array as one pipeline object; assign
    # it before wrapping it in @().
    $parsed = ConvertFrom-Json -InputObject $HilTargetsJson
    $actual = @($parsed)
    $pins = Get-Zkt282LhrcentralPins
    if ($actual.Count -lt 1 -or $null -eq $actual[0] -or $actual[0] -is [string] -or
        $actual[0].mac -cne $pins[0].mac) {
        # Not the LHRCENTRAL scope: the nationwide prefix rule decides.
        . (Join-Path $PSScriptRoot 'journal-hil-scope.ps1')
        Assert-JournalHilScope -TargetsJson $HilTargetsJson
        return
    }
    if ($actual.Count -gt $pins.Count) { throw 'ZKT 2.8.2 LHRCENTRAL scope has at most three targets' }
    $connectors = @()
    for ($index = 0; $index -lt $actual.Count; $index++) {
        $keys = @($actual[$index].PSObject.Properties | ForEach-Object { [string]$_.Name })
        if ($keys.Count -ne 3 -or $keys -cnotcontains 'connector_id' -or
            $keys -cnotcontains 'mac' -or $keys -cnotcontains 'terminal_serial') {
            throw 'ZKT 2.8.2 LHRCENTRAL scope requires exact identity fields'
        }
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if ($actual[$index].$field -isnot [string]) { throw "ZKT 2.8.2 HIL $field must be text" }
        }
        if ($actual[$index].mac -cne $pins[$index].mac -or
            $actual[$index].terminal_serial -cne $pins[$index].terminal_serial) {
            throw 'ZKT 2.8.2 LHRCENTRAL scope changed an ESP, a terminal serial or their order'
        }
        if ($actual[$index].connector_id -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
            throw "ZKT 2.8.2 $($pins[$index].name) needs the exact connector ID ADD shows for it"
        }
        $connectors += $actual[$index].connector_id
    }
    if (@($connectors | Sort-Object -Unique).Count -ne $connectors.Count) {
        throw 'ZKT 2.8.2 LHRCENTRAL scope repeats a connector ID'
    }
}
