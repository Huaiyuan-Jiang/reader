param(
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$ReportDirectory,
    [switch]$RequireSignature
)
$ErrorActionPreference = 'Stop'
$exePath = (Resolve-Path -LiteralPath $Exe).Path
$reportPath = [IO.Path]::GetFullPath($ReportDirectory)
New-Item -ItemType Directory -Force -Path $reportPath | Out-Null
$signature = Get-AuthenticodeSignature -LiteralPath $exePath
if ($RequireSignature) {
    if ($signature.Status -ne 'Valid') { throw "Trusted Authenticode signature required: $($signature.Status)" }
    if ($null -eq $signature.TimeStamperCertificate) { throw 'Timestamp certificate required' }
}
$smokePath = Join-Path $reportPath 'smoke-result.json'
if (Test-Path -LiteralPath $smokePath) { throw 'Use a fresh report directory; stale smoke result exists' }
$process = Start-Process -FilePath $exePath -ArgumentList @('--self-test', ('"' + $smokePath + '"')) -PassThru -WindowStyle Hidden
if (-not $process.WaitForExit(120000)) {
    # Kill only the process tree created by this verifier.
    & taskkill.exe /PID $process.Id /T /F | Out-Null
    throw 'Executable self-test timed out'
}
if ($process.ExitCode -ne 0) { throw "Executable self-test failed: $($process.ExitCode)" }
$smoke = Get-Content -LiteralPath $smokePath -Raw | ConvertFrom-Json
$expected = @('pdf-open', 'pdf-render-numpy', 'pdf-multiprocessing', 'epub-open', 'dpapi-and-ai-imports', 'claude-sdk-import', 'windows-ocr-imports', 'shelf-ui')
if (-not $smoke.ok -or @(Compare-Object $expected @($smoke.checks)).Count -ne 0) { throw 'Incomplete executable self-test' }
$hash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
@{ signatureStatus = [string]$signature.Status; timestamped = ($null -ne $signature.TimeStamperCertificate); sha256 = $hash; checks = $smoke.checks } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $reportPath 'verification.json') -Encoding utf8
"$hash  $([IO.Path]::GetFileName($exePath))" | Set-Content -LiteralPath (Join-Path $reportPath 'SHA256SUMS.txt') -Encoding ascii
Write-Output "Verified executable: $($signature.Status); 8 runtime checks passed"
