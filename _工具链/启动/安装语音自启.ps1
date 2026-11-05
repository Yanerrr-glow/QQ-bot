# 安装/卸载「语音服务」开机自启（Windows 任务计划，登录即启动守护）
#
# 用法：
#   .\安装语音自启.ps1            # 注册任务并立即启动
#   .\安装语音自启.ps1 -Status    # 看任务状态、上次结果与当前进程
#   .\安装语音自启.ps1 -Uninstall # 停止并删除任务（不影响已启动的进程）
#
# 为什么用「登录时」而不是「开机时」：SSH 密钥、用户目录都挂在 Administrator 下，
# 而本机已配置自动登录（AutoAdminLogon=1），登录时触发等价于开机自启，且不需要保存密码。
[CmdletBinding()]
param(
  [switch]$Uninstall,
  [switch]$Status,
  [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$TaskName = 'QQBot-VoiceService'
$Script = Join-Path $PSScriptRoot '启动语音服务.ps1'
if (-not (Test-Path -LiteralPath $Script)) { throw "找不到启动脚本：$Script" }

$UserId = "$env:USERDOMAIN\$env:USERNAME"
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Show-Status {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $task) {
    Write-Host "任务 $TaskName 未安装。" -ForegroundColor Yellow
  } else {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "任务 $TaskName：$($task.State)"
    Write-Host "  上次运行：$($info.LastRunTime)  结果：0x$('{0:X}' -f $info.LastTaskResult)"
    Write-Host "  下次运行：$($info.NextRunTime)"
  }
  $watch = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ([string]$_.CommandLine) -like '*启动语音服务.ps1*' -and ([string]$_.CommandLine) -like '*-Watch*'
  })
  Write-Host "守护进程：$($watch.Count) 个"
  $engine = Get-NetTCPConnection -LocalPort 9880 -State Listen -ErrorAction SilentlyContinue
  Write-Host "引擎 9880：$([bool]$engine)"
  try {
    $h = & ssh.exe -o BatchMode=yes -o ConnectTimeout=8 -o ClearAllForwardings=yes qqbot `
      "curl -s -m 5 http://172.17.0.1:19881/healthz" 2>$null
    Write-Host "服务器 relay healthz：$("$h".Trim())"
  } catch {
    Write-Host "服务器 relay healthz：检查失败（$($_.Exception.Message)）" -ForegroundColor Yellow
  }
}

if ($Status) { Show-Status; exit 0 }

if ($Uninstall) {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除任务 $TaskName（已启动的进程需另跑 -Stop 停止）。" -ForegroundColor Cyan
  } else {
    Write-Host "任务 $TaskName 本来就不存在。" -ForegroundColor DarkGray
  }
  exit 0
}

$action = New-ScheduledTaskAction -Execute $PowerShell `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -Watch" `
  -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force `
  -Description 'QQ bot 语音服务：GPT-SoVITS(9880) + SSH 反向隧道，守护并自动重启' | Out-Null

Write-Host "已注册任务 $TaskName（登录时启动，当前用户 $UserId）。" -ForegroundColor Green

if (-not $NoStart) {
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 3
}
Show-Status
