# 安装依赖：建 venv（Python 3.12）并用阿里云镜像安装 requirements.txt。
# 用法： & '.\_工具链\安装依赖.ps1'
#
# 走镜像的原因：国内直连 pypi.org 经常静默超时，报错信息不直观。
# 不需要镜像就传自己的： & '.\_工具链\安装依赖.ps1' -Mirror 'https://pypi.org/simple/'
[CmdletBinding()]
param(
    [string]$PythonPath = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    [string]$Mirror     = 'https://mirrors.aliyun.com/pypi/simple/'
)

$ErrorActionPreference = 'Stop'
$root    = Split-Path $PSScriptRoot -Parent
$venv    = Join-Path $root '.venv'
$venvPy  = Join-Path $venv 'Scripts\python.exe'
$reqFile = Join-Path $root 'requirements.txt'

Write-Host '=== [1/4] 检查 Python 解释器 ===' -ForegroundColor Cyan
if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-Host "找不到 Python：$PythonPath" -ForegroundColor Red
    Write-Host '请先安装：winget install --id Python.Python.3.12 --scope user' -ForegroundColor Yellow
    exit 1
}
& $PythonPath --version

Write-Host '=== [2/4] 创建虚拟环境 ===' -ForegroundColor Cyan
if (Test-Path -LiteralPath $venvPy) {
    Write-Host "已存在，跳过：$venv" -ForegroundColor DarkGray
} else {
    & $PythonPath -m venv $venv
    if (-not (Test-Path -LiteralPath $venvPy)) { Write-Host 'venv 创建失败' -ForegroundColor Red; exit 1 }
    Write-Host "已创建：$venv"
}

Write-Host '=== [3/4] 升级 pip ===' -ForegroundColor Cyan
& $venvPy -m pip install --upgrade pip -i $Mirror --quiet

Write-Host '=== [4/4] 安装依赖 ===' -ForegroundColor Cyan
& $venvPy -m pip install -r $reqFile -i $Mirror
if ($LASTEXITCODE -ne 0) { Write-Host '依赖安装失败' -ForegroundColor Red; exit $LASTEXITCODE }

Write-Host ''
Write-Host '依赖安装完成。已装包：' -ForegroundColor Green
& $venvPy -m pip list
Write-Host ''
Write-Host '下一步：Copy-Item .env.example .env 并填写 DEEPSEEK_API_KEY' -ForegroundColor Yellow
exit 0
