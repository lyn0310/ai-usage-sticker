@echo off
rem Build AIUsageSticker.exe (one-file, no console, custom icon + themes)
rem Put this file, the .py, ai_usage_sticker.ico, and the themes folder in the
rem SAME folder, then double-click.
cd /d "%~dp0"

set ICONOPTS=
if exist ai_usage_sticker.ico set ICONOPTS=--icon ai_usage_sticker.ico --add-data "ai_usage_sticker.ico;."

set THEMEOPTS=
if exist themes set THEMEOPTS=--add-data "themes;themes"

echo [1/2] Installing PyInstaller and Pillow...
python -m pip install pyinstaller pillow

echo [2/2] Building exe (this can take a few minutes)...
python -m PyInstaller --noconfirm --onefile --windowed --name AIUsageSticker ^
  --collect-all playwright --collect-all curl_cffi %ICONOPTS% %THEMEOPTS% ^
  ai_usage_sticker.py

echo.
echo Done! Your exe is here:  dist\AIUsageSticker.exe
pause
