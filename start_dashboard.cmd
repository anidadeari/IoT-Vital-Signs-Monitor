@echo off
cd /d "%~dp0"
title IoT Vital Signs Dashboard Server

echo Starting FastAPI on this computer...
echo Local dashboard: http://127.0.0.1:8000/dashboard
echo ESP32 endpoint:   http://10.33.186.50:8000/api/data
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  py -m uvicorn main:app --host 0.0.0.0 --port 8000
  goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
  python -m uvicorn main:app --host 0.0.0.0 --port 8000
  goto :end
)

echo Python was not found in PATH.
echo Open the project terminal and run:
echo python -m uvicorn main:app --host 0.0.0.0 --port 8000
pause

:end
