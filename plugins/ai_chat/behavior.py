"""行为计数：把"反复"变成**代码能数清的事实**，而不是指望模型自觉。

## 为什么要有它

现场：主人明说"她过于频繁地提到时间"，自动迭代在 **02:34 就把规则写进了
表层人设**（`别老提时间、几点、睡不睡——提多了像在催人`），但它**照样提**。
A/B 实测（用真实对话各生成 12 次）也证实：**加规则量不出改善**。

原因是结构性的 —— 组装后的 system 里：

| 块 | 字符数 |
|---|---|
| 底层人设（含【提问】【追问】两节**在教它怎么问**） | 2370 |
| 表层人设（**「有时候先问回去」**） | 1161 |
| 当前时间锚（每轮现给确切时间戳，如「02:51 凌晨」） | 141 |
| **「别反复提时间」那一条规则** | **23** |

规则是**抽象的、一成不变的**；而"它这一轮又提了时间"是**可数的**。
所以这里把后者数出来，在**回复前**用一句**当轮专用**的抑制指令压过去 ——
和项目里其它地方的分工一致：算术与状态交给代码，语义交给模型。

## 判据现在从哪来

原来窗口、阈值、正则、抑制文案**全部硬编码在本文件里**，只覆盖"提时间"与"追问"两个特质。
现在它们登记在 `persona/active/traits.json` 的 `guards` 下，本文件按注册表构造判定器 ——
**于是"加一个可数守卫"变成加一条数据，而不是改这个文件。**

**内置的 `TIME_RE` / `ASK_RE` 与默认窗口阈值继续保留**：注册表缺失或损坏时守卫照常工作。
闸门不能因为一个 JSON 读不到就消失 —— 与 `persona.py` 保留 `_LEGACY_CONFLICTS` 同一个理由。

**刻意不过滤 `parked` 特质**：停用的是"铁律那一行"，而可数守卫是另一套机制。
实测恰恰证明管用的是守卫（写规则量不出差异），所以停用铁律**不该**连守卫一起关掉。

## 判据

只看**最近 window 条自己的回复**（默认 3 条）：
* 某条规则的 `pattern` 命中 ≥ `threshold` 条（默认 2）→ 判定"这一类话上头了"

`ASK_RE` 刻意**只认追问句**（"你问这个想干嘛""你是要干嘛"），不认普通的问句结尾 ——
大部分正常回复结尾带个"？"（实测线上占 35.9%），把它算进去等于每条都命中。
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger("ai_chat.behavior")

# 每个会话只留最近几条自己的回复（判定"反复"够用，也不占内存）
_KEEP = 8
_DEFAULT_WINDOW = 3
_DEFAULT_THRESHOLD = 2

_recent: dict[str, list[tuple[float, str]]] = {}

# 提时间 / 催睡觉这类话（对照线上实测抓到的 29 条样本写的）
TIME_RE = re.compile(
    r"几点|一点半|两点|两点多|多晚|这么晚|都\s*\d+\s*点|该睡|去睡|别熬|早点睡|熬夜|不早了|"
    r"这个点|天亮|快\d+点|几号|深夜"
)
# **只认追问/反问**，不认普通问句（否则命中率天然 35%+，闸门形同虚设）
ASK_RE = re.compile(
    r"你问(这个|这)?(想)?干嘛|问这个干嘛|你是要干嘛|你到底想|你想干嘛|"
    r"你(为什么|怎么)(老|总|又)?问|你是不是有(什么|啥)目的|"
    r"你到底打算|你打算绕到|你是不打算"
)

# 内置默认（注册表读不到时用它）。每条是
# `(id, label, 正则, 抑制文案, window, threshold)` —— **窗口与阈值是每条规则自己的**：
# 有的毛病 3 条就够判（提时间），有的要 5 条（评论对话）。
# `id` 会成为 stats() 的键名，所以沿用 time / ask。
_DEFAULT_RULES: tuple[tuple[str, str, "re.Pattern[str]", str, int, int], ...] = (
    (
        "time",
        "提时间",
        TIME_RE,
        "**这一轮绝对不要再提时间**（几点、多晚、该睡、去睡、熬夜、这个点）——"
        "你最近 %d 条里已经提了 %d 次。哪怕对方正好在聊时间，也只用一句客观回答，"
        "不要顺势催他。",
        3,
        2,
    ),
    (
        "ask",
        "追问",
        ASK_RE,
        "**这一轮不要追问、不要反问、不要分析对方为什么这么问**——"
        "你最近 %d 条里已经追问了 %d 次。答完就停，不用把话头抛回去。",
        3,
        2,
    ),
)

_spec_cache: tuple | None = None


def _positive_int(value: object, default: int) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _build_spec() -> tuple:
    """按 `persona/active/traits.json` 的 guards 构造判定器；注册表用不上就退回内置默认。

    返回 `(默认窗口, 默认阈值, rules)`，`rules` 每条是
    `(id, label, 正则, 抑制文案, window, threshold)`。

    **窗口与阈值按每条规则解析**（规则自己的 > guards 块上的 > 内置默认）。
    原来它们是全局量、后写的覆盖先写的 —— 加第三条守卫时会**悄悄改掉前两条的行为**，
    这是个真缺陷，不是风格问题。
    """
    rules: list[tuple[str, str, re.Pattern[str], str, int, int]] = []
    try:
        from . import config
    except ImportError:
        # 没有包上下文 —— `_行为闸门验证.py` 会把本模块**单独加载**来测纯逻辑。
        # 这是**正常路径**而不是错误，所以不打异常栈，直接用内置默认。
        return _DEFAULT_WINDOW, _DEFAULT_THRESHOLD, _DEFAULT_RULES
    try:
        for trait in config.load_traits():
            guards = trait.get("guards")
            if not isinstance(guards, dict):
                continue
            block_win = _positive_int(guards.get("window"), _DEFAULT_WINDOW)
            block_th = _positive_int(guards.get("threshold"), _DEFAULT_THRESHOLD)
            for rule in guards.get("rules") or []:
                if not isinstance(rule, dict):
                    continue
                pat = str(rule.get("pattern") or "")
                note = str(rule.get("note") or "")
                if not pat or not note:
                    continue
                try:
                    compiled = re.compile(pat)
                except re.error:
                    logger.warning("guards 里的正则有误，跳过：%r", pat[:40])
                    continue
                rules.append((
                    str(rule.get("id") or "?"),
                    str(rule.get("label") or "?"),
                    compiled,
                    note,
                    _positive_int(rule.get("window"), block_win),
                    _positive_int(rule.get("threshold"), block_th),
                ))
    except Exception:  # noqa: BLE001 - 守卫绝不能因为注册表坏了而消失
        logger.exception("guards 构造失败，退回内置判定")
        return _DEFAULT_WINDOW, _DEFAULT_THRESHOLD, _DEFAULT_RULES
    if not rules:
        return _DEFAULT_WINDOW, _DEFAULT_THRESHOLD, _DEFAULT_RULES
    return _DEFAULT_WINDOW, _DEFAULT_THRESHOLD, tuple(rules)


def _spec() -> tuple:
    """判定器（构造一次即可：guards 是配置，不像表层人设那样会被运行时改写）。"""
    global _spec_cache
    if _spec_cache is None:
        _spec_cache = _build_spec()
    return _spec_cache


def note_reply(conv: str, text: str) -> None:
    """记下自己刚发出的一条回复。失败不影响回复（只是闸门少一份依据）。"""
    if not conv or not (text or "").strip():
        return
    bucket = _recent.setdefault(conv, [])
    bucket.append((time.time(), str(text)))
    del bucket[:-_KEEP]


def _hits(lines: list[str], pattern: re.Pattern[str]) -> int:
    return sum(1 for x in lines if pattern.search(x))


def _lines(conv: str, window: int) -> list[str]:
    bucket = _recent.get(conv) or []
    return [t for _, t in bucket[-window:]]


def _render(note: str, seen: int, hit: int) -> str:
    """填 note 里的 %d 占位。

    文案里万一出现别的百分号（比如"完成度 80%"），格式化会抛 —— 那就原样返回，
    **绝不能让一句提示语把整条回复打挂**。
    """
    try:
        return note % (seen, hit)
    except (TypeError, ValueError):
        return note


def suppress_note(conv: str) -> str:
    """返回**当轮专用**的抑制指令；没上头就返回空串（不占 prompt）。

    **每条规则用自己的窗口与阈值** —— 见 `_build_spec()` 里那段说明。
    """
    _d_win, _d_th, rules = _spec()

    clauses: list[str] = []
    hit_log: list[str] = []
    for _rule_id, label, pattern, note, win, th in rules:
        lines = _lines(conv, win)
        if not lines:
            continue
        n = _hits(lines, pattern)
        if n >= th:
            clauses.append(_render(note, len(lines), n))
            hit_log.append("%s %d/%d" % (label, n, len(lines)))
    if not clauses:
        return ""

    logger.info("行为闸门命中 conv=%s：%s", conv, "、".join(hit_log))
    return "【本轮特别提醒（由系统统计最近几条回复得出）】\n- " + "\n- ".join(clauses)


def stats(conv: str) -> dict:
    """`{"window": <最宽的那条规则的窗口>, "<规则id>": 命中数, …}`。

    报的是**最宽的**规则窗口 —— 几条规则的窗口不同，单报一个数只能这么取；
    每条规则的命中数仍按**它自己的**窗口算。
    """
    _d_win, _d_th, rules = _spec()
    widest = max([r[4] for r in rules] or [_DEFAULT_WINDOW])
    out: dict = {"window": len(_lines(conv, widest))}
    for rule_id, _label, pattern, _note, win, _th in rules:
        out[rule_id] = _hits(_lines(conv, win), pattern)
    return out


def clear(conv: str) -> None:
    _recent.pop(conv, None)
