function Assert-Zkt267HilScope {
    param([string]$HilTargetsJson)

    if ([string]::IsNullOrWhiteSpace($HilTargetsJson)) {
        throw 'ZKT 2.6.7 signing and publication require the ordered exact HIL scope'
    }
    $expectedPath = Join-Path $PSScriptRoot 'hil-targets-2.6.7.json'
    $expected = ConvertFrom-Json -InputObject (Get-Content -LiteralPath $expectedPath -Raw)
    $expected = @($expected)
    try {
        $actual = ConvertFrom-Json -InputObject $HilTargetsJson
        $actual = @($actual)
    } catch {
        throw 'ZKT 2.6.7 HIL scope is not valid JSON'
    }
    if ($expected.Count -ne 5 -or $actual.Count -ne $expected.Count) {
        throw 'ZKT 2.6.7 requires all five ordered HIL targets'
    }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        $properties = @($actual[$index].PSObject.Properties | ForEach-Object { $_.Name })
        if ($properties.Count -ne 3 -or
            $properties -cnotcontains 'connector_id' -or
            $properties -cnotcontains 'mac' -or
            $properties -cnotcontains 'terminal_serial') {
            throw 'ZKT 2.6.7 HIL scope contains unexpected target fields'
        }
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if ($actual[$index].$field -isnot [string] -or
                $actual[$index].$field -cne $expected[$index].$field) {
                throw 'ZKT 2.6.7 HIL scope differs from the reviewed target order or identity'
            }
        }
    }
}
