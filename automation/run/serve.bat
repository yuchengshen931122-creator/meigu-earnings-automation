@echo off
REM ASCII ONLY - see dispatch.bat
REM Supervisor for the live dashboard server.
REM Runs every 5 min from Task Scheduler: exits immediately if the port is
REM already listening, otherwise starts the server. That covers both autostart
REM and crash recovery, and needs no elevation (unlike /sc onlogon).
setlocal
set PORT=8787
REM pythonw = no console window. Point at your pythonw.exe full path if
REM plain "pythonw" is not visible to Task Scheduler's environment.
set PY=pythonw
REM ROOT = the automation folder (parent of this run\ folder).
REM If your absolute path contains non-ASCII characters and cmd.exe
REM chokes on it, replace this with the 8.3 short path (see: dir /x).
set ROOT=%~dp0..
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

netstat -ano | findstr ":%PORT% " | findstr LISTENING >nul
if %ERRORLEVEL%==0 exit /b 0

if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
cd /d "%ROOT%"
echo ==== %DATE% %TIME% serve start ==== >> "%ROOT%\logs\serve.log"
start "" /b "%PY%" "%ROOT%\tools\serve_dashboard.py" --port %PORT% --poll 15 >> "%ROOT%\logs\serve.log" 2>&1
exit /b 0
