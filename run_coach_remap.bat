@echo off
cd /d "%~dp0"
echo Starting AC Live Coach with --remap: your saved reference for the
echo car/track you load will be cleared and rebuilt finer on lap 1.
python ac_live_coach.py --remap
pause
