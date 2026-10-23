@echo off
chcp 65001 >nul
setlocal
rem 不部署到云端，仅在本机跑一次：签到 + 派猫 + 微信推送，用来验证配置是否正确
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

if not exist "%~dp0config.json" (
  echo [ERROR] config.json not found.
  echo Please copy config.example.json to config.json and fill in your values.
  pause
  exit /b 1
)

%PYEXE% "%~dp0scripts\buddy_daily.py" --config "%~dp0config.json"

echo.
pause
