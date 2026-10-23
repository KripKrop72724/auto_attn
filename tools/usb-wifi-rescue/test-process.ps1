$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
    Add-Type -AssemblyName System.Windows.Forms
} else {
    Add-Type 'namespace Windows.Forms { public static class Application { public static void DoEvents() {} } }'
}
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'repair.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$function = $ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Run-Tool'}, $true)
if (-not $function) { throw 'Actual repair process runner is missing' }
Invoke-Expression $function.Extent.Text
function Say([string]$Text) {}
$script:folder = Join-Path ([IO.Path]::GetTempPath()) ('SLIC process test ' + [guid]::NewGuid().ToString('N'))
$script:extract = $script:folder
$script:tool = (Get-Process -Id $PID).Path
New-Item -ItemType Directory $script:folder | Out-Null
try {
    $child = Join-Path $script:folder 'child tool.ps1'
    @'
param([string]$Mode)
[Console]::Out.WriteLine('OUT-MARKER')
[Console]::Error.WriteLine('ERR-MARKER')
if ($Mode -eq 'large') {
    $chunk = 'x' * 4096
    for ($i = 0; $i -lt 64; $i++) {
        [Console]::Out.WriteLine($chunk)
        [Console]::Error.WriteLine($chunk)
    }
}
if ($Mode -eq 'nonzero') { exit 7 }
if ($Mode -eq 'timeout') { Start-Sleep -Seconds 15 }
exit 0
'@ | Set-Content -LiteralPath $child -Encoding UTF8
    foreach ($mode in @('zero','large','nonzero','timeout')) {
        $failed = $false; $message = ''
        $limit = 30; if ($mode -eq 'timeout') { $limit = 2 }
        $timer = [Diagnostics.Stopwatch]::StartNew()
        try {
            $text = Run-Tool $mode @('-NoProfile','-NonInteractive','-File',$child,$mode) $limit
        } catch { $failed = $true; $message = $_.Exception.Message }
        $meta = Get-Content (Join-Path $script:folder ($mode + '.process.json')) -Raw | ConvertFrom-Json
        $outText = [IO.File]::ReadAllText((Join-Path $script:folder ($mode + '.stdout.txt')))
        $errText = [IO.File]::ReadAllText((Join-Path $script:folder ($mode + '.stderr.txt')))
        if ($outText -notmatch 'OUT-MARKER' -or $errText -notmatch 'ERR-MARKER') { throw "Lost stdout/stderr: $mode" }
        if ($mode -in @('zero','large')) {
            if ($failed -or $meta.exit_code -ne 0 -or $meta.timed_out -or $text -notmatch 'ERR-MARKER') { throw "Successful command rejected: $mode $message" }
            if ($mode -eq 'large' -and ($outText.Length -lt 262144 -or $errText.Length -lt 262144)) { throw 'Large concurrent output was truncated' }
        } elseif ($mode -eq 'nonzero') {
            if (-not $failed -or $meta.exit_code -ne 7 -or $meta.timed_out -or $message -notmatch 'exit code 7') { throw 'Nonzero exit was not preserved/refused' }
        } else {
            if (-not $failed -or -not $meta.timed_out -or $message -notmatch 'timed out' -or $timer.Elapsed.TotalSeconds -gt 10) { throw 'Timeout did not stop the child and retain logs' }
        }
        Write-Output "PASS: actual repair runner $mode; stdout/stderr and exit status retained"
    }
} finally {
    Remove-Item -LiteralPath $script:folder -Recurse -Force
}
