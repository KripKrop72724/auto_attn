$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath ".env.add" -PathType Leaf)) {
    throw "Missing .env.add"
}

function Invoke-Docker {
    param([Parameter(Mandatory = $true)][string[]] $Arguments)

    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$compose = @("compose", "--env-file", ".env.add", "-f", "docker-compose.add.yml")
Invoke-Docker -Arguments ($compose + @("config", "--quiet"))
Invoke-Docker -Arguments ($compose + @("build", "--pull"))
Invoke-Docker -Arguments ($compose + @("up", "-d", "--remove-orphans"))
Invoke-Docker -Arguments ($compose + @("ps"))
