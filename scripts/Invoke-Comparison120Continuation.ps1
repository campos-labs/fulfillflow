#Requires -Version 7.0
[CmdletBinding()]
param([switch]$PrepareReview)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$exitCode = 2
try {
    $repository = Split-Path -Parent $PSScriptRoot
    $process = Get-Process -Id $PID
    $launcher = @{ executable = $process.Path; version = $PSVersionTable.PSVersion.ToString() }
    $python = Join-Path $repository '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Frozen Python unavailable.' }
    $mode = if ($PrepareReview) { '--prepare-review' } else { '--execute' }
    Push-Location -LiteralPath $repository
    try {
        $launcher | ConvertTo-Json -Compress | & $python -X utf8 -B -m benchmarks.comparison_continuation $mode
        $exitCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($exitCode -ne 0) {
        [Console]::Error.WriteLine("Comparison continuation stopped with exit code $exitCode; no automatic retry. Preserve diagnostics.")
    }
} catch {
    [Console]::Error.WriteLine("Comparison continuation launcher stopped: $($_.Exception.Message)")
    $exitCode = 2
}
exit $exitCode

