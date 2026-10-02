"""人设信号：把「他当场提的要求」**先记下来**，而不是立刻改人设。

## 这一步在整个"人设自我迭代"里的位置

它分两步：

| 步骤 | 做什么 | 风险 |
|---|---|---|
| **第 1 步（本模块）** | **只记账**：识别出"他在纠正我的说话方式"，落一条信号。**不改任何人设** | 无 |
| 第 2 步（未做） | 同一类第 3 次时**提议**改一次槽位，要他点头才改 | 低 |

为什么不直接做第 2 步：改槽位的阈值该定 **2 次还是 3 次**、误判长什么样 ——
**这些问题只能靠真实数据回答**。先记一两个星期，看 `/人设 信号` 里的分布，
再定阈值。否则就是拍脑袋。

## 它和 `/风格`、`_NL_RULES` 的关系

人格分层之后，聊天里**已经没有改人设的入口**了。所以本模块的定位也跟着变了：
它不再为"自动调槽位的阈值"攒数据（槽位机制已删），而是**自我迭代最看重的输入** ——
"主人亲口说过三次别啰嗦"，远比模型自己从聊天里猜准（见 `persona_iter.py`）。

| 路径 | 现在 | 本模块的介入 |
|---|---|---|
| `/人设 <键> <值>`、`/风格 <要求>` | **已被拒绝**（会回答"改人格要去编辑文件"） | 记一条 `executed=False` 的信号 |
| 自然语言「以后叫我哥哥」 | **已被拒绝**（同上） | 记一条 `executed=False` 的信号 |
| 自然语言（`command_natural` 关着） | 不认 | **仍然记一条** ← 这是这一步的最大收益 |

最后一行是关键：**开关关着的时候，他的偏好以前是完全丢掉的**。
现在它至少进了账本，`/人设 信号` 能看到"他一直想让我别说那么长"。

`executed` 这个字段因此**恒为 False**（已无路径能改人设），但保留它是刻意的：
将来若真有某条路径能改人设，账本需要能区分"说过"与"改过"。

## 刻意做窄

只识别**说话方式**上的纠错（长短、称呼、正经度、语气词、表情）。
**刻意不碰**：
* 「记住我喜欢拿铁」这类**内容偏好** —— 那是长期记忆的活（`/记忆 存`、自动抽取），
  混进人设信号只会污染两边；
* 「不要保存这张图」这类**图片策略** —— 已有独立机制（`state.set_image_policy`）；
* 需要推断才能得出的偏好（"他好像不太喜欢…"）—— 那是路线 B 的活，且间接。

宁可漏，不可错：账本里混进一堆误判，第 2 步的阈值就没法定了。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import clock, config, packs, settings

logger = logging.getLogger("ai_chat.signals")

_FILE = "signals.json"
_MAX_ITEMS = 3000       # 账本上限（按时间淘汰最旧的）
_DEDUP_SECONDS = 120    # 同一个人在同一会话里说同一句，两分钟内只记一次
_MAX_TEXT = 120


# --------------------------------------------------------------------- 识别
# 结构：(kind, [正则], 说明)。**只认明确的"说话方式"要求**，不猜。
# `kind` 是稳定的英文标识：账本是给人看的，但归类必须机器可读。
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("length_short", (
        r"别(?:再)?(?:那么|这么|太)?(?:啰嗦|废话|长|多说)",
        r"(?:说|讲|写)(?:短|少|简单|简洁)(?:一?点|些)?",
        r"(?:太|好)(?:长|啰嗦|多)了",
        # 「简短」**单独出现**也要认：`/风格 简短` 的参数就是这两个字，
        # 而它后面没有"点/些"（实测漏过）。同理收「简洁」「短一点」「说短些」。
        r"(?:简短|简洁|精炼|精简)(?:一?点|些)?",
        # 「短句」是槽位里的**取值**（`/人设 回复长度 短句`），必须认。
        # 这一条和下面的 length_long 都要**避开槽位名本身**：「回复长度」含一个"长"字，
        # 早先 `(?:详细|长)(?:一?点|些)?` 把它命中了，账本里多出一条语义相反的记录（实测踩到）。
        r"(?:短句|短一点|说短些|少一点)",
        r"(?:少说|别说那么多|别写小作文|不用那么长|不用(?:说|写)那么(?:多|长))",
        r"(?:说重点|讲重点|别铺垫|直接说)",
    )),
    ("length_long", (
        r"(?:说|讲|写)(?:详细|多|全)(?:一?点|些)?",
        r"(?:多|再)说(?:一?点|些|几?句|点)",
        r"详细(?:一?点|些)?",          # 不再用裸「长」：会命中「回复长度」这个槽位名
        r"别(?:那么|这么)?(?:简短|简单|省字)",
        r"(?:可以|能)(?:再)?多说(?:一?点|些)",
        r"(?:字数|内容)(?:多|长)(?:一?点|些)?",
    )),
    # 否定式必须排在肯定式前面（与 `_NL_RULES` 同一个坑）：
    # 「以后别叫我主人了」同时命中「别叫我」和「叫我」，先判否定才不会把称呼改成「主人了」。
    ("no_call", (
        r"(?:别|不要|不准|不许)(?:再)?(?:叫我|喊我|称呼我为?)",
        r"别用.{0,6}称呼我",
        r"不要叫(?:我|俺)",
    )),
    ("call", (
        r"(?:以后)?(?:叫我|称呼我|叫我做|喊我)",
    )),
    ("formal_on", (
        r"(?:正经|认真|严肃)(?:一?点|些)",
        r"别(?:那么|这么)?(?:贫|闹|跳脱|随意|没个正形)",
        r"(?:好好|认真)说话",
    )),
    ("formal_off", (
        r"(?:轻松|随意|活泼|俏皮)(?:一?点|些)",
        r"别(?:那么|这么)?(?:正经|严肃|板着)",
        r"放开(?:一?点|些)?(?:说|聊)?",
    )),
    ("less_tone", (
        r"别(?:老是|总|一直)?(?:用|加)?语气词",
        r"语气词(?:太|有点)多",
        r"(?:别|不要)(?:老是|总)?(?:用|加)(?:感叹号|波浪号|~)",
    )),
    ("more_tone", (
        r"语气(?:可以)?(?:软|甜|可爱)(?:一?点|些)",
        r"多说(?:一?点|些)?语气词",
    )),
    ("less_emoji", (
        r"别(?:用|发|加)?(?:emoji|表情符号|颜文字|表情)",
        r"(?:emoji|表情符号|颜文字)(?:太|有点)多",
        r"不要(?:用|加)(?:emoji|颜文字)",
    )),
    ("more_emoji", (
        r"(?:用|加|发)(?:一?点|些)?(?:emoji|表情符号|颜文字)",
    )),
    ("less_proactive", (
        r"别(?:老是|总)?(?:主动|自己)(?:找|搭|接)话",
        r"(?:少|别)(?:主动)?(?:插话|插嘴|搭话)",
        r"不用(?:老是|总)?(?:主动)说话",
    )),
    ("more_proactive", (
        r"(?:多|要)(?:主动)(?:找|说|搭|接)?话?",
        r"主动(?:一?点|些)",
        r"别(?:那么|这么)?闷",
    )),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (kind, re.compile("|".join(pats))) for kind, pats in _PATTERNS
)

# 每种信号"如果将来要改，会改哪个槽位"。
# 值是 `persona._CATALOG` 里的键 —— 不在目录里的（如 `语气词` 是合法键、`颜文字` 也是）
# 都必须是真键，否则第 2 步会静默改不动。`on`/`off` 表示方向。
_PROPOSALS: dict[str, dict[str, str]] = {
    "length_short": {"slot": "回复长度", "to": "短句"},
    "length_long": {"slot": "回复长度", "to": "适中"},
    "no_call": {"slot": "自称禁忌", "to": "(追加禁用词)"},
    "call": {"slot": "称呼", "to": "(取原话里的名字)"},
    "formal_on": {"slot": "严肃度", "to": "0.7"},
    "formal_off": {"slot": "严肃度", "to": "0.2"},
    "less_tone": {"slot": "语气词", "to": "低"},
    "more_tone": {"slot": "语气词", "to": "高"},
    "less_emoji": {"slot": "emoji", "to": "关"},
    "more_emoji": {"slot": "emoji", "to": "开"},
    "less_proactive": {"slot": "主动程度", "to": "0.2"},
    "more_proactive": {"slot": "主动程度", "to": "0.7"},
}

# 人话标签，给 `/人设 信号` 与以后的控制台用
_LABELS: dict[str, str] = {
    "length_short": "希望回复短一点",
    "length_long": "希望回复详细一点",
    "no_call": "不喜欢某个称呼",
    "call": "要求改称呼",
    "formal_on": "希望正经一点",
    "formal_off": "希望轻松一点",
    "less_tone": "语气词太多",
    "more_tone": "希望语气软一点",
    "less_emoji": "emoji/颜文字太多",
    "more_emoji": "希望多用 emoji/颜文字",
    "less_proactive": "别太主动搭话",
    "more_proactive": "希望主动一点",
}

# 同一句话里可能同时出现多个信号（「以后别叫我主人，也别那么啰嗦」），
# 但为了账本干净，一条消息最多记两个 —— 再多基本是误判。
_MAX_KINDS_PER_MESSAGE = 2


def detect(text: str) -> list[dict[str, Any]]:
    """从一句话里认出「他在纠正我的说话方式」。返回 0~2 条信号。

    **纯本地、纯正则、不调模型**：这一步每一轮都要跑，不能有成本。
    宁可漏（认不出的就不记），不可错（误判会污染第 2 步的阈值）。
    """
    raw = " ".join(str(text or "").split())[:_MAX_TEXT]
    if not raw:
        return []
    out: list[dict[str, Any]] = []
    hit: set[str] = set()
    for kind, pattern in _COMPILED:
        if len(out) >= _MAX_KINDS_PER_MESSAGE:
            break
        if kind in hit:
            continue
        m = pattern.search(raw)
        if not m:
            continue
        # **互斥规则**：`no_call` 命中了就不再记 `call`。
        # 为什么必须显式写：`no_call` 的正则里 `(?:叫我|喊我)` 与 `call` 的 `叫我` 是同一段文字，
        # 「以后别叫我主人了」会**同时**命中两者（实测就是）。只靠"先判否定"能保证**取哪一条**，
        # 但保证不了**只记一条** —— 而账本里混进一条语义相反的记录，会让第 2 步的
        # "同类出现 3 次"统计直接翻倍。同类问题在 `_NL_RULES` 里也存在（它靠 `continue` 躲过去）。
        if kind == "call" and "no_call" in hit:
            continue
        hit.add(kind)
        out.append(
            {
                "kind": kind,
                "label": _LABELS.get(kind, kind),
                "quote": m.group(0)[:40],
                "text": raw,
                "propose": _PROPOSALS.get(kind, {}),
            }
        )
    return out


def kinds_text() -> str:
    """可用信号类别清单（给人看的）。"""
    return "\n".join(f"· {k} —— {v}" for k, v in _LABELS.items())


# --------------------------------------------------------------------- 账本
class _Store:
    """一小份 JSON 账本。**独立文件**：它是"观察数据"，不该跟人设或记忆混在一起。

    为什么不进 SQLite：它量很小（上限 3000 条）、只追加、且**可以随时清掉**
    （它不参与任何回复）。放独立 JSON 的好处是能直接打开看、能直接删。
    """

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.loaded = False
        self._lock = threading.Lock()

    def _path(self) -> Path:
        return config.persona_data_dir() / _FILE

    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    def load(self) -> None:
        self.loaded = True
        path = self._path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # 观察数据坏了不该影响任何事：丢账本比丢人设轻得多
            logger.warning("人设信号账本损坏，按空账本继续：%s", path.name)
            return
        if not isinstance(raw, dict):
            return
        self.items = [x for x in (raw.get("items") or []) if isinstance(x, dict) and x.get("kind")]

    def save(self) -> None:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "note": "人设信号账本：他说过的「说话方式」要求。只观察，不改人设。",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", clock.localtime()),
                "count": len(self.items),
                "items": self.items[-_MAX_ITEMS:],
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("人设信号账本写盘失败：%s", path.name)


    def reset(self) -> None:
        """丢掉内存副本，下次 `ensure()` 从**当前人格包**的目录重读。

        账本按包分开存（`data/runtime/persona/<包>/signals.json`），
        所以换人格时必须清 —— 否则新角色会带着旧角色的"他说过要这么说话"的记录，
        而自我迭代会把这份记录当成**用户显式要求**（权重远高于自己猜的）。
        """
        self.items = []
        self.loaded = False


_store = _Store()


def reset_store() -> None:
    """换包时由 `packs` 回调：账本从当前包的目录重读。"""
    _store.reset()


packs.on_change(reset_store)


def _same_recent(conv: str, uid: int, kind: str, text: str) -> bool:
    """两分钟内同一会话、同一人、同一类、同一句话 → 视为重复，不再记。"""
    now = clock.now()
    for item in reversed(_store.items[-40:]):
        try:
            if (
                item.get("conv") == conv
                and int(item.get("uid", 0) or 0) == int(uid)
                and item.get("kind") == kind
                and item.get("text") == text
                and (now - float(item.get("ts", 0) or 0)) < _DEDUP_SECONDS
            ):
                return True
        except (TypeError, ValueError):
            continue
    return False


def record(
    signals: list[dict[str, Any]],
    *,
    conv: str,
    uid: int = 0,
    executed: bool = False,
    source: str = "自然语言",
) -> int:
    """把识别到的信号落账。返回真正记下的条数（去重后）。

    `executed=True` 表示这条要求**同时已经被执行了**（`command_natural` 开着时）。
    这个字段很重要：以后统计"说了三次才改"的阈值时，
    必须能把"已经改过的"和"只是说过"的分开。
    """
    if not settings.get("persona_signal_enabled") or not signals:
        return 0
    _store.ensure()
    now = clock.now()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", clock.localtime(now))
    added = 0
    with _store._lock:  # noqa: SLF001 - 同一个模块内的私有锁
        for sig in signals:
            kind = str(sig.get("kind") or "")
            text = str(sig.get("text") or "")
            if not kind or _same_recent(conv, uid, kind, text):
                continue
            _store.items.append(
                {
                    "ts": round(float(now), 3),
                    "at": stamp,
                    "conv": conv,
                    "uid": int(uid or 0),
                    "kind": kind,
                    "label": str(sig.get("label") or _LABELS.get(kind, kind)),
                    "quote": str(sig.get("quote") or "")[:40],
                    "text": text,
                    "propose": sig.get("propose") or _PROPOSALS.get(kind, {}),
                    "executed": bool(executed),
                    "source": str(source)[:20],
                }
            )
            added += 1
        if added:
            if len(_store.items) > _MAX_ITEMS:
                del _store.items[: len(_store.items) - _MAX_ITEMS]
            _store.save()
    if added:
        logger.info(
            "人设信号 +%d（%s）conv=%s executed=%s",
            added, "、".join(s.get("kind", "") for s in signals), conv, executed,
        )
    return added


# --------------------------------------------------------------------- 查询
def all_items() -> list[dict[str, Any]]:
    _store.ensure()
    return list(_store.items)


def counts(kind: str | None = None) -> dict[str, int]:
    """按类别统计次数（账本最有用的视图）。`kind=None` 时返回全部类别。"""
    _store.ensure()
    out: dict[str, int] = {}
    for item in _store.items:
        k = str(item.get("kind") or "")
        if kind is not None and k != kind:
            continue
        out[k] = out.get(k, 0) + 1
    return out


def report(limit: int = 12, *, conv: str = "") -> str:
    """给人看的账本摘要：按次数排序 + 最近几条原话。

    这个视图就是第 2 步定阈值的依据 —— 所以它必须显示**原话**，
    只有类别计数的话没法判断"这条算不算误会"。
    """
    _store.ensure()
    items = [x for x in _store.items if not conv or x.get("conv") == conv]
    if not items:
        return (
            "还没有任何信号。\n"
            "（这个账本只在你说「别那么啰嗦」「叫我哥哥」这类**说话方式**的要求时记录，"
            "内容偏好走长期记忆，不在这里）"
        )

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_kind.setdefault(str(item.get("kind") or "?"), []).append(item)

    lines: list[str] = [f"共 {len(items)} 条信号，{len(by_kind)} 类："]
    for kind, group in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
        label = _LABELS.get(kind, kind)
        executed = sum(1 for x in group if x.get("executed"))
        tail = f"，其中 {executed} 条当时就改过了" if executed else ""
        lines.append(f"\n· 「{label}」{len(group)} 次{tail}")
        prop = _PROPOSALS.get(kind, {})
        if prop:
            lines.append(f"    （第 2 步会提议：{prop.get('slot')} → {prop.get('to')}）")
        for x in group[-2:]:
            lines.append(f"    {x.get('at', '')[:16]} 他说「{x.get('quote') or x.get('text', '')[:20]}」")

    newest = max(items, key=lambda x: float(x.get("ts", 0) or 0))
    lines.append(f"\n最近一条：{newest.get('at', '')[:16]}（{newest.get('label')}）")
    lines.append("\n（**这些还没有改任何人设** —— 现在只记账，改不改由你定。）")
    return "\n".join(lines)


def stats() -> dict[str, Any]:
    _store.ensure()
    return {
        "enabled": bool(settings.get("persona_signal_enabled")),
        "items": len(_store.items),
        "kinds": len(counts()),
        "executed": sum(1 for x in _store.items if x.get("executed")),
    }


def clear() -> int:
    _store.ensure()
    n = len(_store.items)
    _store.items.clear()
    if n:
        _store.save()
    return n
