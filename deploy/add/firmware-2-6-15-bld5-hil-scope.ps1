function Assert-Zkt2615Bld5HilExtension {
    param(
        [string]$ExpectedGitSha,
        [string]$ExpectedImageSha256,
        [string]$ExpectedApplicationSha256,
        [string]$ExistingTargetsJson,
        [string]$ExtendedTargetsJson
    )
    if ($ExpectedGitSha -cne 'a88998346d5b1ce1ddf4d19e3963b7245e46633b' -or
        $ExpectedImageSha256 -cne 'e2a2167fca307d73dbeb495bcc26baa535591794066b02a3de2848589c28887f' -or
        $ExpectedApplicationSha256 -cne '832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6') {
        throw 'BLD5 HIL extension requires the exact published 2.6.15 release identity'
    }
    . (Join-Path $PSScriptRoot 'firmware-2-6-15-hil-scope.ps1')
    Assert-Zkt2615HilScope $ExistingTargetsJson
    $extended = ConvertFrom-Json -InputObject $ExtendedTargetsJson
    $extended = @($extended)
    if ($extended.Count -ne 6) { throw 'BLD5 HIL extension requires exactly six ordered targets' }
    $prefix = ConvertTo-Json -InputObject @($extended[0..4]) -Depth 5 -Compress
    Assert-Zkt2615HilScope $prefix
    $last = $extended[5]
    $keys = @($last.PSObject.Properties | ForEach-Object { $_.Name })
    if ($keys.Count -ne 3 -or
        $keys -cnotcontains 'connector_id' -or $keys -cnotcontains 'mac' -or
        $keys -cnotcontains 'terminal_serial' -or
        $last.connector_id -cne '510baddb-8eff-4817-bc48-549ee34bbd0f' -or
        $last.mac -cne 'ac:27:6e:a4:4e:d4' -or
        $last.terminal_serial -cne 'PGB1261300022') {
        throw 'BLD5 HIL extension differs from the reviewed exact ZKT identity'
    }
}
