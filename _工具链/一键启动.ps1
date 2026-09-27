# QQ 机器人一键启动 / 停止 / 打开控制台。
#
# 用法：
#   & '.\_工具链\一键启动.ps1'              # 启动机器人并打开控制台
#   & '.\_工具链\一键启动.ps1' -Stop        # 停掉机器人
#   & '.\_工具链\一键启动.ps1' -Console     # 只打开控制台网页
#   & '.\_工具链\一键启动.ps1' -NoBrowser   # 启动但不自动开浏览器
#
# 【前置条件】NapCat 必须已经在跑（6700 在监听）。
# 本框架不代管 NapCat 的启动：它的启动方式随平台与安装方式差异很大
# （Windows 注入 QQ / Linux 容器 / 独立部署），见 README「第 3 节 NapCat 侧」。
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Console,
    [switch]$NoBrowser,
    [int]$TimeoutSec = 90
)

$ErrorActionPreference = 'Continue'

$Root       = Split-Path $PSScriptRoot -Parent
$ConsoleUrl = 'http://127.0.0.1:8080/ai/'
$OneBotPort = 6700
$WebPort    = 8080
$BotScript  = Join-Path $PSScriptRoot '启动机器人.ps1'
$VenvPy     = Join-Path $Root '.venv\Scripts\python.exe'

function Write-Step { param($m) Write-Host ("`n== " + $m) -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host ("   [OK]   " + $m) -ForegroundColor Green }
function Write-Warn { param($m) Write-Host ("   [注意] " + $m) -ForegroundColor Yellow }
function Write-Bad  { param($m) Write-Host ("   [失败] " + $m) -ForegroundColor Red }

function Test-Port {
    param([int]$Port)
    return @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -eq $Port }).Count -gt 0
}

function Wait-Port {
    param([int]$Port, [int]$Seconds)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

# ---------------------------------------------------------------- 只开控制台
if ($Console) {
    Start-Process $ConsoleUrl
    Write-Host "已打开控制台：$ConsoleUrl" -ForegroundColor Green
    exit 0
}

# ---------------------------------------------------------------- 停止
if ($Stop) {
    Write-Step '停止 QQ 机器人'
    # 只按命令行匹配本项目入口，避免误杀同机其它 python 程序。
    $py = @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*bot.py*' })
    if ($py.Count -gt 0) {
        $py | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Ok ("已停止机器人（" + $py.Count + " 个进程）")
    } else {
        Write-Ok '机器人本来就没在跑'
    }
    Start-Sleep -Seconds 2
    Write-Host ''
    Write-Host '已停止。NapCat 未受影响，需要时请自行停它。' -ForegroundColor Green
    Start-Sleep -Seconds 2
    exit 0
}

Write-Host '=========================================' -ForegroundColor DarkCyan
Write-Host '           QQ 机器人启动器' -ForegroundColor Cyan
Write-Host '=========================================' -ForegroundColor DarkCyan

# ---------------------------------------------------------------- 前置检查
Write-Step '检查环境'
$missing = @()
if (-not (Test-Path -LiteralPath $VenvPy))  { $missing += "Python 虚拟环境：$VenvPy" }
if (-not (Test-Path -LiteralPath (Join-Path $Root '.env'))) { $missing += "配置文件：$Root\.env" }
if ($missing.Count -gt 0) {
    foreach ($m in $missing) { Write-Bad ("缺少 " + $m) }
    Write-Host ''
    Write-Host '环境不完整。先跑一次 _工具链\自检.ps1 看详细原因。' -ForegroundColor Yellow
    Read-Host '按回车关闭'
    exit 1
}
Write-Ok '虚拟环境 / .env 都在'

# ---------------------------------------------------------------- NapCat 就绪检查
Write-Step 'NapCat（QQ 协议桥）'
if (Test-Port $OneBotPort) {
    Write-Ok ("$OneBotPort 已在监听，NapCat 就绪")
} else {
    Write-Bad ("$OneBotPort 无人监听 —— NapCat 没在跑，或 WS 服务端没配")
    Write-Host '   机器人会一直重连、收不到任何消息。先按 README 第 3 节把 NapCat 起起来。' -ForegroundColor Yellow
    Read-Host '按回车关闭'
    exit 1
}

# ---------------------------------------------------------------- 机器人
Write-Step '机器人（NoneBot2）'
if (Test-Port $WebPort) {
    Write-Ok ("$WebPort 已在监听，机器人早就在跑了，跳过启动")
} else {
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoExit', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$BotScript`"") | Out-Null
    Write-Host '   已在新窗口启动机器人，等 Web 控制台端口就绪...' -ForegroundColor DarkGray

    if (Wait-Port $WebPort $TimeoutSec) {
        Write-Ok ("$WebPort 已就绪，机器人已连上 NapCat")
    } else {
        Write-Warn ("等了 " + $TimeoutSec + " 秒，$WebPort 仍未监听 —— 切到那个窗口看日志")
    }
}

# ---------------------------------------------------------------- 收尾
Write-Step '完成'
Write-Host ("   NapCat     : 127.0.0.1:$OneBotPort") -ForegroundColor DarkGray
Write-Host ('   控制台     : ' + $ConsoleUrl) -ForegroundColor DarkGray

if (-not $NoBrowser) {
    Start-Sleep -Seconds 2
    Start-Process $ConsoleUrl
    Write-Ok '已打开控制台'
}

Write-Host ''
Write-Host '一切就绪，去群里 @ 它吧。' -ForegroundColor Green
Start-Sleep -Seconds 3
