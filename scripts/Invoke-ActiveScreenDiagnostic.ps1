#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateSet('IdleCheck','Execute')][string]$Mode='Execute',
    [ValidateRange(1,99)][int]$IdleAttempt=1
)
function Wait-ActiveScreenChild {
    param([Diagnostics.Process]$Process, [int]$TimeoutMilliseconds)
    $report=@{pid=$Process.Id;timeout_ms=$TimeoutMilliseconds;exited=$false;forced_termination=$false}
    try {
        $report.started_utc=$Process.StartTime.ToUniversalTime().ToString('o')
        $report.exited=$Process.WaitForExit($TimeoutMilliseconds)
        if($report.exited){$report.exit_code=$Process.ExitCode}
        else{$report.condition='child_shutdown_timeout'}
    } catch {
        $report.condition='child_shutdown_observation_failed'
        $report.error_type=$_.Exception.GetType().Name
    }
    return $report
}
$ErrorActionPreference='Stop'
$PSNativeCommandUseErrorActionPreference=$false
$root=Split-Path -Parent $PSScriptRoot
$base=Join-Path $root 'benchmarks/results'
$destination=Join-Path $base $(if($Mode -eq 'IdleCheck'){"active-screen-idle-$($IdleAttempt.ToString('00'))"}else{'active-screen-operation-01'})
$guard=$null
$child=$null
$exitCode=2
$created=$false
$started=[DateTime]::UtcNow
$schemeBefore=@()
$schemeExit=$null
$activeRequestsExit=$null
$childShutdown=$null
try {
    if($Mode -eq 'Execute' -and $IdleAttempt -ne 1){throw 'IdleAttempt is valid only for IdleCheck.'}
    if(Test-Path -LiteralPath $destination){throw 'Destination exists; no retry or overwrite.'}
    if($Mode -eq 'Execute' -and -not(Test-Path -LiteralPath (Join-Path $base 'active-screen-release-01/ready.json'))){throw 'Execution awaits review and explicit release.'}
    New-Item -ItemType Directory -Path $destination | Out-Null
    $created=$true
    Add-Type -Path (Join-Path $PSScriptRoot 'ActiveScreenGuard.cs')
    $schemeBefore=& "$env:SystemRoot/System32/powercfg.exe" /query 2>&1
    $schemeExit=$LASTEXITCODE
    if($schemeExit -ne 0){throw 'Power settings query failed.'}
    $schemeBefore | Out-File -Encoding utf8 (Join-Path $destination 'power-before.txt')
    $guard=[ActiveScreenGuard]::new()
    $started=[DateTime]::UtcNow
    $statePath=Join-Path $destination 'guard.json'
    $env:FULFILLFLOW_ACTIVE_GUARD=$statePath
    $limit=if($Mode -eq 'IdleCheck'){960}else{7200}
    while($true){
        $heartbeat=[DateTime]::new($guard.HeartbeatTicks,[DateTimeKind]::Utc)
        $state=@{ready=$guard.Ready;failure=$guard.Failure;display=$guard.Display;heartbeat_utc=$heartbeat.ToString('o')}
        $temp=Join-Path $destination 'guard-next.json'
        $state | ConvertTo-Json | Set-Content -Encoding utf8 $temp
        [IO.File]::Move($temp,$statePath,$true)
        $elapsed=([DateTime]::UtcNow-$started).TotalSeconds
        if($guard.Failure -ne ''){throw "Environmental condition failed: $($guard.Failure)"}
        if($elapsed -gt 3 -and ([DateTime]::UtcNow-$heartbeat).TotalSeconds -gt 3){throw 'Native observer heartbeat unavailable.'}
        if($elapsed -gt 8 -and -not $guard.Ready){throw 'Observer did not establish screen/session/AC.'}
        if($guard.Ready -and $null -eq $activeRequestsExit){
            $activeRequests=& "$env:SystemRoot/System32/powercfg.exe" /requests 2>&1
            $activeRequestsExit=$LASTEXITCODE
            $activeRequests | Out-File -Encoding utf8 (Join-Path $destination 'requests-active.txt')
        }
        if($Mode -eq 'Execute' -and $guard.Ready -and $null -eq $child){
            $info=[Diagnostics.ProcessStartInfo]::new()
            $info.FileName=Join-Path $root '.venv/Scripts/python.exe'
            $info.WorkingDirectory=$root
            $info.UseShellExecute=$false
            foreach($arg in @('-X','utf8','-B','-m','benchmarks.active_screen_controls','--execute')){$info.ArgumentList.Add($arg)}
            $child=[Diagnostics.Process]::Start($info)
        }
        if($null -ne $child -and $child.HasExited){$exitCode=$child.ExitCode;break}
        if($Mode -eq 'IdleCheck' -and $elapsed -ge $limit){$exitCode=0;break}
        if($Mode -eq 'Execute' -and $elapsed -ge $limit){throw 'Operational guard deadline exceeded.'}
        Start-Sleep -Milliseconds 250
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    if($created){
        @{stage='power-supervision';type=$_.Exception.GetType().Name;message=$_.Exception.Message} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $destination 'error.json')
    }
    $exitCode=2
} finally {
    if($null -ne $guard){$guard.Dispose()}
    if($created){
        if($null -ne $guard){
            @{ready=$false;failure='guard_released';display=$guard.Display;heartbeat_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $destination 'guard.json')
            $guard.Events() | Set-Content -Encoding utf8 (Join-Path $destination 'power-events.txt')
            if(-not $guard.Released){$exitCode=2}
        }
        # The runner observes guard failure within 250 ms during load, and preserves diagnostics.
        # Preparation commands remain bounded by their existing deadline; no forced resource deletion.
        if($null -ne $child){
            $childShutdown=Wait-ActiveScreenChild -Process $child -TimeoutMilliseconds 180000
            if(-not $childShutdown.exited){
                $exitCode=2
                [Console]::Error.WriteLine('Child did not confirm shutdown within 180 seconds. Manual inspection required; no retry. No process or historical resource was forcibly stopped.')
                # Emit before writing: loss of file export must not hide the surviving child.
                [Console]::Error.WriteLine(($childShutdown | ConvertTo-Json -Compress))
            }
            $childShutdown | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $destination 'child-shutdown.json')
        }
        $requests=& "$env:SystemRoot/System32/powercfg.exe" /requests 2>&1
        $requestExit=$LASTEXITCODE
        $requests | Out-File -Encoding utf8 (Join-Path $destination 'requests-after.txt')
        $after=& "$env:SystemRoot/System32/powercfg.exe" /query 2>&1
        $afterExit=$LASTEXITCODE
        $after | Out-File -Encoding utf8 (Join-Path $destination 'power-after.txt')
        if($afterExit -ne 0 -or ($schemeBefore -join "`n") -ne ($after -join "`n")){$exitCode=2}
        @{mode=$Mode;exit_code=$exitCode;duration_seconds=([DateTime]::UtcNow-$started).TotalSeconds;released=($null -ne $guard -and $guard.Released);child_shutdown=$childShutdown;requests_active_exit=$activeRequestsExit;requests_exit=$requestExit;power_before_exit=$schemeExit;power_after_exit=$afterExit;executable=(Get-Process -Id $PID).Path;load_executed=($null -ne $child);guard_sha256=(Get-FileHash (Join-Path $PSScriptRoot 'ActiveScreenGuard.cs')).Hash.ToLowerInvariant();launcher_sha256=(Get-FileHash $PSCommandPath).Hash.ToLowerInvariant()} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $destination 'result.json')
    }
    Remove-Item Env:FULFILLFLOW_ACTIVE_GUARD -ErrorAction SilentlyContinue
}
exit $exitCode
