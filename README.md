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
- `persona/packs/<id>/`：**人格包**（一个目录 = 一套完整人格）；内容怎么写见[人格包内容规范](docs/人格包内容规范.md)，样本见 `persona/packs/example/`，脚手架 `persona/_TEMPLATE/`
  - `persona/packs/whale_v2/`：**v2 结构**示例 —— 瘦身 `base.txt` + `vector.json`（五轴情绪）+ `examples/examples.jsonl`（按场景检索的示例）
- `data/runtime/persona/<id>/`：每个人格自己的运行数据（表层、候选池、信号账本、审计；v2 还含 `vector_state.json`（值 + 日账 + 降温热簿 + 幂等表）/ `relations.json` / `examples.jsonl`）

> 行为状态的写入是**事件化**的：一条消息可能带多个事件（被夸、深夜、未来的兑现奖励），
> 它们共用一个入口 `persona_vector.apply_events()`，按项去重、共用一次轴选择，
> 只有已确认的正向事件参与降温（`1/(1+n/k)`，默认 k=12 / 窗口 600s）。
> 同一份状态的所有写入口（含 `reset()`）共用 `filelock.py` 的跨进程锁；
> 审计写失败会进 `audit_pending`，下次写盘补回，不会出现"值记了、证据永久没了"。
>
> 另有两个**实验轴**（`curiosity` 探索意愿 / `stress` 互动负荷，`persona_scoped.py`）：
> 默认关闭、不进线上五轴，作用域是话题/个人，只在 `验证/_行为维度回放.py` 里离线评估；
> 生产接入选不选，以成对判别的结果为准，不是"看起来更丰富"。
>
> 还有一本很小的「没答完的事」账本（`persona_intents.py`）：只记他明确留下的未完成问题，
> 下一轮回复时提一句。有证据才建、有证据才算完，到期只是过期——沉默不扣分、不算拒绝；
> 它不主动外发、不新增模型调用，关掉情绪（`vector_enabled`）也照样工作。
>
> 注入时还会带一句「为什么是这样」，但只在那条证据**能对上本轮/此前某条消息、
> 方向与当前偏离一致、且没被后续反号事件推翻**时才出现；对不上就只说状态，
> 不编由头。面板看的是另一条路径（`recent()`，操作者视角），与聊天注入分开。
- `data/runtime/`：聊天记录、记忆、配置、状态和日志等运行数据
- `验证/`：离线验证与结构检查脚本

> **评估台（#0015）现在分五层落地，默认全部关着**（`eval_cases_enabled` / `eval_replay_enabled` /
> `eval_grading_enabled` / `eval_sampling_enabled` 都是 `False`）：
> `eval_cases.py`（案例与偏好，一案一文件、`source_type` 与 `provenance` 分开）、
> `eval_replay.py`（同源回放，依赖走 `config.runtime_root()` 落沙盒）、
> `eval_grading.py`（硬约束逐例否决 + 行为 + 盲偏好）、`eval_sampling.py`（以 bot 事件为单位取样）、
> `eval_revision_batch` 仍是唯一提案入口。
> 旧分数链（0-100 特质分）已标 `legacy_unavailable`：`deepseek-chat` 的 `max_tokens=1`
> 首 token 不可控（实测出现过 `'你好'`），它只作历史诊断，不参与任何判据。
> 验证入口：`python 验证/_评估案例验证.py` / `_评估回放验证.py` / `_评估裁判验证.py` /
> `_评估反馈验证.py` / `_评估迭代验证.py`（全部离线、退出码 0 = 通过）。

- `_工具链/`：项目专用启动、维护和发布脚本
- `docs/`：详细手册、开发记录和文档规范

运行数据可能包含群聊内容、个人信息和凭据，应妥善保管；不要将 `data/`、`.env` 或真实聊天内容放入公开仓库。

## 文档入口

| 主题 | 文档 |
|---|---|
| **接手先读**（当前事实与未验证项） | [`docs/交接/交接_2026-11-03.md`](docs/交接/交接_2026-11-03.md) |
| 代码分层与功能扩展入口 | [`docs/架构与扩展_2026-10-22.md`](docs/架构与扩展_2026-10-22.md) |
| 减少 AI 腔的人格写法与迭代方案 | [`自然对话与人格迭代_2026-10-22.md`](docs/临时开发文档/自然对话与人格迭代_2026-10-22.md) |
| 人格结构 v2 与行为矢量（方案，未实施） | [`人格结构v2与行为矢量_方案_2026-10-26.md`](docs/临时开发文档/人格结构v2与行为矢量_方案_2026-10-26.md) |
| 语料库与矢量检索（方案，未实施） | [`语料库与矢量检索_方案_2026-11-03.md`](docs/临时开发文档/语料库与矢量检索_方案_2026-11-03.md) |
| 架构、配置、运行机制、数据文件与开发记录 | [`docs/开发日志.md`](docs/开发日志.md) |
| **人格包每一份文件怎么写**（内容规范） | [`docs/人格包内容规范.md`](docs/人格包内容规范.md) |
| **议题与讨论流程**（讨论归 `issues/`，结论归 `docs/`） | [`issues/`](issues/) 本题目的议题（公开副本不含）；协议见 [`../../issues/README.md`](../../issues/README.md) |
| 人格、目录、运行数据及日志写入规范 | [`docs/文档与日志规范.md`](docs/文档与日志规范.md) |
| 服务器部署 | [`deploy/README.md`](deploy/README.md) |
| 机器人源码模块地图 | [`plugins/ai_chat/README.md`](plugins/ai_chat/README.md) |
| 桌面管理端 | [`desktop/README.md`](desktop/README.md) |

许可与第三方依赖信息以仓库随附文件为准。
