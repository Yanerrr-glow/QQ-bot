"""ai_chat 插件的全部配置项。集中一处，供 __init__ 与 chatlog 共用。

所有值来自 .env（NoneBot2 把 .env 注入 driver.config）。
NoneBot2 的 Config 模型是 extra="allow"，所以自定义字段能原样取到。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from nonebot import get_driver

from . import packs, settings

_cfg = get_driver().config

logger = logging.getLogger("ai_chat.config")

# 项目根 = plugins/ai_chat/config.py 往上三层
_ROOT = Path(__file__).resolve().parent.parent.parent


def _migrate_data_layout(legacy: Path, runtime: Path) -> None:
    """把旧的平铺 data/ 幂等迁到 data/runtime/；遇到同名冲突就停止启动。"""
    if not legacy.is_dir():
        return
    runtime.mkdir(parents=True, exist_ok=True)
    destinations = {
        "persona_surface.txt": runtime / "persona" / "surface.txt",
        "persona_changelog.json": runtime / "persona" / "changelog.json",
        "persona_candidates.json": runtime / "persona" / "candidates.json",
        "persona_signals.json": runtime / "persona" / "signals.json",
        "persona_eval.json": runtime / "persona" / "eval.json",
        "bot.log": runtime / "logs" / "bot.log",
    }
    pending: list[tuple[Path, Path]] = []
    conflicts: list[str] = []
    for source in legacy.iterdir():
        if source.resolve() == runtime.resolve():
            continue
        target = destinations.get(source.name, runtime / source.name)
        if target.exists() and source.name == "bot.log":
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target = target.with_name(f"bot_legacy_{stamp}.log")
        if target.exists():
            conflicts.append(f"{source.name} -> {target.relative_to(runtime)}")
            continue
        pending.append((source, target))
    if conflicts:
        message = "旧 data/ 与 data/runtime/ 存在同名数据，未覆盖任何文件：" + "; ".join(conflicts)
        logger.error(message)
        raise RuntimeError(message)
    for source, target in pending:
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    if pending:
        logger.info("旧运行数据已迁移到 data/runtime/（%d 项）", len(pending))


def _as_bool(value: object, default: bool) -> bool:
    """NoneBot2 从 .env 读到的可能是字符串，统一成 bool。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 模型
API_KEY: str = getattr(_cfg, "deepseek_api_key", "") or ""
BASE_URL: str = getattr(_cfg, "deepseek_base_url", "https://api.deepseek.com")
# 兜底值跟 `.env.example` 与 `settings.py` 的 Spec 默认值保持一致（都是 deepseek-flash）。
# 原来这里写的是 `deepseek-chat` —— 那是旧名，现在**不在 `/models` 列表里**（虽然仍可调用），
# .env 没写这一项时会静默用一个未公开的别名，三处默认值互相不一致也容易被误读。
MODEL: str = getattr(_cfg, "deepseek_model", "deepseek-flash")
TIMEOUT: float = _as_float(getattr(_cfg, "ai_chat_timeout", 60), 60.0)
MAX_CONCURRENCY: int = _as_int(getattr(_cfg, "ai_chat_max_concurrency", 4), 4)

# ---------------------------------------------------------------- 触发
PREFIX: str = getattr(_cfg, "ai_chat_prefix", "") or ""
GROUP_WHITELIST: list[int] = [
    int(x) for x in (getattr(_cfg, "ai_chat_group_whitelist", []) or [])
]
RECORD_ALL: bool = _as_bool(getattr(_cfg, "ai_chat_record_all", True), True)

# 机器人自己在聊天记录里的显示名（与人设保持一致）
BOT_NAME: str = getattr(_cfg, "ai_chat_bot_name", "鲸鱼娘") or "鲸鱼娘"

# 聊天记录里给"机器人自己说过的话"加的标记。**别改成空串** ——
# 模型分不清"这句是我说的"和"这句是别人说的"，就会对着自己上一轮的回复接话，
# 或者把别人夸它的话当成自己说过的。见 chatlog._speaker 与 context.record_legend。
BOT_SELF_LABEL: str = getattr(_cfg, "ai_chat_bot_self_label", "（你）") or "（你）"


# 这里原来有个 `_load_persona()`，只读一个 `persona.txt`。
# 人格分层之后它被下面的 `_read_text_file` + 三层常量取代。
#
# **那个函数留下的教训必须记住**：NoneBot2 的 Config 是 extra="allow"，
# 字段**没配时取到的是 None 而不是"没有这个属性"** —— 所以
# `getattr(cfg, name, "默认值")` 的默认值**永远不会生效**。
# 历史事故：写成 `getattr(..., "persona.txt") or ""` 之后 persona.txt 被静默跳过，
# 机器人退化成一句内置通用提示词，而日志里一个字都没有。
# 现在三个文件名的取值一律 `getattr(cfg, name, None)` 再显式分流 None / ""。


_FALLBACK_PROMPT = "你是一个友好、简洁的群聊助手。回答控制在 300 字以内，能分点就分点。"

def _read_text_file(path: Path, *, label: str) -> str:
    """读一个 UTF-8 文本文件，读不动就返回空串并说一声。"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logging.getLogger("ai_chat.config").warning("%s 读不到：%s", label, path)
        return ""
    except UnicodeDecodeError:
        logging.getLogger("ai_chat.config").warning("%s 不是 UTF-8：%s", label, path)
        return ""


def _persona_path(configured: str) -> Path:
    path = Path(configured)
    return path if path.is_absolute() else _ROOT / path


# ------------------------------------------------------------ 人格源在哪里（一个解析口）
# 人格源文件有**两种**来源，且优先级明确：
#
#   1. 显式配置的路径（`.env` 的 AI_CHAT_PERSONA_FILE / _FORBIDDEN_FILE / _SURFACE_FILE
#      / AI_CHAT_TRAITS_FILE）—— 配了就按它，绕过人格包。这是**逃生舱**，
#      也是出故障时的一键回退路径：三个变量指回旧目录即可回到改造前的行为。
#   2. 当前激活的**人格包**：`persona/packs/<id>/<文件>`。
#      一个包 = 一个角色需要的全部静态文件；`_registry.json` 记默认值，
#      `data/runtime/persona/_active` 记运行时切换的结果。见 `packs.py`。
#
# **为什么把这张表放在这里而不是各模块各拼一次**：
# 原来 `_PERSONA_CONFIGURED` / `_FORBIDDEN_CONFIGURED` / `_TRAITS_NAME` 三处各存一份
# 字面量，加一个角色就要改三处、还容易漏。现在只有这一张表 + 包内文件名表
# （`packs.PACK_FILES`），别处一律走 `persona_file_path()` 这类函数。
def _spec_override(role: str) -> str:
    """某层的显式路径配置。**必须区分 None 与空串**。

    NoneBot2 的 Config 是 extra="allow"，字段没配时取到的是 **None 而不是"没有这个属性"**，
    所以 `getattr(cfg, name, "默认值")` 的默认值**永远不会生效**。
    历史事故：写成 `getattr(..., "persona.txt") or ""` 之后 persona.txt 被静默跳过，
    机器人退化成一句内置通用提示词，而日志里一个字都没有。
    """
    field = {
        "base": "ai_chat_persona_file",
        "forbidden": "ai_chat_forbidden_file",
        "surface": "ai_chat_surface_file",
        "traits": "ai_chat_traits_file",  # 新增：给"注册表放别处"留的口子
    }[role]
    raw = getattr(_cfg, field, None)
    return "" if raw is None else str(raw).strip()


def persona_source_path(role: str) -> Path:
    """某层人格文件的**权威路径**（不保证存在）。

    这是全项目唯一的人格路径解析口：`persona_file_path()` / `forbidden_file_path()` /
    `surface_seed_path()` / `traits_file_path()` 都是它的薄封装，
    各模块不要再自己 `Path(...)` 拼一遍。
    """
    configured = _spec_override(role)
    if configured:
        return _persona_path(configured)
    return packs.active_file(role)


def _data_dir() -> Path:
    """运行时数据目录（`data/runtime/`，容器里位于持久化 data 卷中）。

    **为什么单独抽一个函数**：`LOG_DIR` 在文件后半段才定义，而人格路径
    在模块导入期就可能被读到（`packs` 的激活标记、表层播种）——
    否则导入时就 `NameError`（实测踩过）。`LOG_DIR` 与 `packs` 都复用它，
    避免三处各算一遍。

    ⚠ **它每次现读 `_cfg`，不缓存结果**：`验证\离线验证_桩.py` 会在导入之后
    改 `AI_CHAT_LOG_DIR` 来把落盘接到临时目录，缓存住就会把测试数据写进真实 `data/`
    （那个坑踩过一次：3300 多行用例因此从没被执行过）。
    """
    configured = getattr(_cfg, "ai_chat_log_dir", "") or ""
    legacy = _ROOT / "data"
    runtime = legacy / "runtime"
    path = Path(configured) if configured else runtime
    path = path if path.is_absolute() else _ROOT / path
    if path.resolve() in (legacy.resolve(), runtime.resolve()):
        _migrate_data_layout(legacy, runtime)
        return runtime
    return path


# **把两样东西注入 packs**（本模块是唯一知道它们的地方）：
#   * `default`：`.env` 的 AI_CHAT_PERSONA_PACK（部署期想钉死某个包时用）；
#   * `data_dir`：运行数据根的回调 —— 切换标记、运行数据都要落在卷里的 data/ 下。
# 两样都是惰性使用，所以在这里（`_data_dir` 定义之后、任何读取发生之前）注入正好。
packs.configure(
    default=str(getattr(_cfg, "ai_chat_persona_pack", None) or "").strip(),
    data_dir=_data_dir,
)


def bot_name() -> str:
    """机器人自己的显示名。**每次现读** —— 它可以被 `/昵称`、控制台或**换人格**改。

    四级回落（高 → 低）：

    1. 可调项 `settings.bot_name`（控制台 / `/昵称` / `/人设 切换` 写入 settings.json）；
    2. **当前人格包的 `_pack.json` → `bot_name`**；
    3. `.env` 的 `AI_CHAT_BOT_NAME`（即模块级 `BOT_NAME`）；
    4. 内置默认。

    **为什么人格包要插在 .env 前面**：角色名是人格的一部分。切到「小助手」之后
    还叫「鲸鱼娘」，`chatlog._speaker()` 就认不出它刚说过的话（判定依据是
    「角色名 + QQ 号」），于是它会对着自己上一轮接话 —— 这是静默故障，
    所以身份元数据必须跟着包走。

    **为什么 settings 仍然在最前**：用户手改的值永远最大 ——
    `/昵称 小鱼` 之后不能被包的默认值顶回去。
    """
    try:
        from . import settings as _s  # 局部导入：settings 顶部要 import config
    except Exception:  # noqa: BLE001
        return _pack_bot_name() or BOT_NAME
    override = str(_s.get("bot_name") or "").strip()
    return override or _pack_bot_name() or BOT_NAME


def _pack_bot_name() -> str:
    """当前人格包里声明的角色名（读不到返回空串）。"""
    try:
        return str(packs.active_pack().get("bot_name") or "").strip()
    except Exception:  # noqa: BLE001 - 名字读不出来不能让聊天挂掉
        return ""


def surface_file_path() -> Path:
    """表层人设的**读写路径**。

    * **没有显式配 `AI_CHAT_SURFACE_FILE`**（正常情况）：落在当前人格包的运行数据目录
      —— `data/runtime/persona/<包>/surface.txt`，卷内持久化。
    * **显式配了**：就用那个路径（逃生舱；测试也靠它把表层指到临时目录，
      免得写进真实运行数据）。

    **按包分目录**是包化的关键一步：改造前它平铺在 `data/runtime/persona/surface.txt`，
    于是换人格后前一个角色学到的说话方式会被后一个继承。
    旧目录由 `packs.migrate_layout()` 在启动时归位（幂等）。
    """
    configured = _spec_override("surface")
    if configured:
        return _persona_path(configured)
    name = Path(packs.PACK_FILES["surface"]).name
    return packs.stage_dir() / name


def persona_data_dir() -> Path:
    """**当前人格**的运行状态目录；和只读的人格源文件分开，也和别的人格分开。

    正常情况就是 `data/runtime/persona/<包>/`。

    **配了 `AI_CHAT_SURFACE_FILE` 时跟着它走**（只要它落在运行数据根内）：
    这时候表层被显式指到了别处，而变更日志与候选池都是**表层的附属账本** ——
    账本留在原地会出现"表层在 A、日志在 B"，撤回与采纳就会对着错的账本操作。
    刻意**不**照抄它落在运行数据根之外的情况（比如仓库内某个测试文件）：
    那样子目录会散到源码树里，日志与候选池没有理由跟着跑。
    """
    configured = _spec_override("surface")
    if configured:
        parent = surface_file_path().parent
        try:
            parent.resolve().relative_to(packs.runtime_persona_root().resolve())
        except ValueError:
            pass
        else:
            return parent
    return packs.stage_dir()


def surface_seed_path() -> Path:
    """模板的路径（只用于首次播种；之后不再读写它）。**在包内**。"""
    return persona_source_path("surface")


# 兼容旧名字：`packs` 出现之前它们是拆开算的。
def surface_template_path() -> Path:
    return surface_seed_path()


def seed_surface() -> str:
    """首次启动时把包里的模板播种到 `data/runtime/persona/<包>/`。返回一句可打进启动日志的说明。

    **幂等**：只在目标不存在时播种 —— 之后的自我迭代成果不会被模板覆盖。
    这正是"自我学习不会被重建冲掉"的保证。
    播种失败不抛：表层读不到时 `render()` 少一层，不该让插件起不来。
    """
    if not _spec_override("surface") and not packs.active_id():
        return "表层人设：已关闭（没有可用的人格包，且未配置 AI_CHAT_SURFACE_FILE）"
    target = surface_file_path()
    if target.exists():
        try:
            return "表层人设：读写 %s（%d 字，已存在，未覆盖）" % (
                _rel_to_root(target), len(target.read_text(encoding="utf-8")))
        except OSError:
            return "表层人设：读写 %s（已存在）" % _rel_to_root(target)
    seed = surface_seed_path()
    if not seed.exists():
        return "表层人设：%s 与模板都缺失，本层为空" % _rel_to_root(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        # **二进制读写**：文本模式会在读、写两端各做一次换行转换（Windows 上
        # CRLF↔LF），于是"播种出来的文件"与模板字节不同。内容虽等价，
        # 但没必要引入这种差异 —— 二进制搬运保证**字节级一致**。
        tmp.write_bytes(seed.read_bytes())
        tmp.replace(target)
        return "表层人设：已从模板播种到 %s（重建不再丢）" % _rel_to_root(target)
    except OSError as exc:
        return "表层人设：播种失败（%s），本次将退回读模板" % type(exc).__name__


def _rel_to_root(path: Path) -> str:
    """日志里尽量用相对项目根的路径（绝对路径会把本机目录结构写进日志）。"""
    try:
        return str(path.relative_to(_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


# ------------------------------------------------------------------ 人格三层
# 人格从「一个文件 + 一堆运行时槽位」改成**三个文件、三层寿命**。
#
# | 层 | 文件（当前人格包内） | 谁能写 | 进 prompt 的顺序 |
# |---|---|---|---|
# | 底层人设 | `base.txt` | **只有用户**（直接编辑文件） | 第 1 位（最硬） |
# | 禁止事项 | `forbidden.txt` | **只有用户**（直接编辑文件） | 第 2 位 |
# | 表层人设 | 模板 `surface.txt` → 运行数据 `data/runtime/persona/<包>/surface.txt` | **只有自动迭代**（经冲突闸门） | 第 3 位 |
#
# 为什么这么分：原来「底色」和「可以学的东西」挤在同一个文件里（`persona.txt` 自己
# 第 3 行就写着"下面写的是底色，改起来很慢"），而运行时槽位又是第三处 ——
# 结果"哪些能自动变"这件事没有结构性保证，只靠约定。
# 现在**由文件划分把权限钉死**：自动迭代的代码里根本拿不到另外两个文件的写路径。
#
# 包化（2026-10 起）在里面又加了一层：**一组三层 = 一个包**，
# 所以"换角色"变成"换目录"，而不是"覆盖同一批文件"。见 `packs.py`。
#
# 禁止事项单独成层而不是并进底层，是因为它要被**逐条解析出来做冲突判定**
# （见 `persona.forbidden_items()`）—— 一条禁止事项能挡掉一条表层改动。
_PERSONA_RAW = getattr(_cfg, "ai_chat_persona_file", None)
# 兼容旧名字：分层之前的旧文件叫 `persona.txt`，那时直接改这两个常量来指路径。
# 现在路径由 `_spec_override()` 现算，这两个别名保留只为**读**（旧脚本/旧测试引用）。
_PERSONA_CONFIGURED = "" if _PERSONA_RAW is None else str(_PERSONA_RAW).strip()
_FORBIDDEN_RAW = getattr(_cfg, "ai_chat_forbidden_file", None)
_FORBIDDEN_CONFIGURED = "" if _FORBIDDEN_RAW is None else str(_FORBIDDEN_RAW).strip()
_SURFACE_RAW = getattr(_cfg, "ai_chat_surface_file", None)
_SURFACE_CONFIGURED = "" if _SURFACE_RAW is None else str(_SURFACE_RAW).strip()

# 三层正文的**按包缓存**。换包时清空（见 `_persona_generation()`）。
#
# **为什么不再是 import 期常量**：人格包要求"切了就换人"，而 import 期常量只能
# 重启才变。改法见模块末尾的 `__getattr__` —— `config.BASE_PROMPT` 照旧能读，
# 但值是**现算 + 按包缓存**的。
_persona_memo: dict[str, str] = {}
_persona_generation_seen: str | None = None


def persona_generation() -> str:
    """人格代际：激活包换了就跟着变。所有"按人格算出来的缓存"都挂在这个键上。

    单独抽成一个函数（而不是让各模块自己比 `packs.active_id()`）是为了**只有一个地方**
    定义"什么算换人格"。以后如果引入"同一包内换 profile"这类概念，改这里就够。
    """
    global _persona_generation_seen
    now = packs.active_id()
    if now != _persona_generation_seen:
        _persona_generation_seen = now
        _persona_memo.clear()
    return now


def _persona_layer(role: str) -> str:
    """读某层的正文（带缓存，换包自动失效）。

    三层都用它，所以**不要**在别处再 `path.read_text()` 一次 —— 那条路不会
    跟着换包失效，于是"切了人格但某一层还是旧的"，而且只在切过之后才出现。
    """
    persona_generation()  # 先对齐代际：换包时这一步会清掉 memo
    if role in _persona_memo:
        return _persona_memo[role]
    configured = _spec_override(role)
    if not configured and not packs.active_id():
        # 既没配路径、也没有可用的人格包 → 这一层就是空的。
        # 故意**不去**看旧目录：看起来像"兜底"，实际只会让人以为改了文件没生效。
        _persona_memo[role] = ""
        return ""
    path = persona_source_path(role)
    label = {"base": "底层人设", "forbidden": "禁止事项", "surface": "表层人设"}.get(role, role)
    text = _read_text_file(path, label=label)
    _persona_memo[role] = text
    return text


def _base_layer() -> str:
    return _persona_layer("base")


def _forbidden_layer() -> str:
    return _persona_layer("forbidden")


def active_persona_id() -> str:
    """当前人格包 id（空串 = 没有可用的人格包）。"""
    return packs.active_id()


def _system_prompt_lazy() -> str:
    """现在该用的 system 前缀：三层合成，空了才回落到 `.env` 的静态提示词。"""
    return compose_prompt() or _static_prompt()


def load_surface() -> str:
    """读表层人设。**每次现读** —— 它会被自动迭代改动，缓存在这里就会读到旧内容。

    文件很小（几百字），一次读盘是毫秒级，而且只在组装 prompt 时读。

    ## 从 `data/runtime/persona/<包>/` 读（这是「自我学习不该被重建覆盖」的修法）
    表层是**唯一会被自动迭代写入**的一层，而 `Dockerfile` 会 `COPY persona ./persona`
    —— 于是"线上学到的"会被"本机那份"顶掉，**而且不报错**，只表现为
    "它前几天学会的说话方式又变回去了"。

    现在读写都落在卷里的 `data/`（`packs.stage_dir()`），镜像里那份只作
    **首次播种**的模板（`seed_surface()` 在启动时播一次，幂等）。
    包化之后还多了一层隔离：**每个包一份**，换人格不会互相继承。
    """
    if not _spec_override("surface") and not packs.active_id():
        return ""
    return _read_text_file(surface_file_path(), label="表层人设")


def strip_comments(text: str) -> str:
    """去掉**整行**注释（行首可含空白）。

    为什么需要它：`persona.forbidden_items()` 一直把 `#` 开头的行当注释跳过，
    但 `compose_prompt()` 不跳 —— 于是"注释掉一条铁律"在人设文件里等于一句废话：
    闸门不再认它，可它**照样被送进 prompt**。现在两边口径统一，
    "注释"在三层的任何一层都真正意味着"暂时停用，且随时可恢复"。

    只认整行注释，不做行内切分 —— 人设正文里可能出现句中的 `#`（示例、代码片段），
    按行内切会误伤。
    """
    return "\n".join(
        ln for ln in str(text or "").splitlines() if not ln.lstrip().startswith("#")
    )


def compose_prompt() -> str:
    """把三层按「最硬的在前」拼成 system 前缀。

    顺序有实际意义：越靠前的内容对模型越像"前提"，越靠后越像"补充"。
    所以底色 → 铁律 → 表层，表层即使写得天花乱坠也压不过上面两层。

    **`#` 开头的整行在这里被剥掉** —— 见 `strip_comments()`。

    **每轮现读**：三层都走 `_persona_layer()`（按包缓存），所以换人格之后
    下一轮组装就是新人格，不需要重启进程。
    """
    parts = [
        strip_comments(p)
        for p in (_base_layer(), _forbidden_layer(), load_surface())
        if p
    ]
    parts = [p for p in parts if p.strip()]
    return "\n\n".join(parts)


def _static_prompt() -> str:
    configured = getattr(_cfg, "ai_chat_system_prompt", "") or ""
    return str(configured).strip() or _FALLBACK_PROMPT


# 三层的**文件路径出口** —— `persona.py` 与迁移脚本按名字取，不各自拼路径。
# 返回值是"打算用哪个路径"，**不保证文件存在**：底层/禁止事项允许缺失（那就没有这一层），
# 表层缺失时由写入方创建。
def persona_file_path() -> Path:
    """底层人设（它是谁）的路径：显式配置优先，否则取当前人格包内的 base.txt。"""
    return persona_source_path("base")


def forbidden_file_path() -> Path:
    """禁止事项（铁律）的路径。"""
    return persona_source_path("forbidden")


# ---------------------------------------------------------------- 特质注册表
# 人格约束的**元数据**：每个特质是什么、怎么测、有哪些表达通道、闸门关键词。
# 它是**配置**（随镜像走，不像表层那样会被运行时改写）。
# 规则正文仍然只在三层文件里 —— 注册表只做索引，不复制文本（见 README §5.6.14）。
#
# ⚠ **注册表是人格相关的**：`gate_terms` 决定冲突闸门认哪些词。换人格之后
# 缓存必须失效，否则新角色会带着旧角色的关键词运行（表现为"铁律挡不住新角色的
# 偏离"或反过来），而且没有任何报错。所以它挂 `persona_generation()`。
_traits_cache: list[dict[str, Any]] | None = None
_traits_generation: str | None = None


def traits_file_path() -> Path:
    """特质注册表的路径：显式配置优先，否则取当前人格包内的 traits.json。"""
    return persona_source_path("traits")


def load_traits() -> list[dict[str, Any]]:
    """读特质注册表。**读不到或结构不对一律返回空列表。**

    这是"闸门不裸奔"的一半：`persona.py` 用它派生冲突关键词与否定白名单，
    拿不到就回退到内置的 `_LEGACY_CONFLICTS` —— 注册表损坏不该让安全判定消失
    （公开副本按设计就不带这份文件，所以"缺它也能跑"是硬要求）。
    """
    global _traits_cache, _traits_generation
    gen = persona_generation()
    if _traits_cache is not None and _traits_generation == gen:
        return _traits_cache
    items: list[dict[str, Any]] = []
    path = traits_file_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("traits"), list):
            items = [x for x in raw["traits"] if isinstance(x, dict)]
        else:
            logger.warning("特质注册表结构不对（缺 traits 数组）：%s", path.name)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("特质注册表读不到，闸门回退到内置表：%s（%s）",
                       path.name, type(exc).__name__)
    _traits_cache = items
    _traits_generation = gen
    return items


def reset_persona_cache() -> None:
    """把"按人格算出来的"缓存全部作废。**换包之后必须调它**（`packs.switch()` 会）。

    只清本模块的两处（三层正文、特质注册表）；判据器与内存副本由各自的模块
    通过 `packs.on_change()` 登记清理 —— 见 `packs.py` 顶部那张表。
    """
    _persona_memo.clear()
    global _traits_cache, _traits_generation, _persona_generation_seen
    _traits_cache = None
    _traits_generation = None
    # 让下一次 `persona_generation()` 重新对齐（即使 active_id 没变）。
    _persona_generation_seen = None


packs.on_change(reset_persona_cache)


# `surface_file_path()` 定义在上面（`load_surface` 旁边）—— 它落在 `data/` 而不是项目根，
# 理由见那里的 docstring。**不要在这里再加一个同名函数**：后定义的会覆盖前者。


# ------------------------------------------------------- 人格相关的三个常量（惰性）
# 改造前它们是 import 期常量（`BASE_PROMPT: str = _read_text_file(...)`），
# 于是"改底层人设要重启"是一条写在注释里的约定 —— 人格包要热切换，这条就不成立了。
#
# 现在它们是**模块级 `__getattr__`（PEP 562）**：`config.BASE_PROMPT` 照旧能读，
# 但值是现算 + 按包缓存的；换包之后下一次读就是新人格。
#
# **为什么用 `__getattr__` 而不是 `@property` 或 `lru_cache` 包一层函数**：
# `验证\离线验证_桩.py` 会**给这些名字赋值**来做隔离测试
# （`config.BASE_PROMPT = ""`、`config.SYSTEM_PROMPT = ""`），
# 而 `__getattr__` 只在**正常属性查找失败**时才被调用 —— 赋过值之后读到的是
# 赋进去的那个值，打桩语义原样保住。换成 property 就没法赋值了。
_LAZY_ATTRS = ("BASE_PROMPT", "FORBIDDEN_PROMPT", "SYSTEM_PROMPT")


def __getattr__(name: str) -> Any:
    if name == "BASE_PROMPT":
        return _base_layer()
    if name == "FORBIDDEN_PROMPT":
        return _forbidden_layer()
    if name == "SYSTEM_PROMPT":
        return _system_prompt_lazy()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def persona_layers() -> dict[str, str]:
    """三层正文 + 各自来自哪个文件（启动日志、诊断、`/人设 状态` 用）。"""
    return {
        "base": _base_layer(),
        "forbidden": _forbidden_layer(),
        "surface": load_surface(),
        "base_file": _rel_to_root(persona_file_path()),
        "forbidden_file": _rel_to_root(forbidden_file_path()),
        "surface_file": _rel_to_root(surface_file_path()),
        "traits_file": _rel_to_root(traits_file_path()),
    }


# 兼容旧读法：`PERSONA_SOURCE` 历史上是"底层人设来自哪个文件名"。
# 现在来源是**当前人格包**，所以它报包的 id（空 = 靠 .env 的静态提示词兜底）。
# `PERSONA_PREVIEW` 同理改成函数 —— import 期算一次会在换人格后变成陈旧值，
# 而它的用途（启动日志里显示"现在的人设长什么样"）恰恰要求它是最新的。
def persona_source() -> str:
    return packs.active_id() or "AI_CHAT_SYSTEM_PROMPT"


def persona_preview() -> str:
    return " ".join((_base_layer() or _system_prompt_lazy()).split())[:60]


# ------------------------------------------------------------ 切换人格（统一入口）
def switch_persona(pack_id: str, *, sync_identity: bool = True) -> dict[str, Any]:
    """切到另一个人格包，并把身份元数据一起同步。**群指令、Web、桌面三处都走它。**

    为什么不让调用方各自 `packs.switch()` 再自己写设置：

    1. **身份要跟着包走**。切到「小助手」之后 `bot_name` 还是「鲸鱼娘」的话，
       `chatlog._speaker()` 就认不出它刚说过的话（判据是「角色名 + QQ 号」），
       于是它会对着自己上一轮接话 —— 这是静默故障，必须由切换动作本身负责；
    2. **"切完要不要重启"这类说明只能有一处**。散在三个入口就会有一处忘改。

    返回 `packs.switch()` 的结果，另加两个字段：

    * `identity`：实际同步到 `settings.json` 的身份参数（空 = 没动）；
    * `identity_note`：同步失败/跳过时的说明（给人看）。

    注意：**表层播种不在这一步**。切换后第一次组装 prompt 会读到旧包运行数据目录
    之外的东西 —— 如果新包还没有表层文件，`load_surface()` 读不到内容，
    由 `_startup` 与切换入口各自调一次 `seed_surface()` 补上（幂等）。
    """
    got = packs.switch(pack_id)
    got["identity"] = {}
    got["identity_note"] = ""
    if not got.get("ok") or not sync_identity:
        return got
    patch = packs.identity_patch(str(got["id"]))
    if not patch:
        got["identity_note"] = "包里没有声明身份元数据，机器人显示名与唤醒词保持不变"
        return got
    applied: dict[str, str] = {}
    for key, value in patch.items():
        try:
            settings.set_value(key, value)
            applied[key] = value
        except (KeyError, OSError, ValueError) as exc:
            got["identity_note"] = f"{key} 同步失败（{type(exc).__name__}）"
            logger.warning("切换人格时同步 %s 失败：%s", key, type(exc).__name__)
    got["identity"] = applied
    return got


def seed_surface_for(pack_id: str) -> str:
    """给**指定**的包播种表层（用它自己的模板 → 它自己的运行数据目录）。

    切换前先播一次：切完立刻就有表层内容，不用等下一次启动。
    幂等（目标存在就不动），失败不抛。
    """
    pid = packs.resolve_id(pack_id) or str(pack_id or "").strip()
    if not pid:
        return "表层人设：包 id 为空，未播种"
    target = packs.seeded_stage_file(pid, "surface")
    if target.exists():
        return "表层人设：%s 已存在，未覆盖" % _rel_to_root(target)
    seed = packs.pack_file(pid, "surface")
    if not seed.is_file():
        return "表层人设：%s 里没有 surface.txt 模板，本层为空" % _rel_to_root(seed.parent)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(seed.read_bytes())
        tmp.replace(target)
        return "表层人设：已为 %s 播种 %s" % (pid, _rel_to_root(target))
    except OSError as exc:
        return "表层人设：给 %s 播种失败（%s）" % (pid, type(exc).__name__)


# ---------------------------------------------------------------- 当前时间
# 模型自己是不知道「现在几点」的：不给它，「现在几点了」就只能瞎猜，
# 「早安」和「晚安」也分不出该说哪个。所以每次组装 prompt 时把此刻的时间附上去。
#
# 两个刻意的选择：
# 位置：**放在人设与运行时要求之后**（见 context.build 的八段结构）。
# 早先的注释说"压在最末尾"，那是三段式时代的写法；现在记录读法、长期记忆、
# 机制事实都在它后面，所以它已经不是最后一段了。真正的原则没变：
# **稳定的东西在前、每轮变化的东西在后**，让"人设 + 风格 + 记录读法"这段稳定前缀
# 继续被 DeepSeek 的前缀缓存命中。
# 2. **只由对话路径注入**（`context.system_prompt()` 的第 3 段），不动静态的
#    SYSTEM_PROMPT —— 表情包打分走的是后者（它不需要知道时间，没必要多花 token）。
#    注意：这里**没有** `config.system_prompt()` 这个函数了。曾经有，它负责
#    「人设正文 + 随机人设要素 + 时间」，但对话改走 `context.system_prompt()` 之后
#    就再没人调用它 —— 连带那三个概率项一起失效了很久（见 settings.py「人设」组）。
#    现在整条摘掉，`time_hint()` 仍留着给 context 用。
_WEEKDAYS: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def period_of(hour: int) -> str:
    """把小时换成中文时段词。早晚问候与「现在是早上还是晚上」都靠它。"""
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


def time_parts(when: float | None = None) -> tuple[str, str, str]:
    """(「2026-09-21 08:00」, 「周日」, 「早上」)。

    `when` 传的是**校准后**的时间戳（默认取 `clock.now()`）。
    时间来源统一走 `clock` 模块 —— 它带 NTP 校准，见 clock.py 的说明。
    """
    from . import clock  # 局部导入：clock 需要 config.LOG_DIR，顶部互导会成环

    moment = clock.now() if when is None else float(when)
    lt = time.localtime(moment)
    return time.strftime("%Y-%m-%d %H:%M", lt), _WEEKDAYS[lt.tm_wday], period_of(lt.tm_hour)


def now_stamp() -> str:
    """带时区的完整时间戳（已校准），启动日志与诊断用它 ——
    容器时区不对、或 NTP 没同步上，一眼就能看出来。"""
    from . import clock

    return time.strftime("%Y-%m-%d %H:%M:%S %Z%z", time.localtime(clock.now()))


# --------------------------------------------------------------------- 时间读取
# 机器人"读时间"分两件事，**分工必须清楚**，否则两头都做不好：
#
# | 谁做 | 做什么 | 为什么 |
# |---|---|---|
# | **代码（本段）** | 当前时刻、"过了多久"、跨天判断 | 这些是**精确算术**。让模型自己算「三天前是几号」既慢又会算错，而本地算一次 0 token |
# | **模型** | 读懂"刚吃过饭""昨天下午"这类模糊表达 | 这是**语言理解**，代码做不了 |
#
# 所以这里提供的是"把它需要的事实备齐"，而不是"替它判断该说什么"。
# 结果通过 `time_hint()` 与聊天记录里的相对时间标注进入 prompt。

# 相对时间的分档边界（秒）。用固定区间而不是"聪明"的算法：
# 模型需要的是**稳**，不是花哨。同一段时间的表述每次都一样，它才好据此推理。
# 超过 14 天就不再报"多少天前"（那时说天数已经没意义了，只给日期更有用）。
_REL_JUST_NOW = 10
_REL_MINUTE = 60
_REL_HOUR = 3600
_REL_DAY = 86400
_REL_MAX_DAYS = 14


def relative_time(when: float, now: float | None = None) -> str:
    """把一个时间点说成"多久之前"；超过两周或指未来时返回空串。

    **未来时间返回空串而不是"还没到"** —— 调用方按场景知道该说"之前"还是"之后"。
    早先这里对未来返回「（还没到）」，结果 `/时间 差 3 小时` 拼出
    "也就是（还没到）"这种半截话。现在由调用方决定措辞。
    """
    now = time.time() if now is None else now
    delta = now - float(when)
    if delta < 0:
        return ""
    if delta < _REL_JUST_NOW:
        return "刚刚"
    if delta < _REL_MINUTE:
        return "不到 1 分钟前"
    if delta < _REL_HOUR:
        return f"{int(delta // 60)} 分钟前"
    if delta < _REL_DAY:
        return f"{int(delta // 3600)} 小时前"
    days = int(delta // _REL_DAY)
    if days < _REL_MAX_DAYS:
        return f"{days} 天前"
    return ""


def relative_time_after(when: float, now: float | None = None) -> str:
    """把**未来**时间点说成"多久之后"；已经过去或超过两周返回空串。

    跟 `relative_time()` 对称，避免调用方为了"之后"自己再拼一套分档。
    """
    now = time.time() if now is None else now
    delta = float(when) - now
    if delta < 0:
        return ""
    if delta < _REL_JUST_NOW:
        return "马上就到"
    if delta < _REL_MINUTE:
        return "不到 1 分钟后"
    if delta < _REL_HOUR:
        return f"{int(delta // 60)} 分钟后"
    if delta < _REL_DAY:
        return f"{int(delta // 3600)} 小时后"
    days = int(delta // _REL_DAY)
    if days < _REL_MAX_DAYS:
        return f"{days} 天后"
    return ""


def human_duration(seconds: float) -> str:
    """把一段时长说成人话：`3 小时 12 分` / `2 天 4 小时` / `45 秒`。

    `/时间 差` 用它报"距某个时间点过了多久"。
    """
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{int(seconds)} 秒"
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    if hours:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    return f"{minutes} 分"


def time_hint(when: float | None = None) -> str:
    """给模型看的「当前时间」提示（注入 system prompt）。

    除了年月日时分、星期、时段，还给了**精确到秒的时间戳**与**时区**。理由：

    * 只给到分钟时，模型算"再过 20 分钟是几点"要靠自己推，容易错；
    * 容器时区如果配错了（UTC 而不是 +0800），只有把 `%Z%z` 摆出来才看得出来 ——
      这正是"定时问候在错误钟点触发"那类问题的排查入口。

    提示语里仍然明说"平时不必刻意提它"：给了时间不等于要它逢人就说几点。
    """
    from . import clock

    moment = clock.now() if when is None else float(when)
    stamp, weekday, period = time_parts(moment)
    tz = time.strftime("%Z%z", time.localtime(moment))
    # 校准过就明说一句。没校准不说 —— 免得它把"我没校准"讲给群里听。
    calibrated = "（已按 NTP 校准）" if clock.calibrated() else ""
    return (
        f"（当前时间：{stamp} {weekday}，{period}。"
        f"精确时间戳 {int(moment)}，时区 {tz}{calibrated}。这是真实时间，"
        "可以直接回答时间类的问题、也据此判断当下该说早安还是晚安；"
        "算「多久之前／之后」这类问题可以用这个时间戳直接算；"
        "平时不必刻意提它。）"
    )


# 人格化的异常提示（对应人设里的 TIMEOUTSIGNAL）：出错时也不掉出人设。
# 注意别在这里写括号动作 —— 人设的 NO ACTIONDESC 对提示语同样适用。
#
# 【语域说明（重要）】下面这些是**固定系统文案**：由代码写死、在特定时机
# 原样发出，模型没有即兴发挥的余地。因此它们**不属于人格语域** ——
# `persona/packs/<包>/forbidden.txt` 的铁律管的是"模型自己怎么说"，不适用于这里。
# 这个区分登记在 `persona/packs/<包>/traits.json` 的 `fixed_notice_channels` 里，巡逻脚本会核对；
# **以后新增固定文案时，记得去那里登记一条**（否则"谁在发什么"就没有清单了）。
MSG_TIMEOUT: str = getattr(
    _cfg, "ai_chat_msg_timeout", "想太久了，脑子有点乱……等下再问我一次吧。"
)
MSG_ERROR: str = getattr(
    _cfg, "ai_chat_msg_error", "这边出岔子了，不是你的问题，等会儿再试试？"
)
# 空回复（模型没吐出可用内容）时发到群里的**错误通报**。
# 【判定】它**不是**人设化的兜底话术，措辞刻意中性 —— 因为空回复是**故障**，
# 不该由人格接管。原来那句「我没想出要说什么，换个说法问？」自带问句，与人设铁律
# 「不作话头抛回者」冲突；判定结果是"**改报错、不改人设**"，而不是把故障伪装成一句俏皮话。
# 属"固定系统文案"语域，登记在 persona/packs/<包>/traits.json 的 fixed_notice_channels。
MSG_EMPTY: str = getattr(
    _cfg, "ai_chat_msg_empty", "【出错了】这一轮没能生成回复，已记进日志。"
)
# 未配置 Key 时的系统提示。它只在**初期调试/部署没配好**时出现，而且用途就是告诉运维
# "去哪把 key 填上" —— 所以保留这句直白的说法，不按人设改写（依据见上面的语域说明）。
MSG_NO_KEY: str = getattr(
    _cfg, "ai_chat_msg_no_key", "我还没拿到 API Key，得先去 .env 里填一下。"
)
MSG_NO_QUESTION: str = getattr(
    _cfg, "ai_chat_msg_no_question", "在的，想说什么？"
)

# ---------------------------------------------------------------- 数据目录
# 会话切分间隔 / 已读预算 / 截断长度 / 主人 QQ 等【可调】参数统一放在 settings.py，
# 因为 Web UI 要能改它们并即时生效。这里只留不可调项与路径。
# 运行时数据目录（`_data_dir()` 定义在文件前段：表层人设的路径在导入期就要用它）
LOG_DIR: Path = _data_dir()

# 表情包库（与聊天记录同级，一起被归档引擎排除）
STICKER_DIR: Path = LOG_DIR / "stickers"


# 单群保留的最大消息条数（0 = 不限，文件持续增长）。
MAX_PER_GROUP: int = _as_int(getattr(_cfg, "ai_chat_max_messages", 0), 0)

# ---------------------------------------------------------------- Web 控制台
WEBUI_ENABLED: bool = _as_bool(getattr(_cfg, "ai_chat_webui_enabled", True), True)
WEBUI_PREFIX: str = getattr(_cfg, "ai_chat_webui_prefix", "/ai") or "/ai"
# 控制台认证令牌（2026-09-28 补）。**留空 = 不认证**（维持改造前的行为），
# 此时唯一的安全边界是宿主侧把端口绑在回环上（`deploy/docker-compose.yml` 的
# `127.0.0.1:8080:8080`）。配了就一律要凭证：`?token=`、`X-Auth-Token` 头或 cookie。
# 为什么不做"非回环就拒绝"：容器里 HOST 必须是 0.0.0.0，而经 SSH 隧道进来的请求
# 在容器看来源地址是 docker 网关 —— 按 IP 判会把正当访问一起挡掉（详见 `webui.auth_decision`）。
WEBUI_AUTH_TOKEN: str = str(getattr(_cfg, "ai_chat_webui_auth_token", "") or "").strip()

# 写盘时是否缩进美化（true 便于人工查看，但文件更大、写入更慢）。
LOG_PRETTY: bool = _as_bool(getattr(_cfg, "ai_chat_log_pretty", False), False)
