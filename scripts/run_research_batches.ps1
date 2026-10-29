param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$AlphaRunner,
      [Parameter(Mandatory=$true)][string]$AlphaRuntime,
      [Parameter(Mandatory=$true)][string]$BatchRuntime)
$ErrorActionPreference='Stop'
$repo = Split-Path -Parent $PSScriptRoot
$logRoot = Join-Path $BatchRuntime 'scheduler-logs'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$modelPath = (Get-Content -Raw -LiteralPath (Join-Path $AlphaRuntime 'model-executable.txt')).Trim()
$env:PATH = (Split-Path -Parent $modelPath) + ';' + $env:PATH
$env:PYTHONUTF8='1'
function Invoke-BatchStep([string]$Step) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ')
    $stdout = Join-Path $logRoot ($stamp + '-' + $Step + '.stdout.log')
    $stderr = Join-Path $logRoot ($stamp + '-' + $Step + '.stderr.log')
    $arguments = '-B -X utf8 -m one_trading.economic_batches --runtime "' + $BatchRuntime + '" ' + $Step
    $child = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $repo -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    [ordered]@{completed_at=(Get-Date).ToUniversalTime().ToString('o');step=$Step;exit_code=$child.ExitCode;stdout=$stdout;stderr=$stderr} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $BatchRuntime ('scheduler-' + $Step + '.json')) -Encoding UTF8
    return $child.ExitCode
}
# Reconcile this sidecar's durable claims before the old controller sees them.
$recoveryCode = Invoke-BatchStep 'recover'
if ($recoveryCode -ne 0) { exit $recoveryCode }
# Existing sealed runner remains first for market risk and maturity.
$alpha = Start-Process -FilePath 'powershell.exe' -ArgumentList ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $AlphaRunner + '" -Python "' + $Python + '" -Runtime "' + $AlphaRuntime + '"') -WindowStyle Hidden -PassThru -Wait
if ($alpha.ExitCode -ne 0) { exit $alpha.ExitCode }
exit (Invoke-BatchStep 'run')
