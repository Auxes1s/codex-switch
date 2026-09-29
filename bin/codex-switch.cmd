@echo off
setlocal DisableDelayedExpansion
where.exe pwsh.exe >nul 2>nul
if errorlevel 1 goto WindowsPowerShell
pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex-switch.ps1" %*
exit /b %ERRORLEVEL%
:WindowsPowerShell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex-switch.ps1" %*
exit /b %ERRORLEVEL%
