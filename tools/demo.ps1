# Run in a dedicated PowerShell process so the demo environment is scoped to this session.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = (Resolve-Path (Join-Path $repo '.venv\Scripts\python.exe')).Path
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$demoName = 'agentgate-demo-' + [guid]::NewGuid().ToString('N')
$demoDir = (New-Item -ItemType Directory -Path (Join-Path $tempRoot $demoName)).FullName
Push-Location $demoDir
try {
    $env:PYTHONPATH = $repo
    $env:AGENTGATE_PROFILE = 'real'
    $env:AGENTGATE_GATEWAY_DB = Join-Path $demoDir 'gateway.db'
    $env:AGENTGATE_BANK_DB = Join-Path $demoDir 'bank.db'
    $env:AGENTGATE_BANK_DB_URL = 'sqlite+aiosqlite:///' + $env:AGENTGATE_BANK_DB.Replace('\', '/')
    $env:AGENTGATE_BANK_SERVICE_CREDENTIAL = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    $env:AGENTGATE_AUDIT_SIGNING_KEY_PATH = Join-Path $demoDir 'audit.pem'
    $env:AGENTGATE_DISPUTE_TRUST_PATH = Join-Path $demoDir 'trust.json'
    $env:AGENTGATE_TOKEN_SIGNING_KEY_PATH = Join-Path $demoDir 'token.pem'
    $env:AGENTGATE_MANDATE_SIGNING_KEY_PATH = Join-Path $demoDir 'mandate.pem'
    $env:D06_BILLPAL_KEY = Join-Path $demoDir 'billpal.pem'
    $env:D06_OPS_ID = 'ops_admin'
    if (-not $env:D06_CUSTOMER_PIN) {
        $pin = Read-Host 'Choose a four-digit Customer PIN' -AsSecureString
        $env:D06_CUSTOMER_PIN = [Net.NetworkCredential]::new('', $pin).Password
    }
    if (-not $env:D06_OPS_PASSWORD) {
        $password = Read-Host 'Choose a disposable Ops password' -AsSecureString
        $env:D06_OPS_PASSWORD = [Net.NetworkCredential]::new('', $password).Password
    }
    & $python -m tools.seed --profile real
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "SDK key file for this session: $env:D06_BILLPAL_KEY"
    & $python -m tools.dev stack --profile real
    exit $LASTEXITCODE
} finally {
    Pop-Location
    $resolved = (Resolve-Path -LiteralPath $demoDir).Path
    if ($resolved -eq [IO.Path]::GetFullPath((Join-Path $tempRoot $demoName))) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
