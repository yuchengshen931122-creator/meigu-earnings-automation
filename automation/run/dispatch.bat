@echo off
REM This is the rolling worker: every 30 min it re-reads the tracking sheet,
REM so any date added or changed since the last cycle is picked up automatically.
setlocal
REM Point PY at your python.exe (full path) if plain "python" is not
REM visible to Task Scheduler's environment.
set PY=python
REM ROOT = the automation folder (parent of this run\ folder).
REM If your absolute path contains non-ASCII characters and cmd.exe
REM chokes on it, replace this with the 8.3 short path (see: dir /x).
set ROOT=%~dp0..
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
cd /d "%ROOT%"
echo ==== %DATE% %TIME% dispatch ==== >> "%ROOT%\logs\dispatch.log"
"%PY%" "%ROOT%\tools\dispatcher.py" --once --execute >> "%ROOT%\logs\dispatch.log" 2>&1
echo ---- exit %ERRORLEVEL% ---- >> "%ROOT%\logs\dispatch.log"
exit /b %ERRORLEVEL%
