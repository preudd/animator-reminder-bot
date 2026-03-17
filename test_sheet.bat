@echo off
setlocal EnableExtensions

cd /d "%~dp0"

chcp 65001 >nul

echo === Animator Reminder Bot: TEST SHEET (send now) ===

REM 1) Create venv if missing
if not exist ".venv\Scripts\python.exe" (
  echo Creating venv...
  python -m venv .venv
  if errorlevel 1 goto :FAIL_VENV
)

REM 2) Install requirements (safe to run multiple times)
echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :FAIL_REQ

REM 3) Ensure .env exists
if not exist ".env" goto :FAIL_ENV

REM 4) Ensure credentials.json exists (unless custom path is used)
if not exist "credentials.json" if exist "credentials.json.json" (
  echo Found credentials.json.json - copying to credentials.json ...
  copy /Y "credentials.json.json" "credentials.json" >nul
)

REM 5) Run one-off test
echo Sending test message to Telegram (check your chat)...
".venv\Scripts\python.exe" bot.py --test-sheet
echo Exit code: %errorlevel%

echo Done.
pause
endlocal
exit /b 0

:FAIL_VENV
echo Failed to create venv. Close running python processes and try again.
pause
exit /b 1

:FAIL_REQ
echo Failed to install requirements.
pause
exit /b 1

:FAIL_ENV
echo ERROR: .env not found. Create it (you can copy from .env.example) and fill TELEGRAM_TOKEN / CHAT_ID / GOOGLE_SHEET_NAME.
pause
exit /b 1

