@echo off
REM ASCII ONLY - see dispatch.bat
REM Picks up podcasts that landed on disk and archives them: upload to Drive,
REM insert the podcast link into the existing memo tab, tick the Podcast
REM checkbox, then push to LINE if that ticker has not been pushed yet.
REM
REM The quarter folder used to be hardcoded (set QUARTER=2Q26). That is a silent
REM time bomb: the day the season rolls over, this keeps scanning the old empty
REM folder, every new audio file sits there forever waiting for an upload that
REM never comes, and nothing fails - sweep just reports "0 files" and exits 0.
REM So derive it instead, and sweep the previous bucket too, because stragglers
REM (companies whose call slips into the next quarter) still belong to the old one.
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

REM No quotes around the command: cmd's for/f parser chokes when the command
REM starts with a quoted path. Safe here because both PY and ROOT are
REM space-free (ROOT is the 8.3 short path for exactly this reason).
for /f "usebackq tokens=1,2" %%a in (`%PY% %ROOT%\tools\quarter_label.py`) do (
  set QUARTER=%%a
  set PREV=%%b
)

if "%QUARTER%"=="" (
  echo ==== %DATE% %TIME% sweep FAILED to derive quarter ==== >> "%ROOT%\logs\sweep.log"
  exit /b 1
)

echo ==== %DATE% %TIME% sweep %QUARTER% ==== >> "%ROOT%\logs\sweep.log"
"%PY%" "%ROOT%\tools\sweep_podcasts.py" --quarter %QUARTER% --publish >> "%ROOT%\logs\sweep.log" 2>&1
set RC=%ERRORLEVEL%

REM Previous bucket: missing folder returns 1 and that is fine, not an error.
echo ==== %DATE% %TIME% sweep %PREV% (carryover) ==== >> "%ROOT%\logs\sweep.log"
"%PY%" "%ROOT%\tools\sweep_podcasts.py" --quarter %PREV% --publish >> "%ROOT%\logs\sweep.log" 2>&1

echo ---- exit %RC% ---- >> "%ROOT%\logs\sweep.log"
exit /b %RC%
