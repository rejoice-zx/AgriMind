param([string]$PythonExe = "python")
$ErrorActionPreference = "Stop"

& $PythonExe (Join-Path $PSScriptRoot 'verify_all.py')
exit $LASTEXITCODE
