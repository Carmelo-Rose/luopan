@echo off
cd /d D:\workspace\claude\code\luopan
set python=D:\workspace\claude\code\luopan\.venv\Scripts\python.exe

call "%python%" run.py --multi --no-push >> data\cron_multi.log 2>&1
echo [cron] --multi --no-push exit=%ERRORLEVEL% >> data\cron_multi.log

call "%python%" run.py --acc --no-push >> data\cron_acc.log 2>&1
echo [cron] --acc --no-push exit=%ERRORLEVEL% >> data\cron_acc.log

rem Wait 15 min before pushing. Use waitfor NOT timeout:
rem timeout aborts instantly under wscript hidden window (stdin redirected);
rem waitfor needs no console, times out after 900s (errorlevel 1 is normal).
waitfor /t 900 LuopanPushDelay > nul 2>&1

call "%python%" run.py --multi --flush >> data\cron_multi.log 2>&1
echo [cron] --multi --flush exit=%ERRORLEVEL% >> data\cron_multi.log

call "%python%" run.py --acc --flush >> data\cron_acc.log 2>&1
echo [cron] --acc --flush exit=%ERRORLEVEL% >> data\cron_acc.log
