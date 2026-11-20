@echo off
cd /d "%~dp0.."
py -3 -m db.serve data\full
if errorlevel 1 pause
