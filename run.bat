@echo off
rem Double-click to open the stock screener menu. Works from any folder.
cd /d "%~dp0"
title NSE Stock Screener
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
    echo First-time setup: creating Python environment, please wait...
    py -3.13 -m venv .venv || py -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
)
".venv\Scripts\python.exe" main.py
