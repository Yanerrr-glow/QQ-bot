"""功能目录：桌面端「功能」目录里有哪些子菜单、每个功能归哪些参数、看哪些运行细节。

## 为什么要单独一份目录（而不是每个功能写一个页面）

功能的"设置"和"细节"都来自服务端同一份数据：

* **设置** —— `GET /api/state` 的 `settings` 切片（`settings.describe()`，按组返回）。
* **细节** —— `GET /api/state` 的 `status` / `memory` / `image` / `persona` 切片。

所以一个功能 = **若干参数组（可裁剪到具体键）** + **若干读状态的小函数**。
把这件事写成数据表，而不是 19 个手写页面，换来三条：服务端加一个参数，
凡是归到某功能里的组都会自动出现；状态字段改名只改这里一处；
「功能」中心页与子菜单页天然一致（同一份目录）。

## 与服务端的分工（别在这里越界）

* 这里**只做取数与过滤**，不做业务判断 —— "哪些参数算这个功能的"是 UI 归类，
  不是服务端契约。服务端 `_SPECS` 的 `group` 是**起点**：功能可以整组拿，
  也可以只拿组里的几个键（`groups={"基础": ("sticker_enabled",)}`）。
* 组名对不上（服务端改了分组名）**不会报错**：取不到就渲染成空，
  并由 `missing_groups()` 汇报出来。那是"服务端改了名"，不是"桌面端崩了"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: 读状态的小函数：拿到 `/api/state` 的取用助手（`base.StateSlice`），返回要展示的 chip 列表。
#: chip = `(标签, 值, 级别)`，级别取 `info` / `ok` / `warn` / `err`。
StateProbe = Callable[[Any], list[tuple[str, Any, str]]]


@dataclass(frozen=True)
class FeatureEntry:
    """一个功能（「功能」目录下的一个子菜单）。"""

    id: str
    title: str
    subtitle: str = ""
    description: str = ""
    category: str = "对话"
    #: 归属的参数组。值为空元组 = 整组都要；非空 = 只要这几个键（顺序即展示顺序）。
    groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 本功能的开关参数键（None = 没有单一开关，如「基础」）。供中心页显示开/关。
    enable_key: str | None = None
    #: 读运行细节（只读，不撒谎：取不到的值一律显示 `-`，不猜）。
    state: tuple[StateProbe, ...] = ()


# ---------------------------------------------------------------------- 取值助手


def settings_index(groups: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    """把 `settings.describe()` 的组列表摊平成 `key → spec`。"""
    index: dict[str, dict[str, Any]] = {}
    for group in groups or []:
        for item in group.get("items") or []:
            key = str(item.get("key") or "")
            if key:
                index[key] = item
    return index


def setting_value(groups: list[dict[str, Any]] | None, key: str, default: Any = None) -> Any:
    spec = settings_index(groups).get(key)
    if spec is None:
        return default
    value = spec.get("value")
    return default if value is None else value


def feature_groups(
    feature: FeatureEntry, groups: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """按功能裁剪/排序参数组。

    * 整组要：原样带过去（组内顺序**保持服务端顺序**，与「参数」页一致）。
    * 只要几个键：按功能里声明的顺序重排，且**丢掉本组里不属于它的键**。
    * 组不存在（服务端改过分组名）：静默跳过 —— 由 `missing_groups()` 汇报。
    """
    by_name = {str(g.get("group") or ""): g for g in (groups or [])}
    out: list[dict[str, Any]] = []
    for name, keys in feature.groups.items():
        group = by_name.get(name)
        if group is None:
            continue
        items = list(group.get("items") or [])
        if keys:
            wanted = {str(k) for k in keys}
            items = [item for item in items if str(item.get("key") or "") in wanted]
        if items:
            out.append({"group": name, "items": items})
    return out


def feature_key_order(feature: FeatureEntry) -> tuple[str, ...]:
    """功能内所有键的声明顺序（用于同一组内的排序）。"""
    order: list[str] = []
    for keys in feature.groups.values():
        order.extend(str(k) for k in keys)
    return tuple(order)


def missing_groups(feature: FeatureEntry, groups: list[dict[str, Any]] | None) -> list[str]:
    """服务端**没有**给出的组名 —— 非空即"服务端分组名变了，本功能的参数会少一块"。"""
    have = {str(g.get("group") or "") for g in (groups or [])}
    return [name for name in feature.groups if name not in have]


def param_total(feature: FeatureEntry, groups: list[dict[str, Any]] | None) -> int:
    return sum(len(g.get("items") or []) for g in feature_groups(feature, groups))


# ---------------------------------------------------------------------- 状态探针
# 只读、不猜：拿不到就显示 `-`。**不要在这里发请求** —— 数据全在 `/api/state` 一份里。


def _st(state: Any) -> dict[str, Any]:
    try:
        raw = dict(state.status or {})
    except Exception:  # noqa: BLE001 - 状态切片缺了就当作没有，不让页面炸
        return {}
    return raw


def _setting(state: Any, key: str, default: Any = None) -> Any:
    """从 state 的 settings 组里取一个键的当前值。"""
    try:
        groups = list(state.settings_groups or [])
    except Exception:  # noqa: BLE001
        return default
    return setting_value(groups, key, default)


def _basic_state(state: Any) -> list[tuple[str, Any, str]]:
    groups = list(state.settings_groups or [])
    name = _setting(state, "bot_name") or "（跟随 .env）"
    master = _setting(state, "master_qq")
    return [
        ("机器人显示名", name, "info"),
        ("主人 QQ", master, "info"),
        ("让模型知道时间", "开" if _setting(state, "time_context", True) else "关", "info"),
    ]


def _clock_state(state: Any) -> list[tuple[str, Any, str]]:
    time_info = dict(_st(state).get("time") or {})
    offset = time_info.get("offset_seconds", time_info.get("offset", 0))
    return [
        ("当前读数", time_info.get("now") or "-", "info"),
        ("宿主时钟", time_info.get("raw") or "-", "info"),
        ("偏移", f"{offset}s", "warn" if abs(float(offset or 0)) > 5 else "ok"),
        ("NTP 状态", time_info.get("status") or "-", "info"),
        ("上次同步", time_info.get("synced_at") or "从未", "info"),
    ]


def _context_state(state: Any) -> list[tuple[str, Any, str]]:
    status = _st(state)
    return [
        ("已知会话", len(list(status.get("groups") or [])), "info"),
        ("分条发送", "开" if _setting(state, "sentence_split", True) else "关", "info"),
        ("看图", "开" if _setting(state, "chat_vision", True) else "关", "info"),
        ("回应纯图片", "开" if _setting(state, "reply_to_images") else "关", "info"),
    ]


def _files_state(state: Any) -> list[tuple[str, Any, str]]:
    enabled = bool(_setting(state, "file_enabled", True))
    return [
        ("读取消息文件", "开" if enabled else "关", "ok" if enabled else "info"),
        ("大小上限", f"{_setting(state, 'file_max_kb')} KB", "info"),
        ("内容截断", f"{_setting(state, 'file_max_chars')} 字符", "info"),
    ]


def _persona_state(state: Any) -> list[tuple[str, Any, str]]:
    persona = dict(state.persona or {})
    stats = dict(persona.get("stats") or {})
    current = dict(persona.get("pack") or {})
    return [
        ("当前人格包", current.get("name") or stats.get("pack_name") or "-", "ok"),
        ("可用包", stats.get("pack_count", 0), "info"),
        ("底层人设", f"{stats.get('base_chars', 0)} 字", "info"),
        ("表层人设", f"{stats.get('surface_chars', 0)} 字", "info"),
        ("待采纳候选", stats.get("pending", 0), "info"),
    ]


def _persona_iter_state(state: Any) -> list[tuple[str, Any, str]]:
    persona = dict(state.persona or {})
    iteration = dict(persona.get("iter") or {})
    evaluation = dict(persona.get("eval") or {})
    return [
        ("自动迭代", "开" if _setting(state, "persona_iter_enabled") else "关",
         "ok" if _setting(state, "persona_iter_enabled") else "info"),
        ("已跑", f"{iteration.get('runs', 0)} 次", "info"),
        ("已写入", f"{iteration.get('written', 0)} 条", "info"),
        ("评估台", "开" if evaluation.get("enabled") else "关",
         "ok" if evaluation.get("enabled") else "warn"),
    ]


def _wake_state(state: Any) -> list[tuple[str, Any, str]]:
    words = str(_setting(state, "wake_words") or "")
    return [
        ("唤醒", "开" if _setting(state, "wake_enabled") else "关",
         "ok" if _setting(state, "wake_enabled") else "info"),
        ("唤醒词", f"{len([w for w in words.split(',') if w.strip()])} 个", "info"),
        ("命中概率", _setting(state, "wake_chance"), "info"),
    ]


def _attention_state(state: Any) -> list[tuple[str, Any, str]]:
    return [
        ("注意力", "开" if _setting(state, "attention_enabled") else "关",
         "ok" if _setting(state, "attention_enabled") else "info"),
        ("对话租约", "开" if _setting(state, "attention_lease_enabled") else "关", "info"),
        ("回复冷却", f"{_setting(state, 'attention_reply_cooldown')} 秒", "info"),
    ]


def _random_reply_state(state: Any) -> list[tuple[str, Any, str]]:
    enabled = bool(_setting(state, "random_reply_enabled"))
    return [
        ("随机插话", "开" if enabled else "关", "ok" if enabled else "info"),
        ("概率", _setting(state, "random_reply_chance"), "info"),
        ("冷却", f"{_setting(state, 'random_reply_cooldown')} 秒", "info"),
    ]


def _sticker_state(state: Any) -> list[tuple[str, Any, str]]:
    stats = dict(_st(state).get("stickers") or {})
    return [
        ("库容量", f"{int(stats.get('count') or 0)} 张", "info"),
        ("总占用", _mb(stats.get("total_bytes")), "info"),
        ("被用过", f"{int(stats.get('used') or 0)} 次", "info"),
        ("疑似重复", f"{int(stats.get('duplicates') or 0)} 组",
         "warn" if int(stats.get("duplicates") or 0) else "info"),
    ]


def _memory_state(state: Any) -> list[tuple[str, Any, str]]:
    memory = dict(state.memory or {})
    stats = dict(memory.get("stats") or {})
    return [
        ("事实", stats.get("facts", 0), "info"),
        ("群事件", stats.get("events", 0), "info"),
        ("人物", stats.get("people", stats.get("profile", 0)), "info"),
        ("受保护", stats.get("protected", 0), "info"),
        ("上限", stats.get("limit", "-"), "info"),
    ]


def _recall_state(state: Any) -> list[tuple[str, Any, str]]:
    """翻旧账（消息索引）的现状。

    ⚠ **会话摘要这一半没有实时数字**：`summary.stats()` / `msgindex.stats()` 都不在
    `/api/state` 的响应里（服务端只把它们给了 `/机制` 指令与启动日志）。所以这里
    只展示开关与**参数**，不编数字 —— 要看摘要条数就等这两个 stats 进 state，
    或走 `/机制 记忆`（见 `plugins/ai_chat/introspect.py`）。
    """
    return [
        ("翻旧账", "开" if _setting(state, "msgindex_enabled") else "关",
         "ok" if _setting(state, "msgindex_enabled") else "info"),
        ("会话摘要", "开" if _setting(state, "summary_enabled", True) else "关",
         "ok" if _setting(state, "summary_enabled", True) else "info"),
        ("带进 prompt 的索引条数", _setting(state, "msgindex_limit"), "info"),
        ("摘要召回条数", _setting(state, "summary_recall_count"), "info"),
    ]


def _image_state(state: Any) -> list[tuple[str, Any, str]]:
    image = dict(state.image or {})
    mode = str(image.get("global_mode") or "-")
    return [
        ("全局策略", mode, "info"),
        ("会话自定义", f"{len(dict(image.get('convs') or {}))} 个", "info"),
    ]


def _search_state(state: Any) -> list[tuple[str, Any, str]]:
    search = dict(_st(state).get("search") or {})
    available = bool(search.get("available"))
    label = "可用" if available else ("未配端点" if search.get("enabled") else "关")
    return [
        ("联网搜索", label, "ok" if available else "warn"),
        ("后端", search.get("backend") or "-", "info"),
        ("端点", search.get("endpoint") or "（未配）", "info"),
        ("每条消息上限", search.get("max_per_message", "-"), "info"),
    ]


def _mode_state(state: Any) -> list[tuple[str, Any, str]]:
    enabled = bool(_setting(state, "mode_enabled"))
    return [
        ("说话模式", "开" if enabled else "关", "ok" if enabled else "info"),
        ("判定余量", _setting(state, "mode_min_margin"), "info"),
        ("粘滞", f"{_setting(state, 'mode_sticky_seconds')} 秒", "info"),
    ]


def _proactive_state(state: Any) -> list[tuple[str, Any, str]]:
    status = _st(state)
    proactive = dict(status.get("proactive") or {})
    day_count = dict(proactive.get("day_count") or {})
    last_spoke = dict(proactive.get("last_spoke") or {})
    groups = list(status.get("groups") or [])
    return [
        ("总开关", "开" if status.get("proactive_enabled") else "关",
         "ok" if status.get("proactive_enabled") else "info"),
        ("已知群", len(groups), "info"),
        ("今日发言", sum(int(v) for v in day_count.values()), "info"),
        ("额度", _setting(state, "proactive_max_per_day"), "info"),
        ("最近发言", "、".join(f"{k} {v}" for k, v in last_spoke.items()) or "还没有", "info"),
    ]


def _greet_state(state: Any) -> list[tuple[str, Any, str]]:
    greet = dict(_st(state).get("greet") or {})
    times = dict(greet.get("times") or {})
    chips: list[tuple[str, Any, str]] = [
        ("定时问候", "开" if greet.get("enabled") else "关", "ok" if greet.get("enabled") else "info"),
    ]
    chips.append(("三个时间点", " / ".join(str(v) for v in times.values()) or "-", "info"))
    due = list(greet.get("due_now") or [])
    chips.append(("当前待发", "、".join(map(str, due)) if due else "无", "warn" if due else "info"))
    chips.append(("今天", greet.get("today") or "-", "info"))
    return chips


def _mb(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "-"
    return f"{size / 1024 / 1024:.2f} MB"


# ---------------------------------------------------------------------- 目录本体
# 顺序 = 导航里的顺序 = 「功能」中心页里的顺序。组名与 `settings.py` 的 `Spec.group` 对齐。

FEATURES: tuple[FeatureEntry, ...] = (
    FeatureEntry(
        id="basic",
        title="基础与身份",
        subtitle="它是谁、谁是主人、要不要知道时间",
        description=(
            "机器人的身份设定：主人的 QQ 号与称呼、它自己的显示名，以及"
            "“每次回复要不要把当前时间附给模型”。改这里的显示名等同于在群里发 `/昵称`，"
            "聊天记录、人设自述、挑图语境会一起跟着换。"
        ),
        category="基础",
        groups={"基础": ("master_qq", "master_title", "bot_name", "time_context", "model")},
        state=(_basic_state,),
    ),
    FeatureEntry(
        id="clock",
        title="时间校准",
        subtitle="NTP 偏移量与同步状态",
        description=(
            "**不改系统时钟**：只存一个偏移量，本程序所有时间读数都加它。"
            "定时问候判断“到 08:00 了吗”用的就是这个钟 —— 差 8 小时就等于早安安在半夜。"
            "同步失败会保留上一次的偏移，不会因为断网就让时钟变准。"
        ),
        category="基础",
        groups={"时间校准": ()},
        enable_key="ntp_enabled",
        state=(_clock_state,),
    ),
    FeatureEntry(
        id="context",
        title="上下文与分条发送",
        subtitle="会话切分、已读预算、多条发送",
        description=(
            "决定“它看到多少上下文”以及“怎么把话发出来”。分条发送是为了像真人打字"
            "（逐句分次发、之间停一下），而不是把一大段一次性砸出去。"
        ),
        category="对话",
        groups={"上下文": ()},
        state=(_context_state,),
    ),
    FeatureEntry(
        id="files",
        title="文件读取",
        subtitle="群文件/私聊文件进上下文",
        description=(
            "群里或私聊发来的文件会被读进对话上下文（文本类读内容，二进制只报元信息）。"
            "超过大小上限的只报文件名与体积 —— 不下载内容。"
        ),
        category="对话",
        groups={"文件": ()},
        enable_key="file_enabled",
        state=(_files_state,),
    ),
    FeatureEntry(
        id="persona",
        title="人格",
        subtitle="人格包、三层人设与表层上限",
        description=(
            "它是谁：当前人格包、底层/表层/禁止事项三层。**三层正文只能直接编辑文件**，"
            "控制台刻意不给编辑框（`base.txt` / `forbidden.txt` 只有主人能改）；"
            "要看三层正文与改动日志去顶层的「人格评估台」页。"
            "这里可调的是与人格相关的几个开关与表层上限。"
        ),
        category="人格",
        groups={"人设": ("persona_signal_enabled", "surface_max_chars", "surface_max_items")},
        state=(_persona_state,),
    ),
    FeatureEntry(
        id="persona_iter",
        title="人格自动迭代与评估",
        subtitle="自动改表层人设、评估台打分",
        description=(
            "让机器人按聊天记录反思并**自动写入表层人设**（底层与禁止事项永远不动），"
            "以及人格评估台（裁判模型给各特质打分）。两个动作都会花 token。"
        ),
        category="人格",
        groups={
            "人设": (
                "persona_iter_enabled",
                "persona_iter_auto_apply",
                "persona_iter_interval",
                "persona_iter_max",
                "persona_iter_min_lines",
            ),
            "人设评估": (),
        },
        enable_key="persona_iter_enabled",
        state=(_persona_iter_state,),
    ),
    FeatureEntry(
        id="wake",
        title="唤醒词",
        subtitle="点名概率、冷却",
        description=(
            "被人点名（唤醒词命中）时的响应规则。注意唤醒词**本身**来自当前人格包"
            "（也可用 `/唤醒词` 改），这里的三个参数只管“命中之后怎么办”。"
        ),
        category="注意力",
        groups={"唤醒": ("wake_enabled", "wake_chance", "wake_cooldown")},
        enable_key="wake_enabled",
        state=(_wake_state,),
    ),
    FeatureEntry(
        id="attention",
        title="注意力与对话租约",
        subtitle="该不该插话、一轮对话持续多久",
        description=(
            "注意力机制决定“这条消息值不值得它开口”；对话租约让一轮对话里的后续消息"
            "不必每次都重新判定。参数多，默认值已经调过，不熟就别动。"
        ),
        category="注意力",
        groups={"注意力": ()},
        enable_key="attention_enabled",
        state=(_attention_state,),
    ),
    FeatureEntry(
        id="random_reply",
        title="随机插话",
        subtitle="低概率主动搭话",
        description="按概率在群里随机接一句（与「主动发言」是两套机制：那个按时间与沉寂度触发）。",
        category="注意力",
        groups={"随机插话": ()},
        enable_key="random_reply_enabled",
        state=(_random_reply_state,),
    ),
    FeatureEntry(
        id="stickers",
        title="表情包",
        subtitle="挑图、判分、视觉与近似去重",
        description=(
            "表情包库的挑图与入库规则。库本身（看缩略图、删图）在「参数」页旁边的"
            "「表情包库」页 —— 那是数据，这里是策略。"
        ),
        category="内容",
        groups={"表情包": (), "基础": ("sticker_enabled",)},
        enable_key="sticker_enabled",
        state=(_sticker_state,),
    ),
    FeatureEntry(
        id="memory",
        title="长期记忆",
        subtitle="记什么、怎么召回、上限与淘汰",
        description=(
            "长期记忆的写入与召回规则（事实 / 群事件 / 人物画像）。"
            "记忆**条目**的增删保护在「记忆库」页，这里只调策略。"
        ),
        category="内容",
        groups={"长期记忆": ()},
        enable_key="memory_enabled",
        state=(_memory_state,),
    ),
    FeatureEntry(
        id="recall",
        title="翻旧账与会话摘要",
        subtitle="消息索引与旧对话摘要",
        description=(
            "让「你上周是不是说过…」这类问题查得出原话：**翻旧账**给每条消息建词面索引，"
            "**会话摘要**在每轮对话结束时归纳一两句、接在被丢弃的更早记录后面。"
            "两者的实时条数在 `/api/state` 里没有（服务端把它们给了 `/机制 记忆`），"
            "所以这里只显示开关与参数。"
        ),
        category="内容",
        groups={"翻旧账": (), "会话摘要": ()},
        enable_key="msgindex_enabled",
        state=(_recall_state,),
    ),
    FeatureEntry(
        id="image_policy",
        title="图片策略",
        subtitle="收不收图、按会话区分",
        description=(
            "机器人要不要记住新图。**有效策略 = 会话自定义优先，没有则跟全局**，"
            "所以设置会话级策略在「图片策略（会话）」页（那里能看到“我明明禁存了它怎么还在收”）。\n"
            "⚠ 那个页面的标题带「（会话）」后缀是刻意的：页面标题是跳转用的身份"
            "（`MainWindow.goto_page` 按标题查），同名会二义。"
        ),
        category="内容",
        groups={"图片策略": ()},
        enable_key="image_policy_enabled",
        state=(_image_state,),
    ),
    FeatureEntry(
        id="search",
        title="联网搜索",
        subtitle="搜索后端、读正文、记忆与渲染",
        description=(
            "让它在回答前查一次网。**“开着但没配 key”是最常见的“看起来没用”**："
            "deepseek 后端要 `DEEPSEEK_API_KEY`（端点留空即可，用官方 Search 接口）。"
        ),
        category="对外",
        groups={"联网搜索": ()},
        enable_key="search_enabled",
        state=(_search_state,),
    ),
    FeatureEntry(
        id="commands",
        title="指令与说话模式",
        subtitle="斜杠指令、自然语言指令、模式判定",
        description=(
            "两件事：**指令**（要不要前缀、能不能用自然语言下指令）与"
            "**说话模式**（判断当前该用哪种口吻，以及判定结果粘滞多久）。"
        ),
        category="对外",
        groups={"指令": (), "说话模式": ()},
        enable_key="mode_enabled",
        state=(_mode_state,),
    ),
    FeatureEntry(
        id="proactive",
        title="主动发言",
        subtitle="主动开口的时机与当日额度",
        description=(
            "机器人自己挑一个活跃群开口（与「随机插话」不同：它按时间间隔与群内沉寂度触发）。"
            "会调用模型并真的发消息，可能产生费用；「总览」页有“主动发言一次”的手动入口。"
        ),
        category="主动",
        groups={"主动发言": ()},
        enable_key="proactive_enabled",
        state=(_proactive_state,),
    ),
    FeatureEntry(
        id="greet",
        title="定时问候",
        subtitle="早/午/晚三个时间点与补发规则",
        description=(
            "到点问候一次（早安 / 午安 / 晚安）。判定用的是**校准后**的时钟"
            "（见「时间校准」），所以钟不准的时候问候也会跑到奇怪的钟点上去。"
        ),
        category="主动",
        groups={"定时问候": ()},
        enable_key="greet_enabled",
        state=(_greet_state,),
    ),
)

FEATURE_BY_ID: dict[str, FeatureEntry] = {feature.id: feature for feature in FEATURES}

#: 中心页与导航的分组顺序（按目录里第一次出现的顺序）。
CATEGORY_ORDER: tuple[str, ...] = tuple(
    dict.fromkeys(feature.category for feature in FEATURES)
)


def feature_for_title(title: str) -> FeatureEntry | None:
    """按页面标题反查功能（`FeaturePage` 的 `title` 就是目录里的标题）。"""
    for feature in FEATURES:
        if feature.title == title:
            return feature
    return None


def features_of(category: str) -> list[FeatureEntry]:
    return [feature for feature in FEATURES if feature.category == category]
