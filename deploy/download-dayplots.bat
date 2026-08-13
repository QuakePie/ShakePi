@echo off
rem Download dayplots using configurable SSH settings.
rem Double-click to enter your Pi username and password, or pass:
rem   download-dayplots.bat -RemoteUser your-user -RemoteHost raspberrypi.local
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0download-dayplots.ps1" %*
pause
