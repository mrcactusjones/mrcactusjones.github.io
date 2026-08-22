@echo off
REM Fallback for when PowerShell execution policy gets in the way.
cd /d "%~dp0.."
py -3 run.py daily --provider ppt --log || python run.py daily --provider ppt --log
