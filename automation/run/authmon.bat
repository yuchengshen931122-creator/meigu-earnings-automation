@echo off
REM ASCII ONLY - see dispatch.bat
REM Auth fast loop, every 10 minutes: check all five auth lines (Google OAuth,
REM Drive write, NotebookLM, claude CLI, LINE), self-repair what is repairable
REM (Google token refresh, NotebookLM "auth refresh" - the warm-up that used to
REM live in MeiguNlmWarm now happens here), write state/auth_status.json for
REM the live dashboard, and push a LINE alert only when a line is down and
REM could not be repaired. A recovery notice is pushed when it comes back.
REM
REM Always exits 0: a red light here is reported through LINE and the dashboard,
REM not through the task result. If this script itself stops running, the hourly
REM MeiguWatchdog raises authmon_stale (state file older than 65 minutes).
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
echo ==== %DATE% %TIME% authmon ==== >> "%ROOT%\logs\authmon.log"
"%PY%" "%ROOT%\tools\auth_monitor.py" >> "%ROOT%\logs\authmon.log" 2>&1
echo ---- exit %ERRORLEVEL% ---- >> "%ROOT%\logs\authmon.log"
exit /b 0
