param(
 [Parameter(Mandatory=$true)][string]$Python,
 [Parameter(Mandatory=$true)][string]$Repo,
 [Parameter(Mandatory=$true)][string]$Runtime
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath $Repo).Path
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path
$modelPathFile = Join-Path $runtimePath 'model-executable.txt'
if (Test-Path -LiteralPath $modelPathFile) {
 $modelPath = (Get-Content -LiteralPath $modelPathFile -Raw -Encoding utf8).Trim()
 if ((Test-Path -LiteralPath $modelPath) -and ([IO.Path]::GetFileName($modelPath) -eq 'codex.exe')) {
  $env:PATH = [IO.Path]::GetDirectoryName($modelPath) + ';' + $env:PATH
 }
}
$env:ONE_AUTONOMY_SCHEDULED = '1'
Set-Location -LiteralPath $repoPath
$logDir = Join-Path $runtimePath 'scheduler-logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir ((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ') + '.log')
try {
 $modelCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
 [pscustomobject]@{ at=(Get-Date).ToUniversalTime().ToString('o'); model_executable_available=[bool]$modelCommand } | ConvertTo-Json | Out-File -LiteralPath (Join-Path $runtimePath 'scheduler-model-path-check.json') -Encoding utf8
 & $Python -X utf8 -m one_trading.autonomy --runtime $runtimePath tick 2>&1 | Out-File -LiteralPath $logPath -Encoding utf8
 $resultCode = $LASTEXITCODE
 exit $resultCode
} catch {
 $_ | Out-File -LiteralPath $logPath -Append -Encoding utf8
 exit 1
}
