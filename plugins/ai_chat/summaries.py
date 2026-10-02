"""会话摘要：把「刚刚结束的那一轮」压成一两句客观记录，接在被丢弃的背景后面。

## 它填的是哪个漏

`chatlog.render_background()` 的预算是 `read_budget`（默认 2000 字符），从最新往回累加，
**装不下的更早记录在这一轮 prompt 里直接丢弃**（盘上还在，但没有任何路径把它捞回来）。
按中文群常见的 10~40 字一条算，2000 字符大约只装得下 50~100 条 ——
而实测单个群一天就能到几百条。所以「模型只看得见最近几十条」是常态。

摘要层把这段空白补上：每一轮会话结束时生成一句客观归纳，注入时挂在原文背景之后。
于是预算是「**原文 + 摘要**」而不是「原文 + 一个『更早的 N 条已省略』」。
**原文一条都不删** —— 这里只增加一层可回顾的索引，不替代任何东西。

## 为什么触发点是「会话切分」而不是定时任务

`chatlog.append()` 在两条消息间隔超过 `session_gap` 时 `session += 1`。
那一刻：

1. **上一轮的内容已经定型**，不会再有新消息进来（这正是「总结」需要的边界）；
2. `messages[].session` 已经能精确定位「哪些消息属于那一轮」——
   **不需要为摘要另建任何索引**；
3. 切分本身很稀有（默认要隔 5 分钟），所以它天然是低成本的触发点，
   不会像「每 N 条消息总结一次」那样在活跃群里反复烧 token。

## 刻意不做的事

* **不写进 `memories.json`**：摘要的寿命与失效条件和事实条目不同（它按会话轮次组织，
  且天然会越来越多），混进去会让 `compact()` 的淘汰策略同时管两种语义。
  它落在自己的 `data/runtime/session_summaries.json` 里。
* **不删原文**：摘要只是「回顾入口」。原始聊天记录永远留在 chatlog 里。
* **失败不重试、不抛**：摘要丢了只是那一轮没有回顾入口，属于可接受的降级；
  让摘要失败影响聊天是本末倒置。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any


from . import chatlog, clock, config, llm, settings

logger = logging.getLogger("ai_chat.summaries")


_FILE = "session_summaries.json"
_MAX_TEXT = 240        # 单条摘要上限（提示词里也要求 200 字以内）
_MIN_MESSAGES = 3      # 少于这么多条的一轮不值得总结（"嗯""哈哈"那几句）
# 每个会话最多留多少条。真值走 settings 的 `summary_max_per_conv`，
# 这里只是 spec 缺失时的兜底（见 `_max_per_conv()`）。
_MAX_PER_CONV = 40

_state: dict[str, list[dict[str, Any]]] = {}
_loaded = False
_lock = asyncio.Lock()

# 摘要库坏了就别再往上写（见 `_load()`）。与 memories.json 的策略不同：
# 那个进只读、**绝不覆盖**；摘要因为"只是回顾入口"允许清空重来，
# 但**至少要先把坏文件改名留证**，否则读不出又写回等于把证据抹掉。
_readonly_reason = ""


def _max_per_conv() -> int:
    try:
        value = int(settings.get("summary_max_per_conv"))
    except (TypeError, ValueError):
        return _MAX_PER_CONV
    return value if value > 0 else _MAX_PER_CONV

# 已经总结到哪一轮（防同一轮被总结两次）。只放内存：重启后最多重复总结一次，
# 代价是一次小请求，不值得为它再落一份盘。
_summarized_upto: dict[str, int] = {}
# 正在总结中的会话，避免并发触发同一轮
_inflight: set[str] = set()


def _path() -> Path:
    return config.LOG_DIR / _FILE


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", clock.localtime())


def _load() -> None:
    global _loaded, _readonly_reason
    if _loaded:
        return
    _loaded = True
    path = _path()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        # 摘要库坏了不是灾难：它只是"回顾入口"，原文还在 chatlog 里。
        # 所以策略与 memories.json 不同 —— **清空重来即可**，不必长期只读。
        #
        # 但**必须先改名留证**：原来只记一条 warning 就
        # 按空库继续，而下一次 `_save()` 会把那个损坏文件**原地覆盖** ——
        # 「读不出来就当没有」和「把证据抹掉」是两件事。现在与 chatlog / memory
        # 用同一个姿势：改名成 `<名字>.corrupt-<时间戳>`，然后重新开始。
        #
        # **`_readonly_reason` 只在"连改名都失败"时才设**：设早了会让留证之后的
        # 正常写入被自己拦住（写这个函数时就是这么错的，测试里钉住了这条）。
        reason = f"{type(exc).__name__}"
        bak = path.with_name(
            f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        try:
            path.replace(bak)
            logger.error(
                "会话摘要库损坏（%s），已改名留证并重新开始：%s → %s（原文不受影响）",
                reason, path.name, bak.name,
            )
        except OSError:
            # 改名都失败（权限/被占用）→ 这时才真的只能放弃写盘，否则会覆盖证据
            _readonly_reason = reason
            logger.error(
                "会话摘要库损坏（%s）且改名失败，本次运行不再写摘要：%s",
                reason, path,
            )
        return
    if not isinstance(raw, dict):
        logger.warning("会话摘要库顶层不是对象，按空库继续：%s", path.name)
        return
    for conv, items in (raw.get("conv") or {}).items():
        if isinstance(items, list):
            _state[str(conv)] = [x for x in items if isinstance(x, dict) and x.get("text")]


def _save() -> None:
    if _readonly_reason:
        logger.warning(
            "会话摘要库处于只读（原因：%s），本次不写盘", _readonly_reason,
        )
        return
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "note": "会话轮次摘要。只是「回顾入口」，原文永远留在 chatlog_*.json 里。",
            "updated_at": _now(),
            "conv": _state,
        }
        tmp = path.with_name(path.name + ".tmp")
        # 先 flush + fsync 再 replace：摘要库是**整份重写**的，
        # 少了 fsync 的话掉电/强杀可能留下一个"文件名对、内容是半截"的 JSON，
        # 而那正是上一步要防的东西。
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except OSError as exc:
        logger.warning(
            "会话摘要写盘失败（不影响聊天）：%s（%s: %s）",
            path.name, type(exc).__name__, exc,
        )


# --------------------------------------------------------------------- 生成
_SUMMARY_PROMPT = """下面是一个群/私聊**某一轮**对话的完整记录。请用一两句中文客观归纳这一轮发生了什么。

要求：
- 只写记录里**确实出现**的事：谁说了什么、做了什么决定、聊到了什么话题；
- **不要**写情绪氛围、关系变化、"气氛如何"这类主观判断 —— 除非记录里有人明说了；
- **不要**把一件事安到错误的人头上（谁说的就记谁说的，名字照抄原记录）；
- 不要写"这段对话讨论了…"这种套话，直接写内容；
- 不超过 {limit} 字，一句话能说清就一句话；
- 如果这一轮确实没有值得回顾的内容（纯寒暄、纯刷屏），只输出两个字：无

【这一轮的记录】
{lines}
"""


def _render_lines(msgs: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for m in msgs:
        who = str(m.get("name", ""))
        if m.get("is_bot"):
            who = f"{who}（你）"
        text = chatlog.ConversationLog.clip_text(m.get("text", ""), 200)
        if text:
            out.append(f"[{m.get('time', '')} {who}] {text}")
    return "\n".join(out)


async def _summarize(msgs: list[dict[str, Any]]) -> str:
    """调一次模型生成摘要。失败返回空串，绝不抛。"""
    lines = _render_lines(msgs)
    if not lines:
        return ""
    prompt = _SUMMARY_PROMPT.replace("{limit}", str(_MAX_TEXT)).replace("{lines}", lines)
    try:
        resp = await asyncio.wait_for(
            llm.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
            ),
            timeout=config.TIMEOUT,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    except Exception:  # noqa: BLE001 - 摘要失败绝不能影响聊天
        logger.info("会话摘要生成失败（不影响聊天）")
        return ""
    text = " ".join(text.split())
    if not text or text.strip("。.") in ("无", "（无）", "none"):
        return ""
    return text[:_MAX_TEXT]


async def summarize_session(conv: str, session: int) -> dict[str, Any] | None:
    """给指定会话轮次生成并保存摘要。返回条目或 None。"""
    if not settings.get("summary_enabled") or not llm.api_key():
        return None
    log = chatlog.get_log_sync(conv)
    if log is None:
        return None
    msgs = log.session_range(session, include_bot=True)
    if len(msgs) < int(settings.get("summary_min_messages") or _MIN_MESSAGES):
        return None

    text = await _summarize(msgs)
    if not text:
        return None

    participants: list[str] = []
    for m in msgs:
        name = str(m.get("name", ""))
        if name and name not in participants and not m.get("is_bot"):
            participants.append(name)

    item = {
        "session": int(session),
        "text": text,
        "from": str(msgs[0].get("time", "")),
        "to": str(msgs[-1].get("time", "")),
        "from_ts": float(msgs[0].get("ts") or 0),
        "to_ts": float(msgs[-1].get("ts") or 0),
        "messages": len(msgs),
        "who": participants[:6],
        "at": _now(),
    }
    async with _lock:
        _load()
        items = _state.setdefault(conv, [])
        # 同一轮重复触发时覆盖（例如进程重启后重复跑了一次）
        items[:] = [x for x in items if int(x.get("session", -1)) != int(session)]
        items.append(item)
        items.sort(key=lambda x: float(x.get("to_ts") or 0))
        cap = _max_per_conv()
        if len(items) > cap:
            del items[: len(items) - cap]
        await asyncio.to_thread(_save)
    logger.info(
        "会话摘要已生成 conv=%s session=%d（%d 条消息 → %d 字）",
        conv, session, len(msgs), len(text),
    )
    return item


def on_session_rolled(conv: str, session: int, spawn: Any = None) -> None:
    """会话刚切分时调用：**非阻塞**地给上一轮生成摘要。

    `session` 传切分**之后**的新轮次号，这里总结的是 `session - 1`。
    这个函数不返回任何东西、也不抛异常 —— 它的调用点在消息落盘的主路径上，
    绝不能因为摘要让消息处理慢下来。

    `spawn` 是调用方传进来的任务启动器（`__init__._spawn`）。**必须传**：
    插件自己那个 `_spawn` 会持有任务的强引用，避免被 GC 提前回收 ——
    这里若自己 `asyncio.create_task` 而不留引用，摘要任务可能跑到一半就消失。
    """
    prev = int(session) - 1
    if prev <= 0:
        return
    if not settings.get("summary_enabled"):
        return
    if _summarized_upto.get(conv, -1) >= prev:
        return
    key = f"{conv}#{prev}"
    if key in _inflight:
        return
    if spawn is None:
        return  # 没有任务启动器就放弃摘要（离线脚本常见），不影响主流程
    _inflight.add(key)

    async def _runner() -> None:
        try:
            item = await summarize_session(conv, prev)
            if item is not None:
                _summarized_upto[conv] = max(_summarized_upto.get(conv, -1), prev)
        except Exception:  # noqa: BLE001
            logger.exception("会话摘要任务异常 conv=%s session=%d", conv, prev)
        finally:
            _inflight.discard(key)

    try:
        spawn(_runner())
    except Exception:  # noqa: BLE001 - 启动失败也只是没有摘要
        _inflight.discard(key)
        logger.info("会话摘要任务未能启动 conv=%s session=%d", conv, prev)


# --------------------------------------------------------------------- 读取
def recent(conv: str, limit: int | None = None, before_session: int | None = None) -> list[dict[str, Any]]:
    """取最近的几轮摘要（按时间正序返回，方便直接拼成一段叙事）。"""
    _load()
    items = list(_state.get(conv) or [])
    if before_session is not None:
        items = [x for x in items if int(x.get("session", 0)) < int(before_session)]
    items.sort(key=lambda x: float(x.get("to_ts") or 0))
    n = int(settings.get("summary_recall_count")) if limit is None else int(limit)
    if n > 0:
        items = items[-n:]
    return items


def render(conv: str, budget: int | None = None, before_session: int | None = None) -> str:
    """把最近几轮摘要渲染成 prompt 小节；没有可用的返回空串。

    从**最新往回**装预算：最新的回顾最有用，装不下的更早摘要舍弃。
    """
    if not settings.get("summary_enabled"):
        return ""
    budget = int(settings.get("summary_budget")) if budget is None else int(budget)
    items = recent(conv, before_session=before_session)
    if not items:
        return ""

    kept: list[str] = []
    used = 0
    for item in reversed(items):  # 从最新往回
        line = f"- {item.get('from', '')}~{item.get('to', '')}：{item.get('text', '')}"
        if budget > 0 and used + len(line) > budget and kept:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    if not kept:
        return ""
    return (
        "【更早几轮的摘要】（原文已经不在上面的记录里了，这些是当时发生的事的归纳）\n"
        + "\n".join(kept)
    )


def stats() -> dict[str, Any]:
    _load()
    return {
        "conversations": len(_state),
        "items": sum(len(v) for v in _state.values()),
        "enabled": bool(settings.get("summary_enabled")),
        "min_messages": int(settings.get("summary_min_messages")),
        "recall_count": int(settings.get("summary_recall_count")),
        "budget": int(settings.get("summary_budget")),
        "max_per_conv": _max_per_conv(),
        # 非空 = 摘要库损坏过、本次运行不写盘。控制台看到它就该去看日志里那条留证路径。
        "readonly_reason": _readonly_reason,
    }


# --------------------------------------------------------------------- 缺口盘点
# 触发点是「会话切分」，也就是只有**功能上线之后**新结束的轮次才会被总结。
# 上线之前盘上已有的那些轮次永远不会被总结，而 `render_background()` 又确实
# 会把它们丢掉 —— 于是那段时间只剩一句「更早的 N 条原文没有放进本节」，
# 后面是**没有摘要的空白**。这个缺口不会自愈（切分只处理刚结束的那一轮）。
# `_工具链/维护/_回填摘要.py` 负责补，这里提供"缺口在哪"的只读盘点。
def session_is_missing(conv: str, session: int) -> bool:
    """这一轮是不是**缺摘要**。"""
    return int(session) not in {
        int(x.get("session", -1)) for x in (_state.get(conv) or [])
    }


def missing_sessions(conv: str) -> list[int]:
    """这个会话里「有消息、但没有摘要」的轮次号，升序。"""
    log = chatlog.get_log_sync(conv)
    if log is None:
        return []
    _load()
    have = {int(x.get("session", -1)) for x in (_state.get(conv) or [])}
    sessions: set[int] = set()
    for m in log.messages:
        try:
            sid = int(m.get("session") or 0)
        except (TypeError, ValueError):
            continue
        if sid > 0:
            sessions.add(sid)
    return sorted(sessions - have)
