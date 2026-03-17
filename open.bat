@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo === Animator Reminder Bot: START ===

REM 1) Create venv if missing
if not exist ".venv\Scripts\python.exe" (
  echo Creating venv...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create venv. Close running python processes and try again.
    pause
    exit /b 1
  )
)

REM 2) Install requirements (safe to run multiple times)
echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install requirements.
  pause
  exit /b 1
)

REM 3) Ensure .env exists
if not exist ".env" (
  if exist ".env.example" (
    echo Creating .env from .env.example...
    copy /Y ".env.example" ".env" >nul
  )
)

REM 4) Start bot in background and save PID into bot.pid
echo Starting bot in background...
powershell -NoProfile -Command ^
  "$p = Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList 'bot.py' -WorkingDirectory (Get-Location) -WindowStyle Minimized -PassThru; " ^
  "$p.Id | Set-Content -Encoding ASCII -Path 'bot.pid'; " ^
  "Write-Host ('Started with PID ' + $p.Id)"

echo Done.
echo Tip: use close.bat to stop.
pause
endlocal
