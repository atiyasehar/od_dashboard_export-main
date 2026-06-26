# Start the OD dashboard using deploy.env from the project root.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$EnvFile = Join-Path $Root "deploy.env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line -match "^([^=]+)=(.*)$") {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim().Trim('"').Trim("'")
            if ($name) { Set-Item -Path "env:$name" -Value $value }
        }
    }
    Write-Host "Loaded $EnvFile"
} else {
    Write-Host "No deploy.env found — copy deploy.example.env to deploy.env and edit PostgreSQL settings."
}

python scripts/run_dashboard.py --bundle-root . @args
