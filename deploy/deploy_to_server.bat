@echo off
rem Double-click: puts the bot on your free Oracle Cloud server (first time) - see README "free cloud server".
rem To send only code changes later (keeps the server's wallet and data):  deploy_to_server.bat -CodeOnly
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_to_server.ps1" %*
pause
