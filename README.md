# QQ - bot
基于 NoneBot2、NapCat 和 OneBot v11 的 QQ 群机器人。默认使用 DeepSeek，也支持兼容 OpenAI API 的模型服务。

机器人通常在被 @ 或被唤醒时回复，使用持久化聊天记录组织上下文，并提供人格约束、长期记忆、图片与文件读取、联网搜索和本地管理控制台。

桌面管理控制台可从项目根目录双击 `启动桌面控制台.cmd` 启动；关闭启动窗口不会关闭控制台。

## 快速开始

### Windows

1. 安装 Python 3.12，并准备好 NapCat；确保 OneBot 正向 WebSocket 默认监听 `127.0.0.1:6700`。
2. 在项目根目录创建环境配置：

   ```powershell
   Copy-Item .env.example .env
   notepad .env
   ```

   至少填写 DeepSeek API Key、机器人名称、主人 QQ 等必需配置。`.env` 含密钥，不要提交或分享。
3. 安装依赖并启动：

   ```powershell
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
   & '.\_工具链\启动\安装依赖.ps1'
   & '.\_工具链\启动\启动机器人.ps1'
   ```

   也可双击 `start-bot.cmd` 使用交互式启动与自检。
4. 启动后打开 <http://127.0.0.1:8080/ai/>。Web 控制台没有认证，请保持本机访问；远程使用 SSH 隧道。

### Linux / 服务器

参阅 [`deploy/README.md`](deploy/README.md)，其中包含 Docker、NapCat、持久化数据和运维步骤。

## 目录概览

- `plugins/ai_chat/`：机器人业务插件与[源码模块说明](plugins/ai_chat/README.md)
- `desktop/`：独立桌面管理端，使用说明见 [`desktop/README.md`](desktop/README.md)
- `persona/active/`：人格源文件与注册表
- `data/runtime/`：聊天记录、记忆、配置、状态和日志等运行数据
- `验证/`：离线验证与结构检查脚本
- `_工具链/`：项目专用启动、维护和发布脚本
- `docs/`：详细手册、开发记录和文档规范

运行数据可能包含群聊内容、个人信息和凭据，应妥善保管；不要将 `data/`、`.env` 或真实聊天内容放入公开仓库。

## 文档入口

| 主题 | 文档 |
|---|---|
| 架构、配置、运行机制、数据文件与开发记录 | [`docs/开发日志.md`](docs/开发日志.md) |
| 人格、目录、运行数据及日志写入规范 | [`docs/文档与日志规范.md`](docs/文档与日志规范.md) |
| 服务器部署 | [`deploy/README.md`](deploy/README.md) |
| 机器人源码模块地图 | [`plugins/ai_chat/README.md`](plugins/ai_chat/README.md) |
| 桌面管理端 | [`desktop/README.md`](desktop/README.md) |
| 桌面管理端与插件开发 | [`desktop/README.md`](desktop/README.md) |

许可与第三方依赖信息以仓库随附文件为准。
