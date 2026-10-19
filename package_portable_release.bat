@echo off
REM ===================================================================
REM  EGO Courier - build + package portable release zip  (ASCII only)
REM
REM  Produces a self-contained bundle that needs no Python on the
REM  target machine, then zips it as:
REM     dist\EGO_Courier_portable_v<VER>_win64.zip
REM
REM  Usage:  package_portable_release.bat
REM  Env:    EGO_VERSION   override version (default: __version__)
REM ===================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title EGO Courier - portable release

set "PY="
for %%V in (3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python 3.10-3.12 not found.
  pause
  exit /b 1
)

echo [1/4] Checking build dependencies...
%PY% -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  echo [INFO] Installing PyInstaller...
  %PY% -m pip install pyinstaller
  if errorlevel 1 goto :fail
)

for /f "delims=" %%i in ('%PY% -c "import ego_relay;print(ego_relay.__version__)"') do set "VER=%%i"
if defined EGO_VERSION set "VER=%EGO_VERSION%"
echo       Version: !VER!

echo [2/4] Building portable bundle...
%PY% -m PyInstaller --noconfirm --clean ego_courier_portable.spec
if errorlevel 1 goto :fail

set "APP=dist\EGO Courier"
if not exist "%APP%\EGO Courier.exe" (
  echo [ERROR] Build output missing: %APP%\EGO Courier.exe
  goto :fail
)

echo [3/4] Self-test (launch exe headless, expect clean exit)...
set "QT_QPA_PLATFORM=offscreen"
set "EGO_SELFTEST_EXIT_MS=4000"
start "" /wait "%APP%\EGO Courier.exe"
set "RC=!ERRORLEVEL!"
set "QT_QPA_PLATFORM="
set "EGO_SELFTEST_EXIT_MS="
if not "!RC!"=="0" (
  echo [ERROR] Self-test failed with exit code !RC!. See log\gui_launch.log
  goto :fail
)
echo       OK

echo [4/4] Packing zip...
set "ZIP=dist\EGO_Courier_portable_v!VER!_win64.zip"
if exist "!ZIP!" del /q "!ZIP!"
if exist "%TEMP%\_egocourier_zip" rd /s /q "%TEMP%\_egocourier_zip"
mkdir "%TEMP%\_egocourier_zip"
robocopy "%APP%" "%TEMP%\_egocourier_zip\EGO Courier" /E /NFL /NDL /NJH /NJS /NP >nul
%PY% -c "import shutil,sys; shutil.make_archive(sys.argv[1][:-4], 'zip', sys.argv[2])" "!ZIP!" "%TEMP%\_egocourier_zip"
if errorlevel 1 (
  robocopy /? >nul
  echo [ERROR] zip failed.
  goto :fail
)
rd /s /q "%TEMP%\_egocourier_zip"

echo.
echo [OK] !ZIP!
for %%F in ("!ZIP!") do echo      size: %%~zF bytes
echo.
echo Next: create GitHub release and attach the zip, e.g.
echo   gh release create v!VER! "!ZIP!" --title "EGO Courier v!VER!" --notes "..."
echo.
pause
exit /b 0

:fail
echo.
echo [ERROR] Packaging failed. See output above.
pause
exit /b 1
