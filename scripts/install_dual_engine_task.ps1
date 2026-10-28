param(
 [Parameter(Mandatory=$true)][string]$Python,
 [Parameter(Mandatory=$true)][string]$Runtime,
 [string]$TaskName = 'ONE-DualEngine-Forward-v1-20261029',
 [switch]$Activate
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path
if ($runtimePath.StartsWith($repoPath + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) {
 throw 'Runtime must be outside Git.'
}
$runner = Join-Path $PSScriptRoot 'run_dual_engine.ps1'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $runner + '" -Python "' + $pythonPath + '" -Repo "' + $repoPath + '" -Runtime "' + $runtimePath + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $repoPath
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'ONE public-data hourly forward research and paper-only maintenance; no live orders. Continues beyond seven days while host is awake and user logged in.'
if (-not $Activate) {
 $task | ConvertTo-Json -Depth 8
 Write-Output 'VALIDATED_NOT_ACTIVATED'
 exit 0
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task exists; inspect it before replacing.' }
$modelCommand = Get-Command codex.exe -ErrorAction SilentlyContinue
if ($modelCommand) { $modelCommand.Source | Out-File -LiteralPath (Join-Path $runtimePath 'model-executable.txt') -Encoding utf8 }
$gitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if ($gitCommand) { $gitCommand.Source | Out-File -LiteralPath (Join-Path $runtimePath 'git-executable.txt') -Encoding utf8 }
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null
Export-ScheduledTask -TaskName $TaskName | Out-File -LiteralPath (Join-Path $runtimePath 'scheduler-task.xml') -Encoding utf8
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime,LastTaskResult,NextRunTime,NumberOfMissedRuns | ConvertTo-Json
