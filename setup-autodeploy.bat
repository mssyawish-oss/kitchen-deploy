@echo off
REM ============================================================
REM  ONE-TIME on the shop server PC: arm auto-deploy.
REM  Right-click this file > "Run as administrator".
REM  After this, anything you publish from the Mac lands here
REM  automatically within ~3 minutes. You only run this ONCE.
REM ============================================================

REM >>> The line below is filled in for you once the repo exists <<<
set REPO=https://github.com/mssyawish-oss/kitchen-deploy.git
set SYNC=C:\KitchenDashboard-sync

echo.
echo Installing git if it isn't here yet...
where git >nul 2>nul || winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements

echo.
echo Getting the deploy folder...
if exist "%SYNC%" ( cd /d "%SYNC%" & git pull ) else ( git clone %REPO% "%SYNC%" )

echo.
echo Registering the auto-deploy task (checks every 3 minutes, hardened)...
REM Run as SYSTEM so it fires even when nobody is logged in (the #1 cause of it silently stalling).
schtasks /Create /F /TN "KitchenDashboardAutoDeploy" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"%SYNC%\autodeploy.ps1\"" /SC MINUTE /MO 3 /RL HIGHEST /RU SYSTEM

echo Hardening the task (run if a check was missed, don't skip on battery/sleep, retry on failure)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=Get-ScheduledTask -TaskName 'KitchenDashboardAutoDeploy'; $s=$t.Settings; $s.StartWhenAvailable=$true; $s.DisallowStartIfOnBatteries=$false; $s.StopIfGoingOnBatteries=$false; $s.ExecutionTimeLimit='PT10M'; $s.RestartCount=3; $s.RestartInterval='PT1M'; Set-ScheduledTask -TaskName 'KitchenDashboardAutoDeploy' -Settings $s | Out-Null; Write-Host 'Task hardened.'"

echo Running one deploy check right now...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SYNC%\autodeploy.ps1"

echo.
echo ============================================================
echo  Done. Auto-deploy is now ON.
echo  Publish from the Mac and it appears here within ~3 minutes.
echo  Log:  C:\KitchenDashboard\autodeploy.log
echo ============================================================
pause
