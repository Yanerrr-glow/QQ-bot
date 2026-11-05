"""行为矢量：全局情绪 + 对每个人的关系偏移。

## 它解决什么

只靠文本规则，bot 的"情绪"是每轮重新猜的：上一句低落、下一句又元气。
这里把情绪变成**有状态、会衰减、有限幅**的数值：

* `vector.json` 定义轴、基线、半衰期、单轮/单日上限、模式；
* `vector_state.json` 存当前值，读取时按半衰期回归基线；
* `relations.json`（`persona_relation`）提供"对这个人"的偏移。

## 三条硬约束

1. **限幅**：单轮单轴 ≤ `max_step`，单日 ≤ `max_daily`；越限裁剪并记审计。
2. **衰减**：`v = base + (v-base) * 0.5 ** (dt/half_life)`；极端值超过 `max_hold_h` 后加速回家。
3. **情绪不改变底线**：矢量只影响"愿不愿意、话多话少、软硬"，
   不放开身体接触、不放开人身攻击；那两条在 `forbidden.txt` 与 `boundary` 里。

## 模型自评

默认关（`vector_llm_enabled`）。打开后模型可在回复末尾写
`<vec mood=-0.04 social=+0.02>`；代码解析、白名单、裁剪、剥离后再发出去。
用户消息里的同名标记一律当普通文本。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import time
from typing import Any

from . import config, filelock, persona_relation, persona_workspace, settings

logger = logging.getLogger("ai_chat.persona_vector")

_STATE = "vector_state.json"
_AUDIT = "vector_audit.jsonl"
_TAG_RE = re.compile(r"<vec\s+([^>]{1,200})>", re.I)

# ---- 事件与批次（#0010）---------------------------------------------------
# 事件类型是**事件层**的概念：同一条消息可能同时有语义项（被夸）与时间项（深夜），
# 两者要各自去重、各自决定要不要降温。数值正负不能替代事件类型 —— 辱骂会
# 同时让 mood 下降、defiance 上升，按符号推断就会把它当成"正向事件"。
EVENT_KINDS = ("insult", "intimate", "praise", "night", "code", "llm_vec", "intent_reward")
# **只有已确认的正向语义事件参与降温**。负向（辱骂）照旧受单步与日限额约束，
# 但不乘热度因子：群里反复被骂本来就该持续受伤，压着不让掉才是怪事。
HEAT_KINDS = ("praise", "intimate")

_MAX_SEEN = 500                 # 幂等表上限（按时间淘汰）
_SEEN_TTL = 24 * 3600.0         # 幂等期限：过了这个点，同一条消息重投递会重新记
_MAX_BUCKETS = 200              # 热簿桶上限
_MAX_AUDIT_PENDING = 16         # 待审计队列上限：满了宁可拒绝新变更，也不静默丢证据
_AUDIT_ROTATE_BYTES = 2 * 1024 * 1024
_AUDIT_TAIL_BYTES = 64 * 1024

_DEFAULT_AXES: list[dict[str, Any]] = [
    {"id": "mood", "name": "心情", "scope": "global", "low": "低落、委屈", "high": "开心、雀跃",
     "baseline": 0.15, "half_life_h": 6, "max_step": 0.08, "max_daily": 0.5, "max_hold_h": 12},
    {"id": "social", "name": "社交意愿", "scope": "both", "relation_weight": 0.5,
     "low": "想独处、不想说话", "high": "想黏人、想找人聊",
     "baseline": 0.1, "half_life_h": 8, "max_step": 0.08, "max_daily": 0.5, "max_hold_h": 12},
    {"id": "defiance", "name": "逆反", "scope": "both", "relation_weight": 0.3,
     "low": "顺从、愿意配合", "high": "抗拒、闹脾气",
     "baseline": -0.2, "half_life_h": 2, "max_step": 0.06, "max_daily": 0.35, "max_hold_h": 4},
    {"id": "energy", "name": "精力", "scope": "both", "relation_weight": 0.2,
     "low": "疲惫、懒得动", "high": "活跃、兴奋",
     "baseline": 0.0, "half_life_h": 4, "max_step": 0.08, "max_daily": 0.5, "max_hold_h": 8},
    {"id": "vulnerability", "name": "示弱", "scope": "both", "relation_weight": 0.4,
     "low": "逞强、嘴硬", "high": "撒娇、求安慰",
     "baseline": 0.0, "half_life_h": 3, "max_step": 0.08, "max_daily": 0.45, "max_hold_h": 6},
]


def _now() -> float:
    from .persona_workspace import now
    return now()


def _sget(key: str, default: Any = None) -> Any:
    try:
        got = settings.get(key)
        return default if got is None else got
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    return bool(_sget("vector_enabled", True))


def _path() -> str:
    return str(config.vector_state_path())


def _load_vector() -> dict[str, Any]:
    raw = config.load_vector()
    return raw if raw.get("axes") else {"axes": _DEFAULT_AXES, "modes": [], "inject": {}}


def _axes() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in _load_vector().get("axes") or []:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            out[str(item["id"])] = item
    return out or {str(a["id"]): a for a in _DEFAULT_AXES}


def _blank(now: float | None = None) -> dict[str, Any]:
    return {"version": 2, "at": _now() if now is None else now, "values": {},
            "extreme_since": {}, "daily": {},
            "heat": {"window": 0.0, "k": 0.0, "buckets": {}}, "seen": {}, "audit_pending": []}


def _migrate(raw: Any) -> dict[str, Any]:
    """把旧版（version 1）状态补齐到 version 2。**只补键，不改值**。"""
    state = dict(raw) if isinstance(raw, dict) else _blank()
    values = state.get("values") if isinstance(state.get("values"), dict) else {}
    state["values"] = {k: float(v) for k, v in values.items() if isinstance(v, (int, float))}
    for key in ("extreme_since", "daily"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    heat = state.get("heat") if isinstance(state.get("heat"), dict) else {}
    buckets = heat.get("buckets") if isinstance(heat.get("buckets"), dict) else {}
    state["heat"] = {
        "window": float(heat.get("window") or 0.0),
        "k": float(heat.get("k") or 0.0),
        "buckets": {str(k): list(v) for k, v in buckets.items() if isinstance(v, list)},
    }
    if not isinstance(state.get("seen"), dict):
        state["seen"] = {}
    if not isinstance(state.get("audit_pending"), list):
        state["audit_pending"] = []
    state["version"] = 2
    if not isinstance(state.get("at"), (int, float)):
        state["at"] = _now()
    return state


def _read_state(path=None) -> dict[str, Any]:
    target = config.vector_state_path() if path is None else path
    if not target.exists():
        return _blank()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("矢量状态损坏，按默认继续：%s", target.name)
        return _blank()
    return _migrate(raw)


def _write_state(state: dict[str, Any], path=None) -> bool:
    """原子写。**返回是否成功** —— 原来吞掉 OSError 会让"审计丢了"永远发现不了。"""
    target = config.vector_state_path() if path is None else path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError:
        logger.warning("矢量状态写盘失败：%s", target)
        return False



def _decay(values: dict[str, float], since: float, now: float, extreme: dict | None = None) -> dict[str, float]:
    axes = _axes()
    out: dict[str, float] = {}
    dt_h = max(0.0, (now - since) / 3600.0)
    for axis_id, axis in axes.items():
        base = float(axis.get("baseline", 0.0))
        value = float(values.get(axis_id, base))
        half_life = max(0.5, float(axis.get("half_life_h", 6) or 6))
        extra_h = 0.0
        began = float((extreme or {}).get(axis_id) or 0)
        hold = max(0.5, float(axis.get("max_hold_h", 12)))
        if began:
            extra_h = max(0.0, (now - max(since, began + hold * 3600)) / 3600)
        out[axis_id] = base + (value - base) * (0.5 ** ((dt_h + extra_h * 3) / half_life))
    return out


def state() -> dict[str, Any]:
    """当前全局值（已按时间衰减）。"""
    raw = _read_state()
    values = raw.get("values") if isinstance(raw.get("values"), dict) else {}
    now = _now()
    decayed = _decay({k: float(v) for k, v in values.items() if isinstance(v, (int, float))},
                     float(raw.get("at") or now), now, raw.get("extreme_since"))
    return {"at": raw.get("at"), "values": decayed, "extreme_since": raw.get("extreme_since") or {}}


def _daily_used(state: dict[str, Any], axis_id: str, today: str) -> float:
    """当天该轴已用掉的位移（gross）。`today` 由调用方按同一时钟算好传入。"""
    daily = state.get("daily") if isinstance(state.get("daily"), dict) else {}
    if str(daily.get("date") or "") != str(today):
        return 0.0
    used = daily.get("travel")
    if not isinstance(used, dict):
        used = daily.get("delta") if isinstance(daily.get("delta"), dict) else {}
    try:
        return abs(float(used.get(axis_id, 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _audit_file():
    return config.persona_data_dir() / _AUDIT


def _tail_rows(path, *, limit: int = 200, max_bytes: int = _AUDIT_TAIL_BYTES,
               with_meta: bool = False):
    """只读审计文件尾部，返回解析成功的行（旧→新）。坏行跳过，不抛。

    只读尾部是因为它只增不减：整读会随着运行时间线性变慢，而"最近的几行"
    才是解释与去重要用的。`with_meta=True` 时额外返回 `complete`——
    **从文件头读全了吗**。解释链要靠它决定"能不能下否定结论"：
    只读到尾部时，"没找到反号事件"说明不了任何事（#0011 P1-7）。
    """
    empty: Any = ([], True) if with_meta else []
    try:
        size = path.stat().st_size
    except OSError:
        return empty
    complete = size <= max_bytes
    rows: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            start = max(0, size - max_bytes)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return empty
    for line in chunk.splitlines():
        text = line.decode("utf-8", "ignore").strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            continue                      # 首行可能被截断，跳过即可
        if isinstance(row, dict):
            rows.append(row)
    rows = rows[-limit:]
    return (rows, complete) if with_meta else rows


def _parse_stamp(text: Any) -> float:
    """审计里的 `%Y-%m-%d %H:%M:%S` → 时间戳。解析不了返回 0。"""
    try:
        return time.mktime(time.strptime(str(text), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _append_audit(rows: list[dict[str, Any]]) -> bool:
    """追加审计行；**按 event_id 幂等**，所以崩溃后重放不会写出重复行。"""
    if not rows:
        return True
    path = _audit_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > _AUDIT_ROTATE_BYTES:
                path.replace(path.with_name(path.stem + ".1.jsonl"))
        except OSError:
            pass
        seen = {str(r.get("event_id")) for r in _tail_rows(path) if r.get("event_id")}
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                event_id = str(row.get("event_id") or "")
                if event_id and event_id in seen:
                    continue
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                if event_id:
                    seen.add(event_id)
        return True
    except OSError:
        logger.warning("矢量审计写盘失败：%s", path)
        return False


def _flush_pending(state: dict[str, Any], now: float) -> bool:
    """把「待审计」补写进文件。**幂等**：已存在同 `event_id` 的行不会重复写。"""
    pending = [r for r in (state.get("audit_pending") or []) if isinstance(r, dict)]
    if not pending:
        return True
    if not _append_audit(pending):
        return False
    written = {str(r.get("event_id")) for r in pending}
    state["audit_pending"] = [r for r in (state.get("audit_pending") or [])
                              if str((r or {}).get("event_id")) not in written]
    state["at"] = float(state.get("at") or now)
    return _write_state(state)



# ---------------------------------------------------------------- 事件与批次
#
# 一条消息可能同时产生多个事件：被夸（语义）+ 深夜（时间事实）+ 未来的兑现奖励。
# 它们必须**各自去重、各自决定要不要降温**，但**共用一次轴选择**——否则
# "这一轮到底动了哪两轴"会取决于调用次数。


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


def _pack_id() -> str:
    try:
        from . import packs

        return str(packs.active_id() or "default")
    except Exception:  # noqa: BLE001 - 拿不到包 id 不能影响情绪
        return "default"


def _event_id(*parts: Any) -> str:
    body = "|".join(str(p) for p in parts)
    return "e:" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


def _heat_blank() -> dict[str, Any]:
    return {"factor": 1.0, "n": 0, "k": 0.0, "window": 0.0, "bucket": ""}


def _heat_cfg() -> tuple[float, float]:
    window = float(_sget("vector_heat_window", 600) or 600)
    k = float(_sget("vector_heat_k", 12.0) or 12.0)
    return max(60.0, window), max(0.0, k)


def build_event(deltas: dict[str, float], *, kind: str = "code", item: str = "",
                verdict: dict[str, Any] | None = None, evidence: list[dict[str, Any]] | None = None,
                message_id: Any = None) -> dict[str, Any]:
    """组装一个事件。纯函数：不读盘、不改状态。"""
    return {"kind": str(kind or "code"), "item": str(item or ""),
            "deltas": dict(deltas or {}),
            "verdict": verdict if isinstance(verdict, dict) else None,
            "evidence": list(evidence or []), "message_id": message_id}


def _heat_eligible(ev: dict[str, Any]) -> tuple[bool, str]:
    """这个事件能不能降温。**只有已确认的正向事件可以**（#0010 P1-3）。"""
    kind = str(ev.get("kind") or "")
    if kind not in HEAT_KINDS:
        return False, f"{kind or '未知'} 不参与降温"
    verdict = ev.get("verdict")
    if isinstance(verdict, dict):
        if str(verdict.get("confidence") or "") != "clear":
            return False, "判定不是 clear"
        if str(verdict.get("target") or "") != "bot":
            return False, "态度对象不是 bot"
    return True, ""


def heat_eligible(ev: dict[str, Any]) -> tuple[bool, str]:
    """公开别名：`#0012` 的奖励事件要用同一套资格判定，不该去碰私有名。"""
    return _heat_eligible(ev)


def _prune(state: dict[str, Any], now: float) -> None:
    """按窗口与上限修剪热簿与幂等表。每次写盘前跑一次，代价是线性的。"""
    window, k = _heat_cfg()
    kept: dict[str, list] = {}
    for key, rows in (state["heat"].get("buckets") or {}).items():
        fresh = [row for row in rows
                 if isinstance(row, list) and row and float(row[0]) >= now - window]
        if fresh:
            kept[str(key)] = fresh[-50:]
    if len(kept) > _MAX_BUCKETS:
        ordered = sorted(kept.items(), key=lambda kv: float(kv[1][-1][0]), reverse=True)
        kept = dict(ordered[:_MAX_BUCKETS])
    state["heat"] = {"window": window, "k": k, "buckets": kept}
    seen = state.get("seen") if isinstance(state.get("seen"), dict) else {}
    fresh = {str(k2): float(v) for k2, v in seen.items()
             if isinstance(v, (int, float)) and not isinstance(v, bool) and now - float(v) <= _SEEN_TTL}
    if len(fresh) > _MAX_SEEN:
        fresh = dict(sorted(fresh.items(), key=lambda kv: kv[1], reverse=True)[:_MAX_SEEN])
    state["seen"] = fresh


def _bucket_n(state: dict[str, Any], bucket: str, now: float) -> int:
    window, _k = _heat_cfg()
    rows = state["heat"]["buckets"].get(bucket) or []
    return sum(1 for row in rows
               if isinstance(row, list) and row and float(row[0]) >= now - window)


def _split(total: float, parts: dict[int, float]) -> dict[int, float]:
    """把 `total` 按 `parts` 比例分给各下标；残差给 |比例| 最大者（同值按下标）。

    **残差必须落在某一项上**，否则 `Σ 逐项 == 顶层` 会有 1e-17 级的缺口，
    而恒等式断言是按 1e-12 做的。
    """
    scale = math.fsum(parts.values())
    if not parts:
        return {}
    if abs(scale) < 1e-15:
        return {idx: 0.0 for idx in parts}
    out = {idx: total * (value / scale) for idx, value in parts.items()}
    residual = total - math.fsum(out.values())
    pick = sorted(parts, key=lambda i: (-abs(parts[i]), i))[0]
    out[pick] += residual
    return out


def _empty_result(why: str) -> dict[str, Any]:
    return {"ok": False, "why": why, "event_id": "", "dedup": False, "applied": {},
            "nominal": {}, "clipped": [], "dropped": [], "heat": _heat_blank(),
            "stages": {"step": {}, "heat": {}, "daily": {}}, "values": {},
            "audit_ok": True, "audit_pending_count": 0, "results": []}


def _new_item(index: int, ev: dict[str, Any], *, conv: str, uid: str, phase: str,
              message_id: Any, axes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    kind = str(ev.get("kind") or "code")
    item_key = str(ev.get("item") or kind)
    event_id = str(ev.get("event_id") or "") or _event_id(
        _pack_id(), conv, uid, phase, message_id, item_key, kind)
    row: dict[str, Any] = {
        "index": index, "event_id": event_id, "item_key": item_key, "kind": kind,
        "event": {"event_id": event_id, "message_id": message_id, "phase": phase,
                  "kind": kind, "item_key": item_key,
                  "verdict": ev.get("verdict") if isinstance(ev.get("verdict"), dict) else None,
                  "evidence": list(ev.get("evidence") or [])},
        "deltas": {}, "nominal": {}, "clipped": [], "dropped": [],
        "contrib": {}, "applied": {}, "heat": _heat_blank(), "dedup": False,
        "dedup_key": "", "eligible": False,
    }
    if kind not in EVENT_KINDS:
        row["dropped"].append({"axis": "", "stage": "kind", "why": f"未知事件类型 {kind}"})
        return row
    raw = ev.get("deltas") if isinstance(ev.get("deltas"), dict) else {}
    for axis_id, delta in raw.items():
        axis_id = str(axis_id)
        if axis_id not in axes:
            row["dropped"].append({"axis": axis_id, "stage": "unknown_axis", "why": f"没有轴 {axis_id}"})
            continue
        if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(float(delta)):
            row["dropped"].append({"axis": axis_id, "stage": "bad_value", "why": "不是有限数值"})
            continue
        row["deltas"][axis_id] = float(delta)
    row["nominal"] = dict(row["deltas"])
    row["dedup_key"] = (f"{conv}|{uid}|{phase}|{message_id}|{item_key}"
                        if message_id is not None else "")
    row["eligible"], _why = _heat_eligible(ev)
    return row


def apply_events(evs: list[dict[str, Any]], *, source: str = "event", conv: str = "",
                 uid: str = "", message_id: Any = None, phase: str = "pre",
                 is_master: bool = False) -> dict[str, Any]:
    """**唯一写入入口**：一批事件一次落盘。

    返回键固定（`ok/why/event_id/dedup/applied/nominal/clipped/dropped/heat/
    stages/values/audit_ok/audit_pending_count/results`），失败路径也补齐。
    """
    if not enabled():
        return _empty_result("vector_disabled")
    events = [ev for ev in (evs or []) if isinstance(ev, dict)]
    if not events:
        return _empty_result("no-events")
    now = _now()
    path = config.vector_state_path()
    axes = _axes()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return _empty_result("busy")
        state = _read_state(path)
        _prune(state, now)
        # **补写入口必须在去重提前返回之前**：重投递同一条消息也要能把积压补出去。
        _flush_pending(state, now)
        return _run_batch(state, events, path=path, axes=axes, source=source, conv=conv,
                          uid=uid, message_id=message_id, phase=phase, now=now,
                          is_master=is_master)


def _run_batch(state: dict[str, Any], events: list[dict[str, Any]], *, path, axes,
               source: str, conv: str, uid: str, message_id: Any, phase: str,
               now: float, is_master: bool = False) -> dict[str, Any]:
    global_ceiling = float(_sget("vector_max_step", 0.08) or 0.08)
    today = persona_workspace.today()
    daily = state.get("daily") if isinstance(state.get("daily"), dict) else {}
    if str(daily.get("date") or "") != today:
        daily = {"date": today, "delta": {}, "travel": {}}
    if not isinstance(daily.get("travel"), dict):
        daily["travel"] = {k: abs(float(v)) for k, v in (daily.get("delta") or {}).items()}
    if not isinstance(daily.get("delta"), dict):
        daily["delta"] = {}
    travel = daily["travel"]
    delta_book = daily["delta"]

    current = _decay(state.get("values") or {}, float(state.get("at") or now), now,
                     state.get("extreme_since"))
    before_all = dict(current)
    extreme = state.get("extreme_since") if isinstance(state.get("extreme_since"), dict) else {}

    # ① 归一化 + 去重过滤
    items = [_new_item(idx, ev, conv=conv, uid=uid, phase=phase, message_id=message_id, axes=axes)
             for idx, ev in enumerate(events)]
    for row in items:
        if row["dedup_key"] and row["dedup_key"] in state["seen"]:
            row["dedup"] = True
            row["deltas"] = {}
            row["dropped"].append({"axis": "", "stage": "dup", "why": "这条消息的这个事件已入账"})
    live = [row for row in items if row["deltas"] and not row["dedup"]]

    # ② 单步限幅 + 批内汇总 + 轴选择
    for row in live:
        for axis_id, delta in list(row["deltas"].items()):
            step = min(float(axes[axis_id].get("max_step", global_ceiling)), global_ceiling)
            if abs(delta) > step + 1e-12:
                row["clipped"].append({"axis": axis_id, "stage": "step",
                                       "from": round(delta, 6),
                                       "to": round(math.copysign(step, delta), 6)})
                row["deltas"][axis_id] = math.copysign(step, delta)
    sums: dict[str, float] = {}
    for row in live:
        for axis_id, delta in row["deltas"].items():
            sums[axis_id] = sums.get(axis_id, 0.0) + delta
    order = list(axes)
    picked = sorted(sums, key=lambda a: (-abs(sums[a]), order.index(a) if a in order else 99))[:2]
    for row in live:
        for axis_id in list(row["deltas"]):
            if axis_id not in picked:
                row["dropped"].append({"axis": axis_id, "stage": "axis_limit",
                                       "why": "按轴序只取前两轴"})
                row["deltas"].pop(axis_id)
    step_sums = {axis_id: round(math.fsum(row["deltas"].get(axis_id, 0.0) for row in live), 6)
                 for axis_id in picked}

    # ③ 逐项降温（只有合资格的事件乘因子；总开关关掉时全员 1.0，也不记账）
    window, k = _heat_cfg()
    heat_on = bool(_sget("vector_heat_enabled", True))
    for row in live:
        factor, n, bucket = 1.0, 0, ""
        if heat_on and row.get("eligible") and row["deltas"]:
            bucket = f"{_pack_id()}:{uid or conv}:{row['kind']}"
            n = _bucket_n(state, bucket, now)
            factor = 1.0 / (1.0 + n / k) if k > 0 else 1.0
        row["heat"] = {"factor": round(factor, 6), "n": n, "k": k, "window": window, "bucket": bucket}
        for axis_id in list(row["deltas"]):
            row["deltas"][axis_id] *= factor
    heat_sums = {axis_id: round(math.fsum(row["deltas"].get(axis_id, 0.0) for row in live), 6)
                 for axis_id in picked}

    # ④ 批次同轴限幅：**两项各 +0.08 不能变成 +0.16**
    for axis_id in picked:
        total = math.fsum(row["deltas"].get(axis_id, 0.0) for row in live)
        step = min(float(axes[axis_id].get("max_step", global_ceiling)), global_ceiling)
        if abs(total) > step + 1e-12:
            scale = step / abs(total)
            for row in live:
                if axis_id in row["deltas"]:
                    row["deltas"][axis_id] *= scale
                    row["clipped"].append({"axis": axis_id, "stage": "batch_step",
                                           "why": "批次同轴限幅"})

    # ⑤ 日限额 + ⑥ 值域：按轴算总量，再按比例落回各项
    applied_total: dict[str, float] = {}
    for axis_id in picked:
        parts = {row["index"]: row["deltas"][axis_id] for row in live if axis_id in row["deltas"]}
        total0 = math.fsum(parts.values())
        if not parts:
            continue
        axis = axes[axis_id]
        cap = min(float(axis.get("max_daily", 0.5)), float(_sget("vector_daily_limit", 0.5) or 0.5))
        allowed = max(0.0, cap - _daily_used({"daily": daily}, axis_id, today))
        total1 = max(-allowed, min(allowed, total0))
        prev = current.get(axis_id, 0.0)
        total2 = max(-1.0, min(1.0, prev + total1)) - prev
        if abs(total1 - total0) > 1e-12:
            for row in live:
                if axis_id in row["deltas"]:
                    row["clipped"].append({"axis": axis_id, "stage": "daily", "why": "日限额裁剪"})
        if abs(total2 - total1) > 1e-12:
            for row in live:
                if axis_id in row["deltas"]:
                    row["clipped"].append({"axis": axis_id, "stage": "range", "why": "值域夹紧"})
        eff = (total2 / total0) if abs(total0) > 1e-15 else 1.0
        final_parts = {idx: value * eff for idx, value in parts.items()}
        applied_parts = _split(total2, final_parts)
        for row in live:
            if row["index"] in final_parts:
                row["contrib"][axis_id] = final_parts[row["index"]]
                if abs(applied_parts.get(row["index"], 0.0)) > 1e-12:
                    row["applied"][axis_id] = applied_parts[row["index"]]
                elif abs(total2) < 1e-12 and abs(total0) > 1e-12:
                    row["dropped"].append({"axis": axis_id, "stage": "cancel",
                                           "why": "反向事件互相抵消"})
                else:
                    stage = "daily_cap" if abs(total1) < 1e-12 else "range"
                    row["dropped"].append({"axis": axis_id, "stage": stage, "why": "裁剪后为 0"})
        # travel 记 **gross**：反向不抵消，否则正负对冲能把日额度刷成零成本
        travel[axis_id] = round(float(travel.get(axis_id, 0.0))
                                + math.fsum(abs(v) for v in final_parts.values()), 8)
        delta_book[axis_id] = round(float(delta_book.get(axis_id, 0.0)) + total2, 8)
        current[axis_id] = prev + total2
        applied_total[axis_id] = total2
        base = float(axis.get("baseline", 0.0))
        if abs(current[axis_id] - base) > 0.6 and not extreme.get(axis_id):
            extreme[axis_id] = now
        elif abs(current[axis_id] - base) <= 0.6 and extreme.get(axis_id):
            extreme.pop(axis_id, None)

    # ⑦ 记账：幂等、热簿、状态
    for row in live:
        if row["dedup_key"]:
            state["seen"][row["dedup_key"]] = now
        if heat_on and row.get("eligible") and any(abs(v) > 1e-6 for v in row["contrib"].values()):
            bucket = row["heat"].get("bucket") or ""
            if bucket:
                state["heat"]["buckets"].setdefault(bucket, []).append([now, row["event_id"]])

    # 逐项 effect：`after - decayed_before == applied` 要逐行成立
    # 模式前/后：**同一事务、同一时刻、同一对象**（#0011 P1-5）。读上一条审计会混进
    # 另一个人的模式，也会漏掉两次事件之间的衰减。
    offsets: dict[str, float] = {}
    if uid:
        try:
            offsets = persona_relation.offset(uid, is_master=is_master)
        except Exception:  # noqa: BLE001 - 关系拿不到就按无偏移算
            offsets = {}
    before_modes = [m["id"] for m in modes_hit(
        effective_from(before_all, uid, is_master=is_master, offsets=offsets, axes=axes))]
    after_modes = modes_hit(effective_from(current, uid, is_master=is_master,
                                           offsets=offsets, axes=axes))
    mode_info = {"hit": [m["id"] for m in after_modes], "before": before_modes,
                 "changed": before_modes != [m["id"] for m in after_modes],
                 "scope": {"uid": uid, "is_master": bool(is_master)},
                 "lines": [line for m in after_modes for line in m["do"][:2]][:4]}

    running = dict(before_all)
    rows: list[dict[str, Any]] = []
    for row in items:
        if not (row["applied"] or row["dropped"] or row["clipped"]):
            continue
        effects = []
        for axis_id, value in row["applied"].items():
            before = running.get(axis_id, 0.0)
            after = before + value
            running[axis_id] = after
            effects.append({"axis": axis_id, "nominal": round(row["nominal"].get(axis_id, 0.0), 8),
                            "decayed_before": before, "applied": value, "after": after,
                            "heat": row["heat"].get("factor", 1.0)})
        rows.append({"at": _stamp(now), "source": source, "conv": conv, "uid": uid,
                     "applied": {a: round(v, 4) for a, v in row["applied"].items()},
                     "event": row["event"], "effect": effects, "heat": row["heat"],
                     "mode": mode_info,
                     "stages": {"step": step_sums, "heat": heat_sums,
                                "daily": {a: round(v, 8) for a, v in applied_total.items()}},
                     "dropped": row["dropped"], "clipped": row["clipped"],
                     "event_id": row["event_id"]})
    if len(state.get("audit_pending") or []) + len(rows) > _MAX_AUDIT_PENDING:
        return _empty_result("audit_backlog")

    state["values"] = current
    state["at"] = now
    state["daily"] = daily
    state["audit_pending"] = list(state.get("audit_pending") or []) + rows
    if not _write_state(state, path):
        return _empty_result("write-failed")
    audit_ok = _append_audit(rows)
    if audit_ok and rows:
        written = {row["event_id"] for row in rows}
        state["audit_pending"] = [r for r in (state.get("audit_pending") or [])
                                  if str(r.get("event_id")) not in written]
        _write_state(state, path)

    merged: dict[str, float] = {}
    for row in items:
        for axis_id, value in row["applied"].items():
            merged[axis_id] = merged.get(axis_id, 0.0) + value
    return {
        "ok": bool(merged) and audit_ok,
        "why": "" if merged else "全部被裁剪或去重",
        "event_id": items[0]["event_id"] if items else "",
        "dedup": bool(items) and all(row["dedup"] for row in items),
        "applied": {a: v for a, v in merged.items()},
        "nominal": {a: math.fsum(r["nominal"].get(a, 0.0) for r in items) for a in merged},
        "clipped": [c for row in items for c in row["clipped"]],
        "dropped": [d for row in items for d in row["dropped"]],
        "heat": next((row["heat"] for row in items if row["heat"].get("bucket")), _heat_blank()),
        "stages": {"step": step_sums, "heat": heat_sums,
                   "daily": {a: round(v, 8) for a, v in applied_total.items()}},
        "values": {k: round(v, 3) for k, v in current.items()},
        "audit_ok": bool(audit_ok),
        "audit_pending_count": len(state.get("audit_pending") or []),
        "results": [{"event_id": row["event_id"], "item_key": row["item_key"],
                     "kind": row["kind"], "dedup": row["dedup"],
                     "applied": dict(row["applied"]),
                     "nominal": row["nominal"], "heat": row["heat"],
                     "dropped": row["dropped"], "clipped": row["clipped"]} for row in items],
    }


def apply_event(ev: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """单个事件：`apply_events([ev])` 的薄封装。"""
    result = apply_events([ev], **kwargs)
    results = result.get("results") or []
    if len(results) == 1:
        merged = dict(result)
        merged.update({"item_key": results[0]["item_key"],
                       "event_id": results[0]["event_id"]})
        return merged
    return result


def apply(deltas: dict[str, float], *, source: str = "code", conv: str = "", uid: str = "") -> dict[str, Any]:
    """按限额写入一组变化（旧入口，保留为薄封装）。

    它走**非语义事件**（`code` / `llm_vec`）：`<vec>` 自评与代码事件不参与降温，
    这样 `验证/_人格学习回归.py` 里"100 次交替 apply 合计 ≈ 日上限"的契约不变。
    """
    if not deltas:
        return {"ok": False, "why": "没有变化"}
    kind = "llm_vec" if str(source) == "llm" else "code"
    phase = "post" if kind == "llm_vec" else "code"
    result = apply_events([build_event(deltas, kind=kind, item="legacy")],
                          source=source, conv=conv, uid=uid, phase=phase, message_id=None)
    result.setdefault("values", {})
    return result



def record_llm(text: str) -> tuple[str, dict[str, float]]:
    """剥离模型回复里的 `<vec …>` 并返回解析出的 delta。

    **只解析模型自己生成的内容**；调用方只对模型输出调用它，不要对用户消息调用。
    未知轴、非数字、超范围一律丢弃。
    """
    deltas: dict[str, float] = {}
    parse_enabled = bool(_sget("vector_llm_enabled", False))
    axes = set(_axes())
    for match in _TAG_RE.finditer(text or ""):
        if not parse_enabled:
            continue
        for pair in re.split(r"[\s,]+", match.group(1)):
            key, _, raw = pair.partition("=")
            key = key.strip().lower()
            if key not in axes:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                deltas[key] = max(-0.05, min(0.05, value))
    clean = _TAG_RE.sub("", text or "").strip()
    return clean, deltas


def _mode_hit(mode: dict[str, Any], values: dict[str, float]) -> bool:
    when = mode.get("when") if isinstance(mode.get("when"), dict) else {}
    for axis_id, expr in when.items():
        value = values.get(str(axis_id))
        if value is None:
            return False
        text = str(expr).strip()
        try:
            if text.startswith("<="):
                if not value <= float(text[2:]):
                    return False
            elif text.startswith(">="):
                if not value >= float(text[2:]):
                    return False
            elif text.startswith("<"):
                if not value < float(text[1:]):
                    return False
            elif text.startswith(">"):
                if not value > float(text[1:]):
                    return False
        except (TypeError, ValueError):
            return False
    return bool(when)


def effective_from(values: dict[str, float], uid: str = "", *, is_master: bool = False,
                   offsets: dict[str, float] | None = None,
                   axes: dict[str, dict[str, Any]] | None = None) -> dict[str, float]:
    """**纯函数**：从给定的全局值算出对某个人的有效值。

    `offsets` / `axes` 由调用方在**同一事务、同一时刻**取一次传进来 —— 让它在内部
    读 `persona_relation.offset()` 会让两次调用之间关系变一次，#0011 的
    「前后模式」就会自相矛盾（#0011 P1-5）。
    """
    axis_defs = axes if isinstance(axes, dict) and axes else _axes()
    rel = offsets if isinstance(offsets, dict) else (
        persona_relation.offset(uid, is_master=is_master) if uid else {})
    out: dict[str, float] = {}
    for axis_id, axis in axis_defs.items():
        value = float((values or {}).get(axis_id, axis.get("baseline", 0.0)))
        if str(axis.get("scope") or "global") in ("both", "relation") and rel.get(axis_id):
            value += float(rel[axis_id]) * float(axis.get("relation_weight", 0.0) or 0.0)
        out[axis_id] = max(-1.0, min(1.0, value))
    return out


def effective(uid: str = "", *, is_master: bool = False) -> dict[str, float]:
    return effective_from(state()["values"], uid, is_master=is_master)


def modes_hit(values: dict[str, float]) -> list[dict[str, Any]]:
    """当前值命中的模式（纯函数）。`tags()` / `block()` / 审计共用一份，别各算一遍。"""
    out: list[dict[str, Any]] = []
    for mode in _load_vector().get("modes") or []:
        if isinstance(mode, dict) and mode.get("id") and _mode_hit(mode, values):
            out.append({"id": str(mode["id"]),
                        "do": [str(x) for x in (mode.get("do") or []) if str(x).strip()]})
    return out


def effective_samples(limit: int = 5) -> list[dict[str, Any]]:
    """面板用：按关系表列几个对象的三层值（全局 / 偏移 / 有效）。只读。"""
    out: list[dict[str, Any]] = []
    try:
        items = persona_relation.snapshot().get("items") or []
    except Exception:  # noqa: BLE001 - 关系表拿不到就不显示，不影响回复
        items = []
    values = state()["values"]
    axes = _axes()
    for item in items[:max(0, int(limit))]:
        uid = str(item.get("uid") or "")
        if not uid:
            continue
        is_master = bool(item.get("master"))
        offsets = persona_relation.offset(uid, is_master=is_master)
        out.append({
            "uid": uid, "who": str(item.get("who") or ""), "is_master": is_master,
            "values": {k: round(v, 3) for k, v in
                       effective_from(values, uid, is_master=is_master,
                                      offsets=offsets, axes=axes).items()},
            "offsets": {k: round(float(v), 3) for k, v in offsets.items()},
        })
    return out


def explain(uid: str = "", conv: str = "", *, limit: int = 3,
            ttl_h: float | None = None) -> dict[str, Any]:
    """**给某个人、某个会话**看的解释链。uid+conv 双匹配是硬条件。"""
    ttl = float(_sget("vector_explain_ttl_h", 6.0) or 6.0) if ttl_h is None else float(ttl_h)
    now = _now()
    rows, complete = _tail_rows(_audit_file(), with_meta=True)
    picked: list[dict[str, Any]] = []
    for row in rows:
        event = row.get("event") if isinstance(row.get("event"), dict) else {}
        if str(row.get("uid") or "") != str(uid or ""):
            continue
        if str(row.get("conv") or "") != str(conv or ""):
            continue
        at = _parse_stamp(row.get("at"))
        picked.append({
            "at": row.get("at"), "kind": event.get("kind") or row.get("source") or "",
            "verdict": event.get("verdict") or {}, "evidence": event.get("evidence") or [],
            "effect": row.get("effect") or [], "mode": row.get("mode") or {},
            "stale": bool(at) and (now - at) > ttl * 3600,
        })
    picked = picked[-max(1, int(limit)):]
    by_axis: dict[str, dict[str, Any]] = {}
    for row in picked:
        for eff in row["effect"]:
            axis = str(eff.get("axis") or "")
            if axis and abs(float(eff.get("applied") or 0.0)) > 1e-12:
                by_axis[axis] = {"at": row["at"], "applied": eff.get("applied")}
    return {"rows": picked, "by_axis": by_axis, "complete": complete,
            "file": str(_audit_file()), "ttl_h": ttl}


def recent(limit: int = 5) -> list[dict[str, Any]]:
    """**操作者视角**：最近几条审计（不过滤 uid/conv）。只给面板/指令，不进聊天上下文。"""
    ttl = float(_sget("vector_explain_ttl_h", 6.0) or 6.0)
    now = _now()
    out: list[dict[str, Any]] = []
    for row in _tail_rows(_audit_file(), limit=max(1, int(limit))):
        event = row.get("event") if isinstance(row.get("event"), dict) else {}
        at = _parse_stamp(row.get("at"))
        out.append({
            "at": row.get("at"), "source": row.get("source") or "",
            "uid": row.get("uid") or "", "conv": row.get("conv") or "",
            "kind": event.get("kind") or row.get("source") or "",
            "applied": row.get("applied") or {}, "dropped": row.get("dropped") or [],
            "mode": row.get("mode") or {},
            "stale": bool(at) and (now - at) > ttl * 3600,
        })
    return out


def _reason_line(uid: str, conv: str, values: dict[str, float],
                 message_id: Any) -> str:
    """「为什么现在是这样」的一句话。**7 条准入**见 #0011 方案；任一不满足就返回空串。

    宁可不说，也不编：历史事件被后续反号推翻、被 reset 覆盖、读数不完整、
    或该轴已经回落到阈值以下时，都不该给模型一个由头。
    """
    if not uid or not conv:
        return ""
    ttl = float(_sget("vector_explain_ttl_h", 6.0) or 6.0)
    threshold = float((_load_vector().get("inject") or {}).get("threshold", 0.2) or 0.2)
    rows, complete = _tail_rows(_audit_file(), with_meta=True)
    if not complete or not rows:
        return ""
    now = _now()
    axes = _axes()
    best: tuple[int, dict[str, Any], dict[str, Any]] | None = None
    for index, row in enumerate(rows):
        event = row.get("event") if isinstance(row.get("event"), dict) else {}
        if event.get("reused") or str(event.get("phase") or "") != "pre":
            continue
        if not any(int((item or {}).get("message_id") or 0) > 0
                   for item in (event.get("evidence") or [])):
            continue
        at = _parse_stamp(row.get("at"))
        if not at or (now - at) > ttl * 3600:
            continue
        if str(row.get("uid") or "") != str(uid) or str(row.get("conv") or "") != str(conv):
            continue
        for eff in row.get("effect") or []:
            axis = str(eff.get("axis") or "")
            applied = float(eff.get("applied") or 0.0)
            base = float((axes.get(axis) or {}).get("baseline", 0.0))
            value = float(values.get(axis, base))
            if abs(applied) < 1e-12 or abs(value - base) < threshold:
                continue
            if (applied > 0) != (value > base):
                continue                              # 方向必须相容
            later = rows[index + 1:]
            flipped = any(
                (float(other.get("applied") or 0.0) > 0) != (applied > 0)
                and abs(float(other.get("applied") or 0.0)) > 1e-12
                for row2 in later for other in (row2.get("effect") or [])
                if str(other.get("axis")) == axis)     # **全局轴**上判断，不先按 uid 筛
            reset_after = any(
                str(row2.get("source") or "") == "reset"
                and str(row2.get("axis") or "all") in ("all", axis)
                for row2 in later)
            if flipped or reset_after:
                continue
            best = (index, row, eff)
    if best is None:
        return ""
    _, row, _eff = best
    event = row.get("event") or {}
    act = str((event.get("verdict") or {}).get("act") or "")
    same_turn = message_id is not None and str(event.get("message_id")) == str(message_id)
    when = "刚才" if same_turn else "此前"
    word = act if act in ("骂", "亲密", "夸赞") else "话"
    tap = "本轮消息" if same_turn else "此前的互动"
    return f"【为什么是这样】- 因为他{when}那句{word}（依据：{tap}）"


def observe(text: str, *, conv: str = "", uid: str = "", is_master: bool = False,
            verdict: dict[str, Any] | None = None,
            message_id: Any = None) -> dict[str, float]:
    """从这一轮对方说的话里读「值得记录的事件」，更新行为状态。

    ## 输入：优先用**已经解释过的判断**

    `verdict` 是本轮语境解释的结果（`context_analysis.analyze()`）。**有它就一定用它**——
    否则会出现「检索按模型解释、状态按关键词」的分裂（方案 §13.4 明令禁止）。
    没有时才退回 `scene.judge()` 的规则层结论（只做快速否定）。

    ## 为什么不再按词表加减

    原来这里写死了两张表：`谢谢/厉害 → mood+0.04`、`滚/闭嘴/废物 → mood-0.05`。
    同一个词的意思取决于语境，按词扣分会把「他夸我」和「他引述别人骂我」算成同一件事。

    现在只认 `confidence=clear` 的结论：

    | 判定 | 变化 |
    |---|---|
    | `act=骂` 且 `target=bot` | mood ↓、defiance ↑ |
    | `act=亲密` 且 `target=bot` | mood ↑、social ↑ |
    | `act=夸赞` 且 `target=bot` 且 `tone≠开玩笑` | mood ↑、social ↑（2026-11-03 加，见 #0007） |
    | 其余（unknown / fuzzy / 指向第三方 / 转述） | **不更新，只随时间衰减** |

    「夸赞」只有深判能确认：规则层对夸赞**只给 fuzzy**（它没有 tone，判不了讽刺与玩笑式夸），
    所以这里真正的门槛是 `confidence=clear` —— 规则层那一路进不来。

    唯一的非语义项是**深夜**（`hour < 5` 降 energy）——时间事实，不是语义判断。
    """
    if not enabled():
        return {}
    body = str(text or "")
    deltas: dict[str, float] = {}
    kind = ""
    semantic_verdict: dict[str, Any] | None = None

    try:
        source = verdict
        if source is None:
            from . import scene

            source = scene.judge(body)
        semantic_verdict = source if isinstance(source, dict) else None
        act = str((source or {}).get("act") or "")
        target = str((source or {}).get("target") or "")
        tone = str((source or {}).get("tone") or "")
        if str((source or {}).get("confidence") or "") == "clear" and target == "bot":
            if act == "骂":
                deltas["mood"] = -0.05
                deltas["defiance"] = 0.04
                kind = "insult"
            elif act == "亲密":
                deltas["mood"] = 0.04
                deltas["social"] = 0.02
                kind = "intimate"
            elif act == "夸赞" and tone != "开玩笑":
                # 玩笑式夸赞**不升情绪** —— 这是产品选择，不是技术必然（#0007 待人工拍板）。
                # 规则层给不出 tone，所以走到这里的一定是深判结论。
                deltas["mood"] = 0.04
                deltas["social"] = 0.02
                kind = "praise"
    except Exception:  # noqa: BLE001 - 判类出错不能让情绪更新崩掉
        logger.debug("语境判类失败，本轮不更新状态", exc_info=True)

    # 时间事实：深夜本来就会累。这一条与语义无关，保留；但它**不参与降温**。
    events: list[dict[str, Any]] = []
    if deltas:
        events.append(build_event(deltas, kind=kind or "code", item=kind or "semantic",
                                  verdict=semantic_verdict, message_id=message_id))
    if time.localtime(_now()).tm_hour < 5:
        deltas["energy"] = -0.02
        events.append(build_event({"energy": -0.02}, kind="night", item="night",
                                  message_id=message_id))

    if events:
        try:
            apply_events(events, source="event", conv=conv, uid=uid,
                         message_id=message_id, phase="pre", is_master=is_master)
        except Exception:  # noqa: BLE001 - 状态写不进去也不能拖垮回复
            logger.debug("事件写入失败 conv=%s", conv, exc_info=True)
    try:
        if uid:
            from . import scene_state as _sst_bound

            kind, keyword = _sst_bound.boundary_of(body)
            if kind:
                persona_relation.note_stop(uid, is_master=is_master)
                # 同时写进语境边界：边界不能被关系值抵消（§13.1）
                try:
                    from . import scene_state

                    scene_state.note_boundary(conv, body[:60], kind=kind, word=keyword)
                except Exception:  # noqa: BLE001
                    logger.debug("写语境边界失败 conv=%s", conv, exc_info=True)
            # 提到话题并不等于授权互损；许可由显式确认入口记录。
    except Exception:  # noqa: BLE001
        logger.debug("话题学习失败 uid=%s", uid, exc_info=True)
    return deltas


def tags(uid: str = "", *, is_master: bool = False) -> list[str]:
    """当前显著轴与命中模式，供示例库检索。"""
    out: list[str] = []
    try:
        values = effective(uid, is_master=is_master)
        axes = _axes()
    except Exception:  # noqa: BLE001
        return out
    threshold = float((_load_vector().get("inject") or {}).get("threshold", 0.2) or 0.2)
    for axis_id, axis in axes.items():
        base = float(axis.get("baseline", 0.0))
        value = float(values.get(axis_id, base))
        if abs(value - base) >= threshold:
            out.append(f"{axis_id}:{'high' if value > base else 'low'}")
    for mode in modes_hit(values):
        out.append(f"mode:{mode['id']}")
    return out


def block(*, conv: str = "", is_master: bool = False, uid: str = "", query: str = "",
          private: bool = True, name: str = "", message_id: Any = None) -> str:
    """组装"此刻的状态 + 对这个人"两段。没有显著内容时返回空串。

    `message_id` 只用来判断"这个原因是本轮还是此前"（#0011）：同一条消息 → 「刚才」，
    否则 → 「此前」。少了它也能出原因行，只是措辞不会声称"刚才"。
    """
    if not enabled():
        return ""
    try:
        values = effective(uid, is_master=is_master)
        axes = _axes()
    except Exception:  # noqa: BLE001
        logger.debug("矢量块生成失败", exc_info=True)
        return ""
    threshold = float((_load_vector().get("inject") or {}).get("threshold", 0.2) or 0.2)
    top_k = max(1, min(3, int(_sget("vector_inject_top_k", 2) or 2)))
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for axis_id, axis in axes.items():
        base = float(axis.get("baseline", 0.0))
        value = float(values.get(axis_id, base))
        if abs(value - base) < threshold:
            continue
        weight = 1.0 + float(axis.get("relation_weight", 0.0) or 0.0)
        scored.append((abs(value - base) * weight, axis_id, axis))
    scored.sort(key=lambda item: -item[0])
    picked = scored[:top_k]

    parts: list[str] = []
    if picked:
        lines = ["【此刻的状态】"]
        for _, axis_id, axis in picked:
            value = values[axis_id]
            base = float(axis.get("baseline", 0.0))
            side = str(axis.get("high") if value > base else axis.get("low") or "").strip()
            lines.append(f"- {axis.get('name', axis_id)}偏{'高' if value > base else '低'}（{side}）")
        mode_lines: list[str] = []
        for mode in modes_hit(values):
            for action in mode["do"][:2]:
                mode_lines.append(f"- {action}")
            if len(mode_lines) >= 4:
                break
        if mode_lines:
            lines.append("【这个状态下的做法】")
            lines.extend(mode_lines)
        if bool(_sget("vector_explain_enabled", True)):
            try:
                reason = _reason_line(uid, conv, values, message_id)
            except Exception:  # noqa: BLE001 - 解释失败不能拖垮状态注入
                logger.debug("原因行生成失败 conv=%s", conv, exc_info=True)
                reason = ""
            if reason:
                lines.append(reason)
        parts.append("\n".join(lines))

    try:
        rel = persona_relation.block(uid, is_master=is_master, private=private, name=name)
    except Exception:  # noqa: BLE001
        rel = ""
    if rel:
        parts.append(rel)
    text = "\n\n".join(parts)
    budget = int(_sget("vector_budget_chars", 380) or 380)
    if len(text) > budget:
        text = text[:budget].rstrip()
    return text


def set_param(axis_id: str, field: str, value: Any) -> dict[str, Any]:
    """改一个轴的参数（评估台的 `axis.patch` / `vector` op 落点）。

    写的是运行副本；没有就先把包内 `vector.json` 复制过来。
    """
    ranges = {"baseline": (-1, 1), "half_life_h": (0.5, 720),
              "max_step": (0.001, 0.2), "max_daily": (0.001, 1),
              "max_hold_h": (0.5, 720), "relation_weight": (0, 1)}
    if field not in ranges:
        return {"ok": False, "why": "只允许修改已登记的数值参数"}
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return {"ok": False, "why": "参数必须是有限数值"}
    low, high = ranges[field]
    if not low <= value <= high:
        return {"ok": False, "why": f"{field} 必须在 {low}..{high} 之间"}
    path = config.persona_data_dir() / "vector.json"
    try:
        if not path.exists():
            source = config.vector_file_path()
            if not source.exists():
                return {"ok": False, "why": "没有 vector.json 可改"}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read_bytes())
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "why": f"读 vector.json 失败：{exc}"}
    for axis in raw.get("axes") or []:
        if isinstance(axis, dict) and str(axis.get("id")) == str(axis_id):
            axis[str(field)] = value
            break
    else:
        return {"ok": False, "why": f"没有轴 {axis_id}"}
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        return {"ok": False, "why": f"写 vector.json 失败：{exc}"}
    return {"ok": True, "axis": axis_id, "field": field, "value": value, "file": str(path)}


def stats() -> dict[str, Any]:
    try:
        values = state()["values"]
        return {"values": {k: round(float(v), 3) for k, v in values.items()},
                "file": str(config.vector_state_path())}
    except Exception:  # noqa: BLE001
        return {"values": {}, "file": ""}


def reset(axis: str = "") -> dict[str, Any]:
    """清零（或清某轴）。**同锁**：它同样是「读-改-写」，与更新并发会互相覆盖。

    另外记一条 `source="reset"` 的审计行：人工把值改掉这件事没有事件支撑，
    #0011 的解释链要靠它来**拒绝**归因（"当前的高值是 reset 出来的，不是被谁夸出来的"）。
    """
    now = _now()
    path = config.vector_state_path()
    with filelock.exclusive(path.with_name(path.name + ".lock")) as got:
        if not got:
            return {"ok": False, "why": "busy"}
        state = _read_state(path)
        if axis:
            state.setdefault("values", {}).pop(axis, None)
        else:
            state["values"] = {}
            state["extreme_since"] = {}
        state["at"] = now
        row = {"at": _stamp(now), "source": "reset", "axis": axis or "all",
               "applied": {}, "event_id": _event_id("reset", axis or "all", now)}
        state["audit_pending"] = list(state.get("audit_pending") or []) + [row]
        if not _write_state(state, path):
            return {"ok": False, "why": "write-failed"}
        if _append_audit([row]):
            state["audit_pending"] = [r for r in state.get("audit_pending") or []
                                      if str(r.get("event_id")) != row["event_id"]]
            _write_state(state, path)
        return {"ok": True, "reset": axis or "all", "audit_ok": True}
