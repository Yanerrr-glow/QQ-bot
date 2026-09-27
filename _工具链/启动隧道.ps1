<#
启动到 QQ 机器人服务器的 SSH 隧道，并自动打开 Web 控制台。

首次运行会引导你做三件事（只做一次）：
  1. 生成 SSH 密钥（免密码登录）
  2. 把公钥装到服务器
  3. 往 ~/.ssh/config 写一个 Host 别名（含端口转发）

之后每次运行都只是「起隧道 + 开浏览器」，几秒钟的事。

为什么用 SSH config 而不是把 IP 写在这个脚本里：
  IP、密钥路径这类东西属于个人环境，放 ~/.ssh/config 里更合适 ——
  这种脚本是要提交进仓库的，不该夹带你的服务器地址。
#>

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 可改配置
$alias   = 'qqbot'      # SSH 别名，以后也能直接用 `ssh qqbot`
$localUi = 18080        # 本地访问控制台的端口（避开常被占用的 8080）
$localNc = 16099        # 本地访问 NapCat 面板的端口

# ---------------------------------------------------------------- 前置检查
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host "[x] 找不到 ssh 命令。" -ForegroundColor Red
    Write-Host "    Windows 10/11 自带 OpenSSH 客户端；没有的话去「设置 → 应用 → 可选功能」装一个。" -ForegroundColor DarkGray
    Read-Host "按回车关闭"
    exit 1
}

$sshDir  = Join-Path $env:USERPROFILE '.ssh'
$cfgPath = Join-Path $sshDir 'config'
$keyPath = Join-Path $sshDir $alias

if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir -Force | Out-Null }

# ---------------------------------------------------------------- 首次配置
$configured = (Test-Path $cfgPath) -and
              [bool](Select-String -Path $cfgPath -Pattern "^Host\s+$alias\s*$" -Quiet)

if (-not $configured) {
    Write-Host "=== 首次配置（只做这一次）===" -ForegroundColor Cyan
    $ip = (Read-Host "请输入服务器公网 IP").Trim()
    if (-not $ip) { Write-Host "[x] IP 不能为空" -ForegroundColor Red; Read-Host "按回车关闭"; exit 1 }

    if (-not (Test-Path $keyPath)) {
        Write-Host "`n[1/3] 生成 SSH 密钥..." -ForegroundColor Yellow
        Write-Host "      接下来会问你保存路径和密码 —— 直接连按回车（留空密码）即可。" -ForegroundColor DarkGray
        ssh-keygen -t ed25519 -f $keyPath
        if (-not (Test-Path $keyPath)) { Write-Host "[x] 密钥没生成成功" -ForegroundColor Red; Read-Host "按回车关闭"; exit 1 }
    } else {
        Write-Host "`n[1/3] 密钥已存在，跳过：$keyPath" -ForegroundColor DarkGray
    }

    Write-Host "`n[2/3] 把公钥装到服务器（这一步要输一次服务器密码）..." -ForegroundColor Yellow
    $pub = (Get-Content "$keyPath.pub" -Raw).Trim()
    ssh "root@$ip" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '$pub' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[x] 装公钥失败。确认 IP、密码，以及服务器 22 端口是否放行。" -ForegroundColor Red
        Read-Host "按回车关闭"; exit 1
    }

    Write-Host "`n[3/3] 写入 ~/.ssh/config ..." -ForegroundColor Yellow
    $block = @"

# ===== QQ 机器人服务器（由 启动隧道.ps1 写入）=====
Host $alias
    HostName $ip
    User root
    IdentityFile $keyPath
    LocalForward $localUi 127.0.0.1:8080
    LocalForward $localNc 127.0.0.1:6099
    ServerAliveInterval 30
    ServerAliveCountMax 3
"@
    # 用无 BOM 的 UTF-8 追加：OpenSSH 读 config 时不喜欢开头有 BOM
    [System.IO.File]::AppendAllText($cfgPath, $block, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "      已写入 $cfgPath" -ForegroundColor Green
    Write-Host "`n配置完成。以后直接运行本脚本即可，不用再输 IP 和密码。" -ForegroundColor Green
}

# ---------------------------------------------------------------- 起隧道
Write-Host "`n建立隧道（$alias）..." -ForegroundColor Cyan
$tunnel = Start-Process ssh -ArgumentList $alias -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 3

if ($tunnel.HasExited) {
    Write-Host "[x] 隧道立刻就退出了。手动跑一次看报错：" -ForegroundColor Red
    Write-Host "      ssh $alias" -ForegroundColor White
    Read-Host "按回车关闭"
    exit 1
}

Write-Host "隧道已建立（PID $($tunnel.Id)）" -ForegroundColor Green
Write-Host ""
Write-Host "  Web 控制台   http://127.0.0.1:$localUi/ai/" -ForegroundColor White
Write-Host "  NapCat 面板  http://127.0.0.1:$localNc/webui" -ForegroundColor White
Write-Host ""
Start-Process "http://127.0.0.1:$localUi/ai/"

Write-Host "关闭隧道：Stop-Process -Id $($tunnel.Id)" -ForegroundColor DarkGray
Write-Host "（关掉本窗口不影响隧道，它是独立进程）" -ForegroundColor DarkGray
Read-Host "`n按回车关闭本窗口"
