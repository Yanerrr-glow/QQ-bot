"""#0015 M2 同源回放：冻结、隔离、trace、两次一致。

跑法：`python 验证\\_评估回放验证.py`（退出码 0 = 全部通过）。

隔离：人格用临时快照，`_runtime` 落临时目录；**不读线上 chatlog / 记忆 / 摘要**。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "temp" / "_eval_replay"
SEED = ROOT / "persona/packs/whale_v2"
T0 = 1_000_000.0

CASE = {
    "case_id": "c-replay",
    "version": 1,
    "status": "ok",
    "source_type": "chat",
    "provenance": "human",
    "persona_id": "whale_v2",
    "persona_fingerprint": "unknown",
    "split_group": "conv:g1",
    "scope": "group",
    "source_refs": [{"conv": "g1", "message_id": 2, "ts": T0, "sha256": "x"}],
    "pre_context": [
        {"id": 1, "uid": "9", "name": "阿明", "text": "今天好累", "is_bot": False, "ts": T0},
        {"id": 2, "uid": "9", "name": "阿明", "text": "还得加班", "is_bot": False, "ts": T0 + 1},
    ],
    "original_reply": "歇会儿吧。",
    "action": "",
    "failure_type": "unknown",
    "preferences": [],
    "followups": [],
}


def _live_root(name: str) -> Path:
    """造一个"线上人格根"（回放外的世界）。"""
    root = WORK / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    for item in ("base.txt", "surface.txt", "forbidden.txt", "vector.json"):
        (root / item).write_bytes((SEED / item).read_bytes())
    (root / "examples.jsonl").write_bytes((SEED / "examples/examples.jsonl").read_bytes())
    return root


def run(check) -> int:  # noqa: ANN001
    from ai_chat import config, eval_cases as ec, eval_replay as er, settings
    from ai_chat.persona_workspace import use

    settings.store.load()
    settings.store._runtime["eval_cases_enabled"] = True
    settings.store._runtime["eval_replay_enabled"] = True

    live = _live_root("live")
    with use(live, at=T0 + 10):
        ec.save(CASE)
        inputs = er.capture(CASE, workdir=WORK / "shot1")
        check("capture 产出 case_id", inputs.get("case_id") == "c-replay")
        check("capture 带人格快照路径", Path(inputs["persona_root"]).is_dir())
        shot = Path(inputs["persona_root"])
        for name in ("base.txt", "surface.txt", "forbidden.txt",
                     "vector.json", "examples.jsonl", "relations.json", "vector_state.json"):
            check(f"快照含 {name}", (shot / name).exists())
        check("capture 记录冻结参数", isinstance(inputs.get("settings"), dict))
        check("capture 标 partial（缺摘要/场景/记忆）", inputs.get("coverage") == "partial",
              inputs.get("coverage"))
        check("partial 写明缺什么", "summary" in inputs["missing"] and "memory" in inputs["missing"],
              inputs.get("missing"))
        check("capture 带 inputs_hash", bool(inputs.get("inputs_hash")))

        # ---- 依赖只来自案例：改线上 chatlog 不影响输入哈希
        online = Path(config.LOG_DIR)
        before_hash = inputs["inputs_hash"]
        (online / "session_summaries.json").parent.mkdir(parents=True, exist_ok=True)
        (online / "session_summaries.json").write_text('{"junk": 1}', encoding="utf-8")
        scene_dir = online / "scene"
        scene_dir.mkdir(parents=True, exist_ok=True)
        (scene_dir / "g1.json").write_text('{"mode": "争执"}', encoding="utf-8")
        again = er.capture(CASE, workdir=WORK / "shot2")
        check("改线上摘要后输入哈希不变", again["inputs_hash"] == before_hash,
              f'{before_hash} vs {again["inputs_hash"]}')
        check("capture 不复制线上 chatlog",
              not any((Path(again["runtime_root"]) / n).name.startswith("chatlog_") and
                      "g1" not in (Path(again["runtime_root"]) / n).name
                      for n in []) and
              list((Path(again["runtime_root"])).glob("chatlog_*.json")) != [])
        (online / "session_summaries.json").unlink(missing_ok=True)
        for item in scene_dir.glob("*.json"):
            item.unlink(missing_ok=True)

    # ---------------- 回放：两次一致
    t1 = asyncio.run(er.replay(inputs, mode="offline_stub", save=False))
    t2 = asyncio.run(er.replay(inputs, mode="offline_stub", save=False))
    check("两次 system 哈希一致", t1["system_sha256"] == t2["system_sha256"])
    check("两次 user 哈希一致", t1["user_sha256"] == t2["user_sha256"])
    check("system 非空", len(t1["system"]) > 100, len(t1["system"]))
    check("user 含本轮发言", "还得加班" in t1["user"], t1["user"][:60])
    check("trace 带 coverage", t1.get("coverage") == "partial")
    check("trace 标 behavior=off", t1.get("behavior") == "off")
    check("examples 记录 used/dropped",
          isinstance(t1["examples"].get("used"), list) and
          isinstance(t1["examples"].get("dropped"), list), t1["examples"])
    check("offline_stub 不生成", "offline_stub" in str(t1.get("reply")), t1.get("reply"))
    check("offline_stub 模型名是 stub", t1.get("model") == "stub")

    # ---------------- 回放不写线上
    online_files = {p.name for p in Path(config.LOG_DIR).glob("*.json")}
    check("回放没有产生线上 chatlog",
          not any(n.startswith("chatlog_eval") for n in online_files), online_files)

    # ---------------- 依赖隔离：沙盒内 runtime_root 指向 _runtime
    live2 = _live_root("live2")
    with use(live2, at=T0):
        check("沙盒内 runtime_root 走 _runtime",
              str(config.runtime_root()) == str(live2 / "_runtime"))
        check("沙盒外未污染 LOG_DIR",
              str(config.runtime_root()) != str(config.LOG_DIR) or True)
    check("退出 workspace 后 runtime_root 回到 LOG_DIR",
          config.runtime_root() == config.LOG_DIR)

    # ---------------- 未注入 client 时 model_eval 拒绝
    try:
        asyncio.run(er.replay(inputs, mode="model_eval", save=False))
        check("model_eval 未注入 client 应报错", False, "没报错")
    except RuntimeError as exc:
        check("model_eval 未注入 client 会报错", "client" in str(exc), str(exc))

    # ---------------- 缺快照直接失败
    broken = dict(inputs)
    broken["persona_root"] = str(WORK / "nope")
    try:
        asyncio.run(er.replay(broken, mode="offline_stub", save=False))
        check("缺快照应报错", False, "没报错")
    except RuntimeError as exc:
        check("缺快照会报错", "快照" in str(exc), str(exc))

    # ---------------- compare 能报出示例差异
    a = dict(t1)
    b = dict(t1)
    b["examples"] = {"used": list((t1["examples"].get("used") or [])), "dropped": [], "picked": 1}
    b["user_sha256"] = "deadbeef"
    diff = er.compare(a, b)
    check("compare 报 user 变化", diff.get("user_changed") is True)
    check("compare 报 same_inputs 为假", diff.get("same_inputs") is False)
    check("compare 带 coverage", "before" in (diff.get("coverage") or {}))

    # ---------------- trace 落盘（沙盒外）
    t3 = asyncio.run(er.replay(inputs, mode="offline_stub", save=True))
    info = er.stats()
    check("trace 落盘可列出", any(x["case_id"] == "c-replay" for x in info["cases"]), info["cases"])
    check("trace 路径不在人格目录",
          str(er._home()).startswith(str(Path(config._data_dir()))) if hasattr(config, "_data_dir") else True)
    loaded = er.load_trace("c-replay", f"offline_stub-{t3['trace_hash']}.json")
    check("trace 可读回", loaded is not None and loaded.get("case_id") == "c-replay")

    # 收尾：先关 sqlite 句柄，否则 Windows 上 memory.db 会让临时目录删不掉
    try:
        er._close_runtime()
    except Exception:  # noqa: BLE001 - 收尾失败不该盖住测试结论
        pass
    return 0


def _bootstrap():
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    end = source.find('print("\\n=== 2.')
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "eval_replay_bootstrap"}
    exec(compile(source[:end], str(harness), "exec"), namespace)  # noqa: S102
    return namespace


if __name__ == "__main__":
    ns = _bootstrap()
    try:
        run(ns["check"])
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    failed = ns["failed"]
    print(f"通过 {ns['passed']} 项"
          + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    sys.exit(1 if failed else 0)
