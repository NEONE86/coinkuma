param(
 [Parameter(Mandatory=$true)][string]$Python,
 [Parameter(Mandatory=$true)][string]$Repo,
 [Parameter(Mandatory=$true)][string]$Runtime
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath $Repo).Path
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path
$initialGitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
$initialModelCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
$modelPathFile = Join-Path $runtimePath 'model-executable.txt'
if (Test-Path -LiteralPath $modelPathFile) {
 $modelPath = (Get-Content -LiteralPath $modelPathFile -Raw -Encoding utf8).Trim()
 if ((Test-Path -LiteralPath $modelPath) -and ([IO.Path]::GetFileName($modelPath) -eq 'codex.exe')) {
  $env:PATH = [IO.Path]::GetDirectoryName($modelPath) + ';' + $env:PATH
 }
}
$gitPathFile = Join-Path $runtimePath 'git-executable.txt'
if (Test-Path -LiteralPath $gitPathFile) {
 $gitPath = (Get-Content -LiteralPath $gitPathFile -Raw -Encoding utf8).Trim()
 if ((Test-Path -LiteralPath $gitPath) -and ([IO.Path]::GetFileName($gitPath) -eq 'git.exe')) {
  $env:PATH = [IO.Path]::GetDirectoryName($gitPath) + ';' + $env:PATH
 }
}
$env:ONE_AUTONOMY_SCHEDULED = '1'
Set-Location -LiteralPath $repoPath
$logDir = Join-Path $runtimePath 'scheduler-logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$logPath = Join-Path $logDir ((Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ') + '.log')
try {
 $modelCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
 [pscustomobject]@{ at=(Get-Date).ToUniversalTime().ToString('o'); model_executable_available=[bool]$modelCommand; model_available_before_pin=[bool]$initialModelCommand; git_available_before_pin=[bool]$initialGitCommand; git_executable_available=[bool](Get-Command git.exe -ErrorAction SilentlyContinue) } | ConvertTo-Json | Out-File -LiteralPath (Join-Path $runtimePath 'scheduler-model-path-check.json') -Encoding utf8
 $errorLogPath = $logPath + '.stderr.log'
 $pythonArguments = '-X utf8 -m one_trading.autonomy --runtime "' + $runtimePath + '" tick'
 $runProcess = Start-Process -FilePath $Python -ArgumentList $pythonArguments -WorkingDirectory $repoPath -WindowStyle Hidden -RedirectStandardOutput $logPath -RedirectStandardError $errorLogPath -Wait -PassThru
 $resultCode = $runProcess.ExitCode
 [pscustomobject]@{ completed_at=(Get-Date).ToUniversalTime().ToString('o'); exit_code=$resultCode; stdout=$logPath; stderr=$errorLogPath } | ConvertTo-Json | Out-File -LiteralPath (Join-Path $runtimePath 'scheduler-last-process.json') -Encoding utf8
 exit $resultCode
} catch {
 $_ | Out-File -LiteralPath $logPath -Append -Encoding utf8
 exit 1
}
