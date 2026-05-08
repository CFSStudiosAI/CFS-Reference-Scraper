@echo off
setlocal
cd /d "%~dp0"

REM ---- 1. Create virtual environment on first run ------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    py -m venv .venv
    if errorlevel 1 (
        echo.
        echo [error] Could not create venv. Is Python installed?
        echo         Try: https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
)

REM ---- 2. Activate it ----------------------------------------------------
call ".venv\Scripts\activate.bat"

REM ---- 3. Install / update dependencies ----------------------------------
echo [setup] Checking dependencies...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo [error] pip install failed. Scroll up for details.
    echo.
    pause
    exit /b 1
)

REM Always update yt-dlp — TikTok rotates their internal API every few weeks
REM and the fix is almost always "use the latest yt-dlp".
echo [setup] Updating yt-dlp...
pip install -q -U yt-dlp

REM Seed a fresh creator list from the example template if one doesn't exist
REM (ships in the repo so first-clone "just works")
if not exist "input\tiktok_users.csv" (
    if exist "input\tiktok_users.csv.example" (
        copy /Y "input\tiktok_users.csv.example" "input\tiktok_users.csv" >nul
        echo [setup] Created input\tiktok_users.csv from the example template.
    )
)

REM ---- 4. Launch the app -------------------------------------------------
REM web.py opens the browser AND kicks off the scraper in the background.
echo.
echo Launching CFSStudios.AI TT Scraper...
echo Browser will open at http://127.0.0.1:5000/
echo Background scrape begins automatically (log: logs\scraper_YYYY-MM-DD.log).
echo Press Ctrl+C in this window to stop everything.
echo.
python web.py
