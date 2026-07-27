@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d %~dp0
for /f "usebackq tokens=1,* delims==" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -File D:\hyk_sort\ops\get-project-env.ps1 -Project luopan`) do set "%%A=%%B"
set "python=%~dp0.venv\Scripts\python.exe"

rem Midnight run: collect + write to Feishu Base only, skip WeCom push.
rem Pending events stay notified=0 and get flushed together with the next
rem daytime round's --flush (existing catch-up logic in main.py already
rem covers events left over from a previous round).

call "%python%" run.py --multi --no-push >> data\cron_multi.log 2>&1
set "MULTI=!ERRORLEVEL!"
echo [cron] midnight --multi --no-push exit=!MULTI! >> data\cron_multi.log

if "!MULTI!"=="0" (
  rem Same shared rank-API budget as the daytime script: cool down before
  rem the acc lane instead of starting it on an empty bucket.
  echo [cron] midnight cooldown 1200s before --acc >> data\cron_acc.log
  waitfor /t 1200 LuopanAccCooldown > nul 2>&1
  call "%python%" run.py --acc --no-push >> data\cron_acc.log 2>&1
  set "ACC=!ERRORLEVEL!"
  echo [cron] midnight --acc --no-push exit=!ACC! >> data\cron_acc.log
) else (
  set "ACC=skip"
)

rem Sync the risk dashboard after every collection attempt, including failures.
call "%python%" risk_feishu.py sync-luopan --cleanup >> data\risk_sync.log 2>&1

if not "!MULTI!"=="0" (
  echo [cron] midnight --multi failed; risk synced >> data\cron_multi.log
  exit /b !MULTI!
)
if not "!ACC!"=="0" (
  echo [cron] midnight --acc failed; risk synced >> data\cron_acc.log
  exit /b 1
)

exit /b 0
