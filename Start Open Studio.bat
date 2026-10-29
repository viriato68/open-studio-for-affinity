@echo off
title Open Studio for Affinity
cd /d "%~dp0"

rem Use the Windows Python launcher if available, otherwise plain python
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 server.py
) else (
    python server.py
)

if errorlevel 1 (
    echo.
    echo Something went wrong. Is Python installed? https://www.python.org/downloads/
    pause
)
