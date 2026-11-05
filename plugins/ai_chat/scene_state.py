"""会话级「语境状态」：记住这一段对话在聊什么、跟谁、什么口气，以及明确的边界。

## 为什么不是缓存「某个词是什么意思」

同一个词的意思不在词里。「小笨蛋」在安慰、还击、挑逗、陌生人搭话四种语境下含义相反，
还取决于「上次这么说时对方什么反应」。所以这里存的是**会话摘要**（话题 / 对象 / 互动性质 /
已表达的边界 / 证据），不是词到情绪的映射表。

`scene.judge()` 判不出来的软词（`needs_context=True`），由上层结合这份状态去解释。

## 三条硬规则

1. **版本**：每次写入 `version += 1`。带 `base_version` 的更新若与磁盘不符就**丢弃**
   ——后台分析晚到很正常，晚到的结果不能覆盖更新的判断。
2. **重试不累计**：同一条消息的更新用 `message_id` 去重，重试不会让状态走两遍。
3. **边界不会被关系抵消**：`boundaries` 是显式拒绝，永远优先于「关系很熟」这类背景。
   关系只提供背景，不构成许可。

## 边界

这里的 `mode`（认真讨论 / 互相调侃 / 争执 / 冷场）是**描述性**的，不是情绪数值；
它不直接改五轴，也不参与「词 → 扣分」。要改状态仍走 `persona_vector.apply()` 那条路，
且必须带证据。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("ai_chat.scene_state")

MODES: tuple[str, ...] = ("认真讨论", "互相调侃", "争执", "冷场", "不明确")
_MAX_EVIDENCE = 8
_MAX_BOUNDARIES = 12
_STALE_HOURS = 3.0


def _root() -> Path:
    from . import config          # 延迟导入：本模块的纯函数部分要能离线测试

    return Path(config.runtime_root()) / "scene"


def _path(conv: str, root: Path | None = None) -> Path:
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(conv or ""))
    return (root or _root()) / f"{safe or 'unknown'}.json"


def empty(conv: str) -> dict[str, Any]:
    return {
        "conv": str(conv),
        "version": 0,
        "updated_at": 0.0,
        "topic": "",
        "partners": {},
        "mode": "不明确",
        "confidence": "unknown",
        "boundaries": [],
        "evidence": [],
        "last_turn": {},
        # 深判缓存槽（#0002）。**独立于 `last_turn`**：后者会被 attention 与
        # `record_llm()` 整体覆盖（`merge()` 里是 `state["last_turn"] = dict(turn)`），
        # 往上补键会被它们的写入冲掉。`load()` 的白名单是 `k in state`，
        # 所以只要这里列出来，读盘就自动保留。
        "last_analysis": {},
        # 边界修订号：**只增不减**，不受「最多 12 条」截断影响。
        # 用条数当判据会漏掉"满 12 条后又加了一条"（条数仍是 12）。
        "boundaries_rev": 0,
    }


def load(conv: str, *, root: Path | None = None) -> dict[str, Any]:
    """读状态。**读不到或损坏都返回空状态**，不抛——它是辅助信息，不能拖垮回复。"""
    path = _path(conv, root)
    if not path.exists():
        return empty(conv)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("语境状态读不动，按空处理：%s", path.name)
        return empty(conv)
    if not isinstance(raw, dict):
        return empty(conv)
    state = empty(conv)
    state.update({k: v for k, v in raw.items() if k in state})
    state["conv"] = str(conv)
    for key in ("partners", "last_turn"):
        if not isinstance(state.get(key), dict):
            state[key] = {}
    for key in ("boundaries", "evidence"):
        if not isinstance(state.get(key), list):
            state[key] = []
    return state


def save(conv: str, state: dict[str, Any], *, root: Path | None = None) -> bool:
    """原子写。失败只记日志。"""
    path = _path(conv, root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        logger.debug("语境状态写盘失败：%s", path, exc_info=True)
        return False


def _dedupe(rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    """按 `key` 去重，**保留最近一条**，并只留最新 `limit` 条（返回旧→新）。"""
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in reversed(rows):                     # 新 → 旧
        if not isinstance(row, dict):
            continue
        mark = str(row.get(key) or "")
        if mark and mark in seen:
            continue
        seen.add(mark)
        kept.append(row)
        if len(kept) >= limit:                     # 取够最新的 N 条就停
            break
    return list(reversed(kept))


def merge(conv: str, delta: dict[str, Any], *, base_version: int | None = None,
          root: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """增量合并。返回 `{ok, state, dropped?}`。

    `base_version` 给了就做乐观并发：磁盘版本与它不符说明**有更新的判断已经落地**，
    这次（晚到的）结果直接丢弃，只记日志。
    """
    state = load(conv, root=root)
    if base_version is not None and int(state.get("version") or 0) != int(base_version):
        logger.info("语境更新已过期，丢弃：conv=%s 期望 v%s 实际 v%s",
                    conv, base_version, state.get("version"))
        return {"ok": False, "why": "版本已过期，丢弃晚到的结果", "state": state, "dropped": True}

    if not isinstance(delta, dict):
        return {"ok": True, "state": state}

    # 同一条消息的**同一来源**重复更新才跳过。
    # 去重键必须是 (message_id, source) 而不是只有 message_id：
    # 同一条消息会先后有「前置深判（analysis）」和「回复后置补充（llm）」两次写入，
    # 只按 message_id 去重会把后置那次静默吞掉——`<ctx>` 就永远不会生效。
    turn = delta.get("last_turn") or {}
    prev = state.get("last_turn") or {}
    if turn and prev and \
            str(turn.get("message_id") or "") == str(prev.get("message_id") or "") and \
            str(turn.get("source") or "") == str(prev.get("source") or ""):
        return {"ok": True, "state": state, "skipped": "同一条消息的同来源已合并"}

    for key in ("topic", "mode", "confidence"):
        if delta.get(key):
            state[key] = str(delta[key])
    if isinstance(delta.get("partners"), dict):
        state["partners"] = {**state.get("partners", {}), **delta["partners"]}
    if isinstance(delta.get("evidence"), list):
        state["evidence"] = _dedupe(
            list(state.get("evidence") or []) + delta["evidence"], "ref", _MAX_EVIDENCE)
    if isinstance(delta.get("boundaries"), list) and delta["boundaries"]:
        state["boundaries"] = _dedupe(
            list(state.get("boundaries") or []) + delta["boundaries"], "text", _MAX_BOUNDARIES)
        state["boundaries_rev"] = int(state.get("boundaries_rev") or 0) + 1
    if turn:
        state["last_turn"] = dict(turn)
    # 深判缓存：只有深判会传这个键；attention / record_llm 都不传，所以不会被覆盖。
    if isinstance(delta.get("last_analysis"), dict):
        state["last_analysis"] = dict(delta["last_analysis"])

    state["version"] = int(state.get("version") or 0) + 1
    state["updated_at"] = float(now if now is not None else time.time())
    ok = save(conv, state, root=root)
    return {"ok": ok, "state": state}


# ---- 显式边界的识别（2026-11-03 按 #0005 从"纯词表"改成"句式优先"）----
# 原来的词表是 `("别说了", "别开这个", "过了", "别聊这个", "不想聊这个")` ——
# 语料里最典型的边界「别再叫我小笨蛋了」**一个都不命中**，于是边界根本没被记下来，
# 检索侧也就无从过滤。这里按句式识别，并顺手抽出被禁的那个词。
# 放在本模块而不是 `persona_vector`：这里只依赖标准库，离线回放能直接验。
_STOP_TOPIC = ("别说了", "别开这个", "别聊这个", "别说这个", "不想聊这个", "不想聊这个话题")
_STOP_CALL_RE = re.compile(
    r"(?:别再|不要|不许|不准|别这么|别这样|别)\s*(?:叫我|叫|喊我|喊|称呼我)\s*"
    r"[「'\"《]?([^」'\"》，。,！!？?\s]{1,10})")
_STOP_CALL_HINT = ("别再叫", "别叫我", "不要叫", "不许叫", "不准叫", "别这么叫", "别这样叫", "别喊我")


def boundary_of(text: str) -> tuple[str, str]:
    """这句话是不是**显式边界**？返回 `(kind, word)`，不是边界就是 `("", "")`。

    `kind` 取 `称呼` / `话题`；`word` 是被禁的那个词（抽不出就留空 —— 只挡整句不挡词）。
    """
    body = str(text or "")
    match = _STOP_CALL_RE.search(body)
    if match:
        # 去掉句末语气词：「别再叫我小笨蛋**了**」的被禁词是「小笨蛋」，不是「小笨蛋了」
        word = re.sub(r"[了啦啊吧呢嘛呀哦]+$", "", match.group(1)).strip()
        return "称呼", word
    if any(word in body for word in _STOP_CALL_HINT):
        return "称呼", ""
    if any(word in body for word in _STOP_TOPIC):
        return "话题", ""
    return "", ""


def note_boundary(conv: str, text: str, *, kind: str = "", word: str = "",
                  message_id: Any = None, root: Path | None = None) -> dict[str, Any]:
    """记一条**显式边界**（「别再叫我小笨蛋」）。边界不会被关系值抵消。

    ## 为什么要 `kind` 与 `word`（2026-11-03 按 #0005 加）

    原来只存整句（截 60 字），检索侧拿不到"禁的到底是什么"，于是想过滤也没得滤。
    现在分开：

    * `kind`：`称呼` / `话题` / `""`（认不出来就留空，只挡整句不挡词）；
    * `word`：被禁的那个词（「别再叫**小笨蛋**了」→ `小笨蛋`）。
      **抽不出来就留空**，不要编 —— 空 `word` 的后果只是"这条边界不参与词面过滤"。
    """
    body = str(text or "").strip()
    if not body:
        return {"ok": False, "why": "空文本"}
    row: dict[str, Any] = {"text": body, "message_id": message_id, "at": time.time()}
    if kind:
        row["kind"] = str(kind)[:8]
    if word:
        row["word"] = str(word)[:20]
    return merge(conv, {"boundaries": [row]}, root=root)


def age_seconds(state: dict[str, Any], *, now: float | None = None) -> float:
    stamp = float(state.get("updated_at") or 0)
    if stamp <= 0:
        return float("inf")
    return max(0.0, float(now if now is not None else time.time()) - stamp)


def boundary_fingerprint(state: dict[str, Any]) -> str:
    """边界内容的指纹：`(text, message_id, at)` 三元组拼接后取 sha1 前 16 位。

    为什么 rev 之外还要指纹（#0002 审阅 P1-3）：rev 是我们自己维护的计数器，
    万一被旧文件的 schema 冲掉（`load()` 只认已知键）、或有人手工改了 json，
    rev 会撒谎；指纹直接算内容，兜住这种情形。
    """
    import hashlib

    parts = []
    for row in (state or {}).get("boundaries") or []:
        if not isinstance(row, dict):
            continue
        parts.append(f"{row.get('text')}|{row.get('message_id')}|{row.get('at')}")
    return "sha1:" + hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def is_stale(conv: str, *, hours: float = _STALE_HOURS, root: Path | None = None) -> bool:
    """太久没更新 → 下一次交互必须走一次轻量增量判断（否则语境会腐烂）。"""
    return age_seconds(load(conv, root=root)) > hours * 3600


def summary(conv: str, *, root: Path | None = None) -> str:
    """给 prompt / 决策用的一句话摘要。**边界永远排在前面**。"""
    state = load(conv, root=root)
    parts: list[str] = []
    for row in state.get("boundaries") or []:
        text = str(row.get("text") or "").strip()
        if text:
            parts.append(f"他说过「{text}」——这条优先于任何玩笑")
    topic = str(state.get("topic") or "").strip()
    if topic:
        parts.append(f"眼下在聊 {topic}")
    mode = str(state.get("mode") or "")
    if mode and mode != "不明确":
        parts.append(f"语气是{mode}")
    if not parts:
        return ""
    age = age_seconds(state)
    if age == float("inf"):
        return "；".join(parts)
    if age > _STALE_HOURS * 3600:
        return "；".join(parts) + "（这条判断已经过时，别当成现在的情况）"
    return "；".join(parts)


def report(conv: str, *, root: Path | None = None) -> dict[str, Any]:
    """控制台/指令看的状态。"""
    state = load(conv, root=root)
    return {
        "conv": state.get("conv"),
        "version": state.get("version"),
        "age_hours": round(age_seconds(state) / 3600, 2) if age_seconds(state) != float("inf") else None,
        "topic": state.get("topic"),
        "mode": state.get("mode"),
        "confidence": state.get("confidence"),
        "partners": state.get("partners"),
        "boundaries": len(state.get("boundaries") or []),
        "evidence": len(state.get("evidence") or []),
    }


# ---------------------------------------------------------------- 回复自带的标记
#
# 与 `<vec mood=-0.04>` 同一套思路：让模型在回复末尾附一行结构化结果，程序剥掉并在
# 下一轮使用。**它不能影响本轮检索**（回复已经在写了），所以只做「后置更新」。
#
# 为什么要正则而不是 JSON：属性式更短，模型照抄的准确率高；而且标记要能在**文本任意位置**
# 被找到并剥除，JSON 块一旦被拆行就容易剥不干净、漏到群里。
_CTX_RE = re.compile(r"<ctx\b([^>]*?)/?>", re.I | re.S)
_ATTR_RE = re.compile(r"(\w+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s/>]+))")


def extract_marker(text: str) -> tuple[str, dict[str, Any]]:
    """从回复里剥出 `<ctx .../>`，返回 `(干净文本, 增量)`。

    * 没有标记 → 原样返回，增量 `{}`；
    * 有多个 → 以**最后一个**为准（前面可能是在解释这个标记长什么样）；
    * 属性值非法 → 只丢那一个字段，不丢整份。
    """
    raw = str(text or "")
    matches = list(_CTX_RE.finditer(raw))
    if not matches:
        return raw, {}
    last = matches[-1]
    clean = (raw[:last.start()] + raw[last.end():]).strip()

    attrs: dict[str, str] = {}
    for key, v1, v2, v3 in _ATTR_RE.findall(last.group(1) or ""):
        attrs[key.lower()] = (v1 or v2 or v3 or "").strip()

    delta: dict[str, Any] = {}
    mode = attrs.get("mode", "")
    if mode in MODES and mode != "不明确":
        delta["mode"] = mode
    topic = attrs.get("topic", "")[:12]
    if topic:
        delta["topic"] = topic
    conf = attrs.get("conf") or attrs.get("confidence") or ""
    if conf in ("clear", "fuzzy", "unknown"):
        delta["confidence"] = conf
    return clean, delta


def record_llm(text: str, *, conv: str = "", message_id: Any = None,
               root: Path | None = None) -> tuple[str, dict[str, Any]]:
    """调用点用这个：剥离标记并把增量并进状态。**任何异常都不影响返回的文本**。"""
    clean, delta = extract_marker(text)
    if conv and delta:
        try:
            merge(conv,
                  {**delta, "last_turn": {"message_id": message_id, "source": "llm"}}
                  if message_id is not None else delta,
                  root=root)
        except Exception:  # noqa: BLE001 - 语境更新失败不能影响发消息
            logger.debug("语境记录失败 conv=%s", conv, exc_info=True)
    return clean, delta
