@echo off
cd /d %~dp0
set python=%~dp0.venv\Scripts\python.exe

rem Midnight run: collect + write to Feishu Base only, skip WeCom push.
rem Pending events stay notified=0 and get flushed together with the next
rem daytime round's --flush (existing catch-up logic in main.py already
rem covers events left over from a previous round).

call "%python%" run.py --multi --no-push >> data\cron_multi.log 2>&1
echo [cron] midnight --multi --no-push exit=%ERRORLEVEL% >> data\cron_multi.log

call "%python%" run.py --acc --no-push >> data\cron_acc.log 2>&1
echo [cron] midnight --acc --no-push exit=%ERRORLEVEL% >> data\cron_acc.log
