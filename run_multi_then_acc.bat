@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d %~dp0
for /f "usebackq tokens=1,* delims==" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -File D:\hyk_sort\ops\get-project-env.ps1 -Project luopan`) do set "%%A=%%B"
set "python=%~dp0.venv\Scripts\python.exe"

call "%python%" run.py --multi --no-push >> data\cron_multi.log 2>&1
set "MULTI=!ERRORLEVEL!"
echo [cron] --multi --no-push exit=!MULTI! >> data\cron_multi.log

if "!MULTI!"=="0" (
  rem Both lanes share one account-level rank-API budget. Running --acc
  rem straight after --multi hit it with an empty bucket: on 2026-07-27
  rem 16:00 every acc leaf died on code=11001 while --multi had just
  rem finished 16 of 19 categories. Cool down before the acc lane.
  rem waitfor works in the hidden wscript session; timeout does not.
  echo [cron] cooldown 1200s before --acc >> data\cron_acc.log
  waitfor /t 1200 LuopanAccCooldown > nul 2>&1
  call "%python%" run.py --acc --no-push >> data\cron_acc.log 2>&1
  set "ACC=!ERRORLEVEL!"
  echo [cron] --acc --no-push exit=!ACC! >> data\cron_acc.log
) else (
  set "ACC=skip"
)

rem Sync the risk dashboard after every collection attempt, including failures.
call "%python%" risk_feishu.py sync-luopan --cleanup >> data\risk_sync.log 2>&1

if not "!MULTI!"=="0" (
  echo [cron] --multi failed; risk synced; skip flush >> data\cron_multi.log
  exit /b !MULTI!
)

waitfor /t 900 LuopanPushDelay > nul 2>&1

rem ==== WeCom push paused (2026-09-23): every --flush step is commented out ====
rem The push itself is disabled in main.py (_dispatch_summary / _finalize_acc_push),
rem so --flush only reads the sidecar, regenerates the local Excel and logs one
rem "disabled" warning - pure spinning. This script already costs roughly
rem 45 (multi) + 20 (acc cooldown) + 11 (acc) + 15 (the wait above) minutes, and
rem the task's ExecutionTimeLimit is 90 minutes: past it Task Scheduler force-kills
rem the whole process tree and records 0x41321.
rem To restore pushing: uncomment the two blocks in main.py first, then this one.
rem
rem call "%python%" run.py --multi --flush >> data\cron_multi.log 2>&1
rem set "MULTI_FLUSH=!ERRORLEVEL!"
rem echo [cron] --multi --flush exit=!MULTI_FLUSH! >> data\cron_multi.log
rem
rem if "!ACC!"=="0" (
rem   call "%python%" run.py --acc --flush >> data\cron_acc.log 2>&1
rem   set "ACC_FLUSH=!ERRORLEVEL!"
rem   echo [cron] --acc --flush exit=!ACC_FLUSH! >> data\cron_acc.log
rem ) else (
rem   set "ACC_FLUSH=0"
rem   echo [cron] --acc status=!ACC!; skip acc flush only, main lane unaffected >> data\cron_acc.log
rem )
rem
rem if not "!MULTI_FLUSH!"=="0" exit /b !MULTI_FLUSH!
rem if not "!ACC_FLUSH!"=="0" exit /b !ACC_FLUSH!

rem Exit code now reflects the two collection lanes only (a --multi failure has
rem already exited above, so here only an acc collection failure is reported).
if not "!ACC!"=="0" exit /b 1
exit /b 0
