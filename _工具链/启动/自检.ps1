# 启动前体检 —— **薄壳**：真正的检查在 `上手自检.py` 里（跨平台，实现只有那一份）。
# 保留这个名字是因为 `启动机器人.ps1` / `一键启动.ps1` 与文档都按它调用。
#
# 用法： & '.\_工具链\启动\自检.ps1'                  # 检测 + 报告
#        & '.\_工具链\启动\自检.ps1' --fix            # 顺带做能自动做的
#        & '.\_工具链\启动\自检.ps1' --deep --offline # 再跑桩测试 / 不联网
# 退出码：0 = 可以启动；1 = 存在阻塞项。
[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest)

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

if (-not (Test-Path -LiteralPath $checker)) {
    Write-Host ("找不到检查脚本：" + $checker) -ForegroundColor Red
    exit 1
}

# 挑解释器：venv 优先（依赖齐全）；自检本身只用标准库，所以系统 Python 也够用。
$py  = $null
$pre = @()
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPy) {
    $py = $venvPy
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $py = 'py'; $pre = @('-3')          # 绕开 Microsoft Store 那个假的 python3
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = 'python'
}

if (-not $py) {
    Write-Host '找不到 Python，无法自检。' -ForegroundColor Red
    Write-Host '装一个 3.12：https://www.python.org/downloads/' -ForegroundColor Yellow
    Write-Host '或直接双击项目根目录下的 上手自检.exe（自带解释器，不需要装 Python）。' -ForegroundColor Yellow
    exit 1
}

if ($null -eq $Rest) { $Rest = @() }
$argList = @($pre) + @($checker) + @($Rest)
& $py @argList
exit $LASTEXITCODE
