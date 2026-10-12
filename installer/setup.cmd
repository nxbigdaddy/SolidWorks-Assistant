@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-SolidWorksAssistant.ps1"
exit /b %errorlevel%
