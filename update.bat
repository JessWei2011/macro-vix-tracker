@echo off
cd /d "%~dp0"
python update_macro_data.py

rem Opening the html directly via file:// blocks fetch() with CORS, so serve it locally instead.
netstat -ano | findstr :8934 >nul
if errorlevel 1 (
  start "MacroTracker Local Server" /min cmd /c "python -m http.server 8934"
  timeout /t 1 >nul
)
start "" "http://localhost:8934/macro-tracker-offline.html"
pause
