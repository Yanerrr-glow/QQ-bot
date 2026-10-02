# QQ_bot 桌面控制台（`desktop/`）

Windows 本地图形化管理端：**独立进程**，经 HTTP API 管理机器人的运行参数、模型档案、
人格、记忆、图片策略与表情包库，并提供一套 manifest 驱动的桌面 UI 插件机制。

本文件讲"怎么用、怎么扩、边界在哪"；架构和模块说明见 [`../docs/开发日志.md`](../docs/开发日志.md)。

---

## 0. 三条边界（先看这个，再动手）

| 边界 | 怎么保证的 |
|---|---|
| **桌面端不碰机器人运行数据** | 只走 `GET/POST /ai/api/*`。`data/runtime/settings.json`、`data/runtime/memory.db`、`data/runtime/chatlog_*.json` 一律不读不写。 |
| **`plugins/` 里不会有 Qt** | 桌面代码在 `desktop/`，与 `nonebot.load_plugins("plugins")` 的扫描路径完全分开（自检项「desktop/ 与 plugins/ 分离」守着）。 |
| **服务器安装不变重** | 桌面依赖单独一份 `../requirements-desktop.txt`、单独一个 `.venv-desktop`。`requirements.txt` 没有 PySide6。 |

---

## 1. 启动

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
& 'E:\工作区\项目\QQ_bot\_工具链\启动\启动控制台.ps1'            # 首次会自动建 .venv-desktop 并装 PySide6
& '..\_工具链\启动\启动控制台.ps1' -Check                        # 只检查依赖与模块（不弹窗）
& '..\_工具链\启动\启动控制台.ps1' -Reinstall                     # 强制重装桌面依赖
```

日常也可双击项目根目录的 `启动桌面控制台.cmd`。控制台会作为独立无控制台进程运行，启动入口退出后仍保持打开；如果启动失败，入口会保留错误信息供查看。

也可以直接跑（已经装好依赖时）：

```powershell
.\.venv-desktop\Scripts\python.exe -m desktop
```

**自检（不需要 PySide6，也不需要机器人在跑）**：

```powershell
python -m desktop selfcheck        # 或 python 验证\_桌面控制台自检.py
```

退出码 0 = 通过。它起了个本地 HTTP 桩服务，把认证、401/410/非 JSON/503、
图片鉴权、目标隔离、凭据、隧道命令行、插件权限与失败隔离都验了一遍。

---

## 2. 连接：本机直连 / 服务器 SSH 隧道

第一次打开去「连接设置」：

| 场景 | 填什么 |
|---|---|
| 机器人和控制台在同一台机器 | 保持默认「本机（直连）」：`http://127.0.0.1:8080` + 前缀 `/ai` |
| 机器人在服务器上 | 新增「服务器（SSH 隧道）」目标 —— **优先用 SSH 别名模式**（见下） |

**服务器目标推荐用「SSH 别名」模式**：项目里 `_工具链\启动\启动隧道.ps1` 早就写过
`Host qqbot` 这类别名（在 `%USERPROFILE%\.ssh\config` 里）。勾上
「用 `~/.ssh/config` 里已有的 Host 别名」，再点「读 ~/.ssh/config」选一个即可 ——
主机/端口/用户/私钥全交给 OpenSSH。好处有两个：**服务器 IP 与私钥路径不用再抄一遍**；
也不会因为 GUI 里多传了 `-p`/`-i` 而与配置打架（那是配 SSH 别名最常见的翻车点）。

要手工填也行：SSH 主机/用户/私钥，远端 `127.0.0.1:8080`，本地端口留 `auto`。

**先做一件事：在服务端 `.env` 里设 `AI_CHAT_WEBUI_AUTH_TOKEN`。**
`config.py:567` 写得很直白：**留空 = 不认证**，此时唯一的安全边界是端口绑在回环上。
不设令牌的话，"401 怎么处理""令牌按目标隔离""隧道不替代认证"这些机制全都测不到。
（本机实测那台服务器当前就是留空的：无令牌探测直接 200，所以令牌框留空即可跑通；
但**只要端口可能被非回环访问到，就该配上**。）

隧道相关的五条约定：

1. **用系统 OpenSSH**（`C:\WINDOWS\System32\OpenSSH\ssh.exe`），不在应用内实现 SSH 协议。
2. **命令行带 `-N -T -o BatchMode=yes -o ExitOnForwardFailure=yes`**：
    `BatchMode` 让认证失败立刻退出而不是挂住等密码；`ExitOnForwardFailure` 让端口被占时立刻报错。
3. **打开控制台默认就把当前服务器目标的隧道连起来**：省掉每次手点一遍。ssh 子进程带
   `CREATE_NO_WINDOW` 启动，**不会再弹出控制台黑窗口**；它的诊断输出实时进「终端」页。
   非隧道目标（本机直连）什么都不做；失败只提示、不打断，仍可手动重试。
4. **隧道失败不回退公网直连**：服务器目标显示离线并禁止写操作。这是刻意的 ——
   悄悄改走公网等于把管理口暴露出去。
5. **退出只回收本程序启动的 ssh 子进程**，不碰你系统里已有的隧道。

「终端」页（与「总览」同级，见 `desktop/gui/page_terminal.py`）就是内嵌 SSH 终端：
隧道诊断输出与服务器命令结果都显示在这里，可停止正在运行的命令或清空显示，
**不会打开独立终端窗口**。它连的是**当前激活目标**（不是连接设置页里正在编辑的那一行）；
「复制 ssh 命令」把命令放进剪贴板（不含密钥内容）。

---

## 3. 配置与令牌存在哪

### 3.1 配置落点（**默认在项目内**）

```
<项目>\.console\
├─ targets.json      连接目标（**只有令牌引用名，没有令牌本体**）
├─ plugins.json      插件的启用状态
├─ secrets.dat       令牌密文（DPAPI 加密；见 3.2）
└─ data\             日志、隧道状态
```

为什么不是在 `%LOCALAPPDATA%`：

1. 实测踩过 —— 受限/被托管的会话里子进程写 `C:\Users\<你>\AppData\Local\QQ_bot_console`
   会直接 `PermissionError: [WinError 5]`，用户按「保存服务器目标」就炸；
2. 放项目内**跟着项目走**：换机器、搬目录，连接配置与日志还在；
3. 这个目录已被开源导出与 `.dockerignore` 排除，不会被误提交。

落点不是硬编码的，而是**候选链逐个写探针文件、第一个成功的才算数**：
`QQBOT_CONSOLE_HOME`（显式指定）→ 项目内 `.console\` → 系统配置目录。
想放别处就设环境变量：

```powershell
$env:QQBOT_CONSOLE_HOME = 'D:\qqbot-console'
$env:QQBOT_CONSOLE_DATA = 'D:\qqbot-console\data'   # 可选，一般不用设
```

**排查命令**（报"存不了目标/存不了令牌"时第一条就看它）：

```powershell
python -m desktop paths      # 打印候选链 + 每个候选是否可写 + 最终落点
```

界面上的「连接设置」页底部也会显示当前配置落点。

### 3.2 令牌保管

令牌存在配置目录下的 `secrets.dat`，按后端分三种：

| 后端 | 什么时候用 | 说明 |
|---|---|---|
| Windows DPAPI | Windows 默认 | 当前用户加密（`CryptProtectData`），文件拷到别的机器/别的用户下解不开 |
| 降级文件 | 非 Windows / DPAPI 不可用 | 明文混淆；界面会标"降级" |
| 仅本次会话 | 手动选「仅本次会话」，或 `QQBOT_CONSOLE_TOKEN` 环境变量 | 只在内存里，退出即失效 |

落盘失败（配置目录只读）时**自动降级成"仅本次会话"并说清楚**，不会假装存好了。

`targets.json` 里**只有引用名**（`qqbot/<目标id>/webui-token`），没有令牌本体，
所以那份配置可以放心备份/贴给别人看。

其它口径：令牌只走 `X-Auth-Token` 请求头（不拼进 URL）；日志出口统一过脱敏
（`desktop/core/util.py` 的 `redact`）；图片经同一会话取字节（服务端那侧靠首次
认证种下的 `ai_chat_webui_token` cookie）。

---

## 4. 目录结构

```
desktop/
├─ core/            非 GUI 核心，**只用标准库**
│   ├─ apiclient.py    HTTP 客户端：前缀拼接、认证头、错误分类、重试策略
│   ├─ targets.py      多目标定义与 targets.json（不含令牌）
│   ├─ credentials.py  DPAPI / 降级文件 / 仅会话 三种凭据后端
│   ├─ tunnel.py       受管 ssh 子进程（起停、保活、断线检测）
│   ├─ connection.py   把"目标+令牌+隧道+客户端"绑成一个当前连接
│   ├─ paths.py        配置/数据落点（**先探可写再用**：项目内 `.console\` 优先，可被环境变量改写）
│   ├─ logsetup.py     带脱敏的 logging 配置
│   └─ util.py         redact / mask / 截断
├─ sdk/             插件 SDK（同样不依赖 Qt）
│   ├─ manifest.py     plugin.json 解析与校验（**先校验，后 import**）
│   ├─ permissions.py  路由 → 权限白名单表
│   ├─ api.py          PluginAPI / PluginContext / 注册表 / 取消令牌
│   └─ loader.py       发现 → 登记 → 激活 → 停用，逐插件错误隔离
├─ gui/             PySide6 界面（唯一 import Qt 的地方）
│   ├─ app.py          进程入口（`--check` 用来自检依赖；打开控制台时触发隧道自动启动）
│   ├─ main_window.py  导航树（QTreeWidget）+ 顶部状态 + 页面栈 + 插件目录（可收起）
│   ├─ widgets.py      配色、控件工厂、后台任务（QThreadPool）、确认框
│   ├─ base.py         页面基类 + `GET /api/state` 取用助手
│   └─ page_*.py       总览 / 终端 / 参数 / 模型 / 人格 / 记忆 / 图片策略 / 表情包 / 连接 / 插件
├─ plugins/         桌面 UI 插件（**不是** NoneBot 插件）
│   ├─ sticker_health/  示范：页面 + 状态卡 + 操作 + 刻意越权被拒
│   └─ memory_stats/    示范：第二个只读插件，证明互不影响
└─ selfcheck/       无头自检（HTTP 桩 + 20 余项断言）
```

---

## 5. 写一个桌面 UI 插件

### 5.1 最小骨架

```
desktop/plugins/my_plugin/
├─ plugin.json
└─ plugin.py
```

`plugin.json`：

```json
{
  "id": "my_plugin",
  "name": "我的插件",
  "version": "1.0.0",
  "api_version": "1",
  "entrypoint": "plugin.py:create_plugin",
  "permissions": ["api.read.status"],
  "navigation": {"section": "插件", "order": 50},
  "enabled_by_default": true
}
```

`plugin.py`：

```python
from desktop.sdk.api import ActionResult   # 需要时把项目根加进 sys.path（看示范插件）

class MyPlugin:
    def manifest(self):
        return {"id": "my_plugin", "name": "我的插件"}

    def register(self, api):
        # 这里**只登记**，不要 new 控件、不要发请求、不要起线程
        api.register_page("overview", "我的页面", self.build_page)
        api.register_status_card("card", "我的卡片", self.card_value, refresh_interval=60)

    def on_activate(self):
        # 主窗口与导航已就绪：这里才允许建界面、订阅信号、起定时器
        pass

    def on_deactivate(self):
        # 断信号、停定时器、取消请求
        pass

    def build_page(self, ctx):
        from PySide6 import QtWidgets
        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        layout.addWidget(QtWidgets.QLabel("你好"))
        return box

    def card_value(self, ctx):
        state = ctx.api.http.get("/api/state")   # 相对路径；绝对 URL 会被拒
        return f"参数 {len(state.get('settings') or [])} 组"

def create_plugin():
    return MyPlugin()
```

### 5.2 调用时序不能颠倒

| 阶段 | 允许做的事 | 禁止的事 |
|---|---|---|
| discovery | 读 `plugin.json` | import 插件代码（manifest 不合法就到这为止） |
| register | `import` + 调 `api.register_*` | new QWidget / 发请求 / 起线程 |
| on_activate | 建页面、订阅、起定时器 | —— |
| on_deactivate | 断信号、停定时器、取消请求 | 留下后台线程 |

为什么较真：`register_page` 只是**登记工厂**，主窗口可能还没建好；
在 register 阶段碰 Qt 就是随机崩溃。

导航里插件页面挂在**「插件」节点下面**（`QTreeWidget` 的子节点），点前面的箭头即可**收起/展开**。

「插件」页的「重新扫描」只重读 manifest：**已经加载过的插件不会再 `register()` 一遍** ——
`Registry` 是宿主那一份，重复 `register_page()` 会直接抛
`ValueError: 页面 id 重复`，插件随即被打成"加载失败"（2026-10-02 实测报回，已修）。
新扫出来的插件只登记为「已加载（未激活）」，重启桌面程序后才真正生效。

### 5.3 插件能拿到什么（也就只有这些）

`PluginContext` 里只有：`api`、`theme`、`locale`、`token`（取消令牌）。
**没有**主窗口、没有连接管理器、没有 `ApiClient`、没有凭据存储、没有数据库。

`PluginAPI` 提供：

| 方法 | 说明 |
|---|---|
| `register_page(id, title, factory, *, icon, order, permission)` | 独立页面 |
| `register_action(id, title, callback, *, placement, permission, confirm, costly)` | 菜单/工具栏操作；`confirm` 非空则宿主先问一句 |
| `register_status_card(id, title, provider, *, refresh_interval)` | 只读状态卡 |
| `register_settings_section(id, title, schema, on_save)` | 插件**本地**偏好（机器人参数仍须走服务端 API） |
| `http.get/post/delete(path, ...)` | 受控请求：只收相对路径，白名单外一律拒 |
| `toast/confirm/open_dialog` | 宿主 UI |
| `logger` | 自动带插件 ID，输出过脱敏 |

**页面工厂里有一条容易踩死的规矩**：只能用 `gui_ready()`（装了 PySide6 **且已有
`QApplication`**）来判断"能不能建控件"，**不要**用 `qt_available()`。
Qt 在没有 `QApplication` 时创建 `QWidget` 是**致命错误**——进程直接以 `0xC0000409`
退出，`try/except` 拦不住。所以：

```python
def build_page(self, ctx):
    body = "……"
    if not gui_ready():                 # 无 GUI：返回纯数据，宿主会包成只读文本
        return {"title": "我的页面", "text": body, "headless": True}
    from PySide6 import QtWidgets
    ...
```

同一个判据对**整个包**都适用：`desktop/gui/__init__.py` 用 PEP 562 惰性导出
`MainWindow`，否则 `python -m desktop selfcheck` 的导入链会拉起 Qt 并静默崩溃。

**第二条规矩：页面工厂里取数必须异步。** `api.http.*` 是同步的，直接写在
`build_page` 里就等于让 GUI 线程干等一次超时（隧道没起来时十几秒，表现就是
"点插件页卡住"）。用 `ctx.api.run_async(fetch, on_done=..., on_error=...)`：
`fetch` 在后台线程跑（**只许算数据，不许碰控件**），回调在 GUI 线程里执行；
没有 UI 环境时自动退化成同步。两个示范插件都是这么写的。

```python
def build_page(self, ctx):
    ...
    text = QtWidgets.QPlainTextEdit("正在读取…")
    ctx.api.run_async(
        lambda: ctx.api.http.get("/api/state"),        # 后台线程
        on_done=lambda data: text.setPlainText(...),   # GUI 线程
        on_error=lambda msg: text.setPlainText(msg),
    )
    return box
```

### 5.4 权限不是装饰

`permissions.py` 里维护"服务端路由 → 需要哪个权限"的**白名单**：

```
POST /api/settings   → api.write.settings
DELETE /api/memory/{item_id} → api.write.memory
POST /api/speak      → api.action.speak
...
```

判定发生在 `PluginAPI.http.*` 每一次调用上，顺序是：
**先查表**（查不到 = 拒绝，白名单而非黑名单）→ **再看 manifest 有没有声明**。
所以：

- 绝对 URL 自动出局；
- 想借宿主客户端把凭据发到外部地址？路径都过不了；
- 服务端将来加了新路由，插件在 SDK 支持前用不了（宁可少，不可多）。

**必须说清的取舍**：`GET /api/state` 是服务端的聚合端点，一次返回
settings/models/persona/memory/image/stickers/status 全部内容。
所以声明任何 `api.read.*` 的插件都能从这个响应里看到其它切片 ——
真要按切片隔离，得先让服务端把 state 拆开（方案第 4.3 节的方向）。

### 5.5 Qt 线程模型（最容易踩的坑）

- **碰 widget 的代码只能在 GUI 线程。** 在自建线程里改控件 = 随机崩溃。
- 后台动作（探活、批量取图、长请求）用 `QThread`/`QRunnable`，结果用 **signal** 回投。
- `PluginAPI.http.*` 是**同步**的；插件自己负责放到工作线程里，别在 GUI 线程里干等 ——
  建页面时直接用 `ctx.api.run_async(...)`（见 5.3）。
- `ctx.token` 是取消令牌：切目标、卸载页面、停用插件时置位，
  长任务必须在下一次循环里检查 `ctx.token.cancelled`。

### 5.6 权限声明不是沙箱

插件在桌面进程里执行的是**任意 Python**。`permissions` 只约束"它通过宿主 API 能做什么"，
不是"它不能做什么"。所以：只加载你信任的来源；第一阶段没有插件市场，也没有远程下载。

---

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| **"保存服务器目标"报 `[WinError 5] 拒绝访问`** | 配置目录写不进去。跑 `python -m desktop paths` 看候选链；现在默认落项目内 `.console\`，若仍报错就用 `$env:QQBOT_CONSOLE_HOME` 指到可写的盘 |
| 令牌"保存"了但下次打开就没了 | 落盘失败已自动降级成"仅本次会话"（界面/提示里会写明）。换可写目录即可持久化 |
| 测试连接报"连接被拒绝" | 机器人没跑 / `.env` 的 `DRIVER` 不含 `~fastapi` / 端口不对 |
| 报"路径不存在（404）" | API 前缀写错（服务端默认 `/ai`，对应 `AI_CHAT_WEBUI_PREFIX`） |
| 报"未认证（401）" | 服务端配了 token 而桌面端没填；或填错。**这不等于"没有数据"** |
| 报"响应不是 JSON" | 这个端口不是机器人服务（比如连到了 NapCat 面板）；NapCat 是另一个端口 |
| 服务器目标一直离线 | 隧道没起来。打开控制台**会自动起隧道**；失败时去「终端」页看 ssh 诊断输出，或点「复制 ssh 命令」手动跑一遍 |
| 隧道报端口被占 | `本地端口` 改成 `auto`，或换一个固定端口 |
| 表情包缩略图取不回来 | 服务端慢或该图已被删；瓦片上会标"取图失败"，不影响其它功能 |
| 插件列表里有"加载失败" | 看「查看详情」里的错误与 traceback 摘要；它不会影响主程序 |
| 禁用插件后还在 | 设计如此：**完全卸载要重启桌面程序**（不热熔，避免幽灵任务） |

---

## 7. 开源导出说明

`_工具链\发布\_导出开源版.ps1` 会把 `desktop/` 下的 `.py/.json/.md` 一起导出到
`项目\Git-open\QQ_bot_open\`（`data/`、`.venv*`、`.pip-tmp/`、`.work-tmp/`、`.console/`、
`__pycache__` 已被排除）。本目录的代码里**没有**真实群号 / QQ 号 / 昵称，
所以不需要额外的脱敏映射条目；导出后按工作区约定跑一次 `_核对脱敏对齐.ps1`
（退出码必须为 0）即可。
