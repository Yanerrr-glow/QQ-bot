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

from . import settings

_cfg = get_driver().config

logger = logging.getLogger("ai_chat.config")

# 项目根 = plugins/ai_chat/config.py 往上三层
_ROOT = Path(__file__).resolve().parent.parent.parent


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


def _data_dir() -> Path:
    """运行时数据目录（`data/`，容器里是挂载卷）。

    **为什么单独抽一个函数**：`LOG_DIR` 在文件后半段才定义，而表层人设的路径
    必须在 `SYSTEM_PROMPT = compose_prompt()`（模块导入期就会执行）之前可用 ——
    否则导入时就 `NameError`（实测踩过）。`LOG_DIR` 也复用它，避免两处各算一遍。
    """
    configured = getattr(_cfg, "ai_chat_log_dir", "") or ""
    path = Path(configured) if configured else _ROOT / "data"
    return path if path.is_absolute() else _ROOT / path


def bot_name() -> str:
    """机器人自己的显示名。**每次现读** —— 它可以被 `/昵称` 或控制台改。

    优先取可调项 `settings.bot_name`（控制台 / `/昵称` 写入 settings.json），
    留空则回落到 `.env` 的 `AI_CHAT_BOT_NAME`（即模块级 `BOT_NAME`）。

    **为什么不是一个常量**：改了 QQ 昵称之后，聊天记录里必须跟着换名字 ——
    而"分清自己说的话"正是靠 `名字 + bot_uid` 判定的（`_speaker()` 的兜底分支
    就是拿它比的）。名字与落盘不一致会让归属判定退化。
    """
    try:
        from . import settings as _s  # 局部导入：settings 顶部要 import config
    except Exception:  # noqa: BLE001
        return BOT_NAME
    override = str(_s.get("bot_name") or "").strip()
    return override or BOT_NAME


def surface_file_path() -> Path:
    """表层人设的**读写路径**：`data/persona_surface.txt`（卷内，跨重建保留）。

    改这个函数就等于改了"自我学习存在哪"，所以 `persona.py` 的写入与
    `/人设` 的显示都走它，不各自拼路径。
    """
    if not _SURFACE_CONFIGURED:
        return _data_dir() / "persona_surface.txt"
    return _data_dir() / Path(_SURFACE_CONFIGURED).name


def surface_seed_path() -> Path:
    """镜像内那份模板的路径（只用于首次播种；之后不再读写它）。"""
    return _persona_path(_SURFACE_CONFIGURED or "persona_surface.txt")


def seed_surface() -> str:
    """首次启动时把镜像里的模板播种到 `data/`。返回一句可打进启动日志的说明。

    **幂等**：只在目标不存在时播种 —— 之后的自我迭代成果不会被模板覆盖。
    这正是"自我学习不会被重建冲掉"的保证。
    播种失败不抛：表层读不到时 `render()` 少一层，不该让插件起不来。
    """
    if not _SURFACE_CONFIGURED:
        return "表层人设：已关闭（配置为空串）"
    target = surface_file_path()
    if target.exists():
        try:
            return "表层人设：读写 data/%s（%d 字，已存在，未覆盖）" % (
                target.name, len(target.read_text(encoding="utf-8")))
        except OSError:
            return "表层人设：读写 data/%s（已存在）" % target.name
    seed = surface_seed_path()
    if not seed.exists():
        return "表层人设：data/%s 与模板都缺失，本层为空" % target.name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        # **二进制读写**：文本模式会在读、写两端各做一次换行转换（Windows 上
        # CRLF↔LF），于是"播种出来的文件"与模板字节不同。内容虽等价，
        # 但没必要引入这种差异 —— 二进制搬运保证**字节级一致**。
        tmp.write_bytes(seed.read_bytes())
        tmp.replace(target)
        return "表层人设：已从模板播种到 data/%s（之后写在这里，重建不再丢）" % target.name
    except OSError as exc:
        return "表层人设：播种失败（%s），本次将退回读模板" % type(exc).__name__


# ------------------------------------------------------------------ 人格三层
# 人格从「一个文件 + 一堆运行时槽位」改成**三个文件、三层寿命**。
#
# | 层 | 文件 | 谁能写 | 进 prompt 的顺序 |
# |---|---|---|---|
# | 底层人设 | `persona.txt` | **只有用户**（直接编辑文件） | 第 1 位（最硬） |
# | 禁止事项 | `persona_forbidden.txt` | **只有用户**（直接编辑文件） | 第 2 位 |
# | 表层人设 | `persona_surface.txt` | **只有自动迭代**（经冲突闸门） | 第 3 位 |
#
# 为什么这么分：原来「底色」和「可以学的东西」挤在同一个文件里（`persona.txt` 自己
# 第 3 行就写着"下面写的是底色，改起来很慢"），而运行时槽位又是第三处 ——
# 结果"哪些能自动变"这件事没有结构性保证，只靠约定。
# 现在**由文件划分把权限钉死**：自动迭代的代码里根本拿不到另外两个文件的写路径。
#
# 禁止事项单独成层而不是并进底层，是因为它要被**逐条解析出来做冲突判定**
# （见 `persona.forbidden_items()`）—— 一条禁止事项能挡掉一条表层改动。
_PERSONA_RAW = getattr(_cfg, "ai_chat_persona_file", None)
# 分层之后**底层人设的默认文件换成了 `persona_base.txt`**。
# 旧名 `persona.txt` 仍然被识别（`_read_text_file` 会找到它），
# 但默认值必须是新的那份 —— 否则会读到一个"包含三层内容"的旧文件，
# 于是禁止事项被算进底层、闸门的相似度判定也跟着偏。
_PERSONA_CONFIGURED = "persona_base.txt" if _PERSONA_RAW is None else str(_PERSONA_RAW).strip()
_FORBIDDEN_RAW = getattr(_cfg, "ai_chat_forbidden_file", None)
_FORBIDDEN_CONFIGURED = (
    "persona_forbidden.txt" if _FORBIDDEN_RAW is None else str(_FORBIDDEN_RAW).strip()
)
_SURFACE_RAW = getattr(_cfg, "ai_chat_surface_file", None)
_SURFACE_CONFIGURED = (
    "persona_surface.txt" if _SURFACE_RAW is None else str(_SURFACE_RAW).strip()
)

BASE_PROMPT: str = (
    _read_text_file(_persona_path(_PERSONA_CONFIGURED), label="底层人设")
    if _PERSONA_CONFIGURED
    else ""
)
FORBIDDEN_PROMPT: str = (
    _read_text_file(_persona_path(_FORBIDDEN_CONFIGURED), label="禁止事项")
    if _FORBIDDEN_CONFIGURED
    else ""
)


def load_surface() -> str:
    """读表层人设。**每次现读** —— 它会被自动迭代改动，缓存在这里就会读到旧内容。

    文件很小（几百字），一次读盘是毫秒级，而且只在组装 prompt 时读。

    ## 从 `data/` 读（这是「自我学习不该被重建覆盖」的修法）
    表层是**唯一会被自动迭代写入**的一层，而 `Dockerfile` 里有
    `COPY persona_surface.txt ./` —— 于是"线上学到的"会被"本机那份"顶掉，
    而且**不报错**，只表现为"它前几天学会的说话方式又变回去了"。

    现在读写都落在 `data/`（卷内），镜像里那份只作**首次播种**的模板
    （`seed_surface()` 在启动时播一次，幂等）。
    """
    if not _SURFACE_CONFIGURED:
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
    """
    parts = [strip_comments(p) for p in (BASE_PROMPT, FORBIDDEN_PROMPT, load_surface()) if p]
    parts = [p for p in parts if p.strip()]
    return "\n\n".join(parts)


def _static_prompt() -> str:
    configured = getattr(_cfg, "ai_chat_system_prompt", "") or ""
    return str(configured).strip() or _FALLBACK_PROMPT


# 三层的**文件路径出口** —— `persona.py` 与迁移脚本按名字取，不各自拼路径。
# 返回值是"打算用哪个路径"，不保证文件存在：底层/禁止事项允许缺失（那就没有这一层），
# 表层缺失时由写入方创建。
def persona_file_path() -> Path:
    # 兜底名跟着默认值走（`persona_base.txt`）：留 `persona.txt` 会让
    # 「显式写成空串」这种配置把路径指回一个已经改名为备份的旧文件。
    return _persona_path(_PERSONA_CONFIGURED or "persona_base.txt")


def forbidden_file_path() -> Path:
    return _persona_path(_FORBIDDEN_CONFIGURED or "persona_forbidden.txt")


# ---------------------------------------------------------------- 特质注册表
# 人格约束的**元数据**：每个特质是什么、怎么测、有哪些表达通道、闸门关键词。
# 它是**配置**（随镜像走，不像表层那样会被运行时改写），所以解释器内缓存一次即可。
# 规则正文仍然只在三层文件里 —— 注册表只做索引，不复制文本（见 README §5.6.14）。
_TRAITS_NAME = "persona_traits.json"
_traits_cache: list[dict[str, Any]] | None = None


def traits_file_path() -> Path:
    """特质注册表的路径。与三层人设文件同级（项目根 / 容器 WORKDIR）。"""
    return _persona_path(_TRAITS_NAME)


def load_traits() -> list[dict[str, Any]]:
    """读特质注册表。**读不到或结构不对一律返回空列表。**

    这是"闸门不裸奔"的一半：`persona.py` 用它派生冲突关键词与否定白名单，
    拿不到就回退到内置的 `_LEGACY_CONFLICTS` —— 注册表损坏不该让安全判定消失。
    """
    global _traits_cache
    if _traits_cache is not None:
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
    return items


# `surface_file_path()` 定义在上面（`load_surface` 旁边）—— 它落在 `data/` 而不是项目根，
# 理由见那里的 docstring。**不要在这里再加一个同名函数**：后定义的会覆盖前者。


_PERSONA_TEXT: str = BASE_PROMPT
SYSTEM_PROMPT: str = compose_prompt() or _static_prompt()

# 底层人设到底是从哪来的 —— 启动日志与 /机制 都要用，否则"人设没生效"这类问题
# 排查起来会先卡在"它现在读的哪一份"上。
# 分层之后"来源"是**三层各自的文件**，所以这里只标明底层那层用没用文件。
PERSONA_SOURCE: str = (
    persona_file_path().name
    if _PERSONA_TEXT
    else "AI_CHAT_SYSTEM_PROMPT"
)
PERSONA_PREVIEW: str = " ".join((BASE_PROMPT or SYSTEM_PROMPT).split())[:60]


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
# `persona_forbidden.txt` 的铁律管的是"模型自己怎么说"，不适用于这里。
# 这个区分登记在 `persona_traits.json` 的 `fixed_notice_channels` 里，巡逻脚本会核对；
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
# 属"固定系统文案"语域，登记在 persona_traits.json 的 fixed_notice_channels。
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

# 写盘时是否缩进美化（true 便于人工查看，但文件更大、写入更慢）。
LOG_PRETTY: bool = _as_bool(getattr(_cfg, "ai_chat_log_pretty", False), False)
