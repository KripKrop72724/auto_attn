# ZKT 2.8.3 (7 November 2026): 2.8.2 holding 4096 terminal users, for the
# uFace800 terminals at FSDMAIN (2,500 users). Like every 2.7 platform image it
# may reach an exact prefix of the signed nationwide 17. It may instead reach
# an exact ordered prefix of the FSDMAIN ESPs (at most three), none of them one
# of the 17, pinned by ESP hardware ID and terminal serial in
# hil-pins-2.8.3-fsdmain.json. Each connector ID is the one ADD shows for that
# ESP; ADD matches it to the ESP's own connector or offers nothing.
# ADD applies the same rule (apps/add_backend/zk_add/zkt283_fsdmain.py).
# See docs/zone-lite-2.8.3-release.md.

function Get-Zkt283FsdmainPins {
    $path = Join-Path $PSScriptRoot 'hil-pins-2.8.3-fsdmain.json'
    $pins = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($path))
    $pins = @($pins)
    if ($pins.Count -lt 1 -or $pins.Count -gt 3) { throw 'The 2.8.3 FSDMAIN pins must list one to three ESPs' }
    return ,$pins
}

function Assert-Zkt283HilScope {
    param([string]$HilTargetsJson)

    if ([string]::IsNullOrWhiteSpace($HilTargetsJson) -or -not $HilTargetsJson.Trim().StartsWith('[')) {
        throw 'ZKT 2.8.3 HIL scope must be an ordered array'
    }
    # Windows PowerShell 5.1 emits a JSON array as one pipeline object; assign
    # it before wrapping it in @().
    $parsed = ConvertFrom-Json -InputObject $HilTargetsJson
    $actual = @($parsed)
    $pins = Get-Zkt283FsdmainPins
    if ($actual.Count -lt 1 -or $null -eq $actual[0] -or $actual[0] -is [string] -or
        $actual[0].mac -cne $pins[0].mac) {
        # Not the FSDMAIN scope: the nationwide prefix rule decides.
        . (Join-Path $PSScriptRoot 'journal-hil-scope.ps1')
        Assert-JournalHilScope -TargetsJson $HilTargetsJson
        return
    }
    if ($actual.Count -gt $pins.Count) { throw 'ZKT 2.8.3 FSDMAIN scope has at most its pinned ESPs' }
    $connectors = @()
    for ($index = 0; $index -lt $actual.Count; $index++) {
        $keys = @($actual[$index].PSObject.Properties | ForEach-Object { [string]$_.Name })
        if ($keys.Count -ne 3 -or $keys -cnotcontains 'connector_id' -or
            $keys -cnotcontains 'mac' -or $keys -cnotcontains 'terminal_serial') {
            throw 'ZKT 2.8.3 FSDMAIN scope requires exact identity fields'
        }
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if ($actual[$index].$field -isnot [string]) { throw "ZKT 2.8.3 HIL $field must be text" }
        }
        if ($actual[$index].mac -cne $pins[$index].mac -or
            $actual[$index].terminal_serial -cne $pins[$index].terminal_serial) {
            throw 'ZKT 2.8.3 FSDMAIN scope changed an ESP, a terminal serial or their order'
        }
        if ($actual[$index].connector_id -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
            throw "ZKT 2.8.3 $($pins[$index].name) needs the exact connector ID ADD shows for it"
        }
        $connectors += $actual[$index].connector_id
    }
    if (@($connectors | Sort-Object -Unique).Count -ne $connectors.Count) {
        throw 'ZKT 2.8.3 FSDMAIN scope repeats a connector ID'
    }
}
