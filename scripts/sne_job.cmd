@echo off
rem hh-snpr scheduler job (cadence: snpr\config.toml [job] interval_minutes).
rem Historical note: hh views are not limited (only responses are) + the send age
rem gate is 10 minutes (config [send] max_age_minutes) - we must catch
rem vacancies within minutes of publication.
rem Scores without a human (--mode park): auto-send >= auto_approve_min
rem (fresh <= 10 min only), the rest accumulates in data\parked.jsonl.
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
set "LOG=data\scheduler.log"
set "PYTHONIOENCODING=utf8"
set "PYTHONUNBUFFERED=1"

echo ===== %date% %time% ===== >> "%LOG%"
"%PY%" -X utf8 -m snpr sne --mode park >> "%LOG%" 2>&1
set "SNE_RC=%ERRORLEVEL%"

rem the approve page is opened by hand (Otbor.cmd / snpr web), not by the job
exit /b %SNE_RC%