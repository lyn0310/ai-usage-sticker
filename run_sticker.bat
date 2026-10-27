@echo off
rem Claude usage sticker launcher (no console window)
rem Put this file in the SAME folder as claude_usage_sticker_login.py
cd /d "%~dp0"
start "" pythonw claude_usage_sticker_login.py %*
