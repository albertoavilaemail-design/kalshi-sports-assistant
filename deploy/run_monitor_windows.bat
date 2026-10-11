@echo off
REM Run the live monitor on a Windows PC (keep the PC awake during games).
REM Double-click, or schedule with Task Scheduler "At log on".
cd /d %~dp0\..
call .venv\Scripts\activate.bat
kalshi-assistant monitor --loop --interval 60
