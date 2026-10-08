@echo off
REM EGO Courier - Desktop GUI launcher (ASCII only)
cd /d "%~dp0"

set "PY="
where py >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [ERROR] Python not found. Install Python 3 with "Add to PATH".
  pause
  exit /b 1
)

echo.
%PY% -c "import ego_relay;print(ego_relay.__appname__+' v'+ego_relay.__version__)"
echo Starting desktop GUI...
%PY% run_desktop.py
pause
