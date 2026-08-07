@echo off
cd /d "%~dp0"
git add macro_data.json data.txt
git commit -m "Update macro data %date%"
git push
pause
