@echo off
setlocal
cd /d "%~dp0"
python easy_agentloop.py %*
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Command exited with code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
