function Get-FirmwareStorageContract {
    param([Parameter(Mandatory)][string]$ImagePath, [Parameter(Mandatory)][string]$Version)
    # The referenced marker is compiled into the application. Reject a build made
    # with the wrong writer mode before opening the protected signing key.
    $bytes = [IO.File]::ReadAllBytes($ImagePath)
    $ascii = [Text.Encoding]::ASCII.GetString($bytes)
    $markers = [regex]::Matches($ascii, 'ZONE_STORAGE_CONTRACT_V1:[A-Z]+:READ=[0-9]+:LANES=[0-9A-F]+:COMPAT=[0-9.]+')
    if ($Version -notin @('2.5.4', '2.6.0')) {
        if ($markers.Count -gt 0) { throw 'Storage-contract version is not qualified for signing' }
        return $null
    }
    $mode = if ($Version -eq '2.6.0') { 'SEGMENTED' } else { 'LEGACY' }
    $expected = "ZONE_STORAGE_CONTRACT_V1:${mode}:READ=2:LANES=3F:COMPAT=2.5.4"
    if ($markers.Count -ne 1 -or $markers[0].Value -cne $expected) {
        throw 'Missing, ambiguous, or incorrect application storage contract'
    }
    return [ordered]@{
        compatibility_version = '2.5.4'
        read_format = 2
        reader_mask = 63
        schema_version = 1
        write_format = $(if ($mode -eq 'SEGMENTED') { 2 } else { 1 })
    }
}
