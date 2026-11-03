@echo off
rem AC Live Coach launcher -- double-click it, or make a shortcut to it.
rem It runs the coach from its own folder, so the profiles, guides and
rem voices folders resolve correctly wherever the shortcut lives.
setlocal
cd /d "%~dp0"
title AC Live Coach

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3 from python.org
    echo and tick "Add python.exe to PATH", then run this again.
    pause
    exit /b 1
)

rem One-time only: install the voice dependencies if they're missing
rem (the neural voice needs piper-tts; without it the coach falls back
rem to the robotic Windows voice). Skipped once they're installed.
python -c "import piper" >nul 2>nul
if errorlevel 1 (
    echo Installing voice dependencies, one time only...
    python -m pip install -r requirements.txt
)

python ac_live_coach.py %*
echo.
echo Coach exited. Press any key to close this window.
pause >nul
endlocal
