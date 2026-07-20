Set-Location -LiteralPath 'D:\workspace\claude\code\luopan'

$python = 'C:\Users\Administrator.DESKTOP-GRHN4PA\AppData\Roaming\Accio\pre-install\e6550f7e00ff\python\python.exe'

# 延后推送：采集在定时触发时间跑，推送延后到「触发 + 30 分钟」。
# 流程：① 两条线连续采集入库（--no-push）→ ② 睡到触发+30min → ③ flush 推送。
# bat 同步等待本脚本完成（任务时限 60 分钟，采集+等待+flush 约 45 分钟以内）。

$start = Get-Date
$pushDelayMinutes = 30

function Run-Python {
    param([string]$Args, [string]$Log)
    # 用 cmd /c 调用，避免 PowerShell 把 Python stderr 包装成 ErrorRecord 干扰退出码
    cmd /c "`"$python`" $Args >> `"$Log`" 2>&1"
    $code = $LASTEXITCODE
    "`n[cron] $Args exit=$code $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" | Out-File -Append -Encoding utf8 -FilePath $Log
    return $code
}

# ── ① 采集入库（不推送）──────────────────────────────────────────────
$multiCode = Run-Python "run.py --multi --no-push" "data\cron_multi.log"
if ($multiCode -ne 0) {
    "`n[cron] --multi 采集失败，跳过 --acc 和 flush，避免继续触发榜单接口风控或推送旧 sidecar" |
        Out-File -Append -Encoding utf8 -FilePath 'data\cron_multi.log'
    exit $multiCode
}

$accCode = Run-Python "run.py --acc --no-push" "data\cron_acc.log"
if ($accCode -ne 0) {
    "`n[cron] --acc 采集失败，跳过 flush，避免推送旧 sidecar" |
        Out-File -Append -Encoding utf8 -FilePath 'data\cron_acc.log'
    exit $accCode
}

# ── ② 睡到「触发 + 30 分钟」再推送 ───────────────────────────────────
$target = $start.AddMinutes($pushDelayMinutes)
$wait = ($target - (Get-Date)).TotalSeconds
if ($wait -gt 0) {
    "`n[cron] 采集完成，睡 $([int]$wait)s 至 $($target.ToString('HH:mm:ss')) 再推送" |
        Out-File -Append -Encoding utf8 -FilePath 'data\cron_multi.log'
    Start-Sleep -Seconds $wait
}

# ── ③ flush 推送（大盘 + 服配）──────────────────────────────────────
Run-Python "run.py --multi --flush" "data\cron_multi.log"
Run-Python "run.py --acc --flush"   "data\cron_acc.log"
