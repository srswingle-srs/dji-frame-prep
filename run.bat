@echo off
setlocal

REM --- cd to the folder where this .bat lives ---
cd /d "%~dp0"

REM ============================================
REM   DJI Frame Prep
REM ============================================
echo.
echo  ============================================
echo    DJI Frame Prep
echo  ============================================
echo.

REM --- Check Python ---
where python >nul 2>nul
if errorlevel 1 (
    echo  Python is not installed.
    echo.
    where winget >nul 2>nul
    if errorlevel 1 (
        echo  Please install Python from https://www.python.org/downloads/
        echo  IMPORTANT: Check "Add Python to PATH" during install.
        echo.
        pause
        exit /b 1
    )
    echo  Installing Python automatically...
    winget install Python.Python.3.13 --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo  Python installation failed.
        echo  Please install manually from https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
    echo.
    echo  Python installed! Close this window and double-click run.bat again.
    echo.
    pause
    exit /b 0
)

REM --- Check ffmpeg ---
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo  ffmpeg is not installed ^(needed to extract video frames^).
    echo.
    where winget >nul 2>nul
    if errorlevel 1 (
        echo  Please install ffmpeg from https://ffmpeg.org/download.html
        echo.
        pause
        exit /b 1
    )
    echo  Installing ffmpeg automatically...
    winget install Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo  ffmpeg installation failed.
        echo  Please install manually from https://ffmpeg.org/download.html
        echo.
        pause
        exit /b 1
    )
    echo.
    echo  ffmpeg installed! Close this window and double-click run.bat again.
    echo.
    pause
    exit /b 0
)

REM --- Check ffprobe (comes with ffmpeg) ---
where ffprobe >nul 2>nul
if errorlevel 1 (
    echo  ffprobe not found ^(should come with ffmpeg^).
    echo  Try closing this window and running again, or reinstall ffmpeg.
    echo.
    pause
    exit /b 1
)

REM --- Check PyQt6 ---
python -c "import PyQt6" >nul 2>nul
if errorlevel 1 (
    echo  Installing PyQt6 ^(one-time setup^)...
    python -m pip install PyQt6
    if errorlevel 1 (
        echo  Failed to install PyQt6.
        echo  Try manually:  pip install PyQt6
        echo.
        pause
        exit /b 1
    )
    echo  PyQt6 installed.
    echo.
)

echo  Starting GUI...
echo.

REM --- Launch GUI (hidden console, errors go to error_log.txt) ---
start "" pythonw "%~dp0gui.py"

REM --- Console closes immediately; errors are in error_log.txt ---
if errorlevel 1 (
    echo.
    echo  The tool failed to start. Check error_log.txt for details.
    echo.
    pause
) else (
    timeout /t 2 /nobreak >nul
)
