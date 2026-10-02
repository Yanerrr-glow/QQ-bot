# 本地启动的快捷入口：双击 start-bot.cmd 就会跑这个。
#
# 做三件事：**自检**（缺什么明确列出来，能当场填的让你填）→ **起机器人** → **打开 Web 控制台**。
# 假设项目**只在本地运行**：不碰 NapCat 的注入 / 隧道 / 服务器那一套 —— NapCat 你自己先起好，
# 本脚本只保证「OneBot 的 WebSocket 端口在监听」这一件事由你负责（自检会报它通不通）。
#
# 为什么另有一个 .cmd：Windows 上双击 .ps1 默认是"用记事本打开"。那个 .cmd **只写 ASCII** ——
# cmd.exe 是按控制台代码页读批处理的，里面写中文会随机器区域不同而乱码。中文全在这里。
$ErrorActionPreference = 'Continue'

# 控制台按 UTF-8 走：日志里一旦出现 emoji，GBK 控制台会让那一条日志**直接丢失**。
# （设 `[Console]::OutputEncoding` 会同时改控制台输出代码页，所以下面这些中文也正常。）
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

$root = $PSScriptRoot
Set-Location $root

Write-Host ''
Write-Host '=== QQ 群 AI 机器人 · 本地启动 ===' -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------- 1. 找解释器
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
$py = $null
$pre = @()
if (Test-Path -LiteralPath $venvPy) {
    $py = $venvPy                        # 正常路径：装过依赖就有它
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $py = 'py'; $pre = @('-3')           # 没建 venv 也先让自检跑起来，好告诉用户缺什么
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = 'python'
}
if (-not $py) {
    Write-Host '没找到 Python。' -ForegroundColor Red
    Write-Host '装一个 3.12（安装时勾上 Add python.exe to PATH）：https://www.python.org/downloads/' -ForegroundColor Yellow
    Write-Host '或者双击项目根目录的 上手自检.exe —— 它自带解释器，会直接告诉你缺什么。' -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host '还没建虚拟环境（.venv）—— 第一次用请先跑：' -ForegroundColor Yellow
    Write-Host "    & '.\_工具链\启动\安装依赖.ps1'" -ForegroundColor Yellow
    Write-Host '下面先照常自检一遍，把还差的东西列清楚。' -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 2. 自检
# 缺 .env 时会照模板生成，并就地问 API Key / 主人 QQ / 角色名（终端里才问，管道下不问）。
& $py @($pre + @((Join-Path $root '_工具链\启动\上手自检.py')))
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '自检没通过 —— 上面「待办」里列的就是还差的东西，补齐后再双击一次。' -ForegroundColor Yellow
    Write-Host "（只想跳过自检直接起：& '.\_工具链\启动\启动机器人.ps1' -SkipCheck）" -ForegroundColor DarkGray
    exit 1
}

# ---------------------------------------------------------------- 3. 本地专属门槛：
# 自检对"缺 .venv"只给**警告**（Docker 部署的机器本机确实不需要它），但这个脚本的用途就是
# **本机跑** —— 所以这里比自检更严：没有 .venv 就停在这里，别等 bot.py 抛 ModuleNotFoundError。
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host ''
    Write-Host '还差虚拟环境 —— 本机运行必须先装依赖（只装一次）：' -ForegroundColor Yellow
    Write-Host "    & '.\_工具链\启动\安装依赖.ps1'" -ForegroundColor Yellow
    Write-Host '装完再双击一次 start-bot.cmd。' -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------- 4. 起来之后自动打开控制台
# 用一个**隐藏的辅助进程**等端口，这样机器人仍在前台跑、日志直接打在窗口里。
$port = 8080
$envFile = Join-Path $root '.env'
if (Test-Path -LiteralPath $envFile) {
    $hit = Select-String -Path $envFile -Pattern '^\s*PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($hit) { $port = [int]$hit.Matches[0].Groups[1].Value }
}
$url = "http://127.0.0.1:$port/ai/"
$helper = 'for($i=0;$i -lt 60;$i++){try{$c=New-Object Net.Sockets.TcpClient;' +
          '$c.Connect("127.0.0.1",' + $port + ');$c.Close();Start-Process "' + $url + '";break}' +
          'catch{Start-Sleep -Milliseconds 500}}'
Start-Process -WindowStyle Hidden -FilePath 'powershell' `
    -ArgumentList @('-NoProfile', '-Command', $helper) | Out-Null

# ---------------------------------------------------------------- 5. 前台起机器人
Write-Host ''
Write-Host ("控制台会在起来之后自动打开：" + $url) -ForegroundColor DarkGray
Write-Host '按 Ctrl+C 停止。' -ForegroundColor DarkGray
Write-Host ''
& $py @($pre + @((Join-Path $root 'bot.py')))
