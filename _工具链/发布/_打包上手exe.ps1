# 把 `上手自检.py` 打成**单文件 exe**：给"机器上还没装 Python"的人一个双击就能跑的入口。
#
# 产物落 `dist\上手自检.exe`（外加一份 `.sha256`），**不进仓库** —— 走 GitHub Release：
# 把它作为 release asset 挂上去，仓库里只留打包脚本与 README 里的下载链接。
# 这样二进制不占 git 历史（二进制是只增不减的：每重打一次就永久多一个 8 MB 的 blob）。
#
# 二进制**没法自动同步**：它把 `上手自检.py` 烤了进去，**改了源码就要重新打一次**。
# `验证\_语法检查.py` 会在源码明显比 exe 新时提醒（只提醒，不算失败 ——
# clone 出来的仓库里所有时间戳都接近，那种情况报"过期"是误报）。
#
# 用法：
#     & '.\_工具链\发布\_打包上手exe.ps1'              # 构建（首次会自动装 PyInstaller）
#     & '.\_工具链\发布\_打包上手exe.ps1' -KeepBuild   # 保留中间产物，排查打包问题用
#
# 约定：PyInstaller **不能跨平台构建** —— 在 Windows 上只能得到 Windows 的 exe。
# 实现只有一份（`上手自检.py`），这个脚本只负责打包，不重复任何检查逻辑。
[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$KeepBuild
)

# 刻意用 Continue 而不是 Stop：原生命令（python / pip）往 stderr 写**警告**时，
# Stop 会把它当成终止性错误直接中断脚本 —— 实测 `python -m venv` 那句
# "Actual environment location may have moved..." 就会触发。所以一律显式查 $LASTEXITCODE。
$ErrorActionPreference = 'Continue'
            # 【项目根从脚本自身位置向上探测】不写死层级：脚本按用途分了子目录
            # （`_工具链\<组>\`），写死 `-Parent` 的层数下次再分组就会坏（2026-10-02 踩过）。
            # 判据用"含 bot.py 与 plugins 目录"这一层 —— 与工作区的自定位约定一致。
            $ProjectRoot = $PSScriptRoot
            while ($ProjectRoot -and -not (Test-Path (Join-Path $ProjectRoot 'bot.py'))) {
                $ProjectRoot = Split-Path $ProjectRoot -Parent
            }
$root    = $ProjectRoot

$checker = Join-Path $PSScriptRoot '上手自检.py'
$dist    = Join-Path $root 'dist'
$outExe  = Join-Path $dist '上手自检.exe'

if (-not (Test-Path -LiteralPath $checker)) {
    Write-Host ("找不到 " + $checker) -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- 1. 挑解释器
$basePy = $Python
if (-not $basePy) {
    $venvPy = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPy) {
        $basePy = $venvPy
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $basePy = 'py'
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $basePy = 'python'
    }
}
if (-not $basePy) {
    Write-Host '找不到 Python（需要 3.9+ 来构建）。' -ForegroundColor Red
    exit 1
}
$pre = @()
if ($basePy -eq 'py') { $pre = @('-3') }
Write-Host ("构建用解释器：" + $basePy) -ForegroundColor Cyan

# ------------------------------------------- 2. 装 PyInstaller（装进临时 venv，不动系统）
# 刻意不往系统 Python 里装东西：构建一次就污染全局环境不划算。
$buildEnv = Join-Path $env:TEMP 'qqbot-exe-build'
$buildPy  = Join-Path $buildEnv 'Scripts\python.exe'

# **只看文件在不在是不够的**：中途被打断的 venv 会留下一个跑不起来的 python.exe
# （实测报 "not a valid application for this OS platform"），必须真运行一次才算数。
$buildOk = $false
if (Test-Path -LiteralPath $buildPy) {
    & $buildPy --version *> $null
    $buildOk = ($LASTEXITCODE -eq 0)
}
if (-not $buildOk) {
    if (Test-Path -LiteralPath $buildEnv) {
        Write-Host '构建环境不完整，重建中...' -ForegroundColor Yellow
    } else {
        Write-Host '正在创建构建用虚拟环境（一次性）...' -ForegroundColor Cyan
    }
    & $basePy @($pre + @('-m', 'venv', '--clear', $buildEnv))
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $buildPy)) {
        Write-Host '创建构建环境失败。' -ForegroundColor Red
        exit 1
    }
}
$probe = & $buildPy -c "import PyInstaller; print(PyInstaller.__version__)" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host '正在安装 PyInstaller（约十几 MB，只装一次）...' -ForegroundColor Cyan
    & $buildPy -m pip install --quiet --upgrade pip
    & $buildPy -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) { Write-Host '安装 PyInstaller 失败。' -ForegroundColor Red; exit 1 }
    $probe = & $buildPy -c "import PyInstaller; print(PyInstaller.__version__)"
}
Write-Host ("PyInstaller " + ($probe | Select-Object -Last 1)) -ForegroundColor DarkGray

# ---------------------------------------------------------------- 3. 打包
$work = Join-Path $env:TEMP 'qqbot-exe-work'
if (Test-Path -LiteralPath $work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
if (Test-Path -LiteralPath $outExe) { Remove-Item $outExe -Force -ErrorAction SilentlyContinue }

Write-Host '正在打包...' -ForegroundColor Cyan
& $buildPy -m PyInstaller `
    --onefile --console --noconfirm --clean `
    --name '上手自检' `
    --distpath $dist `
    --workpath $work `
    --specpath $work `
    $checker
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $outExe)) {
    Write-Host '打包失败。' -ForegroundColor Red
    exit 1
}

if (-not $KeepBuild) {
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}

$size = [math]::Round((Get-Item -LiteralPath $outExe).Length / 1MB, 1)
$sha = (Get-FileHash -LiteralPath $outExe -Algorithm SHA256).Hash.ToLower()
# 校验和单独留一份：Release 页面上贴出来，下载的人能核对拿到的是不是这一份。
# 文件名含中文，**不能用 `-Encoding ascii`**（会把文件名写成 `????`）；
# 也不用 `-Encoding utf8`（PS 5.1 会加 BOM，`sha256sum -c` 认不出来）。
$shaFile = "$outExe.sha256"
$shaText = $sha + '  上手自检.exe' + [Environment]::NewLine
[System.IO.File]::WriteAllText($shaFile, $shaText, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ''
Write-Host ("打包完成：" + $outExe + "（" + $size + " MB）") -ForegroundColor Green
Write-Host ("SHA256：" + $sha) -ForegroundColor Gray
Write-Host ''
Write-Host '下一步（Release 流程）：' -ForegroundColor Cyan
Write-Host '  1. 在 GitHub 上建一个 Release（建议打 tag，如 v1.0.0）'
Write-Host '  2. 把 dist\上手自检.exe 作为 asset 传上去'
Write-Host '  3. 在 Release 说明里贴上上面那行 SHA256'
Write-Host '它自带解释器 —— 下载的人不需要装 Python，双击即可跑完整自检。' -ForegroundColor Gray
Write-Host '注意：exe 检的是**那台机器/那个项目**的 Python 与 venv，与它自身无关。' -ForegroundColor Gray
exit 0
