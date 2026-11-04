# Installer app publication without a real build: synthetic Windows and macOS
# release folders, as esp-installer-release.yml downloads them, must reach
# ADD's installer app store only as exact, checksummed copies with the
# manifest.json ADD reads (docs/esp-installer.md). A TEST build, another
# version, a file that does not match its SHA256SUMS.txt and every other bad
# input is refused before the store changes. A published version is never
# rewritten with other bytes, publishing the same bytes again changes nothing,
# and older versions and anything else in the store stay as they are.
$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$work = Join-Path ([IO.Path]::GetTempPath()) ('installer-app-test-' + [guid]::NewGuid().ToString('N'))
$publisher = Join-Path $root 'deploy/add/publish-installer-app.ps1'
$store = Join-Path $work 'installer-apps'
$utf8 = New-Object Text.UTF8Encoding($false)
$originalStore = $env:ADD_INSTALLER_APP_STORE_HOST_PATH
$gitSha = '0123456789abcdef0123456789abcdef01234567'

# What ADD's reader requires of manifest.json, checked by Python's own JSON
# parser (which also refuses a byte-order mark).
$contract = @'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    manifest = json.load(handle)
assert list(manifest) == ["schema", "product", "version", "git_sha", "published_at", "files"], list(manifest)
assert manifest["schema"] == 1 and type(manifest["schema"]) is int
assert manifest["product"] == "state-life-device-installer"
assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
assert re.fullmatch(r"[0-9a-f]{40}", manifest["git_sha"])
assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", manifest["published_at"])
assert [(f["platform"], f["arch"], f["kind"]) for f in manifest["files"]] == [
    ("windows", "x64", "setup"), ("macos", "arm64", "dmg")]
for entry in manifest["files"]:
    assert list(entry) == ["platform", "arch", "kind", "filename", "size", "sha256"], list(entry)
    assert re.fullmatch(r"[A-Za-z0-9._-]+", entry["filename"])
    assert type(entry["size"]) is int and entry["size"] > 0
    assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
print("OK")
'@

function Get-TreeState {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return 'absent' }
    # Children carry the root's own path form (8.3 names on Windows runners).
    $base = (Get-Item -LiteralPath $Path -Force).FullName.TrimEnd('\', '/')
    return (@(Get-ChildItem -LiteralPath $Path -Recurse -Force | Sort-Object FullName | ForEach-Object {
        $relative = $_.FullName.Substring($base.Length)
        if ($_.PSIsContainer) { $relative } else { $relative + ':' + (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    }) -join ';')
}

function Get-Sha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function New-ReleaseFolder {
    # A build's artifact folder: the package, SHA256SUMS.txt as build.py writes
    # it, and the screenshots the release artifact also carries.
    param([string]$Name, [string]$FileName, [string]$Content)
    $folder = Join-Path $work $Name
    New-Item -ItemType Directory -Path (Join-Path $folder 'screenshots') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $folder 'screenshots/01-sign-in.png'), 'not a picture', $utf8)
    Set-Package $folder $FileName $Content
    return $folder
}

function Set-Package {
    param([string]$Folder, [string]$FileName, [string]$Content)
    $path = Join-Path $Folder $FileName
    [IO.File]::WriteAllText($path, $Content, $utf8)
    [IO.File]::WriteAllText((Join-Path $Folder 'SHA256SUMS.txt'), "$(Get-Sha256 $path)  $FileName`n", $utf8)
}

function Copy-Folder {
    param([string]$Source, [string]$Name)
    $copy = Join-Path $work $Name
    Copy-Item -LiteralPath $Source -Destination $copy -Recurse
    return $copy
}

function Merge-Arguments {
    param([hashtable]$Base, [hashtable]$Change)
    $merged = @{}
    foreach ($key in $Base.Keys) { $merged[$key] = $Base[$key] }
    foreach ($key in $Change.Keys) { $merged[$key] = $Change[$key] }
    return $merged
}

function Invoke-Publisher {
    param([hashtable]$Arguments)
    $result = @{ Output = @(); Message = '' }
    try { $result.Output = @(& $publisher @Arguments) } catch { $result.Message = $_.Exception.Message }
    return $result
}

function Assert-PublisherRefused {
    param([string]$Case, [hashtable]$Arguments, [string]$Pattern, [string]$Store = $store)
    $before = Get-TreeState $Store
    $result = Invoke-Publisher $Arguments
    if ($result.Message -notmatch $Pattern) { throw "$Case was not refused as expected; got '$($result.Message)'" }
    if ((Get-TreeState $Store) -ne $before) { throw "$Case changed the installer app store" }
}

New-Item -ItemType Directory -Path $work -Force | Out-Null
Push-Location $root
try {
    $setupName = 'StateLife-Device-Installer-Setup-1.1.0.exe'
    $dmgName = 'StateLife-Device-Installer-1.1.0-macOS-arm64.dmg'
    $windows = New-ReleaseFolder 'windows' $setupName 'synthetic Windows Setup 1.1.0'
    $mac = New-ReleaseFolder 'mac' $dmgName 'synthetic macOS disk image 1.1.0'
    $publish = @{ WindowsDirectory = $windows; MacDirectory = $mac; Version = '1.1.0'; GitSha = $gitSha; StoreDirectory = $store }

    # The store already holds an older version, a staging folder of another
    # run and a file of its own; publishing must leave them exactly as they are.
    New-Item -ItemType Directory -Path (Join-Path $store '1.0.0') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $store '1.0.0/manifest.json'), '{"schema": 1, "version": "1.0.0"}', $utf8)
    [IO.File]::WriteAllText((Join-Path $store '1.0.0/StateLife-Device-Installer-Setup-1.0.0.exe'), 'older Setup', $utf8)
    New-Item -ItemType Directory -Path (Join-Path $store '.staging/app-1.0.1-other-run') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $store '.staging/app-1.0.1-other-run/part.bin'), 'another run', $utf8)
    [IO.File]::WriteAllText((Join-Path $store 'README.txt'), 'head office notes', $utf8)
    $kept = @('1.0.0', '.staging', 'README.txt') | ForEach-Object { $_ + '=' + (Get-TreeState (Join-Path $store $_)) }

    # 1. Bad arguments.
    Assert-PublisherRefused 'Version that is not x.y.z' (Merge-Arguments $publish @{ Version = '1.1' }) 'parameter .Version.'
    Assert-PublisherRefused 'Version with a path in it' (Merge-Arguments $publish @{ Version = '..\1.1.0' }) 'parameter .Version.'
    Assert-PublisherRefused 'Short commit' (Merge-Arguments $publish @{ GitSha = '0123456' }) 'parameter .GitSha.'
    Assert-PublisherRefused 'Missing build folder' (Merge-Arguments $publish @{ MacDirectory = (Join-Path $work 'no-such-folder') }) 'The macos build folder .* does not exist'

    # 2. Only the release files of exactly this version.
    $testBuild = New-ReleaseFolder 'windows-test' 'StateLife-Device-Installer-Setup-1.1.0-TEST.exe' 'synthetic TEST Setup'
    Assert-PublisherRefused 'TEST Setup' (Merge-Arguments $publish @{ WindowsDirectory = $testBuild }) 'The windows build has no file StateLife-Device-Installer-Setup-1\.1\.0\.exe'
    $macTest = New-ReleaseFolder 'mac-test' 'StateLife-Device-Installer-1.1.0-macOS-arm64-TEST.dmg' 'synthetic TEST image'
    Assert-PublisherRefused 'TEST disk image' (Merge-Arguments $publish @{ MacDirectory = $macTest }) 'The macos build has no file StateLife-Device-Installer-1\.1\.0-macOS-arm64\.dmg'
    Assert-PublisherRefused 'Another version than the builds' (Merge-Arguments $publish @{ Version = '1.2.0' }) 'has no file StateLife-Device-Installer-Setup-1\.2\.0\.exe'
    $folderNamed = Copy-Folder $mac 'mac-folder-named'
    Remove-Item -LiteralPath (Join-Path $folderNamed $dmgName) -Force
    New-Item -ItemType Directory -Path (Join-Path $folderNamed $dmgName) | Out-Null
    Assert-PublisherRefused 'Folder in place of the disk image' (Merge-Arguments $publish @{ MacDirectory = $folderNamed }) 'has no file'
    $linked = Copy-Folder $mac 'mac-linked'
    Remove-Item -LiteralPath (Join-Path $linked $dmgName) -Force
    $linkMade = $true
    try { New-Item -ItemType SymbolicLink -Path (Join-Path $linked $dmgName) -Target (Join-Path $mac $dmgName) | Out-Null } catch { $linkMade = $false }
    if ($linkMade) {
        Assert-PublisherRefused 'Link in place of the disk image' (Merge-Arguments $publish @{ MacDirectory = $linked }) 'has no file'
    }
    $empty = New-ReleaseFolder 'mac-empty' $dmgName ''
    Assert-PublisherRefused 'Empty disk image' (Merge-Arguments $publish @{ MacDirectory = $empty }) 'is empty'

    # 3. Each file must match the SHA256SUMS.txt its build wrote.
    $changed = Copy-Folder $mac 'mac-changed'
    [IO.File]::WriteAllText((Join-Path $changed $dmgName), 'changed after the build', $utf8)
    Assert-PublisherRefused 'Disk image changed after its checksum' (Merge-Arguments $publish @{ MacDirectory = $changed }) 'does not match its SHA256SUMS\.txt'
    $noSums = Copy-Folder $windows 'windows-no-sums'
    Remove-Item -LiteralPath (Join-Path $noSums 'SHA256SUMS.txt') -Force
    Assert-PublisherRefused 'Build without SHA256SUMS.txt' (Merge-Arguments $publish @{ WindowsDirectory = $noSums }) 'The windows build has no SHA256SUMS\.txt'
    $otherSums = Copy-Folder $windows 'windows-other-sums'
    [IO.File]::WriteAllText((Join-Path $otherSums 'SHA256SUMS.txt'), "$('a' * 64)  StateLife-Device-Installer-Setup-1.0.0.exe`n", $utf8)
    Assert-PublisherRefused 'SHA256SUMS.txt for another file' (Merge-Arguments $publish @{ WindowsDirectory = $otherSums }) 'must list StateLife-Device-Installer-Setup-1\.1\.0\.exe exactly once'
    $twice = Copy-Folder $windows 'windows-listed-twice'
    $line = [IO.File]::ReadAllText((Join-Path $twice 'SHA256SUMS.txt'))
    [IO.File]::WriteAllText((Join-Path $twice 'SHA256SUMS.txt'), $line + $line, $utf8)
    Assert-PublisherRefused 'File listed twice' (Merge-Arguments $publish @{ WindowsDirectory = $twice }) 'exactly once'

    # 4. Publication: exact copies and the contract's manifest.json, in one rename.
    $result = Invoke-Publisher $publish
    if ($result.Message -or ($result.Output -join ',') -cne '1.1.0') { throw "Installer app 1.1.0 was not published: '$($result.Message)'" }
    $published = Join-Path $store '1.1.0'
    if ((@(Get-ChildItem -LiteralPath $published -Force | ForEach-Object Name | Sort-Object) -join ',') -cne
        ((@($dmgName, $setupName, 'manifest.json') | Sort-Object) -join ',')) {
        throw 'Published version does not hold exactly its two files and manifest.json'
    }
    foreach ($pair in @(@($windows, $setupName), @($mac, $dmgName))) {
        if ((Get-Sha256 (Join-Path $published $pair[1])) -cne (Get-Sha256 (Join-Path $pair[0] $pair[1]))) {
            throw "$($pair[1]) was not copied byte for byte"
        }
    }
    $manifestPath = Join-Path $published 'manifest.json'
    $bytes = [IO.File]::ReadAllBytes($manifestPath)
    if ($bytes[0] -eq 0xEF) { throw 'manifest.json starts with a byte-order mark' }
    $contractScript = Join-Path $work 'contract.py'
    [IO.File]::WriteAllText($contractScript, $contract, $utf8)
    $python = (Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if (-not $python) { $python = (Get-Command python3 -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
    if ($python) {
        $preference = $ErrorActionPreference
        try {
            # Windows PowerShell 5.1 turns native stderr into ErrorRecords.
            $ErrorActionPreference = 'Continue'
            $check = @(& $python $contractScript $manifestPath 2>&1 | ForEach-Object { "$_" })
            $exitCode = $LASTEXITCODE
        } finally { $ErrorActionPreference = $preference }
        if ($exitCode -ne 0 -or ($check -join '') -cne 'OK') { throw "manifest.json breaks ADD's contract: $($check -join ' ')" }
    }
    $manifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
    if ($manifest.version -cne '1.1.0' -or $manifest.git_sha -cne $gitSha -or $manifest.product -cne 'state-life-device-installer') {
        throw 'manifest.json names another version, commit or product'
    }
    $expected = @(
        @('windows', 'x64', 'setup', $setupName, (Join-Path $windows $setupName)),
        @('macos', 'arm64', 'dmg', $dmgName, (Join-Path $mac $dmgName))
    )
    if (@($manifest.files).Count -ne 2) { throw 'manifest.json must list exactly two files' }
    foreach ($index in 0..1) {
        $entry = @($manifest.files)[$index]
        $want = $expected[$index]
        if ($entry.platform -cne $want[0] -or $entry.arch -cne $want[1] -or $entry.kind -cne $want[2] -or $entry.filename -cne $want[3] -or
            [long]$entry.size -ne (Get-Item -LiteralPath $want[4]).Length -or $entry.sha256 -cne (Get-Sha256 $want[4])) {
            throw "manifest.json describes $($want[3]) wrongly"
        }
    }
    $keptNow = @('1.0.0', '.staging', 'README.txt') | ForEach-Object { $_ + '=' + (Get-TreeState (Join-Path $store $_)) }
    if (($keptNow -join ';') -cne ($kept -join ';')) { throw 'Publication changed what the store already held' }
    if ((@(Get-ChildItem -LiteralPath (Join-Path $store '.staging') -Force | ForEach-Object Name) -join ',') -cne 'app-1.0.1-other-run') {
        throw 'Publication left its own staging folder behind'
    }

    # 5. The same bytes again change nothing, not even the publication time.
    $before = Get-TreeState $store
    $result = Invoke-Publisher $publish
    if ($result.Message -or ($result.Output -join ',') -cne '1.1.0') { throw "Publishing the same bytes again failed: '$($result.Message)'" }
    if ((Get-TreeState $store) -ne $before) { throw 'Publishing the same bytes again changed the store' }

    # 6. A published version is never rewritten with other bytes.
    $rebuilt = Copy-Folder $mac 'mac-rebuilt'
    Set-Package $rebuilt $dmgName 'synthetic macOS disk image 1.1.0, built again'
    Assert-PublisherRefused 'Same version built again' (Merge-Arguments $publish @{ MacDirectory = $rebuilt }) 'already published with other files; a published version is never rewritten'
    Assert-PublisherRefused 'Same bytes from another commit' (Merge-Arguments $publish @{ GitSha = ('f' * 40) }) 'already published with other files'
    $publishedSetup = Join-Path $published $setupName
    $original = [IO.File]::ReadAllBytes($publishedSetup)
    [IO.File]::WriteAllText($publishedSetup, 'damaged in the store', $utf8)
    Assert-PublisherRefused 'Published file damaged in the store' $publish 'already published with other files'
    [IO.File]::WriteAllBytes($publishedSetup, $original)
    [IO.File]::WriteAllText((Join-Path $published 'extra.txt'), 'not listed', $utf8)
    Assert-PublisherRefused 'Published version with an unlisted file' $publish 'already published with other files'
    Remove-Item -LiteralPath (Join-Path $published 'extra.txt') -Force

    # 7. The store defaults to ADD_INSTALLER_APP_STORE_HOST_PATH, and the build
    # folders may be relative to PowerShell's location, as in the workflow.
    $environmentStore = Join-Path $work 'environment-store'
    $env:ADD_INSTALLER_APP_STORE_HOST_PATH = $environmentStore
    New-ReleaseFolder 'windows-1.2.0' 'StateLife-Device-Installer-Setup-1.2.0.exe' 'synthetic Windows Setup 1.2.0' | Out-Null
    New-ReleaseFolder 'mac-1.2.0' 'StateLife-Device-Installer-1.2.0-macOS-arm64.dmg' 'synthetic macOS disk image 1.2.0' | Out-Null
    Push-Location $work
    try { $result = Invoke-Publisher @{ WindowsDirectory = 'windows-1.2.0'; MacDirectory = 'mac-1.2.0'; Version = '1.2.0'; GitSha = $gitSha } }
    finally { Pop-Location }
    if ($result.Message -or ($result.Output -join ',') -cne '1.2.0') { throw "Installer app 1.2.0 was not published: '$($result.Message)'" }
    if (-not (Test-Path -LiteralPath (Join-Path $environmentStore '1.2.0/manifest.json') -PathType Leaf)) {
        throw 'Installer app 1.2.0 ignored ADD_INSTALLER_APP_STORE_HOST_PATH'
    }
    if (Test-Path -LiteralPath (Join-Path $store '1.2.0')) { throw 'Installer app 1.2.0 was published to the wrong store' }
    if (Test-Path -LiteralPath (Join-Path $environmentStore '.staging')) { throw 'Publication left an empty staging folder behind' }

    $checked = $(if ($python) { "Python's JSON parser" } else { 'PowerShell only; Python not found' })
    $linkCase = $(if ($linkMade) { '' } else { ' (no symbolic link case: links cannot be made here)' })
    Write-Output "PASS: installer apps refuse TEST builds, other versions and files that do not match their SHA256SUMS, publish exact copies with the contract's manifest.json ($checked) in one rename, never rewrite a version and keep the rest of the store$linkCase"
    $global:LASTEXITCODE = 0
} finally {
    Pop-Location
    $env:ADD_INSTALLER_APP_STORE_HOST_PATH = $originalStore
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
