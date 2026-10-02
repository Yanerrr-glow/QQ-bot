"""人格包：把一个角色需要的**全部静态文件**收进一个可插拔目录。

## 为什么需要它

人格本来是"一份文件"（`persona.txt`），后来按**寿命与权限**切成三层
（底层人设 / 禁止事项 / 表层人设），这是对的 —— 本模块**不动那三层结构**。
剩下的问题是它只有**一份**：

* 换角色 = 覆盖同一批文件，旧角色不可回退；
* 表层的自动学习、候选池、信号账本全挤在 `data/runtime/persona/` 平铺一层，
  换角色后**前一个角色学到的说话方式会被后一个角色继承**；
* 角色名、唤醒词、对主人的称谓散在 `.env` 与 `settings.json` 里，换角色时
  不跟着换 —— 而"聊天记录里哪句是我说的"正是靠角色名判定的（`chatlog._speaker`），
  于是它会对着自己上一轮的话接话，还不报错。

本模块把这三件事一起解决：**一个目录 = 一个角色**。

```
persona/
├─ _registry.json            仓库内的默认激活包（跟镜像走，人可读可手改）
├─ _TEMPLATE/                新建人格的脚手架（`_` 前缀 = 不参与扫描）
└─ packs/
    ├─ whale/                一个包 = 一套完整人格
    │   ├─ _pack.json        身份元数据：名字 / 别名 / 唤醒词 / 上限
    │   ├─ base.txt          底层人设（它是谁）
    │   ├─ forbidden.txt     禁止事项（铁律）
    │   ├─ surface.txt       表层模板（只作首次播种）
    │   ├─ traits.json       特质与闸门元数据
    │   └─ assets/           可选：角色卡等随包资源
    └─ assistant/            另一个包

data/runtime/persona/
├─ _active                   运行时激活标记（一行纯文本，原子替换）
├─ whale/                    本包的运行数据，**互相隔离**
│   ├─ surface.txt           当前表层（自动迭代唯一会写的东西）
│   ├─ changelog.json        变更审计
│   ├─ candidates.json       待采纳候选池
│   ├─ signals.json          人设信号账本
│   └─ eval.json             评估台结果
└─ assistant/…
```

## 真源与优先级

激活的是哪个包，按这个顺序决定：

1. `data/runtime/persona/_active`（运行时切换写在这里，**在卷里，不动镜像**）；
2. `persona/_registry.json` 的 `active`（随镜像走的默认值）；
3. 唯一一个 `enabled` 的包（只有 `whale` 这种单包部署走这条）；
4. `.env` 的 `AI_CHAT_PERSONA_PACK`；
5. 全都没有 → 目录里的第一个包（按名字排序），并记一条 warning。

**为什么优先级最低的是 `.env`**：切换人格是启动期的一件"状态"事，不该要求人
改 `.env` 再重启。而 `.env` 那条留着是给"部署时就想钉死某个包"的场景用的。

## 与旧布局的关系（兼容，不删）

改造前的那三个环境变量（`AI_CHAT_PERSONA_FILE` / `AI_CHAT_FORBIDDEN_FILE` /
`AI_CHAT_SURFACE_FILE`）**全部保留**：显式配了它们就等于"这一层直接读那个路径"，
绕过人格包。这是**逃生舱**，也是出故障时的一键回退路径 —— 别顺手删掉。

## 热切换要动哪些缓存

换包必须让所有"按人格算出来的东西"一起失效，否则新角色会带着旧角色的闸门关键词
运行（`traits.json` 的 `gate_terms` 决定冲突闸门认哪些词），**那是静默的失守**。

所以这里只提供一个登记口 `on_change()`，由**持有缓存的模块自己**在 import 时登记：

| 登记者 | 清什么 | 为什么 |
|---|---|---|
| `config` | `_persona_memo`、`_traits_cache` | 三层正文与特质注册表 |
| `behavior` | `_spec_cache` | 按注册表构造的判定器 |
| `persona` | `_log`、`_pending` 的内存副本 | 变更日志与候选池 |

**不要在切换逻辑里挨个 `import` 别的模块去清缓存** —— 那是反向依赖，
新增模块时一定会漏。登记口是单向的。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("ai_chat.packs")

# 项目根 = plugins/ai_chat/packs.py 往上三层。
# **与 config._ROOT 同一个锚点，写法一致**：这里的模块特意不 import config
# （config 要 import 本模块，反向 import 会成环），所以路径只能各算一次。
_ROOT = Path(__file__).resolve().parent.parent.parent

_PERSONA_ROOT = _ROOT / "persona"
_PACKS_DIR = _PERSONA_ROOT / "packs"
_REGISTRY = _PERSONA_ROOT / "_registry.json"
_TEMPLATE_DIR = _PERSONA_ROOT / "_TEMPLATE"
_ACTIVE_MARKER = "_active"
_RUNTIME_PERSONA = "persona"

# 一个包的四份文件，**逻辑角色 → 包内文件名**。
# 这是全项目唯一的"人格文件清单"：config、验证脚本、导出脚本都从这里取，
# 不要在别处再写一份字面量。
PACK_FILES: dict[str, str] = {
    "base": "base.txt",
    "forbidden": "forbidden.txt",
    "surface": "surface.txt",
    "traits": "traits.json",
}

# 必需项：缺了就不该被当成一个能用的包。
_REQUIRED = ("base", "forbidden", "surface")
# traits.json 允许缺失 —— `persona.py` / `behavior.py` 都有内置回退
# （公开副本按设计就不带它：里面含线上真实对话原话）。
_OPTIONAL = ("traits",)

# 一份宽松的包 id 白名单：**只允许 ASCII**。
# 为什么不用中文目录名：这条 id 会成为外部接口的取值（`/人设 切换 <id>`、
# 环境变量、URL 参数），中文要经过一层 URL/编码转换才不出岔子；
# 显示名走 `_pack.json` 的 `name`，所以不必牺牲可读性。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

_registry_default = ""
_data_dir_cb: Callable[[], Path] | None = None
# `_frozen_on` 与 `_frozen` 分开：**必须能钉死成"没有可用包"**
# （`freeze("")`），否则测试/排查脚本没法验证"无包时退回静态提示词"这条路 ——
# 早先写成 `if _frozen:` 时 `freeze("")` 会被当成"解除钉死"，于是静默地去读注册表。
_frozen_on = False
_frozen: str = ""
_invalidators: list[Callable[[], None]] = []

# `active_id()` 的缓存。**只缓存"读出来的结果"，不缓存文件是否存在** ——
# 切换（`switch()`）会直接写这里，所以不需要靠 stat 去探测。
_active_cache: str | None = None


class PackError(Exception):
    """人格包相关错误。消息是给人看的，调用方可以直接回给用户。"""


# --------------------------------------------------------------------- 配置注入
def configure(*, default: str = "", data_dir: Callable[[], Path] | None = None) -> None:
    """由 `config` 在 import 期注入两样东西。

    **为什么是"注入"而不是本模块自己读**：

    * `default`：`AI_CHAT_PERSONA_PACK` 只有 `config` 拿得到
      （NoneBot2 的 Config 是 extra="allow"，环境变量靠 driver 注入）；
    * `data_dir`：运行数据根由 `config._data_dir()` 算（它还负责旧布局迁移）。
      本模块直接 `import config` 会成环，所以这里收一个**回调**。

    两样都是惰性使用（回调在调用时才执行），所以 config 模块导入到一半时
    调这个函数是安全的。
    """
    global _registry_default, _data_dir_cb
    _registry_default = str(default or "").strip()
    if data_dir is not None:
        _data_dir_cb = data_dir


def runtime_root() -> Path:
    """运行数据根（`data/runtime/`）。没注入就退回项目内的默认位置。"""
    if _data_dir_cb is None:
        return _ROOT / "data" / "runtime"
    return Path(_data_dir_cb())


def runtime_persona_root() -> Path:
    """人格运行数据的根：`data/runtime/persona/`。"""
    return runtime_root() / _RUNTIME_PERSONA


def rel_to_root(path: Path | str) -> str:
    """尽量报**相对项目根**的路径。

    为什么要有它：启动日志、控制台与报错里到处都是"人格文件现在在哪"，
    写绝对路径等于把本机的目录结构抄进日志（公开副本与 issue 里都是噪音）。
    本模块自带一份实现而不复用 `config._rel_to_root`：config 要 import 本模块，
    反向 import 会成环。
    """
    try:
        return str(Path(path).resolve().relative_to(_ROOT)).replace("\\", "/")
    except (ValueError, OSError):
        return str(path)


def active_marker_path() -> Path:
    return runtime_persona_root() / _ACTIVE_MARKER


# --------------------------------------------------------------------- 注册表
def registry_path() -> Path:
    return _REGISTRY


def read_registry() -> dict[str, Any]:
    """读 `persona/_registry.json`；读不动返回空 dict（**不抛**）。

    读不动不该让机器人起不来：调用方会退到"目录里唯一的 enabled 包"。
    但一定会记 warning —— "人格没生效"这类问题排查时第一问就是"它读的哪一份"。
    """
    try:
        raw = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("人格注册表读不动，按未配置处理：%s（%s）", _REGISTRY.name, type(exc).__name__)
        return {}
    return raw if isinstance(raw, dict) else {}


def write_registry(active: str) -> bool:
    """把默认激活包写回注册表。**失败返回 False，不抛**。

    为什么要写它：注册表是"仓库里的默认人格"，跟着镜像走。只写运行时标记也能切，
    但下次换机器/重建镜像时那个默认值会退回上一个 —— 于是"切过又变回去了"。
    只读文件系统（部分容器编排）下写不进去是正常的，调用方据此提示即可。
    """
    payload = {"schema": 1, "active": str(active), "note": "当前默认人格包；可手改，也可由 /人设 切换 写入。"}
    try:
        _PERSONA_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = _REGISTRY.with_name(_REGISTRY.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(_REGISTRY)
        return True
    except OSError as exc:
        logger.warning("人格注册表写不进去（只读文件系统？）：%s（%s）", _REGISTRY, type(exc).__name__)
        return False


# --------------------------------------------------------------------- 激活标记
def _write_marker(pack_id: str) -> bool:
    """原子写运行时激活标记。同目录临时文件 + `replace()`，与表层播种同一套做法。"""
    path = active_marker_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(str(pack_id) + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as exc:
        logger.warning("人格激活标记写不进去：%s（%s）", path, type(exc).__name__)
        return False


def _read_marker() -> str:
    try:
        return active_marker_path().read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


# --------------------------------------------------------------------- 包发现
def _read_manifest(directory: Path) -> tuple[dict[str, Any], str]:
    """读一个包的 `_pack.json`。返回 `(数据, 错误说明)`；错误时数据是能用的那部分。"""
    path = directory / "_pack.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "缺 _pack.json"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {}, f"_pack.json 读不动（{type(exc).__name__}）"
    if not isinstance(raw, dict):
        return {}, "_pack.json 顶层不是对象"
    return raw, ""


def _manifest_view(directory: Path) -> dict[str, Any]:
    """把包目录 + manifest 合成一份"给人看也用得着"的视图。

    **目录名优先于 manifest 里的 `id`**：目录是身份（外部接口按它索引），
    两者不一致时以目录为准并记 warning —— 否则"列表里显示 A、实际加载 B"。
    """
    folder = directory.name
    raw, err = _read_manifest(directory)
    mid = str(raw.get("id") or "").strip()
    if mid and mid != folder:
        logger.warning("包 %s 的 _pack.json 里 id=%s 与目录名不一致，以目录名为准", folder, mid)
    alias = raw.get("aliases")
    return {
        "id": folder,
        "name": str(raw.get("name") or folder).strip() or folder,
        "aliases": [str(x).strip() for x in alias if str(x).strip()] if isinstance(alias, list) else [],
        "description": str(raw.get("description") or "").strip(),
        "bot_name": str(raw.get("bot_name") or "").strip(),
        "master_title": str(raw.get("master_title") or "").strip(),
        "wake_words": (str(raw.get("wake_words")) if isinstance(raw.get("wake_words"), str)
                       else ",".join(str(x) for x in raw.get("wake_words") or [])),
        "enabled": raw.get("enabled", True) is not False,
        "surface_max_chars": raw.get("surface_max_chars"),
        "dir": str(directory),
        "manifest_error": err,
        "manifest_raw": raw,
    }


def list_packs(*, include_disabled: bool = False) -> list[dict[str, Any]]:
    """列出所有可用的包（按 id 排序）。

    跳过 `_` 与 `.` 开头的目录 —— `_TEMPLATE/` 是脚手架、`_registry.json` 是文件，
    都不该被当成"能切过去的人格"。
    """
    out: list[dict[str, Any]] = []
    if not _PACKS_DIR.is_dir():
        return out
    for child in sorted(_PACKS_DIR.iterdir()):
        if not child.is_dir() or child.name.startswith("_") or child.name.startswith("."):
            continue
        view = _manifest_view(child)
        if not view["enabled"] and not include_disabled:
            continue
        out.append(view)
    return out


def pack_ids(*, include_disabled: bool = False) -> list[str]:
    return [x["id"] for x in list_packs(include_disabled=include_disabled)]


def resolve_id(token: str) -> str | None:
    """把用户输入的 token 解析成包 id：支持 id、显示名与别名（大小写不敏感）。

    为什么需要它：`/人设 切换 鲸鱼娘` 比 `/人设 切换 whale` 更符合直觉，
    而显示名恰恰是中文的。含混时按"精确 id > 精确显示名 > 别名"顺序取第一个命中。
    """
    want = str(token or "").strip()
    if not want:
        return None
    pool = list_packs(include_disabled=True)
    low = want.lower()
    for key in ("id", "name"):
        for view in pool:
            if str(view[key]).lower() == low:
                return str(view["id"])
    for view in pool:
        if any(str(a).lower() == low for a in view["aliases"]):
            return str(view["id"])
    return None


def pack_dir(pack_id: str) -> Path:
    return _PACKS_DIR / str(pack_id)


def _normalize_id(pack_id: str) -> str:
    got = resolve_id(pack_id)
    return got if got else str(pack_id or "").strip()


def pack_file(pack_id: str, role: str) -> Path:
    """某个逻辑角色在包内的路径（**不保证存在**，调用方自己判）。"""
    if role not in PACK_FILES:
        raise PackError(f"未知的人格文件角色：{role}")
    return pack_dir(_normalize_id(pack_id)) / PACK_FILES[role]


def file_role_map(pack_id: str) -> dict[str, Path]:
    """`{"base": Path, ...}` —— 排查与验证脚本要用的整份视图。"""
    return {role: pack_file(pack_id, role) for role in PACK_FILES}


# --------------------------------------------------------------------- 校验
def validate(pack_id: str) -> dict[str, Any]:
    """检查一个包**能不能用**。返回 `{"ok", "id", "errors", "warnings", "files"}`。

    判据分两档：

    * **errors（不能切过去）**：目录/`_pack.json` 缺失、id 不合法、
      `base.txt` / `forbidden.txt` / `surface.txt` 缺、base 是空的。
      为什么 base 不能空：冲突闸门的 `base_similar` / `base_pronoun` 两条检查
      **以底层人设为依据**，空了就没有依据，自动迭代会被 `reflect_once` 直接拒跑
      （见 `persona_iter`），所以那不是一个"能用的包"。
    * **warnings（能切，但要提醒）**：缺 `traits.json`（闸门回退内置表）、
      `_pack.json` 里 `bot_name` 与 `base.txt` 里的角色名对不上。
      最后这条不是形式主义：`chatlog._speaker()` 用角色名 + QQ 号判定
      "哪句是我说的"，对不上会让它认不出自己刚说过的话。
    """
    raw_id = str(pack_id or "").strip()
    errors: list[str] = []
    warnings: list[str] = []
    directory = pack_dir(raw_id)

    if not _ID_RE.match(raw_id):
        errors.append("id 只能用 ASCII 字母/数字/下划线/连字符，且不超过 32 字符")
    if not directory.is_dir():
        errors.append(f"目录不存在：persona/packs/{raw_id}")
        return {"ok": False, "id": raw_id, "errors": errors, "warnings": warnings, "files": {}}

    raw, err = _read_manifest(directory)
    if err:
        errors.append(err)

    files = file_role_map(raw_id)
    for role in _REQUIRED:
        if not files[role].is_file():
            errors.append(f"缺 {PACK_FILES[role]}")
    for role in _OPTIONAL:
        if not files[role].is_file():
            warnings.append(f"缺 {PACK_FILES[role]}：闸门会回退到内置默认表")

    base_text = ""
    if files["base"].is_file():
        try:
            base_text = files["base"].read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"base.txt 读不动（{type(exc).__name__}）")
        if not base_text:
            errors.append("base.txt 是空的：冲突闸门没有判定依据，自动迭代会被拒跑")

    bot_name = str(raw.get("bot_name") or raw.get("name") or "").strip()
    if bot_name and base_text and bot_name not in base_text:
        warnings.append(f"_pack.json 的角色名「{bot_name}」没出现在 base.txt 里"
                        "（聊天记录的归属判定按这个名字）")
    if not bot_name:
        warnings.append("_pack.json 没有 bot_name：切过去之后机器人显示名不会跟着换")

    return {
        "ok": not errors,
        "id": raw_id,
        "errors": errors,
        "warnings": warnings,
        "files": {k: str(v) for k, v in files.items()},
    }


# --------------------------------------------------------------------- 激活
def _resolve_active() -> str:
    """按优先级决定激活的包（见模块开头的真源列表）。

    **每一级被跳过的原因都要说出来**：跳到后面去"随便挑一个包"的表现是
    "人格莫名其妙换成了另一个"，而日志里只有这一处能说清为什么 ——
    实测踩过一次：注册表指向一个被删掉的包，于是整套用例都跑在另一个包上，
    报的却是"找不到「鲸鱼娘」"这种看不出根因的错。
    """
    available = pack_ids()
    if not available:
        if _read_marker() or str(read_registry().get("active") or "").strip():
            logger.warning("persona/packs/ 下一个可用的包都没有，但标记/注册表里还写着包名")
        return ""

    marker = _read_marker()
    if marker and marker in available:
        return marker
    if marker:
        logger.warning("运行时标记指向的包不在目录里，改按注册表/默认值处理：%s", marker)

    registry_active = str(read_registry().get("active") or "").strip()
    if registry_active and registry_active in available:
        return registry_active
    if registry_active:
        logger.warning("注册表里的 active=%s 在 persona/packs/ 下不存在，忽略", registry_active)

    if _registry_default and _registry_default in available:
        return _registry_default
    if _registry_default:
        logger.warning("AI_CHAT_PERSONA_PACK=%s 在 persona/packs/ 下不存在，忽略", _registry_default)

    if len(available) == 1:
        return available[0]
    logger.warning("没有配置激活的人格包，按名字取第一个：%s（可用：%s）",
                   available[0], "、".join(available))
    return available[0]


def active_id() -> str:
    """当前激活的包 id。**热切换的核心入口** —— 每次调用都可能返回新的值。

    缓存的意义只是省掉重复读标记文件；`switch()` 会同步写缓存，
    所以"切完立刻读"一定拿到新值。
    """
    global _active_cache
    if _frozen_on:
        return _frozen
    if _active_cache is None:
        _active_cache = _resolve_active()
    return _active_cache


def freeze(pack_id: str | None) -> None:
    """把激活包钉死（给测试与"只想看某一个包"的排查脚本用）。

    `freeze(None)` 解除；`freeze("")` 钉死成**没有可用包**（验兜底路径用）。
    **钉死时不再读标记文件与注册表** —— 否则测试写到一半被别的用例改了标记就会串。
    """
    global _frozen_on, _frozen, _active_cache
    if pack_id is None:
        _frozen_on = False
        _frozen = ""
        _active_cache = None
        return
    _frozen_on = True
    _frozen = str(pack_id).strip()
    _active_cache = _frozen


def is_frozen() -> bool:
    return _frozen_on


def active_pack() -> dict[str, Any]:
    """当前激活包的视图（列表里的那一份形状）。包不存在时返回一个"空的但可用"的字典。"""
    pid = active_id()
    if not pid:
        return {"id": "", "name": "", "aliases": [], "description": "", "bot_name": "",
                "master_title": "", "wake_words": "", "enabled": True, "dir": "",
                "manifest_error": "没有可用的人格包", "manifest_raw": {}}
    return _manifest_view(pack_dir(pid))


def active_file(role: str) -> Path:
    """当前激活包里某个逻辑角色的路径（**不保证存在**）。"""
    return pack_file(active_id(), role)


def seeded_stage_file(pack_id: str, role: str) -> Path:
    """某个包**播种后**的运行数据文件名（现在只用于表层）。

    单独抽出来是为了让"给还没激活的包先播个种"这条路不必去改激活状态 ——
    改激活状态会顺带跑一遍缓存失效回调，那不是播种该干的副作用。
    """
    return stage_dir(pack_id) / PACK_FILES[role]


# --------------------------------------------------------------------- 热切换
def on_change(fn: Callable[[], None]) -> None:
    """登记一个"人格换了一定要清缓存"的回调。

    约定：**回调必须自己保证不抛**（拿不准就整段 try）。这里还会兜一层，
    但那种兜底只保证"切换本身不被拖垮"，缓存漏清是静默故障，兜不住。
    """
    if callable(fn) and fn not in _invalidators:
        _invalidators.append(fn)


def _invalidate() -> None:
    for fn in list(_invalidators):
        try:
            fn()
        except Exception:  # noqa: BLE001 - 清缓存失败绝不拖垮切换本身
            logger.exception("人格缓存失效回调失败：%r", fn)


def switch(pack_id: str, *, sync_registry: bool = True) -> dict[str, Any]:
    """切换到另一个包（**热切换**：下一次组装 prompt 就是新人格）。

    返回 `{"ok", "id", "from", "errors", "warnings", "registry_written", "marker_written"}`。

    做了四件事，顺序有讲究：

    1. **先校验**（`validate`）—— 校验不过就一个字都不写，保持原状；
    2. 写运行时标记（**以它为准**），再写注册表（失败只警告：只读文件系统也得能切）；
    3. 清 `_active_cache` 并跑所有失效回调 —— 必须**在写标记之后**，
       否则回调里如果读了 `active_id()` 会重新把旧值缓存回来；
    4. 由调用方决定要不要同步身份元数据（`identity_patch()`）——
       本模块不碰 `settings.json`，那是调用方与用户打交道的地方。

    注意：**不校验"和当前是同一个包"**。同包重切是有用的（重新播种表层、
    清掉被手工改脏的内存副本），所以这里只把 `from == id` 报出去。
    """
    want = str(pack_id or "").strip()
    resolved = resolve_id(want) or want
    report = validate(resolved)
    before = active_id()
    out: dict[str, Any] = {
        "ok": False,
        "id": resolved,
        "from": before,
        "errors": list(report["errors"]),
        "warnings": list(report["warnings"]),
        "registry_written": False,
        "marker_written": False,
    }
    if not report["ok"]:
        return out

    global _active_cache
    out["marker_written"] = _write_marker(resolved)
    if sync_registry:
        out["registry_written"] = write_registry(resolved)
    _active_cache = resolved
    if _frozen_on:
        # 钉死状态下切换没有意义，但也不该假装成功。
        logger.warning("人格已被 freeze(%s) 钉死，切换记入缓存但 active_id() 仍返回钉死的值", _frozen)
    _invalidate()
    out["ok"] = True
    if before != resolved:
        logger.info("人格已切换：%s → %s", before or "(无)", resolved)
    return out


# --------------------------------------------------------------------- 身份元数据
def identity_patch(pack_id: str) -> dict[str, str]:
    """这个包想同步到 `settings.json` 的身份参数。

    只回**非空**的项：留空 = "不干预"，由 `.env` / 用户手改的值说了算。
    这样切换不会把用户自己填的名字/唤醒词清掉。
    """
    view = _manifest_view(pack_dir(_normalize_id(pack_id)))
    out: dict[str, str] = {}
    if view["bot_name"]:
        out["bot_name"] = view["bot_name"]
    if view["wake_words"]:
        out["wake_words"] = view["wake_words"]
    if view["master_title"]:
        out["master_title"] = view["master_title"]
    return out


# --------------------------------------------------------------------- 运行数据
_RUNTIME_FILES = ("surface.txt", "changelog.json", "candidates.json", "signals.json", "eval.json")


def stage_dir(pack_id: str | None = None) -> Path:
    """某个包的运行数据目录：`data/runtime/persona/<id>/`。

    这就是"每个包自己的舞台"——**表层是这里唯一会被自动迭代写的东西**，
    而它按包隔开，所以 A 人格学到的说话方式不会被 B 人格继承。
    """
    pid = _normalize_id(pack_id) if pack_id else active_id()
    return runtime_persona_root() / str(pid)


def migrate_layout() -> dict[str, Any]:
    """把改造前的**平铺**运行数据搬进当前包的子目录。幂等。

    背景：改造前所有运行数据都在 `data/runtime/persona/` 这一层平铺
    （`surface.txt` / `changelog.json` / `candidates.json` / `signals.json` /
    `eval.json`），因为它们那时只服务**一个**人格。包化之后必须按包分开，
    否则切过去的第一个人格会继承上一个的自我学习成果。

    安全约定：

    * 目标同名时**不覆盖**，改写成 `.legacy` 并记 error（与
      `config._migrate_data_layout` 的"冲突就停下来说清楚"同一套态度）；
    * 移动前先把这一层整体**复制**一份到 `data/runtime/_backup_<日期>_人格包迁移/`；
    * 一个都不是平铺状态时直接返回"没有要迁的"（幂等）。

    返回 `{"migrated": [...], "conflicts": [...], "backup": str}`。
    """
    root = runtime_persona_root()
    pid = active_id()
    result: dict[str, Any] = {"migrated": [], "conflicts": [], "backup": "", "target": ""}
    if not pid or not root.is_dir():
        return result

    pending: list[tuple[Path, Path]] = []
    for name in _RUNTIME_FILES:
        source = root / name
        if not source.is_file():
            continue
        pending.append((source, root / pid / name))
    if not pending:
        return result

    target_dir = root / pid
    result["target"] = str(target_dir)

    # 先备份原件（复制，不是移动）：迁移逻辑一旦有 bug，原件还在。
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = runtime_root() / f"_backup_{stamp}_人格包迁移"
    try:
        backup.mkdir(parents=True, exist_ok=True)
        for source, _ in pending:
            (backup / source.name).write_bytes(source.read_bytes())
        result["backup"] = str(backup)
    except OSError as exc:
        # 备份失败就**不迁**：宁可不迁，也不能在没有退路的情况下动运行数据。
        logger.error("人格包迁移的备份失败，本次不迁移：%s（%s）", backup, type(exc).__name__)
        result["conflicts"].append(f"备份失败（{type(exc).__name__}），已放弃迁移")
        return result

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("建人格运行数据目录失败：%s（%s）", target_dir, type(exc).__name__)
        return result

    for source, target in pending:
        if target.exists():
            keep = target.with_name(target.name + ".legacy")
            try:
                source.replace(keep)
                result["conflicts"].append(f"{source.name} → {keep.name}（目标已存在，未覆盖）")
                logger.error("人格运行数据同名，已挪到 %s（**没有覆盖**任何一边）", keep)
            except OSError as exc:
                logger.error("人格运行数据改名失败：%s（%s）", source.name, type(exc).__name__)
            continue
        try:
            source.replace(target)
            result["migrated"].append(source.name)
        except OSError as exc:
            logger.error("人格运行数据迁移失败：%s（%s）", source.name, type(exc).__name__)

    if result["migrated"]:
        logger.info("人格运行数据已按包归位（%d 项 → %s，备份在 %s）",
                    len(result["migrated"]), target_dir, backup.name)
    return result


def stats() -> dict[str, Any]:
    """当前人格包的现状（启动日志、`/人设 状态`、控制台都用它）。"""
    pid = active_id()
    view = active_pack()
    available = pack_ids()
    return {
        "id": pid,
        "name": view["name"],
        "aliases": view["aliases"],
        "description": view["description"],
        "bot_name": view["bot_name"],
        "master_title": view["master_title"],
        "wake_words": view["wake_words"],
        "dir": view["dir"],
        "files": {role: str(path) for role, path in file_role_map(pid).items()} if pid else {},
        "stage": str(stage_dir(pid)) if pid else "",
        "available": available,
        "count": len(available),
        "frozen": (_frozen if _frozen_on else None),
        "registry": str(_REGISTRY),
        "registry_written_at": _registry_mtime(),
        "manifest_error": view.get("manifest_error", ""),
    }


def _registry_mtime() -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_REGISTRY.stat().st_mtime))
    except OSError:
        return ""


def describe() -> str:
    """`/人设 包` 的正文：当前包 + 可用包一览。"""
    st = stats()
    if not st["id"]:
        return ("还没有任何可用的人格包。\n"
                f"把人格文件放进 `persona/packs/<id>/`（可照抄 `persona/_TEMPLATE/`），"
                f"然后 `/人设 切换 <id>`。注册表：{st['registry']}")
    rows = [
        f"【当前人格包】{st['name']}（id={st['id']}）",
        f"  目录：{st['dir']}",
        f"  运行数据：{st['stage']}",
    ]
    if st["description"]:
        rows.append(f"  说明：{st['description']}")
    if st["bot_name"]:
        rows.append(f"  角色名：{st['bot_name']}"
                    + (f"（别名：{'、'.join(st['aliases'])}）" if st["aliases"] else ""))
    if st["wake_words"]:
        rows.append(f"  唤醒词：{st['wake_words']}")
    if st["manifest_error"]:
        rows.append(f"  ⚠ {st['manifest_error']}")
    if st["frozen"]:
        rows.append(f"  ⚠ 已被 freeze({st['frozen']}) 钉死：切换不会改变实际加载的包")
    rows.append("")
    rows.append(f"可用的人格包（共 {st['count']} 个）：")
    for view in list_packs(include_disabled=True):
        mark = "→ " if view["id"] == st["id"] else "  "
        state = "" if view["enabled"] else "［已停用］"
        rows.append(f"{mark}{view['id']}  {view['name']}{state}"
                    + (f" —— {view['description']}" if view["description"] else ""))
    rows.append("")
    rows.append("切换：/人设 切换 <id>（也可用显示名或别名）。切换**立即生效**，不用重启。")
    return "\n".join(rows)


# --------------------------------------------------------------------- 迁移（静态资产）
def legacy_active_dir() -> Path:
    """改造前的人格源文件目录。**只在回退/迁移提示里用**，不再作为真源。"""
    return _PERSONA_ROOT / "active"


def stage_source_fingerprint() -> str:
    """当前包静态文件的指纹（`/人设 状态` 与排查用：确认"读的到底是哪一份"）。"""
    pid = active_id()
    if not pid:
        return ""
    digest = hashlib.sha256()
    for role in sorted(PACK_FILES):
        path = pack_file(pid, role)
        digest.update(role.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()[:12]


def os_env_pack() -> str:
    """`AI_CHAT_PERSONA_PACK` 直接读一次环境变量（诊断脚本用，不走 nonebot Config）。"""
    return str(os.environ.get("AI_CHAT_PERSONA_PACK") or "").strip()
