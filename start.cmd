@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 convert.py --gui %*
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Please install Python 3.10 or newer with Tkinter, then try again.
        pause
        exit /b 1
    )
    python convert.py --gui %*
)
if errorlevel 1 pause
