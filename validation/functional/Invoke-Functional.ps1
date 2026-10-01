[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,39}$')]
    [string]$RunId,
    [ValidateSet('Development', 'Evaluated')]
    [string]$Mode = 'Development',
    [ValidateSet('core', 'tracking', 'all')]
    [string]$Owner = 'all',
    [ValidateRange(1, 3)]
    [int]$Repetitions = 3,
    [string]$NetworkSubnet,
    [string]$ReleaseFile,
    [string]$ReleaseSha256,
    [string]$PythonExecutable = 'C:\Users\natoc\.codex\worktrees\f05e\fulfillflow\.venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
if (-not [System.IO.Path]::IsPathRooted($PythonExecutable) -or
    -not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw 'Python executable must be an existing absolute path.'
}
$sourceRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:PYTHONPATH = (Join-Path $sourceRoot 'src') + [System.IO.Path]::PathSeparator + $PSScriptRoot
    $childArguments = @(
        '-B', (Join-Path $PSScriptRoot 'run_b.py'),
        '--source', $sourceRoot, '--run-id', $RunId,
        '--mode', $Mode.ToLowerInvariant(), '--owner', $Owner,
        '--repetitions', $Repetitions
    )
    if ($NetworkSubnet) {
        $childArguments += @('--network-subnet', $NetworkSubnet)
    }
    if ($ReleaseFile) { $childArguments += @('--release-file', $ReleaseFile) }
    if ($ReleaseSha256) { $childArguments += @('--release-sha256', $ReleaseSha256) }
    & $PythonExecutable @childArguments
    $childExit = $LASTEXITCODE
} finally {
    $env:PYTHONDONTWRITEBYTECODE = $previousBytecode
    $env:PYTHONPATH = $previousPythonPath
}
if ($childExit -ne 0) {
    Write-Host "Functional verification stopped with exit code $childExit; no automatic retry."
}
exit $childExit