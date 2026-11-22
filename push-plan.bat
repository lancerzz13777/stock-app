@echo off
rem Ranks every stock on this PC and sends the result to the laptop server (the paper fund and Quant Bot use it).
rem Runs by itself every evening the PC is on (skips if the server's ranking is under 6 days old). Double-click to run now.
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" main.py push-plan %*
