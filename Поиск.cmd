@echo off
rem hh-snpr: one search run now (search + scoring + letters), no responses sent.
title hh-snpr: search
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf8"
"%PY%" -X utf8 -m snpr sne --mode park
echo.
pause
