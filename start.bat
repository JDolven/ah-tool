@echo off
cd /d "%~dp0"
rem The download from the web version brings its own Python in python\ - otherwise use the installed one.
if exist "python\python.exe" (
  "python\python.exe" run.py
) else (
  python run.py
  if errorlevel 9009 echo Python was not found. Install Python 3.12 or newer from python.org, or download the Windows package with Python included.
)
pause
