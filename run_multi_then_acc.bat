@echo off
REM 定时采集：先跑大盘 --multi，跑完再串行跑服配 --acc。
REM call 是阻塞的：--acc 必须等 --multi 的 python 进程完全退出后才开始，
REM 两者不会并发抢同一个 Chrome profile（持久化 profile 独占）。
cd /d D:\workspace\claude\code\luopan
call ".venv\Scripts\python.exe" run.py --multi >> "data\cron_multi.log" 2>&1
call ".venv\Scripts\python.exe" run.py --acc   >> "data\cron_acc.log" 2>&1
