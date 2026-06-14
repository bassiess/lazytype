@echo off
REM Dubbelklik dit bestand om Lazytype (systeemvak) te starten.
title Lazytype
cd /d "%~dp0"
python dictate_tray.py
echo.
echo Lazytype is gestopt. Druk op een toets om dit venster te sluiten.
pause >nul
