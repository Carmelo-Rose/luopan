@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d %~dp0
for /f "usebackq tokens=1,* delims==" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -File D:\hyk_sort\ops\get-project-env.ps1 -Project luopan`) do set "%%A=%%B"
set "python=%~dp0.venv\Scripts\python.exe"

call "%python%" run.py --multi --no-push >> data\cron_multi.log 2>&1
set "MULTI=!ERRORLEVEL!"
echo [cron] --multi --no-push exit=!MULTI! >> data\cron_multi.log

if not "!MULTI!"=="0" (
  rem Sync the risk dashboard even when collection died, then leave.
  call "%python%" risk_feishu.py sync-luopan --cleanup >> data\risk_sync.log 2>&1
  echo [cron] --multi failed; risk synced; skip both lanes' flush >> data\cron_multi.log
  exit /b !MULTI!
)

rem ==== DSH report delivery: each lane publishes on its own schedule ====
rem With DSH_DELIVERY_ENABLED=1, --flush freezes that lane's batch and publishes it
rem as a card into the lane's daily DSH task (no Excel). WeCom stays paused unless
rem DSH_WECOM_ENABLED=1 (2026-09-23 ops decision). With delivery off, --flush falls
rem back to the old path, where the WeCom push is still commented out in main.py
rem (Excel + one "disabled" warning). Re-enabling WeCom here sends each group summary
rem as soon as that lane's collection ends: the "collect on the hour, push 15 min
rem later" delay this script used to carry is retired, so restore it per lane if the
rem group message needs to stay off the collection timestamp.
rem
rem 大盘 flushes right here instead of after the acc lane: the two lanes are
rem scope-isolated in the DB (video_order vs video_acc), each lane keeps its own
rem sidecar and batch ledger, and the gateway lane table owns the recipients - so
rem the main-lane card never had a data dependency on the acc round. --flush is a
rem read-only query plus one loopback POST; it does not touch the rank API and
rem therefore does not need any cooldown. A failed publish stays in the ledger and
rem retries unchanged at the next flush of this lane. The acc lane publishes the
rem same way right after its own collection below.
call "%python%" run.py --multi --flush >> data\cron_multi.log 2>&1
set "MULTI_FLUSH=!ERRORLEVEL!"
echo [cron] --multi --flush exit=!MULTI_FLUSH! >> data\cron_multi.log

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

rem Sync the risk dashboard after the acc collection attempt, including failures.
call "%python%" risk_feishu.py sync-luopan --cleanup >> data\risk_sync.log 2>&1

rem A failed acc collection must not block anything the main lane already did.
if not "!ACC!"=="0" (
  set "ACC_FLUSH=0"
  echo [cron] --acc status=!ACC!; skip acc flush only, main lane unaffected >> data\cron_acc.log
) else (
  call "%python%" run.py --acc --flush >> data\cron_acc.log 2>&1
  set "ACC_FLUSH=!ERRORLEVEL!"
  echo [cron] --acc --flush exit=!ACC_FLUSH! >> data\cron_acc.log
)

rem Budget: the measured phases are 45 (multi) + 20 (acc cooldown) + 11 (acc) = ~76
rem minutes, plus risk sync and the two flushes (a read-only query and one loopback
rem POST each). Keep the total inside the 120-minute ExecutionTimeLimit, and do not
rem raise that limit: 13:30 -> 16:00 is only 150 minutes and the task is IgnoreNew.
if not "!MULTI_FLUSH!"=="0" exit /b !MULTI_FLUSH!
if not "!ACC_FLUSH!"=="0" exit /b !ACC_FLUSH!
if not "!ACC!"=="0" exit /b 1
exit /b 0
