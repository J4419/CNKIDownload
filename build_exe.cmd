@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ============================================================
rem  Build CNKIDownload.exe  (PyInstaller, single file)
rem
rem  Usage:  build_exe.cmd [path-to-python]
rem  Output: dist\CNKIDownload.exe
rem
rem  The exe is for users who do not want to install Python.
rem  Upload it to a GitHub Release - do NOT commit it to the repo.
rem  The tool needs no third-party packages, so the exe is small.
rem ============================================================

set "PY=%~1"
if "%PY%"=="" set "PY=python"

echo ============================================================
echo   CNKIDownload  -  build single-file exe
echo   python : %PY%
echo   output : %~dp0dist\CNKIDownload.exe
echo ============================================================
echo.

"%PY%" --version >nul 2>&1
if errorlevel 1 goto :nopy

"%PY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo [1/2] Installing PyInstaller ...
  "%PY%" -m pip install --upgrade pyinstaller
  if errorlevel 1 goto :nopip
) else (
  echo [1/2] PyInstaller already present.
)

echo.
echo [2/2] Building ...
"%PY%" -m PyInstaller --noconfirm --clean --onefile --console ^
  --name CNKIDownload ^
  --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0build" ^
  --add-data "1.check_env.py;." ^
  --add-data "2.make_list.py;." ^
  --add-data "3.CNKIDownload.py;." ^
  --add-data "4.ClearProfile.py;." ^
  --hidden-import zipfile ^
  --hidden-import xml.etree.ElementTree ^
  --hidden-import csv ^
  --hidden-import importlib.util ^
  --hidden-import argparse ^
  --hidden-import base64 ^
  --hidden-import platform ^
  --hidden-import secrets ^
  --hidden-import shutil ^
  --hidden-import struct ^
  --hidden-import subprocess ^
  --hidden-import tempfile ^
  --hidden-import urllib.error ^
  --hidden-import urllib.parse ^
  --hidden-import urllib.request ^
  "0.cnki.py"
if errorlevel 1 goto :fail

echo.
echo [OK] Built: %~dp0dist\CNKIDownload.exe
echo.
echo   Verify with:  dist\CNKIDownload.exe --check-env
echo   Then attach it to a GitHub Release. Do not commit the exe.
goto :end

:nopy
echo [FAIL] "%PY%" is not runnable. Pass the full path, e.g.
echo        build_exe.cmd "C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe"
echo.
goto :end

:nopip
echo [FAIL] Could not install PyInstaller. Check your network / proxy and retry.
echo.
goto :end

:fail
echo [FAIL] Build failed. Scroll up for the PyInstaller error.
echo.

:end
echo Press any key to close this window...
pause >nul
