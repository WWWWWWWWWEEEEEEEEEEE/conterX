@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem Find a Python that really has PySide6 installed.
rem Checking only that the file exists is not enough: a Python without
rem PySide6 makes the window flash and vanish with no explanation.
rem NOTE: keep this file ASCII-only. cmd parses .bat with the OEM codepage,
rem so UTF-8 Chinese text here breaks the parser (chcp does not help).
set "PY="
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :try "python"
if not defined PY call :try "py"

if not defined PY (
    echo.
    echo   [conterX] No Python with PySide6 found, cannot start the UI.
    echo.
    echo   Install dependency :  pip install pyside6
    echo   Use a specific one :  "C:\path\to\python.exe" main.py
    echo   Headless selftest  :  python selftest.py
    echo.
    pause
    exit /b 1
)

"%PY%" main.py
if errorlevel 1 pause
exit /b 0

:try
"%~1" -c "import PySide6" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0
