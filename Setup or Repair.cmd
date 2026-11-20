@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failure
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failure
echo.
echo Setup complete. Open Start MinePlan Earth.cmd to launch.
pause
exit /b 0
:failure
echo Setup failed. Read the error above.
pause
exit /b 1
