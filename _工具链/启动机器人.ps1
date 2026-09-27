# 启动 QQ 机器人：先体检，通过后拉起 bot.py。
# 用法： & '.\_工具链\启动机器人.ps1'
#        & '.\_工具链\启动机器人.ps1' -SkipCheck    # 跳过体检直接启动
[CmdletBinding()]
param(
    [switch]$SkipCheck
)

$ErrorActionPreference = 'Continue'
$root   = Split-Path $PSScriptRoot -Parent
$venvPy = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host "虚拟环境不存在：$venvPy" -ForegroundColor Red
    Write-Host '请先运行： & ''.\_工具链\安装依赖.ps1''' -ForegroundColor Yellow
    exit 1
}

if (-not $SkipCheck) {
    & (Join-Path $PSScriptRoot '自检.ps1')
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host '体检未通过。要强行启动请加 -SkipCheck，但配置问题不会自己消失。' -ForegroundColor Yellow
        exit 1
    }
}

# 控制台按 UTF-8 走。Windows 默认是 GBK 代码页，日志里只要出现 emoji
# （用户人设的回复经常带），loguru 就会抛 UnicodeEncodeError，那一条日志直接丢失。
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }

# bot.py 用相对路径加载 plugins/，必须切到项目根目录再跑。
Set-Location -LiteralPath $root

Write-Host ''
Write-Host '=== 启动机器人（Ctrl+C 停止）===' -ForegroundColor Cyan
Write-Host '日志里出现 WebSocket 连接成功即表示已接上 NapCat。' -ForegroundColor DarkGray
Write-Host ''

# 日志落盘一份：窗口被关掉、或机器人是被别的脚本用新窗口拉起来的时候，
# 还能回头查"为什么没反应"。排查链路问题时这一步很省事。
#
# 不用 Tee-Object：PS 5.1 的 Tee-Object 没有 -Encoding 参数，它按 UTF-16 落盘，
# 会和这里的 UTF-8 表头混进同一个文件，记事本打开就是一片乱码。
$logDir = Join-Path $root 'data'
$null = New-Item -ItemType Directory -Path $logDir -Force -ErrorAction SilentlyContinue
$logFile = Join-Path $logDir 'bot.log'
$esc = [char]27

$writer = New-Object System.IO.StreamWriter($logFile, $true, (New-Object System.Text.UTF8Encoding($false)))
$writer.AutoFlush = $true
$writer.WriteLine("[{0}] ======== 启动 ========" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))

try {
    & $venvPy (Join-Path $root 'bot.py') 2>&1 | ForEach-Object {
        Write-Host $_                                              # 窗口里保留 loguru 配色
        $writer.WriteLine(($_ -replace "$esc\[[0-9;]*m", ''))      # 文件里剥掉 ANSI 转义码
    }
} finally {
    $writer.Close()
}
exit $LASTEXITCODE
