# `_工具链\` 速查

本机脚本，按用途分 4 组。**这一组不进 Docker 镜像**（`.dockerignore` 里整目录排除）；
要进镜像的是顶层的 `验证\`。

| 组 | 什么时候用 | 里头的脚本 |
|---|---|---|
| [`启动\`](启动/) | 装依赖、起机器人、起控制台、连 NapCat、做桌面快捷方式 | 安装依赖 / 自检 / 启动机器人 / 启动控制台 / 启动隧道 / 启动NapCat / 重连NapCat / 一键启动 / 创建桌面快捷方式 / 设置头像 / 预览问候 / 诊断状态 / 上手自检 |
| [`维护\`](维护/) | **动数据**：记忆迁移、修重复画像、补提取、回填摘要、水位线初始化、归属排查 | 全部带 `--dry-run`，先看再改 |
| [`发布\`](发布/) | 导出开源版、核对脱敏对齐、打包 `上手自检.exe` | 三条动作都影响副本仓库，改完要跑核对 |
| [`_作者本机\`](_作者本机/) | 只对作者环境有意义（DSH 桥接 agent） | 判据是"只有这台机器能跑" |
| `_表层落卷验证.py` | 表层人设落卷验证（跨用途，留在组外） | —— |

## 常用命令

```powershell
$env:PYTHONIOENCODING = 'utf-8'          # 日志里有 emoji，GBK 控制台会整条丢

# 启动链路
& '.\_工具链\启动\安装依赖.ps1'           # 建 .venv + 镜像装依赖
& '.\_工具链\启动\自检.ps1'               # 启动前体检（依赖/Key/端口/连通性）
& '.\_工具链\启动\启动机器人.ps1'         # 起机器人
& '.\_工具链\启动\启动控制台.ps1'         # 起桌面控制台（首次自动建 .venv-desktop 装 PySide6）
& '.\_工具链\启动\一键启动.ps1'           # NapCat + 机器人 + 开控制台

# 改完代码该跑的
python '验证\_语法检查.py'                # .py 全量编译 + 镜像/Docker 一致性
python '验证\离线验证_桩.py'              # 纯逻辑回归（无需依赖）
python '验证\_桌面控制台自检.py'          # 桌面端自检（23 项）

# 动数据前（都支持 --dry-run）
python '.\_工具链\维护\_记忆库迁移.py' --dry-run
python '.\_工具链\维护\_修复画像重复.py' --dry-run
```

## 两条约定（改脚本前先看）

1. **项目根从脚本自身位置向上探测**，不写死层级：

   ```powershell
   $ProjectRoot = $PSScriptRoot
   while ($ProjectRoot -and -not (Test-Path (Join-Path $ProjectRoot 'bot.py'))) {
       $ProjectRoot = Split-Path $ProjectRoot -Parent
   }
   ```

   写死 `Split-Path $PSScriptRoot -Parent` 的层数，**下次再分组就会坏**（2026-10-02 踩过一次：
   `启动控制台.ps1` 挪进 `启动\` 之后报"找不到依赖清单"）。

2. **含中文的 `.ps1` 必须带 UTF-8 BOM**：PS 5.1 会按 ANSI 解码，中文乱码会破坏语法。
   用编辑器/脚本改完补回：`[System.IO.File]::WriteAllText($p, $t, (New-Object System.Text.UTF8Encoding($true)))`。

## 历史

2026-10-02：从「43 个散文件」整理成 4 组；进镜像的那批挪到顶层 `验证\`，
于是 Dockerfile 只剩一句 `COPY 验证 ./验证`，以后新增验证脚本不用再改
`Dockerfile` 与 `.dockerignore` 两处（那条坑踩过三次，现在由 `验证/_语法检查.py` 钉着）。
