@echo off
cd /d "%~dp0"
del /q "runtime\v42_spb_moscow_live.json" 2>nul
del /q "runtime\v42_moscow_spb_live.json" 2>nul
del /q "runtime\v42_collect.log" 2>nul
del /q "runtime\routes\*.json" 2>nul
echo Live cache for all routes has been reset. Base validated route packs were not changed.
pause
