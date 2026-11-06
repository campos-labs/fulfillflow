#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Candidate,
    [Parameter(Mandatory)][string]$Destination,
    [Parameter(Mandatory)][string]$Audit,
    [string]$EnvironmentFile = '.env.benchmark-v11.example',
    [switch]$PlanOnly
)
$ErrorActionPreference = 'Stop'
$exitCode = 2
$savedEnvironment = @{}
try {
    $repository = Split-Path -Parent $PSScriptRoot
    # Resolve caller-relative paths before setting the child's working directory.
    $options = @{
        candidate = [IO.Path]::GetFullPath($Candidate)
        destination = [IO.Path]::GetFullPath($Destination)
        audit = [IO.Path]::GetFullPath($Audit)
        plan_only = [bool]$PlanOnly
    }
    foreach ($line in Get-Content -LiteralPath $EnvironmentFile) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $pair = $line.Split('=', 2)
        if ($pair.Length -ne 2 -or $pair[0] -notmatch '^[A-Z][A-Z0-9_]*$') {
            throw 'Invalid environment file entry'
        }
        if (-not $savedEnvironment.ContainsKey($pair[0])) {
            $savedEnvironment[$pair[0]] = [Environment]::GetEnvironmentVariable($pair[0], 'Process')
        }
        [Environment]::SetEnvironmentVariable($pair[0], $pair[1], 'Process')
    }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = Join-Path $repository '.venv/Scripts/python.exe'
    $start.WorkingDirectory = $repository
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($argument in @('-X', 'utf8', '-B', '-m', 'benchmarks.pilot_v11')) { $start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::Start($start)
    # JSON travels on stdin, never through PowerShell/native command-line quote parsing.
    $process.StandardInput.WriteLine(($options | ConvertTo-Json -Compress))
    $process.StandardInput.Close()
    $process.WaitForExit()
    $exitCode = $process.ExitCode
    $process.Dispose()
} catch {
    $exitCode = 2
    $message = $_.Exception.Message
    foreach ($entry in Get-ChildItem Env:) {
        if ($entry.Name -match 'PASSWORD|SECRET|TOKEN|SIGNATURE|DATABASE_URL' -and $entry.Value) {
            $message = $message.Replace($entry.Value, '[redacted]')
        }
    }
    $message = $message -replace '\b[a-z][a-z0-9+.-]*://\S+', '[redacted URL]'
    [Console]::Error.WriteLine((@{
        stage = 'launcher'; exit_code = 2
        error_type = $_.Exception.GetType().Name; message = $message
    } | ConvertTo-Json -Compress))
} finally {
    foreach ($key in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $savedEnvironment[$key], 'Process')
    }
}
exit $exitCode
