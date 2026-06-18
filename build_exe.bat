@echo off
setlocal

REM --- cd to the folder where this .bat lives ---
cd /d "%~dp0"

REM ============================================
REM   Build standalone .exe with PyInstaller
REM   Bundles Python + PyQt6 into one folder
REM   ffmpeg must still be installed separately
REM ============================================

echo.
echo  Building DJI Frame Prep standalone .exe...
echo.

REM --- Check PyInstaller ---
python -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo  Installing PyInstaller...
    python -m pip install pyinstaller
    if errorlevel 1 (
        echo  Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

REM --- Build ---
python -m PyInstaller ^
    --name "DJI_Frame_Prep" ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --add-data "core.py;." ^
    --hidden-import PyQt6.sip ^
    --icon NONE ^
    gui.py

if errorlevel 1 (
    echo.
    echo  Build failed. See errors above.
    pause
    exit /b 1
)

echo.
echo  Build complete!
echo  Output: dist\DJI_Frame_Prep\
echo.
echo  To distribute:
echo    1. Copy the dist\DJI_Frame_Prep folder to a USB or zip it
echo    2. Users still need ffmpeg installed ^(run.bat handles this^)
echo    3. Double-click DJI_Frame_Prep.exe to launch
echo.
pause
