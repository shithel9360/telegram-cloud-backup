@echo off
title Telegram Backup Pro - Setup
echo ============================================
echo   Telegram Backup Pro - First Time Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python is not installed!
    echo Please install Python from https://python.org/downloads
    echo Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo [OK] Python found.
echo.
echo Installing required packages...
echo.

python -m pip install --upgrade pip --quiet
python -m pip install telethon fastapi uvicorn pillow piexif opencv-python requests --quiet

echo.
echo ============================================
echo   Setup Complete! Run start.bat to launch.
echo ============================================
pause
