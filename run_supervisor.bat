@echo off
setlocal

if "%WALKIE_TALKIE_ADMIN_TOKEN%"=="" (
  echo WALKIE_TALKIE_ADMIN_TOKEN is not set.
  echo Set it in this terminal before running run_supervisor.bat
  exit /b 1
)

set RUN_ONCE=0
echo %* | findstr /c:"--once" >nul && set RUN_ONCE=1

:loop
python "%~dp0supervisor.py" %*
if "%RUN_ONCE%"=="1" exit /b %errorlevel%
echo [%date% %time%] supervisor exited. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
