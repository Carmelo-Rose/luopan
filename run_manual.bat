@echo off
chcp 65001 >nul
REM ============================================================
REM  Douyin Compass TOP200 monitor - manual runner
REM  Double-click = one normal run (collect -> diff -> push WeCom)
REM  Or run with args in a terminal, e.g.:
REM     run_manual.bat --dry-run          collect+diff, no push
REM     run_manual.bat --mock --dry-run   mock data, no browser
REM     run_manual.bat --list-runs        show run history
REM     run_manual.bat --login            open browser to login
REM  Log is also appended to data\cron.log via run.py itself.
REM ============================================================

cd /d "%~dp0"

if "%~1"=="" (
    echo [RUN] python run.py --scope card_order
    python run.py --scope card_order
) else (
    echo [RUN] python run.py %*
    python run.py %*
)

echo.
echo ===== Done ^(exit code %ERRORLEVEL%^). Press any key to close =====
pause >nul
