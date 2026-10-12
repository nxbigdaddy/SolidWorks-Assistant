@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall-SolidWorksAssistant.ps1"
exit /b %errorlevel%
