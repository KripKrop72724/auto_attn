function Get-JournalHilScope {
    $path = Join-Path $PSScriptRoot 'hil-targets-zkt-270.json'
    $targets = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($path))
    return ,@($targets)
}

function Assert-JournalHilScope {
    param([Parameter(Mandatory = $true)][string]$TargetsJson, [switch]$Complete)
    if (-not $TargetsJson.Trim().StartsWith('[')) { throw 'Journal HIL scope must be an ordered array' }
    $parsed = ConvertFrom-Json -InputObject $TargetsJson
    $actual = @($parsed)
    $expected = Get-JournalHilScope
    if ($actual.Count -lt 1 -or $actual.Count -gt $expected.Count -or
        ($Complete -and $actual.Count -ne $expected.Count)) {
        throw 'Journal HIL scope must retain its exact nationwide prefix'
    }
    for ($index = 0; $index -lt $actual.Count; $index++) {
        $keys = @($actual[$index].PSObject.Properties | ForEach-Object { $_.Name })
        if ($keys.Count -ne 3 -or $keys -cnotcontains 'connector_id' -or
            $keys -cnotcontains 'mac' -or $keys -cnotcontains 'terminal_serial') {
            throw 'Journal HIL scope requires exact identity fields'
        }
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if ($actual[$index].$field -isnot [string] -or
                $actual[$index].$field -cne $expected[$index].$field) {
                throw 'Journal HIL scope changed a target identity or order'
            }
        }
    }
}
