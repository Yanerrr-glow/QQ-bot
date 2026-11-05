# 启动/停止/守护「本机 GPT-SoVITS + SSH 反向隧道」
#
# 用法：
#   .\启动语音服务.ps1            # 起引擎（9880）与隧道（服务器回环 19880），然后退出
#   .\启动语音服务.ps1 -Watch     # 常驻守护：引擎或隧道掉了就拉起来（开机自启用这个）
#   .\启动语音服务.ps1 -Stop      # 先停守护，再停引擎与隧道
#
# 只影响本机进程；不动服务器、不动 bot 容器、不发 QQ 消息。
# 日志：<项目>\data\runtime\logs\voice_service.log
[CmdletBinding()]
param(
  [switch]$Stop,
  [switch]$Watch,
  [string]$SshHost = 'qqbot',
  [int]$Port = 9880,
  [int]$RemotePort = 19880,
  [int]$Interval = 30
)

$ErrorActionPreference = 'Stop'

# ---- 推导：本脚本在 <项目>\_工具链\启动\，向上两级是项目根
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Probe = $PSScriptRoot
$WorkRoot = $null
for ($i = 0; $i -lt 7; $i++) {
  $Probe = Split-Path -Parent $Probe
  if (-not $Probe) { break }
  if (Test-Path -LiteralPath (Join-Path $Probe '可复用工具链')) { $WorkRoot = $Probe; break }
}
if (-not $WorkRoot) { throw '找不到工作区根（含「可复用工具链」的目录）' }
$Engine = Join-Path $WorkRoot '可复用工具链\07_音视频处理\GPT-SoVITS'
$Python = Join-Path $Engine '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "找不到引擎解释器：$Python" }

$LogDir = Join-Path $ProjectRoot 'data\runtime\logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir 'voice_service.log'

function Write-Log([string]$msg) {
  $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
  try {
    if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 2MB) {
      Move-Item $LogFile "$LogFile.1" -Force
    }
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
  } catch { }
  if (-not $Watch -and -not $Stop) { Write-Host $line }
}

function Get-LocalListen([int]$p) {
  Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
}

function Test-Port([int]$p) {
  $client = New-Object System.Net.Sockets.TcpClient
  try {
    $iar = $client.BeginConnect('127.0.0.1', $p, $null, $null)
    if (-not $iar.AsyncWaitHandle.WaitOne(1500)) { return $false }
    $client.EndConnect($iar)
    return $true
  } catch { return $false }
  finally { $client.Close() }
}

function Get-TunnelProcs {
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $cmd = [string]$_.CommandLine
    $_.Name -eq 'ssh.exe' -and $cmd -like '* -R *' -and
      $cmd -like "*:$RemotePort*" -and $cmd -like "*:$Port*"
  }
}

function Get-WatchProcs {
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -like 'powershell*' -and ([string]$_.CommandLine) -like '*启动语音服务.ps1*' -and
      ([string]$_.CommandLine) -like '*-Watch*'
  }
}

function Start-Engine {
  $apiLog = Join-Path $LogDir 'gsv_api.log'
  Write-Log "启动引擎（$Port）"
  Start-Process -FilePath $Python -ArgumentList @('api_v2.py', '-a', '127.0.0.1', '-p', "$Port") `
    -WorkingDirectory $Engine -WindowStyle Hidden `
    -RedirectStandardOutput $apiLog -RedirectStandardError "$apiLog.err" | Out-Null
}

function Reset-Relay {
  # 引擎重启后，服务器 relay 里缓存的"已加载权重"就过期了：
  # 它以为还是上次那套，于是不再调 /set_*_weights，第一句语音会用错音色。
  # 这里重启 relay（缓存清空，下次请求会重新 set）。失败只记日志，不影响本地。
  try {
    & ssh.exe -o BatchMode=yes -o ConnectTimeout=8 -o ClearAllForwardings=yes $SshHost `
      "systemctl restart tts-relay" 2>$null | Out-Null
    Write-Log "已重启服务器 tts-relay（清空权重缓存）"
  } catch {
    Write-Log "重启 tts-relay 失败：$($_.Exception.Message)"
  }
}

function Start-Tunnel {
  Write-Log "启动 SSH 反向隧道：服务器 127.0.0.1:$RemotePort -> 本机 127.0.0.1:$Port"
  $remote = "127.0.0.1:${RemotePort}:127.0.0.1:$Port"
  $sshArgs = @('-N', '-T', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
               '-o', 'ServerAliveCountMax=3', '-o', 'ConnectTimeout=10',
               '-R', $remote, $SshHost)
  Start-Process -FilePath 'ssh.exe' -ArgumentList $sshArgs -WindowStyle Hidden | Out-Null
}

function Test-TunnelRemote {
  try {
    $out = & ssh.exe -o BatchMode=yes -o ConnectTimeout=8 -o ClearAllForwardings=yes $SshHost `
      "ss -ltn 2>/dev/null | grep -c '127.0.0.1:$RemotePort'" 2>$null
    return ("$out".Trim() -match '^[1-9]')
  } catch { return $false }
}

if ($Stop) {
  foreach ($p in @(Get-WatchProcs)) {
    Write-Log "停止守护进程 PID $($p.ProcessId)"
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
  }
  $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ([string]$_.CommandLine) -like '*api_v2.py*' -or
    ($_.Name -eq 'ssh.exe' -and ([string]$_.CommandLine) -like "*:$RemotePort*")
  })
  foreach ($p in $procs) {
    Write-Log "停止 PID $($p.ProcessId)：$($p.Name)"
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
  }
  Write-Host "已停止守护与 $($procs.Count) 个相关进程。" -ForegroundColor Cyan
  exit 0
}

if ($Watch) {
  # 单实例：已有一个守护在跑就退出（任务计划重复触发时不会起两份）
  $mutex = New-Object System.Threading.Mutex($false, 'Global\QQBotVoiceService')
  if (-not $mutex.WaitOne(1000)) {
    Write-Log "已有守护实例，退出"
    exit 0
  }
  Write-Log "守护启动（engine=$Engine，ssh=$SshHost，间隔=${Interval}s）"
  # 开机时网络可能还没就绪：先等一会儿，后面循环里也会重试
  Start-Sleep -Seconds 20
  $tick = 0
  $tunnelFails = 0
  while ($true) {
    try {
      if (-not (Get-LocalListen $Port)) {
        Write-Log "引擎不在监听，拉起"
        Start-Engine
        Reset-Relay
        Start-Sleep -Seconds 5
      }
      if (@(Get-TunnelProcs).Count -eq 0) {
        Start-Tunnel
        Start-Sleep -Seconds 5
      } else {
        $tunnelFails = 0
      }
      # 隧道进程在 != 隧道真通：每 5 轮（约 2.5 分钟）从服务器侧确认一次
      if (($tick % 5) -eq 0) {
        if (-not (Test-TunnelRemote)) {
          $tunnelFails++
          Write-Log "服务器侧未看到 $RemotePort 监听（连续 $tunnelFails 次）"
          if ($tunnelFails -ge 2) {
            Write-Log "重启隧道"
            foreach ($p in @(Get-TunnelProcs)) {
              try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
            }
            Start-Tunnel
            $tunnelFails = 0
          }
        } else {
          $tunnelFails = 0
        }
      }
    } catch {
      Write-Log "守护循环异常：$($_.Exception.Message)"
    }
    $tick++
    Start-Sleep -Seconds $Interval
  }
}

# ---- 一次性启动（手动用）
if (-not (Get-LocalListen $Port)) { Start-Engine; Reset-Relay } else { Write-Host "引擎已在 $Port 监听，跳过。" -ForegroundColor DarkGray }
if (@(Get-TunnelProcs).Count -eq 0) { Start-Tunnel } else { Write-Host 'SSH 反向隧道已在运行，跳过。' -ForegroundColor DarkGray }

Write-Host '等待引擎就绪（最多 90 秒）…'
$ok = $false
for ($i = 0; $i -lt 45; $i++) {
  try {
    $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$Port/docs"
    if ($r.StatusCode -eq 200) { $ok = $true; break }
  } catch { Start-Sleep -Seconds 2 }
}
if (-not $ok) { throw "引擎 90 秒内没就绪，看日志：$LogDir\gsv_api.log" }
Write-Host '引擎就绪。' -ForegroundColor Green
Write-Host ''
Write-Host '从服务器验证链路（应返回 {"ok": true}）：' -ForegroundColor Cyan
Write-Host "  ssh $SshHost `"curl -s http://172.17.0.1:19881/healthz`""
Write-Host 'QQ 里发：/语音 你好，测试一下' -ForegroundColor Cyan
