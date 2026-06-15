@echo off
REM Bruno's Kitchen Dashboard - watchdog launcher.
REM Keeps the dashboard alive: relaunches it on any exit (a tapped Restart, or a crash).
REM KDASH_WATCHDOG=1 tells the app it can just exit cleanly and we'll bring it back.
cd /d "%~dp0"
set KDASH_WATCHDOG=1
:loop
python dashboard_app.py
echo Dashboard stopped - relaunching in 2 seconds...
timeout /t 2 /nobreak >nul
goto loop
