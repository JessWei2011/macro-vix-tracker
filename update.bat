@echo off
cd /d "%~dp0"
python update_macro_data.py
start "" "macro-tracker-offline.html"
pause
