# 注册计划任务。直接「用 PowerShell 运行」即可——脚本会自动弹 UAC 提权。
# 注册带 RunLevel Highest 的任务必须管理员权限，非提权运行会被静默拒绝。

# ── 自动提权：若当前非管理员，弹 UAC 以管理员重新运行本脚本 ──────────────
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host '需要管理员权限，正在弹出 UAC 提权…' -ForegroundColor Yellow
    Start-Process powershell.exe -Verb RunAs `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

# 先清掉所有同类旧任务（含历史乱码名副本），避免重复注册导致两个任务并发抢 Chrome profile
Get-ScheduledTask | Where-Object { $_.TaskName -like 'DouyinCompass*' } |
    Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 90) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# 白天 4 个触发：采集 + 飞书 Base 写入 + 企微推送（原有 run_multi_then_acc.bat 不变）
$action = New-ScheduledTaskAction `
    -Execute 'wscript.exe' `
    -Argument '"D:\workspace\claude\code\luopan\run_hidden.vbs"' `
    -WorkingDirectory 'D:\workspace\claude\code\luopan'

$times = @('08:30', '10:30', '13:30', '16:00')
$triggers = $times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }

Register-ScheduledTask `
    -TaskName 'DouyinCompass_串行采集' `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description 'Douyin Compass 大盘+服配串行采集 08:30/10:30/13:30/16:00，wscript隐藏窗口，venv Python' `
    -Force

# 午夜单独一个任务：只采集+写飞书 Base，不推企微（run_multi_then_acc_midnight.bat
# 只跑 --no-push，不等待、不 --flush）。未推送的事件 notified=0，会随下一轮
# （08:30）的 --flush 一并补发，复用 main.py 既有的补发逻辑，不丢事件。
$midnightAction = New-ScheduledTaskAction `
    -Execute 'wscript.exe' `
    -Argument '"D:\workspace\claude\code\luopan\run_hidden_midnight.vbs"' `
    -WorkingDirectory 'D:\workspace\claude\code\luopan'

$midnightTrigger = New-ScheduledTaskTrigger -Daily -At '00:00'

Register-ScheduledTask `
    -TaskName 'DouyinCompass_串行采集_午夜' `
    -Action $midnightAction `
    -Trigger $midnightTrigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Douyin Compass 大盘+服配串行采集 00:00，只采集写飞书 Base，不推企微' `
    -Force

Write-Host '计划任务注册成功！白天 4 个触发时间：08:30 / 10:30 / 13:30 / 16:00（采集+推企微）' -ForegroundColor Green
Write-Host '午夜任务：00:00（只采集写飞书 Base，不推企微）' -ForegroundColor Green
Write-Host '按任意键关闭…'
$null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
