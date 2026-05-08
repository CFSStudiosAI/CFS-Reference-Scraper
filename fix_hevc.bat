@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [error] No virtual environment yet. Run start.bat once first.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

echo.
echo ============================================================
echo  Scanning library for HEVC videos and transcoding to H.264
echo  This may take a few seconds per file.
echo ============================================================
echo.
python downloader.py --fix-hevc

echo.
echo ============================================================
echo  Done. Press any key to close.
echo ============================================================
pause >nul
