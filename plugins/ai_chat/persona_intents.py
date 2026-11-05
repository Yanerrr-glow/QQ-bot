"""短期待处理事项：有证据地创建、有证据地完成、到期就过期（#0012）。

## 它解决什么

「他刚才问的那个问题我还没答」这件事，现在没有任何地方记着：下一轮只看到聊天记录，
模型不会主动想起来。这里给它一个**有生命周期的小账本**：

* 只在正常对话里消费 —— **不外发、不恢复 proactive、不新增每轮模型调用**；
* 只用本轮深判已经产出的结构化字段（`pending`），不从关键词猜；
* 只存 `message_id + ≤40 字摘要`，不存第二份聊天原文。

## 三条硬规矩

1. **"提起"不等于"解决"**：把事项写进 prompt 只说明"还没答"，不改状态。
2. **不能靠数值变化断言完成**（duty 降 ≠ 事做完）；只认"匹配的答复 + 可核对的证据"。
3. **未回应不是拒绝**：无扣分、无冷落、无 streak；到期只是 `expired`。

## 两份缓存，别混用

* `_JUDGE`：深判前写，存**回合内令牌 → 事项 id**（模型看到的 `[P1]` 只能在这里解析）；
* `_SENT`：注入时**现读** pending 生成，存**实际发出去的 id**。

混成一份的后果：`settle` 之后 `context.build()` 会命中旧文本，把刚结的事项继续注入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from . import config, filelock, persona_workspace, settings

logger = logging.getLogger("ai_chat.persona_intents")

KINDS = ("open_topic", "clarify")
STATUSES = ("pending", "resolved", "expired", "cancelled")
_MAX_ATTEMPTS = 3
_TOMB_TTL = 7 * 86400.0
_MAX_CLAIMS = 200
_CLAIM_TTL = 600.0
_SMALL_TALK = ("嗯", "哦", "好", "好的", "在吗", "哈哈", "收到", "知道了", "？", "?")

_JUDGE: dict[tuple, dict[str, Any]] = {}
_SENT: dict[tuple, dict[str, Any]] = {}


def _now() -> float:
    return persona_workspace.now()


def _sget(key: str, default: Any = None) -> Any:
    try:
        got = settings.get(key)
        return default if got is None else got
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    return bool(_sget("intent_enabled", True))


def _ttl_hours() -> float:
    return max(0.5, float(_sget("intent_ttl_hours", 3.0) or 3.0))


def _max_items() -> int:
    return max(1, int(_sget("intent_max_items", 5) or 5))


def _pack() -> str:
    try:
        from . import packs

        return str(packs.active_id() or "default")
    except Exception:  # noqa: BLE001
        return "default"


def _path():
    return config.persona_data_dir() / "intents.json"


def _scope(conv: str, uid: str) -> str:
    return f"{conv}|{uid}"


def _blank() -> dict[str, Any]:
    return {"version": 1, "at": _now(), "items": {}, "refused": {}, "tombs": {}}


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return _blank()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("待办数据读不动，按空处理：%s", path.name)
        return _blank()
    state = _blank()
    if isinstance(raw, dict):
        for key in ("items", "refused", "tombs"):
            if isinstance(raw.get(key), dict):
                state[key] = raw[key]
        if isinstance(raw.get("at"), (int, float)):
            state["at"] = float(raw["at"])
    return state


def _save(state: dict[str, Any]) -> bool:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        logger.warning("待办数据写盘失败：%s", path)
        return False


def _item_id(scope: str, message_id: Any, kind: str) -> str:
    return "i:" + hashlib.sha1(f"{scope}|{message_id}|{kind}".encode("utf-8")).hexdigest()[:12]


def _rows(state: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    rows = state["items"].get(scope)
    return rows if isinstance(rows, list) else []


def _live(state: dict[str, Any], scope: str, now: float, *, sweep: bool = True) -> list[dict[str, Any]]:
    """本 scope 里仍然 pending 的事项（顺手把过期的标掉）。"""
    out: list[dict[str, Any]] = []
    for row in _rows(state, scope):
        if not isinstance(row, dict):
            continue
        if row.get("status") != "pending":
            continue
        if float(row.get("expires") or 0) <= now:
            if sweep:
                row["status"] = "expired"
                row["expired_at"] = now
                _tomb(state, str(row.get("id") or ""), "expired", now)
            continue
        out.append(row)
    out.sort(key=lambda item: float(item.get("created") or 0))
    return out


def _tomb(state: dict[str, Any], item_id: str, status: str, now: float) -> None:
    if item_id:
        state["tombs"][item_id] = {"at": now, "status": status}
    for key in [k for k, v in state["tombs"].items()
                if isinstance(v, dict) and now - float(v.get("at") or 0) > _TOMB_TTL]:
        state["tombs"].pop(key, None)


def _trim_claims(now: float) -> None:
    for store in (_JUDGE, _SENT):
        for key in [k for k, v in store.items()
                    if not isinstance(v, dict) or now - float(v.get("at") or 0) > _CLAIM_TTL]:
            store.pop(key, None)
        if len(store) > _MAX_CLAIMS:
            ordered = sorted(store.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))
            for key, _ in ordered[: len(store) - _MAX_CLAIMS]:
                store.pop(key, None)


def _key(conv: str, uid: str, message_id: Any) -> tuple:
    return (_pack(), str(conv), str(uid), str(message_id))


# ------------------------------------------------------------------ 创建与查询
def propose(conv: str, uid: str, *, kind: str, message_id: Any, summary: str,
            evidence: list[dict[str, Any]] | None = None, now: float | None = None) -> dict[str, Any]:
    """有证据地建一条待办。**缺证据、缺主键、摘要不合格一律不建**。"""
    if not enabled():
        return {"ok": False, "why": "disabled"}
    stamp = _now() if now is None else float(now)
    body = str(summary or "").strip()
    if message_id is None:
        return {"ok": False, "why": "no-message-id"}
    if str(kind) not in KINDS:
        return {"ok": False, "why": "bad-kind"}
    if not (1 <= len(body) <= 40):
        return {"ok": False, "why": "bad-summary"}
    rows = [row for row in (evidence or [])
            if isinstance(row, dict) and int(row.get("message_id") or 0) > 0]
    if not rows:
        return {"ok": False, "why": "no-evidence"}
    scope = _scope(conv, uid)
    item_id = _item_id(scope, message_id, str(kind))
    path = _path()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return {"ok": False, "why": "busy"}
        state = _load()
        if item_id in state["tombs"]:
            return {"ok": False, "why": "tombstoned"}
        for row in _rows(state, scope):
            if str(row.get("source_message_id")) == str(message_id):
                return {"ok": False, "why": "exists", "item": row}
        live = _live(state, scope, stamp)
        dropped: list[dict[str, Any]] = []
        while len(live) >= _max_items():
            gone = min(live, key=lambda item: float(item.get("expires") or 0))
            gone["status"] = "expired"
            gone["expired_at"] = stamp
            _tomb(state, str(gone.get("id") or ""), "capacity", stamp)
            dropped.append({"id": gone.get("id"), "why": "capacity"})
            live = [item for item in live if item is not gone]
        item = {"id": item_id, "kind": str(kind), "source_message_id": message_id,
                "summary": body, "created": stamp,
                "expires": stamp + _ttl_hours() * 3600, "status": "pending",
                "attempts": 0, "attempted_by": [], "resolved_by": None,
                "evidence": rows[:2]}
        state["items"].setdefault(scope, []).append(item)
        state["at"] = stamp
        _save(state)
    return {"ok": True, "item": item, "dropped": dropped}


def pending(conv: str, uid: str, *, now: float | None = None, limit: int = 1) -> list[dict[str, Any]]:
    if not enabled():
        return []
    stamp = _now() if now is None else float(now)
    path = _path()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return []
        state = _load()
        live = _live(state, _scope(conv, uid), stamp)
        _save(state)
        return live[:max(1, int(limit))]


def pending_count(conv: str, uid: str, *, now: float | None = None) -> int:
    if not enabled():
        return 0
    stamp = _now() if now is None else float(now)
    path = _path()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return 0
        state = _load()
        live = _live(state, _scope(conv, uid), stamp)
        _save(state)
        return len(live)


# ------------------------------------------------------------------ 回合内令牌
def _token_map(conv: str, uid: str, message_id: Any, now: float) -> dict[str, str]:
    found = _JUDGE.get(_key(conv, uid, message_id)) or {}
    tokens = found.get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def claim(conv: str, uid: str, *, message_id: Any, now: float | None = None) -> dict[str, Any]:
    """深判前取一次：给 prompt 用，并把「令牌 → 事项 id」记在本回合里。"""
    stamp = _now() if now is None else float(now)
    _trim_claims(stamp)
    live = pending(conv, uid, now=stamp, limit=2)
    total = max(len(live), pending_count(conv, uid, now=stamp))
    if not live:
        return {"ok": True, "text": "", "ids": [], "tokens": {}, "count": 0}
    lines = ["【还没说完的事】"]
    tokens: dict[str, str] = {}
    for index, row in enumerate(live, 1):
        tokens[f"P{index}"] = str(row.get("id") or "")
        lines.append(f"- [P{index}] 他刚才在问「{row.get('summary')}」，你还没答。"
                     "这只是待答的问题，不是他答应过什么。")
    if total > len(live):
        lines.append(f"- （还有 {total - len(live)} 条，本轮先不提）")
    text = "\n".join(lines)
    if message_id is not None:
        _JUDGE[_key(conv, uid, message_id)] = {"tokens": tokens, "count": total,
                                               "at": stamp, "text": text}
    return {"ok": True, "text": text, "ids": [row.get("id") for row in live],
            "tokens": tokens, "count": total}


def prompt_hint(conv: str, uid: str, *, message_id: Any, now: float | None = None) -> str:
    return str(claim(conv, uid, message_id=message_id, now=now).get("text") or "")


def render(conv: str, uid: str, *, message_id: Any = None, limit: int = 1,
           now: float | None = None) -> str:
    """**注入时现读** pending（剔除已终态/过期），并把实际发出去的 id 存进 `_SENT`。"""
    if not enabled() or not uid:
        return ""
    stamp = _now() if now is None else float(now)
    _trim_claims(stamp)
    live = pending(conv, uid, now=stamp, limit=limit)
    if not live:
        return ""
    lines = ["【还没说完的事】"]
    for row in live:
        lines.append(f"- 他刚才在问「{row.get('summary')}」，你还没答。"
                     "这只是待答的问题，不是他答应过什么。")
    text = "\n".join(lines)
    if message_id is not None:
        _SENT[_key(conv, uid, message_id)] = {"ids": [row.get("id") for row in live],
                                              "text": text, "at": stamp}
    return text


# ------------------------------------------------------------------ 状态推进
def keep(conv: str, uid: str, *, ref: str, message_id: Any) -> dict[str, Any]:
    """模型说"这件事还没完"。**不续期、不新建**，只确认令牌还在。"""
    if not enabled():
        return {"ok": False, "why": "disabled"}
    item_id = _token_map(conv, uid, message_id, _now()).get(str(ref or "").upper())
    if not item_id:
        return {"ok": False, "why": "unknown-token"}
    return {"ok": True, "item_id": item_id}


def settle(conv: str, uid: str, *, ref: str, evidence: list[dict[str, Any]] | None = None,
           message_id: Any = None, now: float | None = None) -> dict[str, Any]:
    """有证据地完成。**结构校验在这里，语义判断留给模型与审计。**"""
    if not enabled():
        return {"ok": False, "why": "disabled"}
    stamp = _now() if now is None else float(now)
    item_id = _token_map(conv, uid, message_id, stamp).get(str(ref or "").upper())
    if not item_id:
        return {"ok": False, "why": "unknown-token"}
    rows = [row for row in (evidence or [])
            if isinstance(row, dict) and int(row.get("message_id") or 0) == int(message_id or 0)]
    if not rows:
        return {"ok": False, "why": "no-evidence"}
    detail = str(rows[0].get("detail") or "").strip()
    scope = _scope(conv, uid)
    path = _path()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return {"ok": False, "why": "busy"}
        state = _load()
        target = next((row for row in _rows(state, scope) if str(row.get("id")) == item_id), None)
        if target is None:
            return {"ok": False, "why": "not-found"}
        if target.get("status") != "pending":
            return {"ok": False, "why": f"already-{target.get('status')}"}
        if len(detail) < 6:
            return {"ok": False, "why": "semantic-too-short"}
        if detail == str(target.get("summary") or ""):
            return {"ok": False, "why": "semantic-repeat"}
        if detail.strip("，。！？!?,.") in _SMALL_TALK:
            return {"ok": False, "why": "semantic-smalltalk"}
        target["status"] = "resolved"
        target["resolved_by"] = message_id
        target["resolved_at"] = stamp
        target["resolved_evidence"] = rows[:1]
        _tomb(state, item_id, "resolved", stamp)
        state["at"] = stamp
        _save(state)
    return {"ok": True, "item_id": item_id}


def cancel(conv: str, uid: str, *, item_id: str = "", reason: str = "",
           now: float | None = None) -> dict[str, Any]:
    if not enabled():
        return {"ok": False, "why": "disabled"}
    stamp = _now() if now is None else float(now)
    scope = _scope(conv, uid)
    path = _path()
    cancelled: list[str] = []
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return {"ok": False, "why": "busy"}
        state = _load()
        for row in _rows(state, scope):
            if not isinstance(row, dict) or row.get("status") != "pending":
                continue
            if item_id and str(row.get("id")) != str(item_id):
                continue
            row["status"] = "cancelled"
            row["cancelled_at"] = stamp
            row["cancel_reason"] = str(reason or "")
            _tomb(state, str(row.get("id") or ""), "cancelled", stamp)
            cancelled.append(str(row.get("id") or ""))
        if cancelled:
            state["at"] = stamp
            _save(state)
    return {"ok": bool(cancelled), "cancelled": cancelled, "why": "" if cancelled else "none"}


def note_stop(conv: str, uid: str, text: str) -> dict[str, Any]:
    """显式停止（"别说了"）→ 该 scope 全部取消。

    显式边界是**确定性**信号（`scene_state.boundary_of` 的句式匹配），所以它不依赖
    矢量开关，也不依赖 `note_boundary` 有没有写盘成功（#0012 P1-3）。
    """
    if not enabled() or not uid:
        return {"ok": False, "why": "disabled"}
    try:
        from . import scene_state

        kind, word = scene_state.boundary_of(text)
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "boundary-error"}
    if not kind:
        return {"ok": False, "why": "not-a-stop"}
    got = cancel(conv, uid, reason=f"boundary:{kind}{':' + word if word else ''}")
    if kind:
        path = _path()
        with filelock.exclusive(path.with_name(path.name + ".lock")) as ok:
            if ok:
                state = _load()
                anchor = f"{kind}:{word}" if word else ""
                if anchor:
                    state["refused"].setdefault(_scope(conv, uid), {})[anchor] = {
                        "at": _now(), "topic": ""}
                    _save(state)
    return got


def note_attempt(conv: str, uid: str, *, message_id: Any,
                 now: float | None = None) -> dict[str, Any]:
    """发送成功后 +1。**只读 `_SENT` 快照**，缺失就不计数（不重新生成）。"""
    if not enabled():
        return {"ok": False, "why": "disabled"}
    stamp = _now() if now is None else float(now)
    snap = _SENT.get(_key(conv, uid, message_id))
    if not isinstance(snap, dict):
        return {"ok": False, "why": "no-snapshot"}
    ids = [str(x) for x in (snap.get("ids") or []) if x]
    if not ids:
        return {"ok": False, "why": "empty"}
    scope = _scope(conv, uid)
    path = _path()
    counted: list[str] = []
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return {"ok": False, "why": "busy"}
        state = _load()
        for row in _rows(state, scope):
            if not isinstance(row, dict) or str(row.get("id")) not in ids:
                continue
            if row.get("status") != "pending":
                continue
            tried = [str(x) for x in (row.get("attempted_by") or [])]
            if str(message_id) in tried:
                continue
            tried.append(str(message_id))
            row["attempted_by"] = tried[-10:]
            row["attempts"] = int(row.get("attempts") or 0) + 1
            counted.append(str(row.get("id") or ""))
            if row["attempts"] >= _MAX_ATTEMPTS:
                row["status"] = "expired"
                row["expired_at"] = stamp
                _tomb(state, str(row.get("id") or ""), "expired", stamp)
        if counted:
            state["at"] = stamp
            _save(state)
    return {"ok": bool(counted), "counted": counted, "why": "" if counted else "none"}


def sweep(*, now: float | None = None) -> dict[str, Any]:
    stamp = _now() if now is None else float(now)
    path = _path()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return {"ok": False, "why": "busy"}
        state = _load()
        before = sum(1 for rows in state["items"].values() if isinstance(rows, list)
                     for row in rows if isinstance(row, dict) and row.get("status") == "pending")
        for scope in list(state["items"]):
            _live(state, scope, stamp)
        after = sum(1 for rows in state["items"].values() if isinstance(rows, list)
                    for row in rows if isinstance(row, dict) and row.get("status") == "pending")
        state["at"] = stamp
        _save(state)
    return {"ok": True, "expired": before - after, "tombs": len(state["tombs"])}


def report(conv: str = "", uid: str = "", *, limit: int = 5) -> dict[str, Any]:
    if not enabled():
        return {"enabled": False, "items": []}
    stamp = _now()
    scope = _scope(conv, uid)
    state = _load()
    rows = [row for row in _rows(state, scope) if isinstance(row, dict)]
    rows.sort(key=lambda item: float(item.get("created") or 0), reverse=True)
    return {"enabled": True, "count": len(_live(state, scope, stamp, sweep=False)),
            "items": rows[:max(1, int(limit))],
            "tombs": len(state.get("tombs") or {})}
