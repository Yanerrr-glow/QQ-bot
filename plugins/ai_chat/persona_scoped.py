"""候选轴（curiosity / stress）的**离线评估**存储与规则入口（#0013）。

## 它解决什么

`vector.json` 的 `axes` 会被 `persona_vector._axes()` 逐项当成真实轴（衰减、注入、
日限额全按它走），所以**候选轴不能塞进 `axes`** —— 加个 `candidate: true` 标记没人读。
这里另起一份存储：作用域是 `(conv, uid, topic)` / `(conv, uid)`，只被离线回放使用。

## 开关

`vector_candidate_axes_enabled` 默认 `False`。为假时：`get()` 返回 `0.0`、
写入口返回 `disabled`，**读也不碰盘**（否则会把线上数据当成实验数据）。

## 规则入口

`observe_event()` 是唯一入口：公共校验（复用/clear/本人/主键/有效证据/幂等/数值）+
按轴的证据映射。**unknown / fuzzy / 转述 / 诉苦都不更新**；压力只认"同一显式拒绝锚"上
的继续施压 —— 拒绝别的事不算。
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from . import config, filelock, packs, persona_workspace, settings

logger = logging.getLogger("ai_chat.persona_scoped")

_FIVE_AXES = ("mood", "social", "defiance", "energy", "vulnerability")
_MAX_VALUES = 200
_MAX_SCOPES = 200
_MAX_SEEN = 200
_SEEN_TTL = 24 * 3600.0
_MAX_REFUSED = 20
_RANGE = (-1.0, 1.0)

_CACHE: dict[str, Any] = {"path": "", "mtime": 0.0, "data": {}}


def _now() -> float:
    return persona_workspace.now()


def _sget(key: str, default: Any = None) -> Any:
    try:
        got = settings.get(key)
        return default if got is None else got
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    """**纯内存判断**：禁用时不加载 fixture，也不碰任何文件。"""
    return bool(_sget("vector_candidate_axes_enabled", False))


def config_path() -> Path:
    raw = str(_sget("vector_candidate_axes_file", "") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else Path(__file__).resolve().parents[2] / path
    return Path(__file__).resolve().parents[2] / "验证" / "行为维度_候选轴.json"


def load_candidates(*, force: bool = False) -> dict[str, Any]:
    """读并校验候选轴定义。**不合格整块拒绝**（不半开）。"""
    if not enabled():
        return {}
    path = config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if not force and _CACHE["path"] == str(path) and _CACHE["mtime"] == mtime:
        return dict(_CACHE["data"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("候选轴定义读不动：%s", path.name)
        return {}
    out: dict[str, Any] = {"candidates": {}, "evidence": {}}
    for item in raw.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        axis_id = str(item.get("id") or "").strip()
        if not axis_id or axis_id in _FIVE_AXES:
            continue
        try:
            baseline = float(item.get("baseline", 0.0))
            half = float(item.get("half_life_h", 3.0))
            step = float(item.get("max_step", 0.06))
            daily = float(item.get("max_daily", 0.3))
        except (TypeError, ValueError):
            continue
        if not half > 0 or not _RANGE[0] <= baseline <= _RANGE[1] or step <= 0 or daily <= 0:
            continue
        out["candidates"][axis_id] = {
            "id": axis_id, "name": str(item.get("name") or axis_id),
            "scope": str(item.get("scope") or "topic"),
            "baseline": baseline, "half_life_h": half, "max_step": step,
            "max_daily": daily,
            "refuse_ttl_h": float(item.get("refuse_ttl_h", 6.0) or 6.0),
        }
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    out["evidence"] = {k: v for k, v in evidence.items() if isinstance(v, dict)}
    if not out["candidates"]:
        return {}
    _CACHE.update({"path": str(path), "mtime": mtime, "data": out})
    return dict(out)


def axis_def(axis: str) -> dict[str, Any]:
    return dict(load_candidates().get("candidates", {}).get(str(axis), {}))


def baseline(axis: str) -> float:
    if not enabled():
        return 0.0                       # 禁用时不加载定义，用内置常量
    return float(axis_def(axis).get("baseline", 0.0))


def key(conv: str, uid: str = "", topic: str = "") -> str:
    return f"{conv}|{uid or '~'}|{topic or '~'}"


def _path() -> Path:
    return config.persona_data_dir() / "scoped_state.json"


def _blank() -> dict[str, Any]:
    return {"version": 1, "at": _now(), "values": {}, "seen": {}, "refused": {}, "daily": {}}


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return _blank()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _blank()
    state = _blank()
    if isinstance(raw, dict):
        for name in ("values", "seen", "refused", "daily"):
            if isinstance(raw.get(name), dict):
                state[name] = raw[name]
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
        logger.warning("候选轴状态写盘失败：%s", path)
        return False


def _trim(state: dict[str, Any], now: float) -> None:
    values = state.get("values") or {}
    if len(values) > _MAX_VALUES:
        ordered = sorted(values.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))
        for k, _ in ordered[: len(values) - _MAX_VALUES]:
            values.pop(k, None)
    scopes = state.get("seen") or {}
    if len(scopes) > _MAX_SCOPES:
        ordered = sorted(scopes.items(), key=lambda kv: max(
            [float(v) for v in (kv[1] or {}).values()] or [0.0]))
        for k, _ in ordered[: len(scopes) - _MAX_SCOPES]:
            scopes.pop(k, None)
    for scope, rows in list(scopes.items()):
        if not isinstance(rows, dict):
            scopes.pop(scope, None)
            continue
        fresh = {k: float(v) for k, v in rows.items()
                 if now - float(v or 0) <= _SEEN_TTL}
        if len(fresh) > _MAX_SEEN:
            fresh = dict(sorted(fresh.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_SEEN])
        scopes[scope] = fresh
    refused = state.get("refused") or {}
    for scope, rows in list(refused.items()):
        if not isinstance(rows, dict):
            refused.pop(scope, None)
            continue
        fresh = {k: v for k, v in rows.items() if isinstance(v, dict)}
        if len(fresh) > _MAX_REFUSED:
            fresh = dict(sorted(fresh.items(), key=lambda kv: float(kv[1].get("at") or 0),
                                reverse=True)[:_MAX_REFUSED])
        refused[scope] = fresh


def _scope_seen(uid: str, conv: str) -> str:
    return f"{packs.active_id() or 'default'}|{conv}|{uid}"


def get(axis: str, scope: str, *, now: float | None = None) -> float:
    """读时按半衰期衰减回 baseline。禁用时返回 0.0，**不碰盘**。"""
    if not enabled():
        return 0.0
    stamp = _now() if now is None else float(now)
    info = axis_def(axis)
    base = float(info.get("baseline", 0.0))
    row = (_load().get("values") or {}).get(f"{axis}|{scope}")
    if not isinstance(row, dict):
        return base
    try:
        value = float(row.get("v", base))
        at = float(row.get("at") or stamp)
    except (TypeError, ValueError):
        return base
    half = max(0.5, float(info.get("half_life_h", 3.0) or 3.0))
    dt_h = max(0.0, (stamp - at) / 3600.0)
    return base + (value - base) * (0.5 ** (dt_h / half))


def add(axis: str, scope: str, delta: float, *, now: float | None = None,
        daily_key: str = "") -> dict[str, Any]:
    """写一个增量。单步/单日/值域都在这里裁；被裁说明原因。"""
    if not enabled():
        return {"ok": False, "why": "disabled"}
    info = axis_def(axis)
    if not info:
        return {"ok": False, "why": "unknown-axis"}
    if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(float(delta)):
        return {"ok": False, "why": "bad-delta"}
    stamp = _now() if now is None else float(now)
    step = float(info.get("max_step", 0.06))
    daily_cap = float(info.get("max_daily", 0.3))
    clipped = []
    value = float(delta)
    if abs(value) > step:
        clipped.append({"stage": "step", "from": value, "to": math.copysign(step, value)})
        value = math.copysign(step, value)
    path = _path()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return {"ok": False, "why": "busy"}
        state = _load()
        _trim(state, stamp)
        bucket = daily_key or scope
        day = state["daily"].get(bucket) if isinstance(state["daily"].get(bucket), dict) else {}
        today = time.strftime("%Y-%m-%d", time.localtime(stamp))
        if str(day.get("date") or "") != today:
            day = {"date": today, "travel": {}}
        used = float((day.get("travel") or {}).get(axis, 0.0) or 0.0)
        allow = max(0.0, daily_cap - used)
        if abs(value) > allow:
            clipped.append({"stage": "daily", "from": value, "to": math.copysign(allow, value)})
            value = math.copysign(allow, value)
        prev = get(axis, scope, now=stamp)
        nxt = max(_RANGE[0], min(_RANGE[1], prev + value))
        value = nxt - prev
        if abs(value) < 1e-12:
            return {"ok": False, "why": "clipped-zero", "clipped": clipped}
        day.setdefault("travel", {})[axis] = round(used + abs(value), 8)
        state["daily"][bucket] = day
        state["values"][f"{axis}|{scope}"] = {"v": nxt, "at": stamp}
        if len(state["values"]) > _MAX_VALUES:      # 插入之后再裁一次，上限才是硬上限
            ordered = sorted(state["values"].items(),
                             key=lambda kv: float((kv[1] or {}).get("at") or 0))
            for key_, _ in ordered[: len(state["values"]) - _MAX_VALUES]:
                state["values"].pop(key_, None)
        state["at"] = stamp
        _save(state)
    return {"ok": True, "applied": value, "value": nxt, "clipped": clipped}


def observe_event(axis: str, verdict: dict[str, Any], *, conv: str, uid: str,
                  anchor: str = "", topic: str = "", message_id: Any = None,
                  now: float | None = None) -> dict[str, Any]:
    """按 verdict 更新候选轴。**唯一入口**，所有拒绝都带 `why`。"""
    if not enabled():
        return {"ok": False, "why": "disabled"}
    stamp = _now() if now is None else float(now)
    if not isinstance(verdict, dict) or verdict.get("reused"):
        return {"ok": False, "why": "reused"}
    if str(verdict.get("confidence") or "") != "clear":
        return {"ok": False, "why": "not-clear"}
    if str(verdict.get("origin") or "") != "self":
        return {"ok": False, "why": "relayed"}
    if message_id is None:
        return {"ok": False, "why": "no-message-id"}
    if not uid:
        return {"ok": False, "why": "no-uid"}
    valid = [row for row in (verdict.get("evidence") or [])
             if isinstance(row, dict) and int(row.get("message_id") or 0) == int(message_id)]
    if not valid:
        return {"ok": False, "why": "no-valid-evidence"}
    scope_key = _scope_seen(uid, conv)
    path = _path()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return {"ok": False, "why": "busy"}
        state = _load()
        _trim(state, stamp)
        seen = state["seen"].setdefault(scope_key, {})
        if str(message_id) in seen:
            return {"ok": False, "why": "dup"}
        seen[str(message_id)] = stamp
        state["at"] = stamp
        _save(state)

    act = str(verdict.get("act") or "")
    target = str(verdict.get("target") or "")
    tone = str(verdict.get("tone") or "")
    scope = key(conv, uid, topic) if axis == "curiosity" else key(conv, uid)
    delta = 0.0
    if axis == "curiosity":
        if act == "分享" and tone == "认真" and target in ("none", "third"):
            delta = 0.05
        elif act == "纠正" and tone == "认真" and target == "bot":
            delta = 0.03
    elif axis == "stress":
        refused = state.get("refused", {}).get(scope_key, {})
        info = axis_def(axis)
        ttl = float(info.get("refuse_ttl_h", 6.0) or 6.0) * 3600
        fresh = {k: v for k, v in refused.items()
                 if isinstance(v, dict) and stamp - float(v.get("at") or 0) <= ttl}
        if str(verdict.get("reception") or "") == "已明确拒绝" and anchor:
            if fresh.get(anchor) is None:
                with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
                    if got:
                        state2 = _load()
                        state2["refused"].setdefault(scope_key, {})[anchor] = {
                            "at": stamp, "topic": topic}
                        state2["at"] = stamp
                        _save(state2)
            return {"ok": True, "recorded": "refused", "anchor": anchor}
        if act in ("骂", "亲密") and target == "bot" and anchor in fresh:
            delta = 0.05
    if abs(delta) < 1e-12:
        return {"ok": False, "why": "no-signal"}
    got = add(axis, scope, delta, now=stamp, daily_key=f"{axis}|{scope}")
    got.update({"axis": axis, "scope": scope, "delta": delta})
    return got


def snapshot(*, now: float | None = None) -> dict[str, Any]:
    if not enabled():
        return {"enabled": False, "values": {}}
    stamp = _now() if now is None else float(now)
    state = _load()
    out: dict[str, float] = {}
    for name in state.get("values") or {}:
        axis, _, scope = str(name).partition("|")
        if axis and scope:
            out[name] = round(get(axis, scope, now=stamp), 4)
    return {"enabled": True, "values": out, "scopes": len(state.get("values") or {})}
