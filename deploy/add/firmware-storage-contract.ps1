function Get-FirmwareStorageContract {
    param([Parameter(Mandatory)][string]$ImagePath, [Parameter(Mandatory)][string]$Version)
    # The referenced marker is compiled into the application. Reject a build made
    # with the wrong writer mode before opening the protected signing key.
    $bytes = [IO.File]::ReadAllBytes($ImagePath)
    $ascii = [Text.Encoding]::ASCII.GetString($bytes)
    $journalMarkers = [regex]::Matches($ascii, 'ZONE_STORAGE_CONTRACT_V[34]:[A-Z0-9:=_.,]+')
    if ($Version -eq '2.7.0') {
        $expectedWriter = 'ZONE_STORAGE_CONTRACT_V4:WRITER:LEGACY=2:JOURNAL=1:READERS=3F:AUTHORITY=ADD:BRIDGE=2.6.17'
        if ($journalMarkers.Count -ne 1 -or $journalMarkers[0].Value -cne $expectedWriter -or
            [regex]::Matches($ascii, 'ZONE_STORAGE_CONTRACT_V[12]:').Count -ne 0) {
            throw 'Missing, ambiguous, or incorrect journal writer contract'
        }
        return [ordered]@{
            allowed_bootstrap_images = [ordered]@{ '2.6.17' = 'f803361f8c3e1ed03f57814793942ebda45bbbd10cd0c1e77239602f5e6e32a1' }
            allowed_bootstrap_versions = @('2.6.17')
            compatibility_version = '2.6.17'
            delivery_authority = 'ADD'
            journal_capture = $true
            journal_read_format = 1
            journal_reader_mask = 63
            journal_write_format = 1
            read_format = 2
            reader_mask = 63
            schema_version = 4
            write_format = 1
        }
    }
    if ($Version -in @('2.6.16', '2.6.17', '2.6.18', '2.6.19', '2.6.20', '2.6.21', '2.6.23')) {
        $expectedBridge = 'ZONE_STORAGE_CONTRACT_V3:BRIDGE:LEGACY=2:JOURNAL=1:READERS=3F:CAPTURE=1:AUTHORITY=1'
        if ($Version -ne '2.6.16') { $expectedBridge += ':VERSION=' + $Version }
        $legacyMarkers = [regex]::Matches($ascii, 'ZONE_STORAGE_CONTRACT_V[12]:')
        if ($journalMarkers.Count -ne 1 -or $journalMarkers[0].Value -cne $expectedBridge -or $legacyMarkers.Count -ne 0) {
            throw 'Missing, ambiguous, or incorrect journal bridge reader/capture contract'
        }
        return [ordered]@{
            allowed_bootstrap_images = [ordered]@{
                '2.4.12' = 'cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589'
                '2.5.2' = '4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b'
                '2.6.15' = '832c0c3d8dac6e41d7cd0a9d4fbe4508e4f66982fa5ddeceaca4dc5adcbd80d6'
            }
            allowed_bootstrap_versions = @('2.4.12', '2.5.2', '2.6.15')
            compatibility_version = $Version
            delivery_authority = 'LEGACY_UNTIL_PERSISTED_ADD_CUTOVER'
            journal_capture = $true
            journal_read_format = 1
            journal_reader_mask = 63
            journal_write_format = 1
            read_format = 2
            reader_mask = 63
            schema_version = 3
            write_format = 1
        }
    }
    if ($journalMarkers.Count -gt 0) { throw 'Journal bridge marker cannot sign another firmware version' }
    $markers = [regex]::Matches($ascii, 'ZONE_STORAGE_CONTRACT_V[12]:[A-Z]+:READ=[0-9]+:LANES=[0-9A-F]+:(?:COMPAT=[0-9.]+|BASE=[0-9.,]+)')
    if ($Version -notin @('2.5.4', '2.6.0', '2.6.1', '2.6.2', '2.6.3', '2.6.4', '2.6.5', '2.6.6', '2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15')) {
        if ($markers.Count -gt 0) { throw 'Storage-contract version is not qualified for signing' }
        return $null
    }
    $mode = if ($Version -eq '2.6.0') { 'SEGMENTED' } else { 'LEGACY' }
    $expected = if ($Version -eq '2.6.15') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12,2.6.13,2.6.14'
    } elseif ($Version -eq '2.6.14') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12,2.6.13'
    } elseif ($Version -eq '2.6.13') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10,2.6.12'
    } elseif ($Version -in @('2.6.11', '2.6.12')) {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9,2.6.10'
    } elseif ($Version -eq '2.6.10') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8,2.6.9'
    } elseif ($Version -eq '2.6.9') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7,2.6.8'
    } elseif ($Version -eq '2.6.8') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6,2.6.7'
    } elseif ($Version -eq '2.6.7') {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2,2.6.6'
    } elseif ($Version -in @('2.6.1', '2.6.2', '2.6.3', '2.6.4', '2.6.5', '2.6.6')) {
        'ZONE_STORAGE_CONTRACT_V2:LEGACY:READ=2:LANES=3F:BASE=2.4.12,2.5.2'
    } else {
        "ZONE_STORAGE_CONTRACT_V1:${mode}:READ=2:LANES=3F:COMPAT=2.5.4"
    }
    if ($markers.Count -ne 1 -or $markers[0].Value -cne $expected) {
        throw 'Missing, ambiguous, or incorrect application storage contract'
    }
    if ($Version -in @('2.6.1', '2.6.2', '2.6.3', '2.6.4', '2.6.5', '2.6.6', '2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15')) {
        $images = [ordered]@{
            '2.4.12' = 'cf9e6e2deff0a237b0bb007fe95e2468fab2503fbceccc8d91c7834f0a6ba589'
            '2.5.2' = '4b4aa0697551f527b48b58e95229cd21e362f6ba25398a2d46263bdbf289146b'
        }
        if ($Version -in @('2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15')) {
            $images['2.6.6'] = '69ec4cf34204d84d76933c30510ed78d46ec11d294f7257697af19047ce6869e'
        }
        if ($Version -in @('2.6.8', '2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15')) {
            $images['2.6.7'] = '3bed51d23d85fe50c03642e95f1d1d1e0b45960ccbf97d551645c0b268da1f1c'
        }
        if ($Version -in @('2.6.9', '2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15')) {
            $images['2.6.8'] = 'fecc5df0223a3c7c8b019a445bcf829bc8d09dd93e920aecc8446908fadeadc6'
        }
        if ($Version -in @('2.6.10', '2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15')) {
            $images['2.6.9'] = 'ad71339fef6926b21a21a05c1e1c4e30a936e0df5be6160871c7283841ad91b8'
        }
        if ($Version -in @('2.6.11', '2.6.12', '2.6.13', '2.6.14', '2.6.15')) {
            $images['2.6.10'] = 'a95370b1487d9c1454dfb932c41432451f24ef69d0abc1292a5c0262c17c243c'
        }
        if ($Version -in @('2.6.13', '2.6.14', '2.6.15')) {
            $images['2.6.12'] = '3f9028126c8dde9816486783a27a9802ed41179cd9f8caf9935deb79a0057fc1'
        }
        if ($Version -in @('2.6.14', '2.6.15')) {
            $images['2.6.13'] = 'c7be4171153333563f9ed6e43f91376d42dfb9d3ca2faefda2ca6d1ac87b2def'
        }
        if ($Version -eq '2.6.15') {
            $images['2.6.14'] = '7d0381fc68a01b34ab6989dfed2b5a1a93f39c293bfd0b7db1734b0871fa6469'
        }
        return [ordered]@{
            allowed_bootstrap_images = $images
            allowed_bootstrap_versions = $(if ($Version -eq '2.6.15') { @('2.4.12', '2.5.2', '2.6.6', '2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.12', '2.6.13', '2.6.14') } elseif ($Version -eq '2.6.14') { @('2.4.12', '2.5.2', '2.6.6', '2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.12', '2.6.13') } elseif ($Version -eq '2.6.13') { @('2.4.12', '2.5.2', '2.6.6', '2.6.7', '2.6.8', '2.6.9', '2.6.10', '2.6.12') } elseif ($Version -in @('2.6.11', '2.6.12')) { @('2.4.12', '2.5.2', '2.6.6', '2.6.7', '2.6.8', '2.6.9', '2.6.10') } elseif ($Version -eq '2.6.10') { @('2.4.12', '2.5.2', '2.6.6', '2.6.7', '2.6.8', '2.6.9') } elseif ($Version -eq '2.6.9') { @('2.4.12', '2.5.2', '2.6.6', '2.6.7', '2.6.8') } elseif ($Version -eq '2.6.8') { @('2.4.12', '2.5.2', '2.6.6', '2.6.7') } elseif ($Version -eq '2.6.7') { @('2.4.12', '2.5.2', '2.6.6') } else { @('2.4.12', '2.5.2') })
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
