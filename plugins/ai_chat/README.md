# `plugins/ai_chat` 代码地图

本目录是 NoneBot 机器人插件。入口在 `__init__.py`；桌面控制台代码位于项目根的 `desktop/`，不要把桌面 UI 插件放进本目录。

## 按职责找模块

| 职责 | 主要模块 | 边界 |
|---|---|---|
| 启动与配置 | `__init__.py`、`config.py`、`settings.py` | 插件注册、路径和不可调配置、参数默认值与验证 |
| 对话组装与模型 | `context.py`、`llm.py`、`instructions.py`、`mode.py` | 构造上下文、调用模型、解析实时指令和对话模式 |
| 人格与行为 | `persona.py`、`persona_iter.py`、`persona_eval.py`、`behavior.py`、`identity.py` | 读取人格层、受控更新表层、评估和输出约束 |
| 消息与记忆 | `chatlog.py`、`memory.py`、`memstore.py`、`summaries.py`、`msgindex.py`、`search_memory.py` | 聊天记录、摘要、长期记忆及检索索引 |
| 会话状态与主动任务 | `state.py`、`attention.py`、`proactive.py`、`greetings.py`、`clock.py`、`signals.py`、`task_manager.py` | 可恢复状态、提醒、主动发言、时间与后台任务 |
| 外部内容与安全 | `search.py`、`fetch.py`、`files.py`、`pdf.py`、`netguard.py`、`render.py` | 搜索、网页/文件读取、出站访问校验和渲染回退 |
| 媒体与界面 | `stickers.py`、`perceptual.py`、`webui.py`、`dsh_bridge.py` | 表情包、图像感知、机器人 Web 控制台和本机桥接 |

改动前先找现有职责归属；新模块应有单一职责，并在本表补一行。避免把配置、持久化、网络访问和业务判断集中塞进 `__init__.py`。

## 持久化写入约定

- 运行数据根目录由 `config._data_dir()` / `config.LOG_DIR` 管理；新增运行时文件放在该目录或其明确子目录，不依赖当前工作目录，也不写回源码目录。
- 只读模板、默认人格和注册表是部署资产；聊天记录、状态、缓存、日志和用户上传内容是运行数据。不要混用这两类路径。
- JSON 使用 UTF-8、`ensure_ascii=False`，结构化输出保持稳定缩进；读写失败必须保留可诊断错误，不静默用空默认值覆盖原文件。
- 可整体替换的 JSON/文本状态先写同目录临时文件，再原子替换目标；追加式日志明确使用行格式。多个并发写入者需要锁或单一写入队列。
- SQLite 通过 `memstore.py` 的存储接口和事务写入，不手工拼接数据库文件，也不直接修改其 WAL/SHM 文件。
- 写入路径、文件格式或 schema 改变时，同步更新本文与根 README 的数据文件说明；需要转换旧数据时提供幂等迁移，并保留可恢复的原件。
- 日志不得包含密钥、令牌、认证头或不必要的原始聊天内容。错误日志记录操作、脱敏后的对象标识和异常，不用完整用户内容充当上下文。

## 人格文件写入权属

人格源文件集中在 `persona/active/`，Docker 构建、自检与发布导出使用同一路径。运行时可变人格状态统一放在 `data/runtime/persona/`（由 `config.persona_data_dir()` 提供），与源文件隔离。

| 文件 | 内容与权属 | 写入规则 |
|---|---|---|
| `persona/active/base.txt` | 身份与底层原则；人工维护 | 仅由项目所有者直接编辑，不由模型或后台任务写入 |
| `persona/active/forbidden.txt` | 高优先级禁止事项；人工维护 | 仅由项目所有者直接编辑；调整后检查相关特质和闸门是否仍一致 |
| `persona/active/surface.txt` | 表层人格初始模板；人工提供种子 | 首次初始化时复制到运行数据；已有部署的有效表层在 `data/runtime/persona/surface.txt` |
| `persona/active/traits.json` | 特质、通道、闸门和检查元数据 | 人工维护的结构化登记；不得把聊天原文或真实身份信息加入可发布副本 |
| `data/runtime/persona/surface.txt` | 当前运行表层 | 只允许受闸门校验的自动迭代写入；变更写入人格变更记录并可撤回 |

修改人格源文件后，核对三层优先级、特质登记和实际运行副本。底层与禁止事项在进程初始化时载入，重启后生效；表层由 `data/runtime/persona/surface.txt` 读取。不要把源码模板的变动误认为线上运行数据已更新。

## 变更检查

涉及人格结构时，检查 `验证/_人设结构检查.py` 与 `验证/_行为闸门验证.py` 的覆盖范围，并更新人格文档。涉及数据路径、文件格式或写入逻辑时，检查 `config.py` 中的路径归属、对应读取者/写入者、备份恢复行为和根 README §8。验证脚本是项目维护入口；需要执行检查时按用户任务或工作区规则选择，不在普通文档变更中顺带运行。
