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
- 写入路径、文件格式或 schema 改变时，同步更新本文与 `docs/开发日志.md` 的数据文件说明；需要转换旧数据时提供幂等迁移，并保留可恢复的原件。
- 日志不得包含密钥、令牌、认证头或不必要的原始聊天内容。错误日志记录操作、脱敏后的对象标识和异常，不用完整用户内容充当上下文。

## 人格包（一个目录一套人格）

人格从"一份文件"走到了"**一个目录一套**"（2026-10 包化）：

```
persona/
├─ _registry.json          默认激活哪个包（跟镜像走）
├─ _TEMPLATE/              新建人格的脚手架（`_` 前缀 = 不参与扫描）
└─ packs/<id>/
    ├─ _pack.json          身份元数据：显示名 / 别名 / 角色名 / 唤醒词
    ├─ base.txt            底层人设（它是谁）—— 必需
    ├─ forbidden.txt       禁止事项（铁律）—— 必需
    ├─ surface.txt         表层模板（只作首次播种）—— 必需
    └─ traits.json         特质 / 通道 / 闸门关键词 —— 可选
```

**激活顺序**（`packs._resolve_active()`，唯一一处定义）：
`data/runtime/persona/_active`（运行时切换写的）＞ `persona/_registry.json` ＞
`.env` 的 `AI_CHAT_PERSONA_PACK` ＞ 唯一一个启用的包。

**热切换**：`config.switch_persona()` 是唯一入口（群指令 / Web / 桌面三处都走它），
它做三件事 —— 写运行时标记、清所有按人格算出来的缓存、同步身份元数据。
三层正文不再有"import 期常量"，所以**切完立即生效，不用重启**。

**缓存失效的登记口**是 `packs.on_change()`：持有缓存的模块自己在 import 时登记
（`config` 的三层正文与特质注册表、`behavior` 的守卫判定器、`persona` 的变更日志与
候选池、`signals` 的信号账本）。**不要在切换逻辑里挨个 import 别的模块去清缓存** ——
那是反向依赖，新增模块时一定会漏，而漏掉的后果是静默的（新角色带着旧角色的闸门关键词运行）。

**逃生舱**：`AI_CHAT_PERSONA_FILE` / `_FORBIDDEN_FILE` / `_SURFACE_FILE` 显式配了就
绕过人格包、直接读那个路径。出故障时指回 `persona/active/` 即可回退到包化前的行为。

**每一份文件里写什么**（三层怎么分工、条目要什么格式、哪些写法会**静默失效**、
改名要同时改哪三处）见 [`docs/人格包内容规范.md`](../../docs/人格包内容规范.md)。
本文件只讲机制与权属；规范里带「判据」的条目由 `验证/_人格包检查.py` 检查，
照规范写好的完整样本是 [`persona/packs/example/`](../../persona/packs/example/)。

## 人格文件写入权属

人格源文件集中在 `persona/packs/<包>/`，Docker 构建、自检与发布导出使用同一路径。运行时可变人格状态按包隔离在 `data/runtime/persona/<包>/`（由 `config.persona_data_dir()` 提供），与源文件隔离。

| 文件 | 内容与权属 | 写入规则 |
|---|---|---|
| `persona/packs/<包>/_pack.json` | 身份元数据：显示名、别名、角色名、唤醒词 | 人工维护；`bot_name` 必须与 `base.txt` 里的角色名逐字一致（`chatlog._speaker()` 用它判定归属） |
| `persona/packs/<包>/base.txt` | 身份与底层原则；人工维护 | 仅由项目所有者直接编辑，不由模型或后台任务写入 |
| `persona/packs/<包>/forbidden.txt` | 高优先级禁止事项；人工维护 | 仅由项目所有者直接编辑；调整后检查相关特质和闸门是否仍一致 |
| `persona/packs/<包>/surface.txt` | 表层人格初始模板；人工提供种子 | 首次初始化时复制到运行数据；已有部署的有效表层在 `data/runtime/persona/<包>/surface.txt` |
| `persona/packs/<包>/traits.json` | 特质、通道、闸门和检查元数据 | 人工维护的结构化登记；不得把聊天原文或真实身份信息加入副本 |
| `data/runtime/persona/<包>/surface.txt` | 当前运行表层 | 只允许受闸门校验的自动迭代写入；变更写入人格变更记录并可撤回 |
| `data/runtime/persona/<包>/{changelog,candidates,signals,eval}.json` | 审计、候选池、信号账本、评估结果 | **按包分开**：换角色不会把上一个角色的自我学习带过去 |

修改人格源文件后，核对三层优先级、特质登记和实际运行副本。三层都是**每轮现读**（`BASE_PROMPT` / `FORBIDDEN_PROMPT` / `SYSTEM_PROMPT` 是模块级惰性属性），改完不用重启；改的是**哪个包**看 `/人设 状态` 或启动日志的「人格包：…」那一行。不要把源码模板的变动误认为线上运行数据已更新。

## 变更检查

涉及人格结构时，检查 `验证/_人设结构检查.py`（逐包）、`验证/_人格包检查.py`（包结构）与 `验证/_行为闸门验证.py`（闸门本身）的覆盖范围，并更新人格文档。涉及数据路径、文件格式或写入逻辑时，检查 `config.py` 中的路径归属、对应读取者/写入者、备份恢复行为和 `docs/开发日志.md` 的数据文件说明。验证脚本是项目维护入口；需要执行检查时按用户任务或工作区规则选择，不在普通文档变更中顺带运行。
