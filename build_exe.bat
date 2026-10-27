@echo off
rem Build ClaudeSticker.exe (one-file, no console, custom icon + themes)
rem Put this file, the .py, claude_sticker.ico, and the themes folder in the
rem SAME folder, then double-click.
cd /d "%~dp0"

set ICONOPTS=
if exist claude_sticker.ico set ICONOPTS=--icon claude_sticker.ico --add-data "claude_sticker.ico;."

set THEMEOPTS=
if exist themes set THEMEOPTS=--add-data "themes;themes"

echo [1/2] Installing PyInstaller and Pillow...
python -m pip install pyinstaller pillow

echo [2/2] Building exe (this can take a few minutes)...
python -m PyInstaller --noconfirm --onefile --windowed --name ClaudeSticker ^
  --collect-all playwright --collect-all curl_cffi %ICONOPTS% %THEMEOPTS% ^
  claude_usage_sticker_login.py

echo.
echo Done! Your exe is here:  dist\ClaudeSticker.exe
pause
