@echo off
REM setup.bat — install Python dependencies, then run init.py to configure paths.
REM Run ONCE after copying this folder to a new machine.

cd /d "%~dp0"
title Plaud pipeline setup
echo === Plaud pipeline setup ===
echo.

REM Check Python
where python >nul 2>&1
if errorlevel 1 (
  echo [!] Python not found in PATH.
  echo     Install Python 3.11+ from https://www.python.org/downloads/
  echo     During install — TICK "Add Python to PATH".
  pause
  exit /b 1
)
python --version

echo.
echo === Installing pip packages ===
echo (anthropic, reportlab, markdown, python-dotenv, faster-whisper, imageio-ffmpeg)
python -m pip install --upgrade pip
python -m pip install anthropic reportlab markdown python-dotenv faster-whisper imageio-ffmpeg

if errorlevel 1 (
  echo [!] pip install failed. See errors above.
  pause
  exit /b 1
)

echo.
echo === Now configuring paths (interactive) ===
echo.
python init.py
if errorlevel 1 (
  echo [!] init failed.
  pause
  exit /b 1
)

echo.
echo === Setup done ===
echo.
echo Next:
echo   1. (опционально) Установи Claude Code CLI для slash-команд:
echo        winget install OpenJS.NodeJS.LTS --silent --scope user
echo        npm install -g @anthropic-ai/claude-code
echo   2. Запусти обработку — bulk_export\local-process.bat (двойной клик)
echo      или в Claude Code:  /local
echo.
pause
