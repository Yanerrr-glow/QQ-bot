"""生产同源回放（#0015 M2）：用生产同一条装配链重跑一个目标轮。

## 它要回答的问题

「如果当时用现在的人格包（或某个候选），这条回复会变成什么？」——
不是「再问一次模型」，而是**输入逐字相同**，再看输出差在哪。

## 同源的三条硬约束

1. **依赖全部来自快照**：人格三层 + vector/examples/relations/vector_state/examples_usage
   走 `snapshot()`；摘要、场景、聊天记录、记忆与检索索引走 `config.runtime_root()`
   （沙盒内指向 `root()/_runtime`，见 #0015 P1-1）。缺哪项就标 `partial`。
2. **不写线上**：整个回放跑在 `persona_workspace.use()` 里，退出即恢复；
   trace 写 `config._data_dir()/eval/traces/`（不在沙盒内，也不在人格目录里）。
3. **不推进时间**：`at` 由案例给定，`persona_workspace.now()` 与 `today()` 都跟着它走。

## 两种模式

* `offline_stub`：模型调用全部走桩（默认**不生成**，只产出输入），零网络；
* `model_eval`：显式注入 `client` 才联网，且产物**只作诊断**，不进入候选通过判据。

## 首期的已知缺口

`behavior.suppress_note()` 读的是**进程内存** `_recent`（`behavior.py:193-195`），
不落盘 —— 回放里这一层是空的。所以 trace 会记 `behavior=off`，并按 `partial` 处理：
它可以用来看「示例/摘要/记忆变了会怎样」，不能拿来断言「候选更好」。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import (chatlog, config, context, context_analysis, persona,
               persona_examples, persona_revision_batch, persona_vector, settings)

logger = logging.getLogger("ai_chat.eval_replay")

# 回放要冻结的参数子集：只冻与「装配结果」有关的那些。
# 不冻 `eval_*`（那是回放自己的开关）与模型档案（模型名记进 trace 即可）。
_FROZEN_KEYS = (
    "summary_enabled", "examples_budget_chars", "vector_enabled",
    "scene_reuse_ttl", "scene_judge_timeout", "scene_judge_thinking", "scene_judge_max_tokens",
    "memory_recall_top", "memory_search_enabled",
)

_STUB_REPLY = "（offline_stub：不生成回复）"


def _home() -> Path:
    """trace 根目录。**不随 workspace 走**：它是回放的产物，不是人格状态。"""
    return Path(config._data_dir()) / "eval"


def _sha(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _now_text(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------- 采集
def capture(case: dict[str, Any], *, workdir: Path | None = None) -> dict[str, Any]:
    """把一个案例变成回放输入。

    产物是一个纯数据 dict —— 可以序列化、可以比对哈希，**不含任何活对象**。
    人格快照取「现在」的这一份；与案例记录的指纹不一致时记 `partial`：
    那说明回放用的不是当时那个人格，结果只能当诊断。
    """
    case_id = str(case.get("case_id") or "")
    conv = str(case.get("conv") or "eval")
    if isinstance(case.get("source_refs"), list) and case["source_refs"]:
        conv = str(case["source_refs"][0].get("conv") or conv)
    # 时间戳必须来自案例：拿 `time.time()` 兜底会让同一案例两次采集得到不同输入，
    # 「同源回放」就无从比对（这条是实测踩出来的）。
    at = case.get("at") or case.get("ts")
    if at is None and isinstance(case.get("source_refs"), list) and case["source_refs"]:
        first = case["source_refs"][0]
        at = first.get("ts") if isinstance(first, dict) else None
    if at is None:
        raise RuntimeError("案例缺时间戳（at / ts / source_refs[].ts）：回放必须有确定的时间")
    at = float(at)
    root = Path(workdir) if workdir else Path(config._data_dir()) / "eval" / "replay" / case_id
    root.mkdir(parents=True, exist_ok=True)
    shot = root / "persona"
    shot.mkdir(parents=True, exist_ok=True)

    before = persona_workspace_root()
    try:
        persona_revision_batch.snapshot(shot)
    except Exception as exc:  # noqa: BLE001 - 采集失败要显式说，不静默降级
        logger.exception("人格快照失败 case=%s", case_id)
        raise RuntimeError(f"人格快照失败：{exc}") from exc

    runtime = shot / "_runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    if before is not None:
        missing.append("persona:采集发生在另一个 workspace 内")

    # 会话记录：只写案例自带的那段，绝不复制线上 chatlog
    ctx = list(case.get("pre_context") or [])
    msgs: list[dict[str, Any]] = []
    for index, item in enumerate(ctx, start=1):
        msgs.append({
            "id": int(item.get("id") or index),
            "ts": float(item.get("ts") or at),
            "time": "",
            "uid": str(item.get("uid") or ""),
            "name": str(item.get("name") or ""),
            "text": str(item.get("text") or ""),
            "is_bot": bool(item.get("is_bot")),
            "read": True,
            "session": 1,
        })
    (runtime / f"chatlog_{conv}.json").write_text(
        json.dumps({"conv": conv, "messages": msgs, "session": 1,
                    "next_id": len(msgs) + 1}, ensure_ascii=False), encoding="utf-8")

    # 场景 / 摘要：案例带了就写，没带就记下缺口
    scene = case.get("scene_snapshot")
    if isinstance(scene, dict) and scene:
        scene_dir = runtime / "scene"
        scene_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in conv)
        (scene_dir / f"{safe or 'unknown'}.json").write_text(
            json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    else:
        missing.append("scene")

    summary = case.get("summary_snapshot")
    if summary:
        (runtime / "session_summaries.json").write_text(
            json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    else:
        missing.append("summary")

    for name in ("memory", "stickers", "msgindex"):
        missing.append(name)   # 首期不采集这三项，显式记缺

    frozen = {key: settings.get(key) for key in _FROZEN_KEYS}
    frozen = {k: v for k, v in frozen.items() if v is not None}

    fingerprint = ""
    try:
        from . import persona_eval

        fingerprint = str((persona_eval.version_info() or {}).get("pack") or "")
    except Exception:  # noqa: BLE001
        pass
    if str(case.get("persona_fingerprint") or "") not in ("", "unknown", fingerprint):
        missing.append("persona:指纹与当时不一致")

    question = ""
    for item in reversed(ctx):
        if not item.get("is_bot"):
            question = str(item.get("text") or "")
            break

    inputs = {
        "case_id": case_id,
        "conv": conv,
        "at": at,
        "at_text": _now_text(at),
        "is_master": bool(case.get("is_master")),
        "uid": str(case.get("uid") or ""),
        "speaker": "你" if case.get("is_master") else "群友",
        "trigger": str(case.get("trigger") or ("addressed" if case.get("is_master") else "random")),
        "question": question,
        "current_id": int(msgs[-1]["id"]) if msgs else None,
        "message_id": int(msgs[-1]["id"]) if msgs else None,
        "private": bool(case.get("scope") == "private"),
        "persona_root": str(shot),
        "runtime_root": str(runtime),
        "settings": frozen,
        "persona_fingerprint": fingerprint,
        "missing": sorted(set(missing)),
    }
    inputs["coverage"] = "partial" if inputs["missing"] else "full"
    inputs["inputs_hash"] = _sha({k: inputs[k] for k in
                                 ("conv", "at", "question", "trigger", "settings",
                                  "missing", "persona_fingerprint")})
    return inputs


def persona_workspace_root() -> Path | None:
    from .persona_workspace import root

    return root()


# --------------------------------------------------------------------- 回放
async def replay(inputs: dict[str, Any], *, mode: str = "offline_stub",
                 client: Any = None, save: bool = True) -> dict[str, Any]:
    """在隔离环境里重跑一次装配。**不进入 workspace 就直接报错**（禁止回退线上）。"""
    from .persona_workspace import use

    root = Path(str(inputs.get("persona_root") or ""))
    if not root.is_dir():
        raise RuntimeError("回放缺少人格快照：先 capture()")
    if persona_workspace_root() is not None:
        raise RuntimeError("已在别的 workspace 内：回放不能嵌套")

    started = time.time()
    trace: dict[str, Any] = {
        "case_id": inputs.get("case_id"),
        "mode": mode,
        "at": inputs.get("at_text"),
        "coverage": inputs.get("coverage"),
        "missing": list(inputs.get("missing") or []),
        "inputs_hash": inputs.get("inputs_hash"),
        "behavior": "off",          # 首期已知缺口，见模块头
        "settings": dict(inputs.get("settings") or {}),
    }
    try:
        trace = await _run_in_sandbox(inputs, mode=mode, client=client, save=save, trace=trace)
    finally:
        _close_runtime()
    trace["elapsed_ms"] = int((time.time() - started) * 1000)
    trace["trace_hash"] = _sha({k: trace.get(k) for k in ("system_sha256", "user_sha256", "examples")})
    if save:
        _save_trace(trace)
    return trace


async def _run_in_sandbox(inputs: dict[str, Any], *, mode: str, client: Any,
                          save: bool, trace: dict[str, Any]) -> dict[str, Any]:
    from .persona_workspace import use

    root = Path(str(inputs.get("persona_root") or ""))
    with use(root, at=float(inputs["at"])), settings.override(inputs.get("settings") or {}):
        _remember_modules()
        log = chatlog.ConversationLog(str(inputs["conv"]), chatlog._path_for(str(inputs["conv"])))
        log.load()

        # 1) 语境判定：offline_stub 用案例给的固定结论，绝不调模型
        if mode == "offline_stub":
            verdict = dict(inputs.get("verdict") or {})
            source = "stub"
        else:
            if client is None:
                raise RuntimeError("model_eval 必须显式注入 client")
            got = await context_analysis.decide(
                str(inputs["question"]), turns=[], boundary="", mode="",
                uid=str(inputs["uid"]), state={}, chat=client, now=float(inputs["at"]),
                ttl=0.0, current_mid=inputs.get("message_id"))
            verdict = dict(got.get("verdict") or {})
            source = str(got.get("source") or "analysis")
        trace["verdict"] = verdict
        trace["verdict_source"] = source

        # 2) 装配：与生产同一条 `context.build`
        record: dict[str, Any] = {}
        messages, user_text = context.build(
            conv=str(inputs["conv"]), log=log, speaker=str(inputs["speaker"]),
            question=str(inputs["question"]), current_id=inputs.get("current_id"),
            trigger=str(inputs["trigger"]),
            is_master=bool(inputs["is_master"]), uid=str(inputs["uid"]),
            bot_uid="", scene_hint=str(verdict.get("scene") or ""),
            state={}, message_id=inputs.get("message_id"),
            private=bool(inputs["private"]), examples_record=record,
        )
        system_text = messages[0]["content"] if messages else ""
        trace["system"] = system_text
        trace["user"] = user_text
        trace["system_sha256"] = _sha(system_text)
        trace["user_sha256"] = _sha(user_text)
        trace["examples"] = {
            "used": list(record.get("used") or []),
            "dropped": list(record.get("dropped") or []),
            "picked": record.get("picked", 0),
            "budget": record.get("budget"),
        }

        # 3) 生成：只有显式注入 client 才调
        if mode == "offline_stub":
            trace["reply"] = _STUB_REPLY
            trace["model"] = "stub"
        else:
            resp = await client(messages=messages, max_tokens=600, temperature=1.0)
            reply = str((resp.choices[0].message.content or "")) if getattr(resp, "choices", None) else ""
            trace["reply"] = reply
            trace["model"] = str(getattr(resp, "model", "") or "")

    return trace


def _save_trace(trace: dict[str, Any]) -> Path:
    directory = _home() / "traces" / str(trace.get("case_id") or "unknown")
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{trace.get('mode')}-{trace.get('trace_hash')}.json"
    path = directory / name
    try:
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        logger.warning("trace 写盘失败：%s", exc)
    return path


# --------------------------------------------------------------------- 对照
def compare(before: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """两份 trace 的差异。**只比输入与注入项**，不比回复质量（那要裁判）。"""
    def _deps(trace: dict[str, Any]) -> dict[str, Any]:
        return {"system": trace.get("system_sha256"), "user": trace.get("user_sha256"),
                "examples": (trace.get("examples") or {}).get("used")}

    same_inputs = _deps(before) == _deps(candidate)
    a = set((before.get("examples") or {}).get("used") or [])
    b = set((candidate.get("examples") or {}).get("used") or [])
    return {
        "same_inputs": same_inputs,
        "system_changed": before.get("system_sha256") != candidate.get("system_sha256"),
        "user_changed": before.get("user_sha256") != candidate.get("user_sha256"),
        "examples_added": sorted(b - a),
        "examples_removed": sorted(a - b),
        "coverage": {"before": before.get("coverage"), "candidate": candidate.get("coverage")},
    }


# --------------------------------------------------------------------- 概况
def _close_runtime() -> None:
    """关掉回放期间打开的 sqlite 句柄。

    为什么必须在回放结束时做：`memory` / `msgindex` 在沙盒里各自开过
    `_runtime/memory.db`，Windows 上句柄不释放，临时目录就删不掉
    （实测：`persona-regression-*` 清理时抛 `PermissionError: WinError 32`）。
    失败也不能影响回放结论，所以全部吞掉并只记 debug。

    `memory.close` 是**实例方法**（`_db.close()`），`msgindex.close` 是模块级函数 ——
    两者取法不同，写成同一个 `getattr(module, "close")` 会漏掉前一个（实测漏过）。
    """
    for name in ("memory", "msgindex"):
        module = _MODULES.get(name)
        if module is None:
            continue
        targets = [module]
        if name == "memory":
            targets.append(getattr(module, "_db", None))
        for target in targets:
            closer = getattr(target, "close", None) if target is not None else None
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    logger.debug("%s.close() 失败", name, exc_info=True)


_MODULES: dict[str, Any] = {}


def _remember_modules() -> None:
    """惰性登记需要收尾的模块（避免顶层 import 拉重链路）。"""
    if _MODULES:
        return
    from . import memory, msgindex

    _MODULES.update({"memory": memory, "msgindex": msgindex})


def stats(limit: int = 20) -> dict[str, Any]:
    """列出已有 trace（按 case 分组），给控制台/报告用。"""
    base = _home() / "traces"
    items: list[dict[str, Any]] = []
    if base.exists():
        for directory in sorted(base.iterdir())[-limit:]:
            if not directory.is_dir():
                continue
            files = sorted(directory.glob("*.json"))
            items.append({"case_id": directory.name, "traces": len(files),
                          "latest": files[-1].name if files else ""})
    return {"root": str(base), "cases": items, "count": len(items)}


def load_trace(case_id: str, name: str) -> dict[str, Any] | None:
    path = _home() / "traces" / str(case_id) / str(name)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
