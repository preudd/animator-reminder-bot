@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo === Animator Reminder Bot: STOP ===

REM 1) Try stop by PID file
if exist "bot.pid" (
  set /p BOTPID=<bot.pid
  if not "%BOTPID%"=="" (
    echo Killing PID %BOTPID% ...
    taskkill /PID %BOTPID% /F >nul 2>nul
  )
  del /Q "bot.pid" >nul 2>nul
)

REM 2) Fallback: kill python processes running bot.py from this folder
for /f "tokens=2 delims==" %%P in ('wmic process where "CommandLine like '%%bot.py%%' and CommandLine like '%%%cd:\=\\%%%' and (Name='python.exe' or Name='pythonw.exe')" get ProcessId /VALUE ^| find "="') do (
  echo Stopping PID %%P
  taskkill /PID %%P /F >nul 2>nul
)

REM 3) Remove lock file in TEMP (if token is known)
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
  if /I "%%A"=="TELEGRAM_TOKEN" (
    set "TOKEN=%%B"
  )
)
set "TOKEN=%TOKEN:"=%"
for /f "tokens=1 delims=:" %%I in ("%TOKEN%") do set "BOTID=%%I"
set "BOTID=%BOTID: =%"
for /f "delims=0123456789" %%Z in ("%BOTID%") do set "BOTID="
if not "%BOTID%"=="" (
  set "LOCK=%TEMP%\animator_reminder_bot_%BOTID%.lock"
  if exist "%LOCK%" (
    del /Q "%LOCK%" >nul 2>nul
    echo Removed lock %LOCK%
  )
)

echo Done.
pause
endlocal
