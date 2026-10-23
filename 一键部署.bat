@echo off
chcp 65001 >nul
setlocal
set "PYEXE="

where py >nul 2>nul
if %errorlevel%==0 set "PYEXE=py -3"

if not defined PYEXE (
  where python >nul 2>nul
  if %errorlevel%==0 set "PYEXE=python"
)

if not defined PYEXE (
  echo [ERROR] Python not found.
  echo Please install Python 3.8 or newer and tick "Add to PATH".
  pause
  exit /b 1
)

%PYEXE% "%~dp0scripts\setup.py"

echo.
pause
