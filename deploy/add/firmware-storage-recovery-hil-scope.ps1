function Get-ZktStorageRecoveryHilScope {
    $path = Join-Path $PSScriptRoot 'hil-targets-storage-recovery.json'
    $targets = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($path))
    return ,@($targets)
}

# The one-shot storage recovery images (2.6.24-2.6.26) are signed and
# published only for the two exact Peshawar connectors, in this reviewed order.
function Assert-ZktStorageRecoveryHilScope {
    param([string]$HilTargetsJson)

    if ([string]::IsNullOrWhiteSpace($HilTargetsJson) -or -not $HilTargetsJson.Trim().StartsWith('[')) {
        throw 'ZKT storage recovery requires its exact ordered HIL scope'
    }
    $expected = Get-ZktStorageRecoveryHilScope
    try {
        # Windows PowerShell 5.1 emits a JSON array as one pipeline object;
        # assign before normalizing so @() does not nest it.
        $parsed = ConvertFrom-Json -InputObject $HilTargetsJson
        $actual = @($parsed)
    } catch {
        throw 'ZKT storage recovery HIL scope is not valid JSON'
    }
    if ($expected.Count -ne 2 -or $actual.Count -ne $expected.Count) {
        throw 'ZKT storage recovery requires both exact Peshawar targets'
    }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        $properties = @($actual[$index].PSObject.Properties | ForEach-Object { $_.Name })
        if ($properties.Count -ne 3 -or
            $properties -cnotcontains 'connector_id' -or
            $properties -cnotcontains 'mac' -or
            $properties -cnotcontains 'terminal_serial') {
            throw 'ZKT storage recovery HIL scope contains unexpected target fields'
        }
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if ($actual[$index].$field -isnot [string] -or
                $actual[$index].$field -cne $expected[$index].$field) {
                throw 'ZKT storage recovery HIL scope differs from the reviewed target order or identity'
            }
        }
    }
}
