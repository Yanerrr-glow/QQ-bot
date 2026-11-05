"""#0011 行为溯源：证据引用、原因行准入、两套读取 API、模式前后与重放一致。

跑法：`python 验证\\_行为溯源验证.py`（退出码 0 = 全部通过）。

隔离：每个用例 `with persona_workspace.use(root, at=T)` —— 状态、审计、锁文件都在临时目录。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "persona/packs/whale_v2"
WORK = ROOT / "temp" / "_trace"

T0 = 1_000_000.0                  # 白天
T_LONG = T0 + 30 * 24 * 3600      # 之后很久（让 TTL 用得上）


def _fresh(name: str) -> Path:
    root = WORK / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "vector.json").write_bytes((SEED / "vector.json").read_bytes())
    return root


def _write_state(root: Path, values: dict, *, at: float = T0) -> None:
    payload = {"version": 2, "at": at, "values": values, "extreme_since": {},
               "daily": {}, "heat": {"window": 600.0, "k": 12.0, "buckets": {}},
               "seen": {}, "audit_pending": []}
    (root / "vector_state.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_audit(root: Path, rows: list[dict]) -> None:
    path = root / "vector_audit.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def _read_audit(root: Path) -> list[dict]:
    path = root / "vector_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _event(kind: str, deltas: dict, *, act: str, target: str = "bot", confidence: str = "clear",
           mid: int = 100, evidence: list | None = None) -> dict:
    return {"kind": kind, "item": kind, "deltas": deltas, "message_id": mid,
            "verdict": {"act": act, "target": target, "origin": "self",
                        "confidence": confidence, "tone": "亲昵", "reception": "可能接受"},
            "evidence": evidence if evidence is not None else [
                {"ref": "本轮消息", "message_id": mid, "detail": "他说这句话时的依据"}]}


class _Fake:
    """假的 chat 调用：返回事先写好的 JSON。"""

    def __init__(self, payload: dict):
        self.payload = payload

    async def __call__(self, **kwargs):  # noqa: ANN003
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(self.payload)), finish_reason="stop")])


def run(check) -> int:  # noqa: ANN001
    from ai_chat import context_analysis as ca, persona_vector as vec, settings
    from ai_chat.persona_workspace import use

    settings.store.load()

    def restore() -> None:
        for key in ("vector_explain_enabled", "vector_explain_ttl_h"):
            settings.store._runtime.pop(key, None)  # noqa: SLF001

    def reason(root: Path, *, uid: str, conv: str, mid: int = 100, at: float = T0) -> str:
        with use(root, at=at):
            return vec.block(conv=conv, uid=uid, message_id=mid, private=True)

    # ---------------------------------------------------------------- 1 零增量不出原因
    root = _fresh("zero")
    _write_state(root, {"mood": 1.0})
    with use(root, at=T0):
        vec.apply_events([_event("praise", {"mood": 0.04}, act="夸赞")],
                         conv="p1", uid="uid:a", message_id=100)
    text = reason(root, uid="uid:a", conv="p1")
    check("1 被夹到零时不出原因行", "为什么是这样" not in text, text)

    # ---------------------------------------------------------------- 2 精度恒等式
    root = _fresh("precision")
    with use(root, at=T0):
        settings.store._runtime["vector_heat_k"] = 7.0  # noqa: SLF001
        for i in range(3):
            vec.apply_events([_event("praise", {"mood": 0.04}, act="夸赞", mid=200 + i)],
                             conv="p1", uid="uid:a", message_id=200 + i)
        restore()
    worst = 0.0
    for row in _read_audit(root):
        for eff in row.get("effect") or []:
            worst = max(worst, abs(float(eff["after"]) - float(eff["decayed_before"])
                                   - float(eff["applied"])))
    check("2 effect 恒等式（1e-12）", worst < 1e-12, f"{worst:.2e}")

    # ---------------------------------------------------------------- 3 自动追加带 mid
    turns = [{"role": "user", "text": "在吗", "mid": 11}]
    with use(root, at=T0):
        got = asyncio.run(ca.analyze(
            "在吗", turns=turns, current_mid=22,
            chat=_Fake({"act": "夸赞", "target": "bot", "origin": "self", "confidence": "clear",
                        "tone": "亲昵", "reception": "可能接受",
                        "evidence": [{"ref": "本轮消息", "detail": "同文本新消息"}]})))
    check("3 同文本不同 mid 仍追加，且证据指向本轮",
          got["evidence"] and got["evidence"][0]["message_id"] == 22, got.get("evidence"))

    # ---------------------------------------------------------------- 4 截断后的编号
    many = [{"role": "user", "text": f"第{i}句", "mid": i} for i in range(1, 10)]
    with use(root, at=T0):
        got = asyncio.run(ca.analyze(
            "第9句", turns=many, current_mid=100,
            chat=_Fake({"act": "分享", "target": "none", "origin": "self", "confidence": "clear",
                        "evidence": [{"ref": "第6条", "detail": "看到第6条"},
                                     {"ref": "第8条", "detail": "越界了"}]})))
    mids = [row["message_id"] for row in got["evidence"]]
    whys = [row.get("why") for row in got["evidence_unresolved"]]
    check("4 第6条解析到 seq[5]（mid=9）", mids == [9], mids)
    check("4 第8条进诊断而不是证据", whys == ["out-of-range"] and not any(m == 0 for m in mids), whys)

    # ---------------------------------------------------------------- 5 同文本不同 mid + 复用
    root5 = _fresh("reuse")
    _write_state(root5, {})
    payload = {"act": "夸赞", "target": "bot", "origin": "self", "confidence": "clear",
               "tone": "亲昵", "reception": "可能接受",
               "evidence": [{"ref": "本轮消息", "detail": "他这句像在夸"}]}
    with use(root5, at=T0):
        first = asyncio.run(ca.decide("小笨蛋", turns=[{"role": "user", "text": "小笨蛋", "mid": 5}],
                                      current_mid=6, chat=_Fake(payload), ttl=600, now=T0))
        state = {"last_analysis": first["write_back"]["last_analysis"]}
        second = asyncio.run(ca.decide("小笨蛋", turns=[{"role": "user", "text": "小笨蛋", "mid": 6}],
                                       current_mid=7, state=state, chat=_Fake(payload),
                                       ttl=600, now=T0 + 5))
    check("5 复用命中时剥掉旧证据",
          second["source"] == "reuse" and second["verdict"]["evidence"] == []
          and second["verdict"].get("evidence_dropped", 0) >= 1, second["verdict"])

    # ---------------------------------------------------------------- 6 纯衰减不归因
    root = _fresh("decay")
    _write_state(root, {"mood": 0.5})
    text = reason(root, uid="uid:a", conv="p1", at=T0 + 3600)
    with use(root, at=T0 + 3600):
        rows = vec.explain("uid:a", "p1")["rows"]
    check("6 没有事件时不出原因行", "为什么是这样" not in text, text)
    check("6 历史为空而不是报错", rows == [], rows)

    # ---------------------------------------------------------------- 7 先夸后骂
    root = _fresh("flip")
    _write_state(root, {"mood": -0.2})       # 基线 0.15：先低于基线，骂之后仍显著
    with use(root, at=T0):
        vec.apply_events([_event("praise", {"mood": 0.04, "social": 0.02}, act="夸赞", mid=300)],
                         conv="p1", uid="uid:a", message_id=300)
        vec.apply_events([_event("insult", {"mood": -0.05, "defiance": 0.04}, act="骂", mid=301)],
                         conv="p1", uid="uid:a", message_id=301)
    text = reason(root, uid="uid:a", conv="p1", mid=301)
    check("7 原因取最后那次（骂）", "为什么是这样" in text and "骂" in text, text)
    check("7 措辞不写轮次标签", "第1条" not in text and "第2条" not in text, text)

    # ---------------------------------------------------------------- 7b A 夸、B 骂（全局翻转）
    root = _fresh("cross")
    _write_state(root, {"mood": -0.2})
    with use(root, at=T0):
        vec.apply_events([_event("praise", {"mood": 0.04, "social": 0.02}, act="夸赞", mid=400)],
                         conv="p1", uid="uid:a", message_id=400)
        vec.apply_events([_event("insult", {"mood": -0.05, "defiance": 0.04}, act="骂", mid=401)],
                         conv="p2", uid="uid:b", message_id=401)
    text_b = reason(root, uid="uid:b", conv="p2", mid=401)
    text_a = reason(root, uid="uid:a", conv="p1", mid=402)
    check("7b B 的会话里原因取 B 那条", "为什么是这样" in text_b and "骂" in text_b, text_b)
    check("7b A 的会话里不出原因（被 B 推翻）", "为什么是这样" not in text_a, text_a)

    # ---------------------------------------------------------------- 7c reset 后不归因
    root = _fresh("reset")
    with use(root, at=T0):
        vec.apply_events([_event("praise", {"mood": 0.04}, act="夸赞", mid=500)],
                         conv="p1", uid="uid:a", message_id=500)
        vec.reset("")
        _write_state(root, {"mood": 0.6}, at=T0)      # 人工把值调高，没有事件支撑
    text = reason(root, uid="uid:a", conv="p1", mid=501)
    check("7c reset 之后不归因", "为什么是这样" not in text, text)

    # ---------------------------------------------------------------- 7d 尾部不完整
    root = _fresh("truncate")
    _write_state(root, {"mood": 0.5})
    filler = [{"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T0)), "source": "event",
               "conv": "g1", "uid": "uid:z", "applied": {}, "pad": "x" * 400} for _ in range(220)]
    _write_audit(root, filler + [{
        "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T0)),
        "source": "event", "conv": "p1", "uid": "uid:a", "applied": {"mood": 0.04},
        "event": {"event_id": "e:x", "message_id": 600, "phase": "pre", "kind": "praise",
                  "verdict": {"act": "夸赞"}, "evidence": [{"ref": "本轮消息", "message_id": 600,
                                                            "detail": "夸"}]},
        "effect": [{"axis": "mood", "applied": 0.04, "decayed_before": 0.3, "after": 0.34,
                    "nominal": 0.04, "heat": 1.0}]}])
    text = reason(root, uid="uid:a", conv="p1", mid=600)
    check("7d 尾部读数不完整时不归因", "为什么是这样" not in text, text[:80])

    # ---------------------------------------------------------------- 8 TTL 内衰减退出阈值
    root = _fresh("ttl")
    _write_state(root, {"mood": 0.4})        # 偏离基线 0.25：刚过阈值
    settings.store._runtime["vector_explain_ttl_h"] = 24.0  # noqa: SLF001
    with use(root, at=T0):
        vec.apply_events([_event("praise", {"mood": 0.04}, act="夸赞", mid=700)],
                         conv="p1", uid="uid:a", message_id=700)
        text_now = vec.block(conv="p1", uid="uid:a", message_id=700, private=True)
    with use(root, at=T0 + 6 * 3600):
        text_later = vec.block(conv="p1", uid="uid:a", message_id=701, private=True)
        rows_later = vec.explain("uid:a", "p1")["rows"]
    restore()
    check("8 刚发生时有原因行", "为什么是这样" in text_now, text_now[:80])
    check("8 衰减到阈值以下不出原因，但历史仍在",
          "为什么是这样" not in text_later and len(rows_later) >= 1,
          (text_later[:60], len(rows_later)))

    # ---------------------------------------------------------------- 9 失效引用
    root = _fresh("unresolved")
    with use(root, at=T0):
        got = asyncio.run(ca.analyze(
            "在吗", turns=[{"role": "user", "text": f"第{i}句", "mid": i} for i in range(1, 4)],
            current_mid=100,
            chat=_Fake({"act": "寒暄", "target": "none", "origin": "self", "confidence": "clear",
                        "evidence": [{"ref": "第9条", "detail": "不存在的引用"}]})))
    check("9 越界引用不进证据", got["evidence"] == [] and got["evidence_unresolved"],
          (got["evidence"], got["evidence_unresolved"]))
    check("9 只剩无效证据时 clear 降级", got["confidence"] == "fuzzy", got["confidence"])

    # ---------------------------------------------------------------- 10 权限三方
    root = _fresh("permission")
    with use(root, at=T0):
        vec.apply_events([_event("praise", {"mood": 0.04}, act="夸赞", mid=800)],
                         conv="p1", uid="uid:a", message_id=800)
        other_uid = vec.explain("uid:b", "p1")
        other_conv = vec.explain("uid:a", "g1")
        block_b = vec.block(conv="p1", uid="uid:b", message_id=800, private=True)
    check("10 换人看不到", other_uid["rows"] == [], other_uid["rows"])
    check("10 换会话看不到", other_conv["rows"] == [], other_conv["rows"])
    check("10 换人时注入也不带原因", "为什么是这样" not in block_b, block_b)

    # ---------------------------------------------------------------- 11 模式同对象
    root = _fresh("mode")
    _write_state(root, {"mood": 0.5, "energy": 0.3})
    with use(root, at=T0):
        vec.apply_events([_event("praise", {"mood": 0.04}, act="夸赞", mid=900)],
                         conv="p1", uid="uid:a", message_id=900, is_master=False)
        row = _read_audit(root)[-1]
    mode = row.get("mode") or {}
    check("11 审计里有模式前后", isinstance(mode.get("before"), list) and isinstance(mode.get("hit"), list), mode)
    check("11 模式带对象", (mode.get("scope") or {}).get("uid") == "uid:a", mode.get("scope"))

    # ---------------------------------------------------------------- 12 模式退出立即生效
    root = _fresh("mode_exit")
    _write_state(root, {"mood": 0.5, "energy": 0.3})
    with use(root, at=T0):
        hot = vec.block(conv="p1", uid="uid:a", message_id=1, private=True)
        hot_modes = vec.modes_hit(vec.state()["values"])
        _write_state(root, {"mood": 0.0, "energy": 0.0}, at=T0)
        cold = vec.block(conv="p1", uid="uid:a", message_id=2, private=True)
    line = (hot_modes[0]["do"][0] if hot_modes and hot_modes[0]["do"] else "")
    check("12 命中模式时注入做法", bool(hot_modes) and (line in hot if line else True), hot[:80])
    check("12 模式退出后下一次不再注入", not line or line not in cold, cold[:80])

    # ---------------------------------------------------------------- 13 重放逐字段一致
    def replay(name: str) -> dict:
        root = _fresh(name)
        _write_state(root, {"mood": 0.2})
        with use(root, at=T0):
            vec.apply_events([_event("praise", {"mood": 0.04, "social": 0.02}, act="夸赞", mid=1000)],
                             conv="p1", uid="uid:a", message_id=1000)
        return _read_audit(root)[-1]

    one, two = replay("replay1"), replay("replay2")
    check("13 重放新增行逐字段相等", one == two, (one.get("event_id"), two.get("event_id")))

    # ---------------------------------------------------------------- 14 旧审计兼容
    root = _fresh("legacy")
    _write_state(root, {"mood": 0.3})
    _write_audit(root, [{"at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T0)),
                         "source": "event", "conv": "p1", "uid": "uid:a",
                         "applied": {"mood": 0.02}}])
    with use(root, at=T0):
        rows = vec.recent(5)
        exp = vec.explain("uid:a", "p1")
    check("14 旧行不抛异常且标成未知", rows and rows[-1]["kind"] == "event", rows)
    check("14 explain 也能读旧行", exp["rows"] and exp["rows"][-1]["kind"] == "event", exp["rows"])

    # ---------------------------------------------------------------- 15 轮转后首事件
    root = _fresh("rotate")
    _write_state(root, {"mood": 0.5, "energy": 0.3})
    pad = [{"at": "2026-01-01 00:00:00", "source": "event", "conv": "g9", "uid": "uid:z",
            "applied": {}, "filler": "y" * 700} for _ in range(3200)]   # > 2 MiB
    _write_audit(root, pad)
    with use(root, at=T0):
        vec.apply_events([_event("praise", {"mood": 0.04}, act="夸赞", mid=1100)],
                         conv="p1", uid="uid:a", message_id=1100)
        rows = _read_audit(root)
    check("15 超过 2 MiB 时轮转成 .1",
          (root / "vector_audit.1.jsonl").exists(),
          [p.name for p in root.iterdir()])
    check("15 轮转后新文件只有首事件且带模式前后",
          len(rows) == 1 and isinstance((rows[0].get("mode") or {}).get("before"), list),
          rows[-1].get("mode") if rows else None)

    restore()
    return 0


def _bootstrap():
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    end = source.find('print("\\n=== 2.')
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "trace_bootstrap"}
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
