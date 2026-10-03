param([Parameter(Mandatory = $true)][string]$Package)
$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$pythonPath = Join-Path $PSScriptRoot '.artifacts\c-runtimes-01\v1.2.0-rc.1\venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Frozen coordinator Python is unavailable.' }
$packagePath = [System.IO.Path]::GetFullPath($Package)
$oldPythonPath = $env:PYTHONPATH
$oldNoBytecode = $env:PYTHONDONTWRITEBYTECODE
$exitResult = 2
Push-Location -LiteralPath $repoRoot
try {
    $env:PYTHONPATH = $repoRoot
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $pythonPath -B -m validation.functional.c_evaluated $packagePath
    $exitResult = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPythonPath
    $env:PYTHONDONTWRITEBYTECODE = $oldNoBytecode
    Pop-Location
}
if ($exitResult -ne 0) { Write-Host 'C evaluation stopped. Preserve diagnostics; do not repeat the command.' }
exit $exitResult
