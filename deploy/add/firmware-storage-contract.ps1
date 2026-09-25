function Get-FirmwareStorageContract {
    param([Parameter(Mandatory)][string]$ImagePath, [Parameter(Mandatory)][string]$Version)
    # The referenced marker is compiled into the application. Reject a build made
    # with the wrong writer mode before opening the protected signing key.
    $bytes = [IO.File]::ReadAllBytes($ImagePath)
    $ascii = [Text.Encoding]::ASCII.GetString($bytes)
    $markers = [regex]::Matches($ascii, 'ZONE_STORAGE_CONTRACT_V[12]:[A-Z]+:READ=[0-9]+:LANES=[0-9A-F]+:(?:COMPAT=[0-9.]+|BASE=[0-9.,]+)')
    if ($Version -notin @('2.5.4', '2.6.0', '2.6.1', '2.6.2', '2.6.3', '2.6.4')) {
        if ($markers.Count -gt 0) { throw 'Storage-contract version is not qualified for signing' }
        return $null
    }
    $mode = if ($Version -eq '2.6.0') { 'SEGMENTED' } else { 'LEGACY' }
    $expected = if ($Version -in @('2.6.1', '2.6.2', '2.6.3', '2.6.4')) {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2'
    } else {
        "ZONE_STORAGE_CONTRACT_V1:${mode}:READ=2:LANES=3F:COMPAT=2.5.4"
    }
    if ($markers.Count -ne 1 -or $markers[0].Value -cne $expected) {
        throw 'Missing, ambiguous, or incorrect application storage contract'
    }
    if ($Version -in @('2.6.1', '2.6.2', '2.6.3', '2.6.4')) {
        return [ordered]@{
            allowed_bootstrap_images = [ordered]@{
                '2.4.12' = 'cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589'
                '2.5.2' = '4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b'
            }
            allowed_bootstrap_versions = @('2.4.12', '2.5.2')
            read_format = 2
            reader_mask = 63
            schema_version = 2
            write_format = 1
        }
    }
    return [ordered]@{
        compatibility_version = '2.5.4'
        read_format = 2
        reader_mask = 63
        schema_version = 1
        write_format = $(if ($mode -eq 'SEGMENTED') { 2 } else { 1 })
    }
}
