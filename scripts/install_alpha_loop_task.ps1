param([Parameter(Mandatory=$true)][string]$Python,[Parameter(Mandatory=$true)][string]$Runtime,[switch]$Activate)
$ErrorActionPreference = 'Stop'
$runtimePath = (Resolve-Path -LiteralPath $Runtime).Path
if (!(Test-Path -LiteralPath (Join-Path $runtimePath 'authority.json'))) { throw 'INITIALIZE_FROZEN_AUTHORITY_FIRST' }
if (!(Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'PYTHON_MISSING' }
$model = (Get-Command codex -ErrorAction Stop).Source
Set-Content -LiteralPath (Join-Path $runtimePath 'model-executable.txt') -Value $model -Encoding UTF8
$runner = Join-Path $PSScriptRoot 'run_alpha_loop.ps1'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $runner + '" -Python "' + $Python + '" -Runtime "' + $runtimePath + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal
if ($Activate) {
    Register-ScheduledTask -TaskName 'ONE-AlphaLoop-v1-20261029' -InputObject $task -Force | Out-Null
    Export-ScheduledTask -TaskName 'ONE-AlphaLoop-v1-20261029' | Set-Content -LiteralPath (Join-Path $runtimePath 'scheduler-task.xml') -Encoding UTF8
    Write-Output 'ACTIVATED: current-user paper research; requires awake host and logged-in user.'
} else { Write-Output 'VALIDATED_NOT_ACTIVATED: use -Activate to install the paper research worker.' }
