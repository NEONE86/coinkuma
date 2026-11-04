@echo off
setlocal
cd /d "%~dp0"
echo ONE Flow Scalper v1 - PAPER ONLY. No real exchange orders.

where py >nul 2>&1
if errorlevel 1 (
    set "ONE_PYTHON=python"
) else (
    set "ONE_PYTHON=py -3"
)

%ONE_PYTHON% --version
if errorlevel 1 (
    echo Python 3.12 or later is required. Install Python and run again.
    pause
    exit /b 2
)

%ONE_PYTHON% -m pip install websockets==16.0
if errorlevel 1 (
    echo Cannot install the required public-feed dependency.
    pause
    exit /b 2
)

if not exist "paper_runs" mkdir "paper_runs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "ONE_RUN_ID=%%I"
if not defined ONE_RUN_ID (
    echo Cannot create run timestamp.
    pause
    exit /b 2
)

set "ONE_RUN_DIR=%~dp0paper_runs\%ONE_RUN_ID%"
echo Starting a 5-minute public Bybit PAPER-only run: %ONE_RUN_DIR%
%ONE_PYTHON% -m one_trading.ai_native.scalp_flow live --enable-public-network --symbol SOLUSDT --capital-usdt 10000 --duration-seconds 300 --max-inputs 100000 --output-dir "%ONE_RUN_DIR%"
set "ONE_RUN_CODE=%errorlevel%"

if exist "%ONE_RUN_DIR%\events.jsonl" (
    echo Checking the saved decisions and fills...
    %ONE_PYTHON% -m one_trading.ai_native.scalp_flow verify --events "%ONE_RUN_DIR%\events.jsonl"
) else (
    echo No journal file created.
)

echo End of PAPER run. Program exit code: %ONE_RUN_CODE%
echo Results directory: %ONE_RUN_DIR%
pause
exit /b %ONE_RUN_CODE%
