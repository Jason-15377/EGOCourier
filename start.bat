@echo off
REM EGO Courier - start web UI (ASCII only to avoid codepage issues)
cd /d "%~dp0"

REM Locate Python: prefer official launcher 'py', fallback to 'python'
set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)

if not defined PY (
  echo.
  echo   [ERROR] Python not found.
  echo   Install Python 3 and check "Add to PATH", or run:
  echo     python "%~dp0run_server.py"
  echo.
  pause
  exit /b 1
)

echo.
%PY% -c "import ego_relay;print(ego_relay.__appname__+' v'+ego_relay.__version__)"
echo   Starting service, browser will open http://127.0.0.1:8360
echo   Keep this window open; closing it stops the service.
echo.
%PY% -m ego_relay.api --port 8360

echo.
echo   [Service exited. If an error appeared above, screenshot it.]
pause
