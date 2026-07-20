@echo off
cd /d %~dp0
set python=%~dp0.venv\Scripts\python.exe

rem Midnight run: collect + write to Feishu Base only, skip WeCom push.
rem Pending events stay notified=0 and get flushed together with the next
rem daytime round's --flush (existing catch-up logic in main.py already
rem covers events left over from a previous round).

call "%python%" run.py --multi --no-push >> data\cron_multi.log 2>&1
echo [cron] midnight --multi --no-push exit=%ERRORLEVEL% >> data\cron_multi.log
if errorlevel 1 (
  echo [cron] midnight --multi collection failed; skip --acc and risk sync >> data\cron_multi.log
  exit /b %ERRORLEVEL%
)

call "%python%" run.py --acc --no-push >> data\cron_acc.log 2>&1
echo [cron] midnight --acc --no-push exit=%ERRORLEVEL% >> data\cron_acc.log
if errorlevel 1 (
  echo [cron] midnight --acc collection failed; skip risk sync >> data\cron_acc.log
  exit /b %ERRORLEVEL%
)

rem Sync business outcomes to the Feishu risk dashboard. This does not start collection.
call "%python%" risk_feishu.py sync-luopan --cleanup >> data\risk_sync.log 2>&1
