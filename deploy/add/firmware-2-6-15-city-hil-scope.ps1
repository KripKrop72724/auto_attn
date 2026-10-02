function Assert-Zkt2615CityHilExtension {
    param(
        [string]$ExpectedGitSha,
        [string]$ExpectedImageSha256,
        [string]$ExpectedApplicationSha256,
        [string]$ExistingTargetsJson,
        [string]$ExtendedTargetsJson
    )
    . (Join-Path $PSScriptRoot 'firmware-2-6-15-bld5-hil-scope.ps1')
    $existing = ConvertFrom-Json -InputObject $ExistingTargetsJson
    $existing = @($existing)
    if ($existing.Count -ne 6) { throw 'City HIL extension must preserve the six published targets' }
    $original = ConvertTo-Json -InputObject @($existing[0..4]) -Depth 5 -Compress
    Assert-Zkt2615Bld5HilExtension `
        -ExpectedGitSha $ExpectedGitSha `
        -ExpectedImageSha256 $ExpectedImageSha256 `
        -ExpectedApplicationSha256 $ExpectedApplicationSha256 `
        -ExistingTargetsJson $original `
        -ExtendedTargetsJson $ExistingTargetsJson
    $extended = ConvertFrom-Json -InputObject $ExtendedTargetsJson
    $extended = @($extended)
    if ($extended.Count -ne 14) { throw 'City HIL extension requires exactly fourteen reviewed targets' }
    for ($index = 0; $index -lt 6; $index++) {
        foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
            if ($extended[$index].$field -cne $existing[$index].$field) {
                throw 'City HIL extension must preserve the published ordered prefix'
            }
        }
    }
    $approvedJson = @'
[
 {"connector_id":"beb5f8eb-e4f3-4620-8f1a-cfa460bb1b73","mac":"ac:27:6e:a5:5a:20","terminal_serial":"CKPG221260245"},
 {"connector_id":"ab5f934a-5430-4ba2-a86d-5ac849276e0d","mac":"a4:cb:8f:d4:61:ac","terminal_serial":"CKPG221260316"},
 {"connector_id":"0e991162-5ab9-467a-a952-4dc4c420691c","mac":"ac:27:6e:a3:10:0c","terminal_serial":"RKQ4245100152"},
 {"connector_id":"06d8706c-8b9e-4a48-b8c7-a76d16da6ed4","mac":"ac:27:6e:a3:de:e8","terminal_serial":"AF4C211861133"},
 {"connector_id":"9726fe6c-2905-447a-9041-2fa606660058","mac":"ac:27:6e:a3:16:d4","terminal_serial":"AEH2232460004"},
 {"connector_id":"8727e27a-77be-41c7-bb9e-5a7f31e4ae67","mac":"ac:27:6e:a4:54:b4","terminal_serial":"CKPG221260408"},
 {"connector_id":"474fd36e-6c75-4e0d-9c1f-cc97c4a442f8","mac":"ac:27:6e:a3:07:f8","terminal_serial":"OCN6060066052700045"},
 {"connector_id":"7d6fadcc-f93e-4c35-b4cc-852af1ffac0c","mac":"a4:cb:8f:d4:67:70","terminal_serial":"RKQ4254900154"}
]
'@
    $approved = ConvertFrom-Json -InputObject $approvedJson
    $approved = @($approved)
    for ($index = 0; $index -lt 14; $index++) {
        $keys = @($extended[$index].PSObject.Properties | ForEach-Object { $_.Name })
        if ($keys.Count -ne 3 -or $keys -cnotcontains 'connector_id' -or
            $keys -cnotcontains 'mac' -or $keys -cnotcontains 'terminal_serial') {
            throw 'City HIL targets require only the exact identity fields'
        }
        if ($index -ge 6) {
            foreach ($field in @('connector_id', 'mac', 'terminal_serial')) {
                if ($extended[$index].$field -cne $approved[$index - 6].$field) {
                    throw 'City HIL extension differs from the reviewed exact active-device scope'
                }
            }
        }
    }
}
