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
. (Join-Path $PSScriptRoot 'ActiveScreenIO.ps1')
$base=Join-Path $root 'benchmarks/results'
$destination=Join-Path $base $(if($Mode -eq 'IdleCheck'){"active-screen-idle-$($IdleAttempt.ToString('00'))"}else{'comparison-active-operation-03'})
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
    if($Mode -ne 'Execute'){throw 'This campaign does not authorize another idle check.'}
    if($Mode -eq 'Execute' -and $IdleAttempt -ne 1){throw 'IdleAttempt is valid only for IdleCheck.'}
    if(Test-Path -LiteralPath $destination){throw 'Destination exists; no retry or overwrite.'}
    if($Mode -eq 'Execute' -and -not(Test-Path -LiteralPath (Join-Path $base 'comparison-active-release-03/ready.json'))){throw 'Execution awaits review and explicit release.'}
    Push-Location -LiteralPath $root
    try {
        & (Join-Path $root '.venv/Scripts/python.exe') -X utf8 -B -m benchmarks.active_comparison_controls --preflight
        if($LASTEXITCODE -ne 0){throw 'Prerequisites failed before execution destination creation; no isolation or load started. See specific diagnostic above.'}
    } finally { Pop-Location }
    New-Item -ItemType Directory -Path $destination | Out-Null
    $created=$true
    Add-Type -Path (Join-Path $PSScriptRoot 'ActiveScreenGuard.cs')
    $beforeCapture=Invoke-PowerCapture '/query' (Join-Path $destination 'power-before')
    $schemeBefore=$beforeCapture.Text
    $schemeExit=$beforeCapture.ExitCode
    if($schemeExit -ne 0){throw 'Power settings query failed.'}

    $guard=[ActiveScreenGuard]::new()
    $started=[DateTime]::UtcNow
    $statePath=Join-Path $destination 'guard.json'
    $env:FULFILLFLOW_ACTIVE_GUARD=$statePath
    $limit=if($Mode -eq 'IdleCheck'){960}else{86400}
    while($true){
        $heartbeat=[DateTime]::new($guard.HeartbeatTicks,[DateTimeKind]::Utc)
        $state=@{ready=$guard.Ready;failure=$guard.Failure;display=$guard.Display;heartbeat_utc=$heartbeat.ToString('o')}
        [ActiveScreenIO]::Publish($statePath,($state | ConvertTo-Json))
        $elapsed=([DateTime]::UtcNow-$started).TotalSeconds
        if($guard.Failure -ne ''){throw "Environmental condition failed: $($guard.Failure)"}
        if($elapsed -gt 3 -and ([DateTime]::UtcNow-$heartbeat).TotalSeconds -gt 3){throw 'Native observer heartbeat unavailable.'}
        if($elapsed -gt 8 -and -not $guard.Ready){throw 'Observer did not establish screen/session/AC.'}
        if($guard.Ready -and $null -eq $activeRequestsExit){
            $activeRequests=Invoke-PowerCapture '/requests' (Join-Path $destination 'requests-active')
            $activeRequestsExit=$activeRequests.ExitCode

        }
        if($Mode -eq 'Execute' -and $guard.Ready -and $null -eq $child){
            $info=[Diagnostics.ProcessStartInfo]::new()
            $info.FileName=Join-Path $root '.venv/Scripts/python.exe'
            $info.WorkingDirectory=$root
            $info.UseShellExecute=$false
            foreach($arg in @('-X','utf8','-B','-m','benchmarks.active_comparison_controls','--execute')){$info.ArgumentList.Add($arg)}
            $child=[Diagnostics.Process]::Start($info)
        }
        if($null -ne $child -and $child.HasExited){$exitCode=$child.ExitCode;break}
        if($Mode -eq 'IdleCheck' -and $elapsed -ge $limit){$exitCode=0;break}
        if($Mode -eq 'Execute' -and $elapsed -ge $limit){throw 'Operational guard deadline exceeded.'}
        Start-Sleep -Milliseconds 250
    }
} catch {
    $cause=$_.Exception.GetBaseException()
    $native=if($cause -is [ComponentModel.Win32Exception]){$cause.NativeErrorCode}else{$null}
    [Console]::Error.WriteLine("Power supervision failed: $($cause.GetType().Name); native=$native; hresult=$($cause.HResult)")
    [Console]::Error.WriteLine($_.Exception.Message)
    if($created){
        @{stage='power-supervision';type=$cause.GetType().Name;native_code=$native;hresult=$cause.HResult;message=$_.Exception.Message} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $destination 'error.json')
    }
    $exitCode=2
} finally {
    if($null -ne $guard){$guard.Dispose()}
    if($created){
        if($null -ne $guard){
            try {
                @{ready=$false;failure='guard_released';display=$guard.Display;heartbeat_utc=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json | ForEach-Object { [ActiveScreenIO]::Publish((Join-Path $destination 'guard.json'),$_) }
            } catch {
                $exitCode=2
                $cause=$_.Exception.GetBaseException()
                [Console]::Error.WriteLine("Final heartbeat publication failed: $($cause.GetType().Name); hresult=$($cause.HResult)")
            }
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
        try {
        $requests=Invoke-PowerCapture '/requests' (Join-Path $destination 'requests-after')
        $requestExit=$requests.ExitCode

        $afterCapture=Invoke-PowerCapture '/query' (Join-Path $destination 'power-after')
        $after=$afterCapture.Text
        $afterExit=$afterCapture.ExitCode

        if($afterExit -ne 0 -or ($schemeBefore -join "`n") -ne ($after -join "`n")){$exitCode=2}
        } catch {
            $exitCode=2
            [Console]::Error.WriteLine("Final power capture failed: $($_.Exception.GetBaseException().GetType().Name)")
        }
        # Dispatch is not evidence that Locust started. Unknown stays null.
        $loadExecuted=if($null -eq $child){$false}else{$null}
        $statusPath=Join-Path $destination 'coordinator-status.json'
        if(Test-Path -LiteralPath $statusPath){
            try{$loadExecuted=(Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json).load_executed}
            catch{[Console]::Error.WriteLine('Coordinator status unreadable; load execution remains unknown.');$exitCode=2}
        }
        @{mode=$Mode;exit_code=$exitCode;duration_seconds=([DateTime]::UtcNow-$started).TotalSeconds;released=($null -ne $guard -and $guard.Released);child_shutdown=$childShutdown;requests_active_exit=$activeRequestsExit;requests_exit=$requestExit;power_before_exit=$schemeExit;power_after_exit=$afterExit;executable=(Get-Process -Id $PID).Path;coordinator_started=($null -ne $child);load_executed=$loadExecuted;guard_sha256=(Get-FileHash (Join-Path $PSScriptRoot 'ActiveScreenGuard.cs')).Hash.ToLowerInvariant();launcher_sha256=(Get-FileHash $PSCommandPath).Hash.ToLowerInvariant()} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $destination 'result.json')
    }
    Remove-Item Env:FULFILLFLOW_ACTIVE_GUARD -ErrorAction SilentlyContinue
}
exit $exitCode
