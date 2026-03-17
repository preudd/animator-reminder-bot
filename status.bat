@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo === Animator Reminder Bot: STATUS ===

set "PIDFILE=bot.pid"

if exist "%PIDFILE%" (
  set /p BOTPID=<"%PIDFILE%"
) else (
  set "BOTPID="
)

if not "%BOTPID%"=="" (
  REM Check if PID exists
  tasklist /FI "PID eq %BOTPID%" | findstr /R /C:"^python\.exe" /C:"^pythonw\.exe" >nul
  if %errorlevel%==0 (
    echo Status: RUNNING
    echo PID: %BOTPID%
    echo (Started from this folder via open.bat)
    goto :EOF
  ) else (
    echo Status: NOT RUNNING (stale bot.pid)
    echo bot.pid had PID %BOTPID%, but process is not found.
    echo You can delete bot.pid safely.
    goto :FALLBACK
  )
)

echo Status: unknown by bot.pid (bot.pid not found)

:FALLBACK
echo Checking for running bot.py from this folder...
wmic process where "CommandLine like '%%bot.py%%' and CommandLine like '%%%cd:\=\\%%%' and (Name='python.exe' or Name='pythonw.exe')" get ProcessId,CommandLine /FORMAT:LIST

echo.
echo Tips:
echo - Start: open.bat
echo - Stop:  close.bat
pause
endlocal
