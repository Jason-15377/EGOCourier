@echo off
REM ===================================================================
REM  EGO Courier - Desktop launcher (ASCII only in this file)
REM
REM  First run on a new PC: creates .venv and installs dependencies
REM  automatically, then starts the GUI. Nothing is installed globally.
REM
REM  Finds Python 3.10-3.12 first: PySide6 / matplotlib have no
REM  wheels for 3.13+, which is the usual cause of a wall of red errors.
REM ===================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title EGO Courier
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY="
for %%V in (3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  py -3 -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) and sys.version_info[:2]<=(3,12) else 1)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  python -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,10) and sys.version_info[:2]<=(3,12) else 1)" >nul 2>nul && set "PY=python"
)

if not defined PY goto :nopython

%PY% bootstrap.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%

:nopython
py -3 -c "import sys" >nul 2>nul && goto :toonew
python -c "import sys" >nul 2>nul && goto :toonew

echo.
echo  --------------------------------------------------------------
echo   [X] Python was not found on this computer.
echo.
echo   EGO Courier desktop needs Python 3.10 - 3.12 to run.
echo.
echo   1. Open https://www.python.org/downloads/release/python-31210/
echo      and download "Windows installer (64-bit)".
echo   2. IMPORTANT: tick "Add python.exe to PATH" during setup.
echo   3. Double-click EGO Courier.bat again.
echo.
echo   If you received a portable zip, use EGO Courier.exe inside it
echo   instead - that version does not need Python.
echo  --------------------------------------------------------------
echo.
pause
exit /b 1

:toonew
echo.
echo  --------------------------------------------------------------
echo   [!] Python is installed, but the version is too new.
echo.
echo   PySide6 / matplotlib do not ship prebuilt wheels for Python
echo   3.13 and later, so installing dependencies would flood this
echo   window with red errors. EGO Courier needs Python 3.10-3.12.
echo.
echo   Install Python 3.12 (tick "Add python.exe to PATH"):
echo   https://www.python.org/downloads/release/python-31210/
echo  --------------------------------------------------------------
echo.
pause
exit /b 1
