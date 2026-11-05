"""取样（#0015 M4）：以**目标事件**为单位把对话送进评估台。

## 单位是「事件」，不是「一条消息」

一条用户消息本身说明不了「回得好不好」；要有「bot 做了什么（回复/发表情/不回复）」才算目标。
所以采样单位固定为 **bot 回复/动作事件 + 它之前的上下文快照**。

**不回复**（SKIP）必须有决策事件 ID 才能入样 —— 不能凭「日志里没有回复」推断沉默，
那会把「它根本没被触发」当成「它选择不说」。

## 四路来源只当字段，双队列分开报告

`route ∈ handpick / stratified / rare / approved`；`queue ∈ diagnostic / representative`。
两队列的成功率**不能混算**：诊断队列是「挑着修」，代表性队列才谈覆盖面。

## 时效：最大一周（D3）

`sample()` 的有效下界是 **`max(游标, now - window)`**，其中 `window` 由
`eval_sampling_window_hours` 决定（默认 168 = 一周，**配置上限也是 168**）。
再老的事件不进评估台：人格与示例一直在变，拿一个月前的对话判今天的回复，
比没有样本更容易误导。

游标与窗口是两件事：游标防「同一条被采两次」，窗口防「太老的被采进来」。

## 首期只做「手选 + 规则候选」

不做四路配额轮转调度器（那需要长期运行的游标与饥饿保护）。现在能保证的是：
按 `(conv, 事件 id)` 去重、按 `since` 游标推进、**一周时效**、每会话配额上限、固定 `seed` 可复现。
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from . import config, settings

logger = logging.getLogger("ai_chat.eval_sampling")

ROUTES = ("handpick", "stratified", "rare", "approved")
QUEUES = ("diagnostic", "representative")
KINDS = ("reply", "sticker", "skip")


def enabled() -> bool:
    try:
        return bool(settings.get("eval_sampling_enabled"))
    except Exception:  # noqa: BLE001
        return False


def _get(key: str, default: Any) -> Any:
    try:
        value = settings.get(key)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def paths() -> dict[str, Path]:
    root = config.persona_data_dir() / "eval"
    return {"queue": root / "queue.jsonl", "cursor": root / "cursor.json",
            "root": root}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        logger.warning("取样队列读不动：%s", path)
    return out


def cursor() -> dict[str, Any]:
    path = paths()["cursor"]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _set_cursor(conv: str, ts: float) -> None:
    path = paths()["cursor"]
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cursor()
    data[conv] = max(float(data.get(conv) or 0.0), float(ts))
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        logger.warning("游标写盘失败：%s", exc)


# --------------------------------------------------------------------- 采样
def collect(conv: str, messages: list[dict[str, Any]], *, context: int = 6,
            since: float = 0.0) -> list[dict[str, Any]]:
    """从一段会话里抽目标事件（**纯函数**，不读盘）。"""
    events: list[dict[str, Any]] = []
    for index, msg in enumerate(messages):
        if not msg.get("is_bot"):
            continue
        ts = float(msg.get("ts") or 0.0)
        if since and ts <= since:
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        prior = messages[max(0, index - context):index]
        if not prior:
            continue
        events.append({
            "event_id": f"{conv}#{msg.get('id')}",
            "kind": "reply",
            "conv": conv,
            "message_id": msg.get("id"),
            "ts": ts,
            "question": str(prior[-1].get("text") or "") if prior else "",
            "reply": text,
            "pre_context": [
                {"uid": "bot" if x.get("is_bot") else str(x.get("uid") or ""),
                 "name": "机器人" if x.get("is_bot") else str(x.get("name") or ""),
                 "text": str(x.get("text") or ""), "ts": x.get("ts"),
                 "is_bot": bool(x.get("is_bot"))}
                for x in prior
            ],
        })
    return events


def sample(conv: str, *, limit: int | None = None, context: int = 6) -> dict[str, Any]:
    """从线上聊天记录里为一个会话采样。**只读**，把游标推进到最晚一条。"""
    if not enabled():
        return {"ok": False, "why": "取样关着（eval_sampling_enabled）"}
    quota = int(_get("eval_sampling_per_conv", 5))
    cap = int(limit or _get("eval_sampling_limit", 20))
    path = config.runtime_root() / f"chatlog_{conv}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "why": f"没有会话记录：{path}"}
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return {"ok": False, "why": "会话记录结构不对"}
    # 时效：游标与窗口取更晚的那个（D3）。窗口上限由 settings 卡在 168 小时。
    window_hours = max(0, min(168, int(_get("eval_sampling_window_hours", 168))))
    floor = time.time() - window_hours * 3600 if window_hours else 0.0
    since = max(float(cursor().get(conv) or 0.0), floor)
    events = collect(conv, messages, context=context, since=since)[:min(quota, cap)]
    seed = int(_get("eval_sampling_seed", 0))
    if seed:
        random.Random(seed + len(events)).shuffle(events)
    return {"ok": True, "conv": conv, "events": events, "since": since,
            "quota": quota, "cursor": float(cursor().get(conv) or 0.0),
            "window_hours": window_hours}


# --------------------------------------------------------------------- 入队
def _key(event: dict[str, Any]) -> str:
    raw = f"{event.get('conv')}|{event.get('message_id')}|{event.get('kind')}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def enqueue(events: list[dict[str, Any]], *, route: str = "handpick",
            queue: str = "diagnostic") -> dict[str, Any]:
    """入队。按 `(conv, message_id, kind)` 去重，重复的计入 `skipped`。"""
    if not enabled():
        return {"ok": False, "why": "取样关着（eval_sampling_enabled）"}
    if route not in ROUTES:
        return {"ok": False, "why": f"route 必须是 {'/'.join(ROUTES)}"}
    if queue not in QUEUES:
        return {"ok": False, "why": f"queue 必须是 {'/'.join(QUEUES)}"}
    path = paths()["queue"]
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {row.get("key") for row in _read_jsonl(path)}
    added, skipped = 0, 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            key = _key(event)
            if key in existing:
                skipped += 1
                continue
            row = dict(event)
            row.update({"key": key, "route": route, "queue": queue,
                        "status": "pending", "added_at": now})
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            existing.add(key)
            added += 1
    for conv in {str(e.get("conv")) for e in events if e.get("ts")}:
        latest = max(float(e.get("ts") or 0) for e in events if str(e.get("conv")) == conv)
        _set_cursor(conv, latest)
    return {"ok": True, "added": added, "skipped": skipped, "total": len(existing)}


def due(limit: int = 10, *, queue: str = "") -> list[dict[str, Any]]:
    """取待处理事件（按入队顺序）。`queue` 非空时只取那一队。"""
    rows = [r for r in _read_jsonl(paths()["queue"]) if r.get("status") == "pending"]
    if queue:
        rows = [r for r in rows if r.get("queue") == queue]
    return rows[:max(1, int(limit))]


def mark(event_key: str, status: str, *, note: str = "") -> dict[str, Any]:
    """把一条事件标成 `done` / `skipped` / `not_run`（重写队列文件）。"""
    if status not in ("pending", "done", "skipped", "not_run"):
        return {"ok": False, "why": "status 必须是 pending/done/skipped/not_run"}
    path = paths()["queue"]
    rows = _read_jsonl(path)
    hit = False
    for row in rows:
        if row.get("key") == event_key:
            row["status"] = status
            row["note"] = note
            row["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            hit = True
    if not hit:
        return {"ok": False, "why": f"没有这条事件：{event_key}"}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "key": event_key, "status": status}


# --------------------------------------------------------------------- 概况
def stats() -> dict[str, Any]:
    rows = _read_jsonl(paths()["queue"])
    by_queue: dict[str, int] = {}
    by_route: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for row in rows:
        by_queue[str(row.get("queue"))] = by_queue.get(str(row.get("queue")), 0) + 1
        by_route[str(row.get("route"))] = by_route.get(str(row.get("route")), 0) + 1
        by_status[str(row.get("status"))] = by_status.get(str(row.get("status")), 0) + 1
    return {
        "enabled": enabled(),
        "total": len(rows),
        "by_queue": by_queue,
        "by_route": by_route,
        "by_status": by_status,
        "cursor": cursor(),
        "files": {k: str(v) for k, v in paths().items()},
    }
