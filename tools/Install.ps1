[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
& $Python (Join-Path $PSScriptRoot 'install.py') --destination $Destination
if ($LASTEXITCODE -ne 0) { throw "Installation failed (exit $LASTEXITCODE); inspect the output above." }
