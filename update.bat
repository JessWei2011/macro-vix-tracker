@echo off
cd /d "%~dp0"
python update_macro_data.py
if errorlevel 1 (
  echo.
  echo update_macro_data.py failed - see the error above.
  pause
  exit /b 1
)

rem Opening the html directly via file:// blocks fetch() with CORS, so serve it locally instead.
rem server.py also exposes /api/analyze/gemini for the AI macro analysis button.
rem Always kill whatever is already on 8934 first, so an old/stale server.py process
rem never lingers and silently serves outdated routes after this script gets updated.
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8934 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
start "MacroTracker Local Server" /min cmd /c "python server.py"
timeout /t 1 >nul
start "" "http://localhost:8934/macro-tracker-offline.html"

rem Success: stay open briefly so you can glance at the result, then close automatically.
timeout /t 3 >nul
exit
