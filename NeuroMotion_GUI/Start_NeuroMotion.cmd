@echo off
setlocal
cd /d "%~dp0"
if exist "NeuroMotion.exe" (
    start "" "NeuroMotion.exe"
    exit /b 0
)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "NeuroMotion.pyw"
) else (
    python "NeuroMotion.pyw"
)
if errorlevel 1 (
    echo NeuroMotion failed to start. See startup_error.log in this folder.
    pause
)
endlocal
