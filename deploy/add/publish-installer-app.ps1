param(
    # The Windows release build: its Setup .exe and the SHA256SUMS.txt beside it.
    [Parameter(Mandatory = $true)][string]$WindowsDirectory,
    # The macOS release build: its .dmg and the SHA256SUMS.txt beside it.
    [Parameter(Mandatory = $true)][string]$MacDirectory,
    # The app version both builds carry (sl_installer.__version__).
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')][string]$Version,
    # The commit both builds came from.
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$GitSha,
    # ADD serves this store read-only as /installer-apps.
    [string]$StoreDirectory = ''
)

# Publishes one version of the State Life Device Installer (docs/esp-installer.md)
# to <store>\<version>\: the Windows Setup, the macOS disk image and manifest.json.
# Each file must match the SHA256SUMS.txt its build wrote, and is checked again
# where ADD reads it. The version directory appears in one rename, and a
# published version is never rewritten with other bytes; publishing the same
# bytes again changes nothing. Older versions and anything else in the store
# stay as they are.
$ErrorActionPreference = 'Stop'
if (-not $StoreDirectory) { $StoreDirectory = $env:ADD_INSTALLER_APP_STORE_HOST_PATH }
if (-not $StoreDirectory) {
    $StoreDirectory = 'C:\Users\Public\Documents\StateLife\AttendanceDeviceDashboard\installer-apps'
}
$utf8 = New-Object Text.UTF8Encoding($false)

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ReleaseFile {
    param([string]$Directory, [string]$Platform, [string]$Arch, [string]$Kind, [string]$Name)
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        throw "The $Platform build folder $Directory does not exist"
    }
    # .NET reads relative paths from the process's folder, not PowerShell's.
    $Directory = (Resolve-Path -LiteralPath $Directory).Path
    $path = Join-Path $Directory $Name
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if (-not $item -or $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "The $Platform build has no file $Name (a TEST build or another version is never published)"
    }
    if ($item.Length -le 0) { throw "$Name is empty" }
    $sums = Join-Path $Directory 'SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $sums -PathType Leaf)) { throw "The $Platform build has no SHA256SUMS.txt" }
    $listed = @([IO.File]::ReadAllLines($sums) | ForEach-Object {
        $match = [regex]::Match($_, '^([0-9A-Fa-f]{64}) [ *](.+)$')
        if ($match.Success -and $match.Groups[2].Value -ceq $Name) { $match.Groups[1].Value.ToLowerInvariant() }
    })
    if ($listed.Count -ne 1) { throw "SHA256SUMS.txt of the $Platform build must list $Name exactly once" }
    $sha256 = Get-Sha256 $item.FullName
    if ($sha256 -cne $listed[0]) { throw "$Name does not match its SHA256SUMS.txt" }
    return [ordered]@{
        platform = $Platform; arch = $Arch; kind = $Kind; filename = $Name
        size = [long]$item.Length; sha256 = $sha256; source = $item.FullName
    }
}

function Test-Published {
    # True when every listed file is in the directory, alone, with its size and SHA-256.
    param([string]$Directory, [object[]]$Files)
    $expected = @(@($Files | ForEach-Object { [string]$_.filename }) + 'manifest.json' | Sort-Object)
    $actual = @(Get-ChildItem -LiteralPath $Directory -Force | ForEach-Object Name | Sort-Object)
    if (($actual -join '|') -cne ($expected -join '|')) { return $false }
    foreach ($file in $Files) {
        $path = Join-Path $Directory ([string]$file.filename)
        $item = Get-Item -LiteralPath $path -Force
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $false }
        if ([long]$item.Length -ne [long]$file.size -or (Get-Sha256 $path) -cne [string]$file.sha256) { return $false }
    }
    return $true
}

function Get-FileKey {
    param([object[]]$Files)
    return (@($Files | ForEach-Object {
        '{0}/{1}/{2}/{3}/{4}/{5}' -f $_.platform, $_.arch, $_.kind, $_.filename, $_.size, $_.sha256
    } | Sort-Object) -join '|')
}

$files = @(
    (Get-ReleaseFile $WindowsDirectory 'windows' 'x64' 'setup' "StateLife-Device-Installer-Setup-$Version.exe"),
    (Get-ReleaseFile $MacDirectory 'macos' 'arm64' 'dmg' "StateLife-Device-Installer-$Version-macOS-arm64.dmg")
)
$manifest = [ordered]@{
    schema = 1
    product = 'state-life-device-installer'
    version = $Version
    git_sha = $GitSha
    published_at = [DateTime]::UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", [Globalization.CultureInfo]::InvariantCulture)
    files = @(foreach ($file in $files) {
        [ordered]@{ platform = $file.platform; arch = $file.arch; kind = $file.kind; filename = $file.filename; size = $file.size; sha256 = $file.sha256 }
    })
}

New-Item -ItemType Directory -Path $StoreDirectory -Force | Out-Null
$store = (Resolve-Path -LiteralPath $StoreDirectory).Path
$final = Join-Path $store $Version
if (Test-Path -LiteralPath $final) {
    # The same bytes again (a re-run of the publish job) change nothing.
    $same = $false
    try {
        $published = [IO.File]::ReadAllText((Join-Path $final 'manifest.json')) | ConvertFrom-Json
        $same = $published.version -ceq $Version -and $published.git_sha -ceq $GitSha -and
            (Get-FileKey @($published.files)) -ceq (Get-FileKey $manifest.files) -and
            (Test-Published $final @($published.files))
    } catch { $same = $false }
    if (-not $same) {
        throw "Installer app $Version is already published with other files; a published version is never rewritten (build a new version)"
    }
    Write-Host "Installer app $Version is already published with exactly these files; nothing changed"
    Write-Output $Version
    return
}

# Staged one level down, where no reader of <store>\<version>\manifest.json
# looks, then renamed into place on the same volume.
$stagingRoot = Join-Path $store '.staging'
$staging = Join-Path $stagingRoot ('app-' + $Version + '-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    # The files first and the manifest that names them last.
    foreach ($file in $files) {
        Copy-Item -LiteralPath $file.source -Destination (Join-Path $staging $file.filename)
    }
    [IO.File]::WriteAllText((Join-Path $staging 'manifest.json'), (ConvertTo-Json -InputObject $manifest -Depth 4) + "`n", $utf8)
    if (-not (Test-Published $staging $manifest.files)) { throw 'The staged installer app files do not match their SHA-256' }
    # One rename; it fails rather than merge if the version appeared meanwhile.
    [IO.Directory]::Move($staging, $final)
}
finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
    if ((Test-Path -LiteralPath $stagingRoot) -and -not (Get-ChildItem -LiteralPath $stagingRoot -Force)) {
        Remove-Item -LiteralPath $stagingRoot -Force
    }
}
try {
    if (-not (Test-Published $final $manifest.files)) { throw 'The published installer app files do not match their SHA-256' }
}
catch {
    # Only this run created $final; never leave a version that failed its check.
    Remove-Item -LiteralPath $final -Recurse -Force
    throw
}
Write-Host "Published installer app $Version to $final"
Write-Output $Version
