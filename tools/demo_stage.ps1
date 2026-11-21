# Extended launcher that starts the entire AgentGate demo stack (gateway :8000, bank :8001, demo_server :8002).
$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $scriptDir 'demo.ps1') @args
exit $LASTEXITCODE
