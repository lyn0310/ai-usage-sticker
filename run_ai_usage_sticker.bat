@echo off
rem AI usage sticker launcher (Claude / ChatGPT) (no console window)
rem Put this file in the SAME folder as ai_usage_sticker.py
cd /d "%~dp0"
start "" pythonw ai_usage_sticker.py %*
