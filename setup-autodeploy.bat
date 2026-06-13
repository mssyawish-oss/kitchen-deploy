@echo off
REM ============================================================
REM  ONE-TIME on the shop server PC: arm auto-deploy.
REM  Right-click this file > "Run as administrator".
REM  After this, anything you publish from the Mac lands here
REM  automatically within ~3 minutes. You only run this ONCE.
REM ============================================================

REM >>> The line below is filled in for you once the repo exists <<<
set REPO=__REPO_URL__
set SYNC=C:\KitchenDashboard-sync

echo.
echo Installing git if it isn't here yet...
where git >nul 2>nul || winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements

echo.
echo Getting the deploy folder...
if exist "%SYNC%" ( cd /d "%SYNC%" & git pull ) else ( git clone %REPO% "%SYNC%" )

echo.
echo Registering the auto-deploy task (checks every 3 minutes)...
schtasks /Create /F /TN "KitchenDashboardAutoDeploy" /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"%SYNC%\autodeploy.ps1\"" /SC MINUTE /MO 3 /RL HIGHEST

echo.
echo ============================================================
echo  Done. Auto-deploy is now ON.
echo  Publish from the Mac and it appears here within ~3 minutes.
echo  Log:  C:\KitchenDashboard\autodeploy.log
echo ============================================================
pause
