@echo off
rem ===================================================================
rem  ICT Gold Bot - one-click launcher for Windows.
rem  Double-click this file. It finds Python, installs what is missing, makes
rem  the MT5 terminal download 90 days of history on its own, saves a copy of
rem  the candles in data\, and runs the backtest.
rem  You can also pass your own arguments:  run.bat scan
rem ===================================================================
setlocal
cd /d "%~dp0"
title ICT Gold Bot

rem --- find a Python the MetaTrader5 package actually has wheels for ---
rem     (MetaTrader5 ships only cp310 / cp311 / cp312, 64-bit Windows)
set "PY="
call :try py -3.12
if not defined PY call :try py -3.11
if not defined PY call :try py -3.10
if not defined PY call :try py
if not defined PY call :try python

if not defined PY (
    echo.
    echo   Python 3.10, 3.11 or 3.12 ^(64-bit^) was not found.
    echo   The MetaTrader5 package has no installer for other versions.
    echo.
    echo   Opening the Python 3.12 download page. During setup, tick
    echo   "Add python.exe to PATH", then run this file again.
    echo.
    start "" https://www.python.org/downloads/release/python-3129/
    pause
    exit /b 1
)
echo   Using: %PY%

rem --- make sure the MetaTrader5 package is there ---
%PY% -c "import MetaTrader5" >nul 2>&1
if errorlevel 1 (
    echo   Installing the MetaTrader5 package...
    %PY% -m pip install --disable-pip-version-check --quiet MetaTrader5
    if errorlevel 1 (
        echo.
        echo   The install failed. Check that this Python is 64-bit.
        pause
        exit /b 1
    )
)

if not exist "ict_gold_bot.py" (
    echo.
    echo   ict_gold_bot.py is not in this folder: %CD%
    pause
    exit /b 1
)

rem --- run: your arguments if you passed any, otherwise the full default run ---
rem     The default pulls 90 days from MetaTrader 5 (the terminal downloads the
rem     history by itself), keeps a copy in data\, and backtests the balanced
rem     preset. Nothing to click in MT5, nothing to scroll.
echo.
if "%~1"=="" (
    %PY% ict_gold_bot.py backtest --preset balanced --days 90 --save-data --verbose
) else (
    %PY% ict_gold_bot.py %*
)

echo.
pause
exit /b

:try
%* -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>&1
if errorlevel 1 exit /b
set "PY=%*"
exit /b
