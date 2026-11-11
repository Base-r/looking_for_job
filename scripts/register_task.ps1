# Регистрация плановой задачи hh-snpr: прогон каждые N минут (по умолчанию 20;
# `python -m snpr job on` передаёт значение из snpr\config.toml [job]).
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
# (обычно не нужен руками: `python -m snpr job on` сам до-регистрирует)
# Джоба скрыта (wscript + run_hidden.vbs): консоль не мигает. Когда есть
# готовые к пуску — открывается видимое окно `snpr approve` + звук.

# -DailyAt "09:00" — раз в день в это время (snpr\config.toml [job] daily_at).

param([int]$IntervalMinutes = 20, [string]$DailyAt = "")

$root = Split-Path -Parent $PSScriptRoot
$vbs = Join-Path $root "scripts\run_hidden.vbs"
$cmd = Join-Path $root "scripts\sne_job.cmd"

$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "`"$vbs`" `"$cmd`"" `
    -WorkingDirectory $root

if ($DailyAt) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
} else {
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
}

# только когда пользователь в системе: окно approve должно быть видимым
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

# WakeToRun: будить компьютер из сна для прогона (нужны разрешённые
# «таймеры пробуждения» в настройках электропитания Windows).
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName "hh-snpr" `
    -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

if ($DailyAt) {
    Write-Host "Задача 'hh-snpr' зарегистрирована: каждый день в $DailyAt (скрыто)."
} else {
    Write-Host "Задача 'hh-snpr' зарегистрирована: каждые $IntervalMinutes мин (скрыто)."
}
Write-Host "Удаление: Unregister-ScheduledTask hh-snpr"