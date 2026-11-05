[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$Uv = 'uv',
    [string]$Output = 'test-results/windows'
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Run this qualification on Windows.' }
$env:GMAT_MCP_TEST_CONFIG = (Resolve-Path -LiteralPath $Config).Path
New-Item -ItemType Directory -Force -Path $Output | Out-Null
& $Uv sync --locked
if ($LASTEXITCODE) { throw 'Locked environment setup failed.' }
& $Uv run gmat-mcp --config $env:GMAT_MCP_TEST_CONFIG doctor
if ($LASTEXITCODE) { throw 'Both native profiles must pass doctor.' }
$Report = Join-Path $Output 'qualification.xml'
& $Uv run pytest -q "--junitxml=$Report"
if ($LASTEXITCODE) { throw 'Windows qualification failed; preserve the report and native logs.' }
