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
    if ($PlanOnly -and $PrepareOnly) {
        throw 'Use either -PlanOnly or -PrepareOnly, never both.'
    }
    $repository = Split-Path -Parent $PSScriptRoot
    $process = Get-Process -Id $PID
    if (-not $process.Path -or -not (Test-Path -LiteralPath $process.Path)) {
        throw 'The running PowerShell executable path is unavailable.'
    }
    $launcher = @{ executable = $process.Path; version = $PSVersionTable.PSVersion.ToString() }
    $mode = if ($PlanOnly) { '--plan-only' } elseif ($PrepareOnly) { '--prepare-only' } else { '--execute' }
    $python = Join-Path $repository '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Control driver Python is unavailable: $python"
    }
    Push-Location -LiteralPath $repository
    try {
        $launcher | ConvertTo-Json -Compress | & $python -X utf8 -B -m benchmarks.paired_controls $mode
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        [Console]::Error.WriteLine("Paired controls stopped with exit code $exitCode; no automatic retry.")
    }
} catch {
    [Console]::Error.WriteLine("Paired control launcher failed: $($_.Exception.Message)")
    $exitCode = 2
}
exit $exitCode
