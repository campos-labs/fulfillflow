#Requires -Version 7.0
[CmdletBinding()]
param(
    [switch]$PlanOnly
)

$ErrorActionPreference = 'Stop'
$exitCode = 2
try {
    $repository = Split-Path -Parent $PSScriptRoot
    $process = Get-Process -Id $PID
    if (-not $process.Path -or -not (Test-Path -LiteralPath $process.Path)) {
        throw 'The running PowerShell executable path is unavailable.'
    }
    $options = @{
        checkout = Join-Path $repository 'benchmarks/results/v10-controls-win9445-source'
        attempts = @(
            (Join-Path $repository 'benchmarks/results/v10-control-mixed-4-win9445-attempt-01'),
            (Join-Path $repository 'benchmarks/results/v10-control-mixed-4-win9445-attempt-02')
        )
        launcher = @{
            executable = $process.Path
            version = $PSVersionTable.PSVersion.ToString()
        }
        plan_only = [bool]$PlanOnly
    }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = Join-Path $repository '.venv/Scripts/python.exe'
    $start.WorkingDirectory = $repository
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($argument in @('-X', 'utf8', '-B', '-m', 'benchmarks.controls_v10')) {
        $start.ArgumentList.Add($argument)
    }
    $child = [Diagnostics.Process]::Start($start)
    $child.StandardInput.WriteLine(($options | ConvertTo-Json -Compress -Depth 4))
    $child.StandardInput.Close()
    $child.WaitForExit()
    $exitCode = $child.ExitCode
    $child.Dispose()
} catch {
    [Console]::Error.WriteLine('The v1.0 control launcher failed before a control could start.')
    $exitCode = 2
}
exit $exitCode
