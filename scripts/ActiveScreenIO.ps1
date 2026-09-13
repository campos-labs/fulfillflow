Add-Type -Path (Join-Path $PSScriptRoot 'ActiveScreenIO.cs')
function Invoke-PowerCapture {
    param([string]$Argument,[string]$Destination)
    $capture=[ActiveScreenIO]::Power($Argument)
    [IO.File]::WriteAllBytes($Destination+'.stdout.bin',$capture.Stdout)
    [IO.File]::WriteAllBytes($Destination+'.stderr.bin',$capture.Stderr)
    @{exit_code=$capture.ExitCode;code_page_before=$capture.CodePageBefore;code_page_after=$capture.CodePageAfter;decode_error=$capture.DecodeError} | ConvertTo-Json | Set-Content -Encoding utf8 ($Destination+'.json')
    if($null -ne $capture.Text){[IO.File]::WriteAllText($Destination+'.txt',$capture.Text,[Text.UTF8Encoding]::new($false))}
    if($capture.DecodeError){throw "Power output decoding failed: $($capture.DecodeError); see $Destination.json"}
    return $capture
}
