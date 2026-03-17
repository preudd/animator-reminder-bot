@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo === Animator Reminder Bot: DEBUG RUN (foreground) ===
echo This will show logs in this window.
echo Press Ctrl+C to stop.
echo.

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv not found. Run open.bat once to set up dependencies.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" bot.py

echo.
echo Bot stopped.
pause
endlocal

