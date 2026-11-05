"""关系偏移：全局情绪之外，对**具体某个人**的态度。

## 为什么单独一层

`vector_state.json` 回答"她今天怎么样"，`relations.json` 回答"她对这个人是什么态度"。
前者小时级波动，后者天/周级缓慢变化。把两者混成一个值，会出现"对 A 生气之后
对 B 也冷"——那不是人，是一条全局变量。

## 输入（全部由代码计算，不额外花 token）

| 来源 | 用途 |
|---|---|
| `memory` 的人物画像（`love / dislike / habit / note`） | 喜欢多 → 更愿意聊；明确不喜欢 → 更不肯迁就 |
| `chatlog` 的互动量与最后发言时间 | 熟人加分、久不联系扣分 |
| `_pack.json` / 调用方传来的 `is_master` | 主人更软、更黏、更愿意迁就 |
| `relations.json` 里的手动值 | 评估台/人最高优先，可锁定 |

`play`（玩闹许可，0~1）决定"能不能互损"；`intimacy`（0~1）决定"距离感"。
**性相关的身体接触不由这两个值放开**：`physical_allowed` 只在"主人 + 私聊"时为真。

## 落盘

`data/runtime/persona/<包>/relations.json`，按 `uid:<QQ号>` 隔离；只存 uid 与派生值，
不复制画像原文，不进公开副本。
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger("ai_chat.persona_relation")

_FILE = "relations.json"
_MSG_TTL = 300.0  # 互动统计缓存 5 分钟：它变化很慢，不必每轮重扫聊天记录
_msg_cache: dict[str, tuple[float, int, float]] = {}


def _now() -> float:
    from .persona_workspace import now
    return now()


def _path() -> Path:
    return config.relations_path()


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"version": 1, "items": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("关系数据损坏，按空数据继续：%s", path.name)
        return {"version": 1, "items": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "items": {}}
    raw.setdefault("items", {})
    if not isinstance(raw["items"], dict):
        raw["items"] = {}
    return raw


def _save(data: dict[str, Any]) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("关系数据写盘失败：%s", path)


def _key(uid: Any) -> str:
    text = str(uid or "").strip()
    if not text:
        return ""
    return text if text.startswith("uid:") else f"uid:{text}"


def _entry(data: dict[str, Any], uid: Any, *, name: str = "", is_master: bool = False) -> dict[str, Any]:
    """取（或创建）某人的关系条目。**只补元数据，不写盘**。"""
    key = _key(uid)
    if not key:
        return {}
    items = data.setdefault("items", {})
    item = items.get(key)
    if not isinstance(item, dict):
        item = {}
        items[key] = item
    item.setdefault("tease_topics", [])
    item.setdefault("no_go", {})
    if name:
        item["who"] = str(name)[:40]
    if is_master:
        item["master"] = True
    return item


def _profile(uid: Any) -> dict[str, Any]:
    """从 memory 里取这个人的画像。取不到返回空 dict。"""
    key = _key(uid)
    try:
        from . import memory

        db = getattr(memory, "_db", None)
        if db is None:
            return {}
        db.ensure()
        profiles = getattr(db, "profile", {}) or {}
        got = profiles.get(key)
        return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001 - 画像读不到不能拖垮回复
        logger.debug("读取人物画像失败 uid=%s", uid, exc_info=True)
    return {}


def _msg_stats(uid: Any) -> tuple[int, float]:
    """跨会话统计这个人的消息条数与最后发言时间。带 5 分钟缓存。"""
    key = _key(uid)
    if not key:
        return 0, 0.0
    now = _now()
    cached = _msg_cache.get(key)
    if cached and now - cached[0] < _MSG_TTL:
        return cached[1], cached[2]
    count, last = 0, 0.0
    try:
        from . import chatlog

        target = key.split(":", 1)[1]
        for log in list(chatlog.all_logs().values()):
            for index, msg in enumerate(log.messages):
                if str(msg.get("uid") or "") != target:
                    continue
                if msg.get("is_bot"):
                    continue
                neighbors = log.messages[max(0, index - 1):index] + log.messages[index + 1:index + 2]
                if not any(n.get("is_bot") and abs(float(n.get("ts") or 0) - float(msg.get("ts") or 0)) <= 180 for n in neighbors):
                    continue
                count += 1
                last = max(last, float(msg.get("ts") or 0))
    except Exception:  # noqa: BLE001
        logger.debug("统计互动量失败 uid=%s", uid, exc_info=True)
    _msg_cache[key] = (now, count, last)
    return count, last


def _profile_counts(prof: dict[str, Any]) -> tuple[int, int]:
    love = len([x for x in (prof.get("love") or []) if str(x or "").strip()])
    dislike = len([x for x in (prof.get("dislike") or []) if str(x or "").strip()])
    return love, dislike


def _limits() -> dict[str, Any]:
    """关系参数（`vector.json` 的 relation / intimacy 块）。读取失败时用内置默认。"""
    raw = config.load_vector()
    relation = raw.get("relation") if isinstance(raw.get("relation"), dict) else {}
    intimacy = raw.get("intimacy") if isinstance(raw.get("intimacy"), dict) else {}
    return {
        "max_offset": float(relation.get("max_offset", 0.35) or 0.35),
        "play_threshold": float(relation.get("play_threshold", 0.4) or 0.4),
        "master_ceiling": float(intimacy.get("master_ceiling", 0.8) or 0.8),
        "other_ceiling": float(intimacy.get("other_ceiling", 0.2) or 0.2),
        "physical_master_only": bool(intimacy.get("physical_master_only", True)),
        "physical_private_only": bool(intimacy.get("physical_private_only", True)),
    }


def ensure(uid: Any, *, name: str = "", is_master: bool = False) -> dict[str, Any]:
    """确保条目存在并返回（不写盘；`note_*` 才会落盘）。"""
    return _entry(_load(), uid, name=name, is_master=is_master)


def derived(uid: Any, *, is_master: bool = False) -> dict[str, Any]:
    """按画像、互动量、主人标记算出关系量。**纯函数**（除读缓存外无副作用）。"""
    data = _load()
    item = _entry(data, uid, is_master=is_master)
    prof = _profile(uid)
    love, dislike = _profile_counts(prof)
    msgs, last = _msg_stats(uid)
    limits = _limits()

    play = 0.2 + 0.05 * math.log10(msgs + 1) + (0.30 if is_master else 0.0)
    play = max(0.0, min(1.0, play))
    if item.get("play_manual") is not None:
        play = max(0.0, min(1.0, float(item["play_manual"])))

    silence_days = 0.0 if not msgs else max(0.0, (_now() - last) / 86400.0)
    base_intimacy = 0.05 + 0.02 * math.log10(msgs + 1) + (0.6 if is_master else 0.0)
    ceiling = limits["master_ceiling"] if is_master else limits["other_ceiling"]
    intimacy = min(base_intimacy, ceiling)
    if item.get("intimacy_manual") is not None:
        intimacy = min(float(item["intimacy_manual"]), ceiling)

    offsets: dict[str, float] = {}
    if is_master:
        offsets["social"] = 0.15
        offsets["vulnerability"] = 0.10
        offsets["defiance"] = -0.10
    offsets["social"] = offsets.get("social", 0.0) - 0.02 * min(silence_days, 5)
    offsets["social"] += 0.02 * math.log10(msgs + 1)
    manual = item.get("offsets") if isinstance(item.get("offsets"), dict) else {}
    for axis, value in manual.items():
        try:
            offsets[str(axis)] = float(value)
        except (TypeError, ValueError):
            continue
    relation = config.load_vector().get("relation") or {}
    half_life = max(1.0, float(relation.get("half_life_d", 14)))
    decay = 0.5 ** (silence_days / half_life)
    play = (0.5 if is_master else 0.2) + (play - (0.5 if is_master else 0.2)) * decay if item.get("play_manual") is None else play
    offsets = {k: v * decay if k not in manual else v for k, v in offsets.items()}
    cap = limits["max_offset"]
    offsets = {k: max(-cap, min(cap, v)) for k, v in offsets.items()}

    return {
        "uid": _key(uid),
        "who": str(item.get("who") or prof.get("display") or ""),
        "master": bool(is_master),
        "play": round(play, 3),
        "intimacy": round(intimacy, 3),
        "offsets": {k: round(v, 3) for k, v in offsets.items()},
        "love": love,
        "dislike": dislike,
        "msgs": msgs,
        "silence_days": round(silence_days, 1),
        "limits": limits,
    }


def offset(uid: Any, *, is_master: bool = False) -> dict[str, float]:
    return dict(derived(uid, is_master=is_master).get("offsets") or {})


def play(uid: Any, *, is_master: bool = False) -> float:
    return float(derived(uid, is_master=is_master)["play"])


def physical_allowed(uid: Any, *, is_master: bool, private: bool) -> bool:
    """身体接触与性行为：**只看对象与场合**，不看 play / intimacy。"""
    limits = _limits()
    if limits["physical_master_only"] and not is_master:
        return False
    if limits["physical_private_only"] and not private:
        return False
    return bool(is_master and private)


def topics(uid: Any, *, is_master: bool = False) -> list[str]:
    """当前能开玩笑的话题。

    * 默认只给低风险的 `appearance` / `skill`（`play` 够才给）；
    * `romance` 需要画像里聊过生活（喜欢/不喜欢非空）；
    * `sex` **不自动给**：由评估台显式加，或从互动里学（`note_topic`）。
    """
    item = _entry(_load(), uid, is_master=is_master)
    play_value = play(uid, is_master=is_master)
    limits = _limits()
    out = set()
    if play_value >= limits["play_threshold"]:
        out.update(str(x) for x in item.get("confirmed_topics", []) if x in {"appearance", "skill", "romance", "sex", "age"})
    no_go = item.get("no_go") if isinstance(item.get("no_go"), dict) else {}
    now = _now()
    for topic, until in list(no_go.items()):
        try:
            if float(until) > now:
                out.discard(str(topic))
        except (TypeError, ValueError):
            out.discard(str(topic))
    return sorted(out)


def note_topic(uid: Any, topic: str, *, is_master: bool = False, confirmed: bool = False) -> dict[str, Any]:
    """记录明确确认的话题许可；普通提及不得调用为 confirmed。"""
    topic = str(topic or "").strip()
    if topic not in {"appearance", "skill", "romance", "sex", "age"} or not confirmed:
        return {"ok": False, "why": "话题许可需要明确确认，提及不算许可"}
    data = _load()
    item = _entry(data, uid, is_master=is_master)
    if float((item.get("no_go") or {}).get(topic, 0)) > _now():
        return {"ok": False, "why": "话题仍在拒绝冷却期"}
    confirmed_topics = item.setdefault("confirmed_topics", [])
    if topic not in confirmed_topics:
        confirmed_topics.append(topic)
    hits = item.setdefault("tease_hits", {})
    hits[topic] = int(hits.get(topic, 0) or 0) + 1
    if hits[topic] >= 2 and topic not in item["tease_topics"]:
        item["tease_topics"].append(topic)
    item["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(data)
    _msg_cache.pop(_key(uid), None)
    return {"ok": True, "topic": topic, "hits": hits[topic], "topics": topics(uid, is_master=is_master)}


def note_stop(uid: Any, topic: str = "", *, is_master: bool = False) -> dict[str, Any]:
    """对方说"别说了 / 过了"：该话题冷却 24 小时，之后仍须明确重新确认，不自动恢复。"""
    data = _load()
    item = _entry(data, uid, is_master=is_master)
    until = _now() + 24 * 3600
    targets = [str(topic)] if topic else ["appearance", "skill", "romance", "sex", "age"]
    for name in targets:
        if not name:
            continue
        item.setdefault("no_go", {})[name] = until
        if name in item.get("confirmed_topics", []):
            item["confirmed_topics"].remove(name)
        if name in (item.get("tease_topics") or []):
            item["tease_topics"].remove(name)
        hits = item.setdefault("tease_hits", {})
        hits.pop(name, None)
    item["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(data)
    return {"ok": True, "cooling": targets, "until": until}


def block(uid: Any, *, is_master: bool, private: bool, name: str = "") -> str:
    """注入"对这个人"的差异。没有差异时返回空串。"""
    try:
        info = derived(uid, is_master=is_master)
    except Exception:  # noqa: BLE001
        logger.debug("关系块生成失败 uid=%s", uid, exc_info=True)
        return ""
    if not info.get("uid"):
        return ""
    who = name or info.get("who") or "这个人"
    lines: list[str] = [f"【对这个人】{who}："]
    limits = info["limits"]
    if is_master:
        lines.append("- 可以更软、更黏、更愿意迁就；身体接触只在私聊，群聊里收着。")
    else:
        lines.append("- 可以熟，但不暧昧：不做亲密身体接触，不用伴侣式称呼；敏感玩笑只在明确许可和成年条件满足时使用。")
        if info["play"] >= limits["play_threshold"]:
            lines.append("- 关系够近，可以损两句；对方说停就停。")
        else:
            lines.append("- 还不算熟，保持礼貌，不互损、不阴阳。")
    topic_list = topics(uid, is_master=is_master)
    if topic_list:
        lines.append("- 能开玩笑的话题：" + "、".join(topic_list) + "。")
    if not private:
        lines.append("- 这是群聊：亲密内容一律收着。")
    return "\n".join(lines)


def set_manual(uid: Any, field: str, value: Any) -> dict[str, Any]:
    """评估台手动改某人的关系值。`play/intimacy` 写 `*_manual`，话题写清单。"""
    key = _key(uid)
    if not key:
        return {"ok": False, "why": "uid 为空"}
    data = _load()
    item = _entry(data, uid)
    try:
        if field in ("play", "intimacy") and (type(value) not in (int, float) or not math.isfinite(value)):
            return {"ok": False, "why": "关系值必须是有限数值"}
        if field == "play":
            item["play_manual"] = max(0.0, min(1.0, float(value)))
        elif field == "intimacy":
            item["intimacy_manual"] = max(0.0, min(1.0, float(value)))
        elif field == "tease_topics":
            topics = {str(x) for x in (item.get("confirmed_topics") or [])}
            if isinstance(value, list):
                topics.update(str(x) for x in value if str(x))
            elif value:
                topics.add(str(value))
            allowed = {"appearance", "skill", "romance", "sex", "age"}
            if not topics <= allowed:
                return {"ok": False, "why": "未知话题"}
            if any(float((item.get("no_go") or {}).get(t, 0)) > _now() for t in topics):
                return {"ok": False, "why": "不能覆盖拒绝冷却期"}
            item["confirmed_topics"] = sorted(topics)
            item["tease_topics"] = sorted(topics)
        elif field == "no_go":
            topic = str(value or "").strip()
            if not topic:
                return {"ok": False, "why": "no_go 需要话题名"}
            item.setdefault("no_go", {})[topic] = _now() + 24 * 3600
            if topic in (item.get("tease_topics") or []):
                item["tease_topics"].remove(topic)
        else:
            return {"ok": False, "why": f"不支持的关系字段：{field}"}
    except (TypeError, ValueError):
        return {"ok": False, "why": "值不是数字"}
    item["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(data)
    return {"ok": True, "uid": key, "field": field, "value": value}


def snapshot(limit: int = 50) -> dict[str, Any]:
    """给控制台看的关系快照：人数 + 前 N 个人的派生值（**不返回画像原文**）。"""
    data = _load()
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    rows: list[dict[str, Any]] = []
    for key, item in list(items.items())[:max(1, limit)]:
        if not isinstance(item, dict):
            continue
        uid = key.split(":", 1)[1] if ":" in key else key
        try:
            info = derived(uid, is_master=bool(item.get("master")))
        except Exception:  # noqa: BLE001
            continue
        info["tease_topics"] = topics(uid, is_master=bool(item.get("master")))
        rows.append(info)
    return {"people": len(items), "items": rows, "file": str(_path())}


def stats(uid: Any = "", *, is_master: bool = False) -> dict[str, Any]:
    if uid:
        return derived(uid, is_master=is_master)
    data = _load()
    items = data.get("items") if isinstance(data.get("items"), dict) else {}
    return {"people": len(items), "file": str(_path())}


def reset(uid: Any = "") -> dict[str, Any]:
    """清掉某人的关系（或全部）。"""
    data = _load()
    key = _key(uid)
    if key:
        data.get("items", {}).pop(key, None)
    else:
        data["items"] = {}
    _save(data)
    _msg_cache.clear()
    return {"ok": True, "cleared": key or "all"}
