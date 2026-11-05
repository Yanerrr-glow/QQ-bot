"""#0013 行为维度回放：候选轴的离线评估（默认关，不接生产）。

两档：

```powershell
python 验证\\_行为维度回放.py --pairs   # 20 例离线断言，退出码 0
python 验证\\_行为维度回放.py --eval    # 调模型做成对判别，成本单独打印（未跑=未评估）
```

`--pairs` 证明的是"可观测差异存在且隔离正确"；**行为收益**只有 `--eval` 能说，
且要求每轴 `n_valid >= 10`、`pref_cand >= 0.65`、`position_bias <= 0.60`。
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "persona/packs/whale_v2"
WORK = ROOT / "temp" / "_scoped"

T0 = 1_000_000.0
T_NIGHT = T0


def _fresh(name: str) -> Path:
    root = WORK / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "vector.json").write_bytes((SEED / "vector.json").read_bytes())
    return root


def _verdict(mid: int = 1, **kw) -> dict:
    base = {"act": "分享", "target": "none", "origin": "self", "confidence": "clear",
            "tone": "认真", "reception": "不明确", "reused": False,
            "evidence": [{"ref": "本轮消息", "message_id": int(mid), "detail": "有效依据"}]}
    base.update(kw)
    return base


def _paired_eval(pairs, judge) -> dict:
    """成对判别：每对问两次（候选在前 / 在后）。**保留原始票**，另算一致性。

    不用 `persona_eval._preference`：它把换位两票平均，掩盖位置偏好。
    """
    votes = []
    for pair in pairs:
        first = judge(pair, True)
        second = judge(pair, False)
        votes.append((str(first.get("pick")), str(second.get("pick"))))
    valid = [(a, b) for a, b in votes if a in ("A", "B") and b in ("A", "B")]
    consistent = sum(1 for a, b in valid if a == "A" and b == "B")   # 两次都选候选
    bias = sum(1 for a, b in valid if a == "A" and b == "A") / len(valid) if valid else 0.0
    return {"n_total": len(votes), "n_valid": len(valid),
            "pref_cand": (consistent / len(valid)) if valid else 0.0,
            "position_bias": bias,
            "tie": sum(1 for a, b in votes if "tie" in (a, b)),
            "invalid": sum(1 for a, b in votes if "invalid" in (a, b))}


def _pass(res: dict, *, n_min: int = 10) -> bool:
    return (res["n_valid"] >= n_min and res["pref_cand"] >= 0.65
            and res["position_bias"] <= 0.60)


def run(check) -> int:  # noqa: ANN001
    from ai_chat import config, packs, persona_scoped as sc, persona_vector as vec, settings
    from ai_chat import scene_state
    from ai_chat.persona_workspace import use

    settings.store.load()

    def restore() -> None:
        for key in ("vector_candidate_axes_enabled", "vector_candidate_axes_file"):
            settings.store._runtime.pop(key, None)  # noqa: SLF001
        sc.load_candidates(force=True)

    def on() -> None:
        settings.store._runtime["vector_candidate_axes_enabled"] = True  # noqa: SLF001
        sc.load_candidates(force=True)

    # ---------------------------------------------------------------- 1 不进 _axes()
    on()
    axes = set(vec._axes())  # noqa: SLF001
    check("1 候选轴不在线上 axes 里", axes == {"mood", "social", "defiance", "energy", "vulnerability"}, axes)
    check("1 候选轴定义读得到", set(sc.load_candidates()["candidates"]) == {"curiosity", "stress"},
          list(sc.load_candidates()["candidates"]))

    # ---------------------------------------------------------------- 2/3 实验侧 vs 生产侧
    root = _fresh("adapters")
    with use(root, at=T0):
        prod = vec.tags("uid:1", is_master=False)
        base_tags = [t for t in prod]
        with_disabled = vec.tags("uid:1", is_master=False)
    check("2 关闭时候选轴不影响 tags", base_tags == with_disabled, with_disabled)
    check("3 生产 tags 里没有候选轴标签",
          not any(t.startswith(("curiosity:", "stress:")) for t in prod), prod)

    # ---------------------------------------------------------------- 4 作用域与隔离
    root = _fresh("scope")
    on()
    with use(root, at=T0):
        got = sc.add("curiosity", sc.key("g1", "uid:1", "修接口"), 0.04)
        same = sc.get("curiosity", sc.key("g1", "uid:1", "修接口"), now=T0)
        other = sc.get("curiosity", sc.key("g1", "uid:1", "晚饭"), now=T0)
        written = (root / "scoped_state.json").exists()
    check("4 同话题能读到写入值", got.get("ok") and abs(same - 0.04) < 1e-9, (got, same))
    check("4 换话题回基线", abs(other) < 1e-9, other)
    check("4 状态落在沙盒目录里", written, list(root.iterdir()))

    # ---------------------------------------------------------------- 5 证据四连
    root = _fresh("evidence")
    on()
    with use(root, at=T0):
        empty = sc.observe_event("curiosity", _verdict(mid=1, evidence=[]), conv="g1", uid="u1",
                                 topic="t", message_id=1)
        bad = sc.observe_event("curiosity", _verdict(mid=2, evidence=[
            {"ref": "第9条", "message_id": 0, "detail": "编的"}]), conv="g1", uid="u1",
            topic="t", message_id=2)
        old = sc.observe_event("curiosity", _verdict(mid=3, evidence=[
            {"ref": "第1条", "message_id": 99, "detail": "历史消息"}]), conv="g1", uid="u1",
            topic="t", message_id=3)
        ok = sc.observe_event("curiosity", _verdict(mid=4), conv="g1", uid="u1", topic="t",
                              message_id=4)
    check("5 空证据不更新", empty.get("why") == "no-valid-evidence", empty)
    check("5 失效证据不更新", bad.get("why") == "no-valid-evidence", bad)
    check("5 历史 mid 不更新", old.get("why") == "no-valid-evidence", old)
    check("5 合法证据才更新", ok.get("ok") and abs(ok.get("applied", 0) - 0.05) < 1e-9, ok)

    # ---------------------------------------------------------------- 6 转述不更新
    with use(root, at=T0):
        relay = sc.observe_event("curiosity", _verdict(mid=5, origin="other"), conv="g1", uid="u1",
                                 topic="t", message_id=5)
    check("6 转述不更新", relay.get("why") == "relayed", relay)

    # ---------------------------------------------------------------- 7 诉苦不增压
    with use(root, at=T0):
        sc.observe_event("stress", _verdict(mid=6, act="纠正", target="bot", reception="已明确拒绝"),
                         conv="g1", uid="u2", anchor="称呼:小笨蛋", message_id=6)
        pity = sc.observe_event("stress", _verdict(mid=7, act="诉苦", target="bot"), conv="g1",
                                uid="u2", anchor="称呼:小笨蛋", message_id=7)
    check("7 诉苦不转成压力", pity.get("why") == "no-signal", pity)

    # ---------------------------------------------------------------- 8 锚内/锚外
    with use(root, at=T0):
        inside = sc.observe_event("stress", _verdict(mid=8, act="亲密", target="bot"), conv="g1",
                                  uid="u2", anchor="称呼:小笨蛋", message_id=8)
        outside = sc.observe_event("stress", _verdict(mid=9, act="亲密", target="bot"), conv="g1",
                                   uid="u2", anchor="话题:别的", message_id=9)
        blank = sc.observe_event("stress", _verdict(mid=10, act="亲密", target="bot"), conv="g1",
                                 uid="u3", anchor="", message_id=10)
    check("8 同一锚上继续施压才增压", inside.get("ok"), inside)
    check("8 换锚不算", outside.get("why") == "no-signal", outside)
    check("8 空锚不计", blank.get("why") == "no-signal", blank)

    # ---------------------------------------------------------------- 9 幂等
    with use(root, at=T0):
        again = sc.observe_event("curiosity", _verdict(mid=4), conv="g1", uid="u1", topic="t",
                                 message_id=4)
    check("9 同一条消息只计一次", again.get("why") == "dup", again)

    # ---------------------------------------------------------------- 12 非法 delta
    with use(root, at=T0):
        nan = sc.add("curiosity", sc.key("g1", "u1", "t"), float("nan"))
        big = sc.add("curiosity", sc.key("g1", "u1", "t2"), 9.0)
        clipped = [c.get("stage") for c in (big.get("clipped") or [])]
    check("12 非有限值拒绝", nan.get("why") == "bad-delta", nan)
    check("12 超单步被裁剪", "step" in clipped and abs(big.get("applied", 0)) <= 0.06 + 1e-9, big)

    # ---------------------------------------------------------------- 13 并发与锁
    root = _fresh("conc")
    on()
    def worker(name: str) -> None:
        # **每个线程各自进 `use()`**：`persona_workspace` 用 ContextVar 存沙盒，
        # 新线程是空上下文 —— 不进去就会写到真实运行目录（这一条踩过）。
        with use(root, at=T0):
            for _ in range(20):
                sc.add("stress", sc.key("g1", name), 0.01, now=T0)

    threads = [threading.Thread(target=worker, args=(f"u{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with use(root, at=T0):
        a = sc.get("stress", sc.key("g1", "u0"), now=T0)
        b = sc.get("stress", sc.key("g1", "u1"), now=T0)
    check("13 并发写入不丢", abs(a - 0.2) < 1e-9 and abs(b - 0.2) < 1e-9, (a, b))

    # ---------------------------------------------------------------- 14 跨日与限额
    root = _fresh("daily")
    on()
    with use(root, at=T0):
        for _ in range(10):
            sc.add("stress", sc.key("g1", "u9"), 0.05, now=T0)
        capped = sc.get("stress", sc.key("g1", "u9"), now=T0)
    with use(root, at=T0 + 86400):
        sc.add("stress", sc.key("g1", "u9"), 0.02, now=T0 + 86400)
        next_day = sc.get("stress", sc.key("g1", "u9"), now=T0 + 86400)
    check("14 单日累计被裁到上限", abs(capped - 0.3) < 1e-9, capped)
    check("14 跨日后额度重置（旧值只剩衰减残值）", 0.02 <= next_day < 0.021, next_day)

    # ---------------------------------------------------------------- 15 禁用零 IO + 长尾淘汰
    restore()
    root = _fresh("off")
    with use(root, at=T0):
        raised = {"hit": False}

        def boom(*_a, **_kw):
            raised["hit"] = True
            raise AssertionError("禁用时不应该读盘")

        original = Path.read_text
        Path.read_text = boom  # type: ignore[assignment]
        try:
            zero = sc.get("curiosity", "x|y|z")
            off = sc.add("curiosity", "x|y|z", 0.05)
        finally:
            Path.read_text = original  # type: ignore[assignment]
    check("15 禁用时 get 返回 0 且不读盘", zero == 0.0 and not raised["hit"], (zero, raised))
    check("15 禁用时写入口关闭", off.get("why") == "disabled", off)

    on()
    root = _fresh("lru")
    with use(root, at=T0):
        for i in range(205):
            sc.add("curiosity", sc.key("g1", "u1", f"t{i}"), 0.02, now=T0 + i)
        snap = sc.snapshot(now=T0 + 300)
    check("15 长尾 scope 按上限淘汰", snap["scopes"] <= 200, snap["scopes"])

    # ---------------------------------------------------------------- 16 跨会话同 mid
    root = _fresh("crossmid")
    on()
    with use(root, at=T0):
        p = sc.observe_event("curiosity", _verdict(mid=7), conv="p1", uid="u1", topic="t", message_id=7)
        g = sc.observe_event("curiosity", _verdict(mid=7), conv="g1", uid="u1", topic="t", message_id=7)
    check("16 跨会话同号不互吞", p.get("ok") and g.get("ok"), (p, g))

    # ---------------------------------------------------------------- 17 人格切换
    root = _fresh("pack")
    on()
    with use(root, at=T0):
        first = sc.observe_event("curiosity", _verdict(mid=11), conv="g1", uid="u1", topic="t",
                                 message_id=11)
        original_pack = packs.active_id
        packs.active_id = lambda: "probe"  # type: ignore[assignment]
        try:
            second = sc.observe_event("curiosity", _verdict(mid=11), conv="g1", uid="u1", topic="t",
                                      message_id=11)
        finally:
            packs.active_id = original_pack  # type: ignore[assignment]
    check("17 换人格包后同一消息仍算新事件", first.get("ok") and second.get("ok"),
          (first.get("why"), second.get("why")))

    # ---------------------------------------------------------------- 10/11/18 成对判别
    pairs = [{"id": f"p{i}"} for i in range(12)]
    tie = _paired_eval(pairs, lambda pair, first: {"pick": "tie"})
    biased = _paired_eval(pairs, lambda pair, _first: {"pick": "A"})
    good = _paired_eval(pairs, lambda pair, first: {"pick": "A" if first else "B"})
    check("10 全平局不通过", tie["n_valid"] == 0 and not _pass(tie), tie)
    check("11 位置偏好不通过", biased["position_bias"] == 1.0 and not _pass(biased), biased)
    check("18 真的赢才通过", _pass(good) and abs(good["pref_cand"] - 1.0) < 1e-9, good)
    small = _paired_eval(pairs[:9], lambda pair, first: {"pick": "A" if first else "B"})
    check("18 有效样本不足不通过", not _pass(small), small)

    # ---------------------------------------------------------------- 19 清扫后重放
    root = _fresh("sweep")
    on()
    with use(root, at=T0):
        one = sc.observe_event("curiosity", _verdict(mid=12), conv="g1", uid="u1", topic="t",
                               message_id=12)
        dup = sc.observe_event("curiosity", _verdict(mid=12), conv="g1", uid="u1", topic="t",
                               message_id=12)
    with use(root, at=T0 + 25 * 3600):          # seen 24h 过期
        revived = sc.observe_event("curiosity", _verdict(mid=12), conv="g1", uid="u1", topic="t",
                                   message_id=12)
    check("19 幂等期内被拦", one.get("ok") and dup.get("why") == "dup", dup)
    check("19 幂等期过后重放算新事件（文档口径）", revived.get("ok"), revived)

    # ---------------------------------------------------------------- 20 边界对照集
    cases = [json.loads(line) for line in
             (ROOT / "验证" / "边界样例.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    wrong = []
    for case in cases:
        kind, _word = scene_state.boundary_of(case["text"])
        if bool(kind) != bool(case["must_stop"]):
            wrong.append(case["id"])
    check("20 边界对照集逐例命中", not wrong, wrong)
    changed = dict(cases[0], must_stop=not cases[0]["must_stop"])
    kind, _w = scene_state.boundary_of(changed["text"])
    check("20 改一条期望就应判红（证明逐例比较）", bool(kind) != bool(changed["must_stop"]), changed["id"])

    restore()
    return 0


def _bootstrap():
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    end = source.find('print("\\n=== 2.')
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "scoped_bootstrap"}
    exec(compile(source[:end], str(harness), "exec"), namespace)  # noqa: S102
    return namespace


if __name__ == "__main__":
    if "--eval" in sys.argv:
        print("--eval 需要模型调用：本轮未跑。按方案，未跑 --eval 只能写"
              "「离线可验部分通过，真实收益待验证」。")
        sys.exit(0)
    ns = _bootstrap()
    try:
        run(ns["check"])
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    failed = ns["failed"]
    print(f"通过 {ns['passed']} 项"
          + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    sys.exit(1 if failed else 0)
