$ErrorActionPreference = "Stop"

$TaskName = "OpenWhispr-AutoEnter-Guard"
$PythonW = "C:\Users\giand\AppData\Local\Programs\Python\Python312\pythonw.exe"
if (-not (Test-Path $PythonW)) {
    $PythonW = (Get-Command pythonw.exe -ErrorAction Stop).Source
}
$ScriptPath = "C:\Users\giand\.openwhispr\auto-enter-patch\patch_openwhispr.py"
if (-not (Test-Path $ScriptPath)) {
    throw "Script nao encontrado em $ScriptPath"
}

$Action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$ScriptPath`" --guard"
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$TriggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($TriggerLogon, $TriggerRepeat) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Reaplica automaticamente o patch Auto-Enter no OpenWhispr caso uma atualizacao sobrescreva o app.asar." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State, @{N='RunLevel';E={$_.Principal.RunLevel}}
