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
    $options = @{
        checkout = Join-Path $repository 'benchmarks/results/v10-controls-win9445-source-06'
        attempts = @(
            (Join-Path $repository 'benchmarks/results/v10-control-mixed-4-win9445-attempt-01'),
            (Join-Path $repository 'benchmarks/results/v10-control-mixed-4-win9445-attempt-02')
        )
        launcher = @{
            executable = $process.Path
            version = $PSVersionTable.PSVersion.ToString()
        }
        plan_only = [bool]$PlanOnly
        prepare_only = [bool]$PrepareOnly
    }
    $python = Join-Path $repository '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Control driver Python is unavailable: $python"
    }
    Push-Location -LiteralPath $repository
    try {
        # Share the caller's console and pass JSON as data, never as shell code.
        $options | ConvertTo-Json -Compress -Depth 4 | & $python -X utf8 -B -m benchmarks.controls_v10
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($exitCode -ne 0) {
        [Console]::Error.WriteLine(
            "The v1.0 control launcher stopped with exit code $exitCode; it did not retry automatically."
        )
    }
} catch {
    [Console]::Error.WriteLine("The v1.0 control launcher failed: $($_.Exception.Message)")
    $exitCode = 2
}
exit $exitCode
