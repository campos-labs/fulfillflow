#Requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$PlanOnly,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$exitCode = 2
try {
    if ($PlanOnly -and $PrepareOnly) { throw 'Choose one mode only.' }
    $repository = Split-Path -Parent $PSScriptRoot
    $process = Get-Process -Id $PID
    if (-not $process.Path -or -not (Test-Path -LiteralPath $process.Path -PathType Leaf)) {
        throw 'The running PowerShell executable is unavailable.'
    }
    $launcher = @{ executable = $process.Path; version = $PSVersionTable.PSVersion.ToString() }
    $python = Join-Path $repository '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Host runner Python is unavailable.' }
    $mode = if ($PlanOnly) { '--plan-only' } elseif ($PrepareOnly) { '--prepare-only' } else { '--execute' }
    Push-Location -LiteralPath $repository
    try {
        $launcher | ConvertTo-Json -Compress | & $python -X utf8 -B -m benchmarks.paired_controls --series warmup $mode
        $exitCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($exitCode -ne 0) {
        [Console]::Error.WriteLine("Warm-up diagnostic stopped with exit code $exitCode. Read the stage and diagnostic path above; no automatic retry.")
    }
} catch {
    [Console]::Error.WriteLine("Warm-up diagnostic launcher failed: $($_.Exception.Message)")
    $exitCode = 2
}
exit $exitCode
