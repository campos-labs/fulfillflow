#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateRange(1, 8)][int]$Attempt,
    [switch]$PlanOnly,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$exitCode = 2
try {
    if (([int]$PlanOnly.IsPresent + [int]$PrepareOnly.IsPresent + [int]($Attempt -gt 0)) -ne 1) {
        throw 'Choose PlanOnly, PrepareOnly, or exactly one Attempt.'
    }
    $repository = Split-Path -Parent $PSScriptRoot
    $process = Get-Process -Id $PID
    if (-not $process.Path -or -not (Test-Path -LiteralPath $process.Path -PathType Leaf)) {
        throw 'The running PowerShell executable is unavailable.'
    }
    $launcher = @{ executable = $process.Path; version = $PSVersionTable.PSVersion.ToString() }
    $python = Join-Path $repository '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Host runner Python is unavailable.' }
    [string[]]$mode = if ($PlanOnly) { '--plan-only' } elseif ($PrepareOnly) { '--prepare-only' } else { '--execute'; "$Attempt" }
    Push-Location -LiteralPath $repository
    try {
        $launcher | ConvertTo-Json -Compress | & $python -X utf8 -B -m benchmarks.sensitivity_controls @mode
        $exitCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($exitCode -ne 0) {
        [Console]::Error.WriteLine("Warm-up sensitivity stopped with exit code $exitCode. Preserve diagnostics and request review; no automatic retry.")
    }
} catch {
    [Console]::Error.WriteLine("Warm-up sensitivity launcher failed: $($_.Exception.Message)")
    $exitCode = 2
}
exit $exitCode
