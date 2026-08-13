@echo off
REM ASCII ONLY - see dispatch.bat
REM Rolling date discovery: re-scans the calendar every run, so newly announced
REM or changed earnings dates flow into the sheet, which the dispatcher then acts on.
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
echo ==== %DATE% %TIME% dates ==== >> "%ROOT%\logs\dates.log"
"%PY%" "%ROOT%\tools\verify_dates.py" --rolling --days 60 --concurrency 2 --write >> "%ROOT%\logs\dates.log" 2>&1
set RC=%ERRORLEVEL%

REM Daily housekeeping, folded into the one task that already runs once a day.
REM
REM The podcast tree is rebuilt from scratch every quarter with a name nobody
REM can predict (see tools/resolve_podcast_tree.py). Until now, pointing
REM config.json at the new one was a manual chore - and forgetting it silently
REM files a whole season of audio into LAST quarter's tree: the folder exists,
REM the upload succeeds, the sheet gets ticked, nothing fails.
REM Resolve it automatically; the tool refuses to guess when the answer is not
REM unambiguous, and MeiguWatchdog alerts on that case.
echo ---- podcast tree ---- >> "%ROOT%\logs\dates.log"
"%PY%" "%ROOT%\tools\resolve_podcast_tree.py" --write >> "%ROOT%\logs\dates.log" 2>&1

REM Rebuild the archive index daily, not just at quarter start: new ticker
REM folders appear inside the trees all season long, and a stale index means
REM a ticker gets filed one level up instead of in its own folder.
echo ---- drive map ---- >> "%ROOT%\logs\dates.log"
"%PY%" "%ROOT%\tools\build_drive_map.py" >> "%ROOT%\logs\dates.log" 2>&1

echo ---- exit %RC% ---- >> "%ROOT%\logs\dates.log"
exit /b %RC%
