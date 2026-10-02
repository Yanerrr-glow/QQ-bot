# 启动 QQ_bot 桌面控制台。
#
# 做三件事：**找解释器** → **确认 PySide6 在不在**（不在就装，装到项目自带的桌面 venv 里）
# → **起控制台**。
#
# 为什么单独一个 venv（`.venv-desktop`）而不是装进系统 Python：
#   PySide6 是几百 MB 的重依赖，而机器人本体（NoneBot）跑在另一个环境里。
#   混在一起的话，服务器上 `pip install -r requirements.txt` 会被迫拖上 Qt。
#   所以桌面依赖单独一份清单（requirements-desktop.txt）、单独一个环境。
#
# 用法：
#     & '.\_工具链\启动\启动控制台.ps1'               # 正常启动
#     & '.\_工具链\启动\启动控制台.ps1' -Check        # 只检查依赖与模块（不弹窗）
#     & '.\_工具链\启动\启动控制台.ps1' -Reinstall    # 强制重装桌面依赖
#     & '.\_工具链\启动\启动控制台.ps1' -Python <别的解释器路径>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Reinstall,
    [string]$Python = ''
)

# 刻意用 Continue：pip / python 往 stderr 写警告时 Stop 会把它当终止性错误。
$ErrorActionPreference = 'Continue'

# 控制台按 UTF-8 走：日志里一旦出现 emoji，GBK 控制台会让那一条直接丢失。
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

            # 【项目根从脚本自身位置向上探测】不写死层级：脚本按用途分了子目录
            # （`_工具链\<组>\`），写死 `-Parent` 的层数下次再分组就会坏（2026-10-02 踩过）。
            # 判据用"含 bot.py 与 plugins 目录"这一层 —— 与工作区的自定位约定一致。
            $ProjectRoot = $PSScriptRoot
            while ($ProjectRoot -and -not (Test-Path (Join-Path $ProjectRoot 'bot.py'))) {
                $ProjectRoot = Split-Path $ProjectRoot -Parent
            }
$root    = $ProjectRoot

$reqFile = Join-Path $root 'requirements-desktop.txt'
$deskEnv = Join-Path $root '.venv-desktop'
$deskPy  = Join-Path $deskEnv 'Scripts\python.exe'
$mirror  = 'https://mirrors.aliyun.com/pypi/simple/'

# pip 的临时目录**显式指定**在项目内、固定一个名字。
# 为什么：pip 会按 TMP/TEMP 落临时文件，装 PySide6 时那几个解包目录能到 200+ MB；
# 一旦 TMP 被指到项目根，就会在项目里留下一堆 `pip-unpack-*` 之类的东西
# （实测发生过一次，还差点被开源导出脚本扫进去）。固定成 `.pip-tmp` 之后，
# 导出/核对脚本的排除规则也能稳定命中，不随环境变量时好时坏。
$pipTmp = Join-Path $root '.pip-tmp'
if (-not (Test-Path -LiteralPath $pipTmp)) { New-Item -ItemType Directory -Path $pipTmp -Force | Out-Null }
$env:TMP = $pipTmp
$env:TEMP = $pipTmp

Write-Host ''
Write-Host '=== QQ_bot 桌面控制台 ===' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path -LiteralPath $reqFile)) {
    Write-Host ("找不到依赖清单：" + $reqFile) -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 1. 桌面专用解释器
$basePy = $Python
if (-not $basePy) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $basePy = 'py'
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $basePy = 'python'
    }
}
if (-not $basePy) {
    Write-Host '没找到 Python。装一个 3.10+（勾上 Add python.exe to PATH）后重试。' -ForegroundColor Red
    Write-Host '下载：https://www.python.org/downloads/' -ForegroundColor Yellow
    Write-Host '提示：现有桌面虚拟环境的基础 Python 已失效；安装 Python 后重新双击项目根目录「启动桌面控制台.cmd」即可重建。' -ForegroundColor Yellow
    exit 1
}

$pre = @()
if ($basePy -eq 'py') { $pre = @('-3') }

# 只看文件在不在不够：被打断的 venv 会留下跑不起来的 python.exe，必须真执行一次。
$envOk = $false
if ((Test-Path -LiteralPath $deskPy) -and -not $Reinstall) {
    & $deskPy --version *> $null
    $envOk = ($LASTEXITCODE -eq 0)
}

if (-not $envOk) {
    if (Test-Path -LiteralPath $deskEnv) {
        Write-Host '桌面环境不完整，重建中…' -ForegroundColor Yellow
    } else {
        Write-Host '首次运行：正在建桌面专用虚拟环境（一次性）…' -ForegroundColor Cyan
    }
    & $basePy @($pre + @('-m', 'venv', '--clear', $deskEnv))
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $deskPy)) {
        Write-Host '创建虚拟环境失败。' -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------- 2. 桌面依赖
$needInstall = $Reinstall
if (-not $needInstall) {
    & $deskPy -c "import PySide6" *> $null
    $needInstall = ($LASTEXITCODE -ne 0)
}

if ($needInstall) {
    Write-Host '正在安装桌面依赖（PySide6 约 100~200 MB，只装一次）…' -ForegroundColor Cyan
    Write-Host ('  源：' + $mirror) -ForegroundColor DarkGray
    & $deskPy -m pip install --quiet --upgrade pip -i $mirror
    & $deskPy -m pip install --upgrade -r $reqFile -i $mirror
    if ($LASTEXITCODE -ne 0) {
        Write-Host '安装失败。可以手动试：' -ForegroundColor Red
        Write-Host ("  & '" + $deskPy + "' -m pip install -r '" + $reqFile + "' -i " + $mirror) -ForegroundColor Yellow
        exit 1
    }
} else {
    Write-Host '桌面依赖已就绪（PySide6 已装）。' -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 3. 起控制台
Set-Location $root
$args = @('-m', 'desktop')
if ($Check) { $args += @('--check') }

Write-Host ''
if ($Check) {
    Write-Host '只做检查，不打开窗口。' -ForegroundColor DarkGray
} else {
    Write-Host '正在启动桌面控制台（启动进程独立运行，可关闭此窗口）…' -ForegroundColor Green
}
Write-Host ''

if (-not $Check) {
    # GUI 与启动器解耦：启动器退出后，桌面控制台及其 SSH 隧道仍由独立进程管理。
    $pythonw = Join-Path (Split-Path $deskPy -Parent) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) {
        Write-Host ("找不到无控制台解释器：" + $pythonw) -ForegroundColor Red
        exit 1
    }
    Start-Process -FilePath $pythonw -ArgumentList @('-m', 'desktop') -WorkingDirectory $root
    Write-Host '桌面控制台已独立启动；关闭此窗口不会关闭控制台。' -ForegroundColor Green
    exit 0
}

& $deskPy @args
$code = $LASTEXITCODE

if ($Check) {
    if ($code -eq 0) {
        Write-Host ''
        Write-Host '检查通过。' -ForegroundColor Green
    } else {
        Write-Host ''
        Write-Host '检查未通过，看上面的输出。' -ForegroundColor Red
    }
}
exit $code
