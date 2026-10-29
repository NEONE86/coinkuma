param([Parameter(Mandatory=$true)][string]$Python,[Parameter(Mandatory=$true)][string]$Runtime)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path
$modelPath = (Get-Content -LiteralPath (Join-Path $runtimePath 'model-executable.txt') -Raw -Encoding UTF8).Trim()
if (!(Test-Path -LiteralPath $modelPath -PathType Leaf)) { throw 'MODEL_EXECUTABLE_MISSING' }
$env:PATH = (Split-Path -Parent $modelPath) + ';' + $env:PATH
$env:PYTHONUTF8 = '1'
$env:ONE_ALPHA_SCHEDULED = '1'
$logs = Join-Path $runtimePath 'scheduler-logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
$stdout = Join-Path $logs ($stamp + '.stdout.log')
$stderr = Join-Path $logs ($stamp + '.stderr.log')
$arguments = '-B -X utf8 -m one_trading.alpha_loop --runtime "' + $runtimePath + '" tick'
$child = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -Wait -PassThru
[pscustomobject]@{completed_at=[DateTime]::UtcNow.ToString('o');exit_code=$child.ExitCode;stdout=$stdout;stderr=$stderr} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runtimePath 'scheduler-last-process.json') -Encoding UTF8
exit $child.ExitCode
