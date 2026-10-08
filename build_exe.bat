@echo off
REM EGO Courier - build Windows .exe (ASCII only)
REM   build_exe.bat            -> dist\EGO Courier\EGO Courier.exe   (onedir, recommended)
REM   build_exe.bat onefile    -> dist\EGO Courier.exe               (single file)
setlocal
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

if /i "%~1"=="onefile" (
  set "EGO_ONEFILE=1"
  echo [INFO] One-file mode: dist\EGO Courier.exe
) else (
  set "EGO_ONEFILE=0"
  echo [INFO] One-dir mode: dist\EGO Courier\EGO Courier.exe
)

echo.
echo [1/3] Checking build dependencies...
%PY% -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  echo [INFO] Installing PyInstaller...
  %PY% -m pip install pyinstaller
  if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller.
    pause
    exit /b 1
  )
)

echo [2/3] Generating logo assets...
%PY% assets\make_logo.py
if errorlevel 1 (
  echo [ERROR] Logo generation failed.
  pause
  exit /b 1
)

echo [3/3] Building with PyInstaller...
%PY% -m PyInstaller --noconfirm --clean ego_courier.spec
if errorlevel 1 (
  echo [ERROR] Build failed. See output above.
  pause
  exit /b 1
)

echo.
echo [OK] Build finished.
if /i "%~1"=="onefile" (
  echo      dist\EGO Courier.exe
) else (
  echo      dist\EGO Courier\EGO Courier.exe
)
pause
endlocal
