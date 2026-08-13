@echo off
REM ASCII ONLY - see dispatch.bat
REM Re-makes podcasts for tickers whose memo finished but whose audio never got
REM produced (NotebookLM auth expired, sources temporarily unreachable, ...).
REM
REM Why this needs to exist: the dispatcher treats podcast as non-blocking. When
REM it fails, the memo is still archived and the row is marked "report done", so
REM the next dispatcher cycle skips that ticker forever. sweep_podcasts only
REM uploads audio that is already on disk - it never generates any. Without this
REM task, every ticker processed during an outage permanently loses its podcast
REM AND its LINE push, with nothing anywhere turning red.
REM
REM Runs on its own schedule rather than inside sweep.bat: one episode can take
REM 15-40 minutes, and sharing sweep's slot would stall the uploads and LINE
REM pushes that sweep is responsible for (MultipleInstances=IgnoreNew).
REM --limit 1 keeps a single run bounded; a backlog drains one per cycle.
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
echo ==== %DATE% %TIME% backfill ==== >> "%ROOT%\logs\backfill.log"
"%PY%" "%ROOT%\tools\backfill_podcasts.py" --run --limit 1 >> "%ROOT%\logs\backfill.log" 2>&1
echo ---- exit %ERRORLEVEL% ---- >> "%ROOT%\logs\backfill.log"
exit /b %ERRORLEVEL%
