@echo off
REM Double-click this on ORDERMATE to install ffmpeg (restores rotisserie/bench camera reads).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_ffmpeg.ps1"
