@echo off
rem hh-snpr: open the approve page in the browser (window can be closed).
chcp 65001 >nul
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 -m snpr web
