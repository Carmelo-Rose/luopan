@echo off
cd /d %~dp0
set python=%~dp0.venv\Scripts\python.exe

call "%python%" run.py --multi --no-push >> data\cron_multi.log 2>&1
echo [cron] --multi --no-push exit=%ERRORLEVEL% >> data\cron_multi.log
if errorlevel 1 (
  echo [cron] --multi collection failed; skip --acc, risk sync, and flush >> data\cron_multi.log
  exit /b %ERRORLEVEL%
)

call "%python%" run.py --acc --no-push >> data\cron_acc.log 2>&1
echo [cron] --acc --no-push exit=%ERRORLEVEL% >> data\cron_acc.log
if errorlevel 1 (
  echo [cron] --acc collection failed; skip risk sync and flush >> data\cron_acc.log
  exit /b %ERRORLEVEL%
)

rem Sync business outcomes to the Feishu risk dashboard. This does not start collection.
call "%python%" risk_feishu.py sync-luopan --cleanup >> data\risk_sync.log 2>&1

rem Wait 15 min before pushing. Use waitfor NOT timeout:
rem timeout aborts instantly under wscript hidden window (stdin redirected);
rem waitfor needs no console, times out after 900s (errorlevel 1 is normal).
waitfor /t 900 LuopanPushDelay > nul 2>&1

call "%python%" run.py --multi --flush >> data\cron_multi.log 2>&1
echo [cron] --multi --flush exit=%ERRORLEVEL% >> data\cron_multi.log

call "%python%" run.py --acc --flush >> data\cron_acc.log 2>&1
echo [cron] --acc --flush exit=%ERRORLEVEL% >> data\cron_acc.log
