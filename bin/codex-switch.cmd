@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex-switch.ps1" %*
exit /b %ERRORLEVEL%
