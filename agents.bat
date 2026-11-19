@echo off
rem Double-click: the 4 paper-trading agents run 9:30-3:15 every market day while this window is open.
cd /d "%~dp0"
title Trading agents (paper)
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" main.py agents
pause
