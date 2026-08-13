@echo off
rem Ship the newest archive-web wheel to the recorder, install it, and restart
rem the web and dayplot-worker services so web-interface changes go live.
rem
rem Build the wheel first (from the archive-web folder):
rem   .venv\Scripts\python -m pip wheel . -w dist --no-deps
rem
rem Double-click this file. You will be asked for the Pi account password for the
rem upload, and for your sudo password on the Pi during the install.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-archive-web.ps1" %*
pause
