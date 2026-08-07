@echo off
cd /d "%~dp0"
python update_macro_data.py

rem Opening the html directly via file:// blocks fetch() with CORS, so serve it locally instead.
rem server.py also exposes /api/analyze/<claude|gemini> for the AI macro analysis buttons.
netstat -ano | findstr :8934 >nul
if errorlevel 1 (
  start "MacroTracker Local Server" /min cmd /c "python server.py"
  timeout /t 1 >nul
)
start "" "http://localhost:8934/macro-tracker-offline.html"
pause
