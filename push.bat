@echo off
cd /d "%~dp0"
git add macro_data.json data.txt macro_analysis.json
git commit -m "Update macro data %date%"
git push
pause
