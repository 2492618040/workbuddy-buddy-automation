@echo off
chcp 65001 >nul
setlocal
rem 双击运行：读取本机 WorkBuddy 最新令牌，加密写入云端 GitHub Secret。
rem 请在普通窗口双击运行（不要在受管控的自动化环境里跑）。
set "PYEXE="

where py >nul 2>nul
if %errorlevel%==0 set "PYEXE=py -3"

if not defined PYEXE (
  where python >nul 2>nul
  if %errorlevel%==0 set "PYEXE=python"
)

if not defined PYEXE (
  echo [ERROR] Python not found.
  pause
  exit /b 1
)

%PYEXE% "%~dp0scripts\update_secret.py"

echo.
pause
