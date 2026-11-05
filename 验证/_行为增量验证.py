"""#0010 行为状态增量：事件批次、去重、降温、锁与审计恢复的离线验证。

跑法：`python 验证\\_行为增量验证.py`（退出码 0 = 22 例全过）。

隔离口径：每个用例都在 `persona_workspace.use(root, at=T)` 里跑 ——
`persona_data_dir()` 优先返回 `workspace.root()`，所以状态、审计、锁文件全落临时目录，
不会碰真实 `data/runtime/persona/<包>/vector_state.json`。
"""

from __future__ import annotations

import contextlib
import json
import math
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "persona/packs/whale_v2"
WORK = ROOT / "temp" / "_vector_events"

T0 = 1_000_000.0                       # 冻结时钟（白天）
T_NIGHT = time.mktime((2026, 11, 5, 3, 30, 0, 0, 0, -1))   # 本地时间 03:30


def _fresh(name: str = "run") -> Path:
    root = WORK / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "vector.json").write_bytes((SEED / "vector.json").read_bytes())
    return root


def _patch_axes(root: Path, **updates) -> None:
    """改临时目录里那份 vector.json 的轴参数（不碰包里那份）。"""
    data = json.loads((root / "vector.json").read_text(encoding="utf-8"))
    for axis in data.get("axes") or []:
        axis_id = str(axis.get("id"))
        if axis_id in updates:
            axis.update(updates[axis_id])
    (root / "vector.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _write_state(root: Path, values: dict, *, at: float, travel: dict | None = None) -> None:
    daily = {"date": "", "delta": {}, "travel": {}}
    if travel:
        daily = {"date": time.strftime("%Y-%m-%d", time.localtime(at)),
                 "delta": dict(travel), "travel": dict(travel)}
    (root / "vector_state.json").write_text(json.dumps(
        {"version": 2, "at": at, "values": values, "extreme_since": {}, "daily": daily,
         "heat": {"window": 0.0, "k": 0.0, "buckets": {}}, "seen": {}, "audit_pending": []},
        ensure_ascii=False), encoding="utf-8")


def _audit_rows(root: Path) -> list[dict]:
    path = root / "vector_audit.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _state(root: Path) -> dict:
    path = root / "vector_state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def run(check) -> int:  # noqa: ANN001
    from ai_chat import filelock, persona_vector as vec, settings
    from ai_chat.persona_workspace import use

    settings.store.load()

    def override(**pairs):
        """内存覆盖（不落盘）。**每次现取 `_runtime`**：`load()` 会重新绑定这个字典，
        缓存下来的引用会写进一份已经没人读的旧字典。"""
        settings.store._runtime.update(pairs)  # noqa: SLF001

    def restore():
        for key in ("vector_max_step", "vector_daily_limit", "vector_heat_enabled",
                    "vector_heat_window", "vector_heat_k"):
            settings.store._runtime.pop(key, None)  # noqa: SLF001

    def praise(delta: float = 0.04) -> dict:
        return vec.build_event({"mood": delta, "social": delta / 2}, kind="praise", item="praise")

    # ---------------------------------------------------------------- 1 幂等
    restore()
    root = _fresh("idem")
    with use(root, at=T0):
        ev = praise()
        first = vec.apply_events([ev], conv="g1", uid="u1", message_id=101, phase="pre")
        snap = (root / "vector_state.json").read_bytes()
        second = vec.apply_events([ev], conv="g1", uid="u1", message_id=101, phase="pre")
        same = (root / "vector_state.json").read_bytes() == snap
    check("1 同一消息同一事件重放：第二次去重", second.get("dedup") is True, second.get("why"))
    check("1 去重时不写盘", same)
    check("1 去重不涨热计数", first["heat"]["n"] == 0 and second["results"][0]["dedup"])

    # ---------------------------------------------------------------- 2 精度恒等式
    root = _fresh("prec")
    with use(root, at=T0):
        override(vector_heat_k=7.0)
        for i in range(4):
            vec.apply_events([praise()], conv="g1", uid="u1", message_id=300 + i, phase="pre")
        rows = _audit_rows(root)
        worst = 0.0
        for row in rows:
            for eff in row.get("effect") or []:
                worst = max(worst, abs(float(eff["after"]) - float(eff["decayed_before"])
                                       - float(eff["applied"])))
        restore()
    check("2 after-decayed_before==applied（1e-12）", worst < 1e-12, f"worst={worst:.3e}")
    check("2 effect 未被 4 位舍入", any(abs((e.get("applied") or 0) % 0.01) > 1e-9
                                    for row in rows for e in (row.get("effect") or [])), "循环小数存在")

    # ---------------------------------------------------------------- 3 降温公式
    root = _fresh("heat")
    with use(root, at=T0):
        factors = []
        for i in range(13):
            got = vec.apply_events([praise()], conv="g1", uid="u1", message_id=400 + i, phase="pre")
            factors.append(got["heat"]["factor"])
    check("3 第 13 次降温因子 = 1/(1+12/12) = 0.5", abs(factors[12] - 0.5) < 1e-9, factors[12])
    check("3 前 12 次因子单调不增", all(factors[i] >= factors[i + 1] - 1e-12 for i in range(12)))

    # ---------------------------------------------------------------- 4 连续 vs 分散
    restore()
    override(vector_max_step=0.02, vector_daily_limit=1.0)
    root = _fresh("spread")
    _patch_axes(root, mood={"max_step": 0.02, "max_daily": 1.0})
    with use(root, at=T0):
        dense = 0.0
        for i in range(30):
            dense += vec.apply_events([praise(0.02)], conv="g1", uid="u1",
                                      message_id=500 + i, phase="pre").get("applied", {}).get("mood", 0)
    root2 = _fresh("spread_far")
    _patch_axes(root2, mood={"max_step": 0.02, "max_daily": 1.0})
    far = 0.0
    for i in range(30):
        with use(root2, at=T0 + i * 601):
            far += vec.apply_events([praise(0.02)], conv="g1", uid="u1",
                                    message_id=600 + i, phase="pre").get("applied", {}).get("mood", 0)
    restore()
    check("4 窗内连发的累计增量更小", dense < far - 1e-6, f"dense={dense:.4f} far={far:.4f}")
    check("4 两组都不超日限额", max(dense, far) <= 1.0 + 1e-9, f"{dense:.4f}/{far:.4f}")

    # ---------------------------------------------------------------- 5 分桶隔离
    root = _fresh("bucket")
    with use(root, at=T0):
        for i in range(12):
            vec.apply_events([praise()], conv="g1", uid="a", message_id=700 + i, phase="pre")
            vec.apply_events([praise()], conv="g1", uid="b", message_id=800 + i, phase="pre")
        state = _state(root)
        counts = {k: len(v) for k, v in state["heat"]["buckets"].items()}
        probe = vec.apply_events([praise()], conv="g1", uid="a", message_id=999, phase="pre")
        from unittest.mock import patch
        with patch.object(vec, "_pack_id", return_value="probe"):
            other = vec.apply_events([praise()], conv="g1", uid="a", message_id=1000, phase="pre")
    a_bucket = [c for k, c in counts.items() if k.endswith(":a:praise")]
    b_bucket = [c for k, c in counts.items() if k.endswith(":b:praise")]
    check("5 两个 uid 各自 12 条", a_bucket == [12] and b_bucket == [12], counts)
    check("5 同 uid 第 13 次热度为 12", probe["heat"]["n"] == 12, probe["heat"]["n"])
    check("5 换人格包后热簿不串", other["heat"]["n"] == 0 and other["heat"]["bucket"].startswith("probe:"),
          other["heat"])

    # ---------------------------------------------------------------- 6a 辱骂不降温
    root = _fresh("insult")
    with use(root, at=T0):
        factors = []
        for i in range(13):
            ev = vec.build_event({"mood": -0.05, "defiance": 0.04}, kind="insult", item="insult")
            got = vec.apply_events([ev], conv="g1", uid="u1", message_id=1100 + i, phase="pre")
            factors.append(got["heat"]["factor"])
    check("6a 辱骂 13 次从不降温", all(abs(f - 1.0) < 1e-12 for f in factors), factors[-1])

    # ---------------------------------------------------------------- 6b 无证据不更新
    root = _fresh("fuzzy")
    with use(root, at=T0):
        before = _state(root).get("values", {})
        out = vec.observe("小笨蛋", conv="g1", uid="u1",
                          verdict={"act": "夸赞", "target": "bot", "confidence": "fuzzy"},
                          message_id=1200)
        after = _state(root).get("values", {})
    check("6b fuzzy 判定不产生增量", out == {} and before == after, (out, after))
    check("6b fuzzy 也不写审计", _audit_rows(root) == [])

    # ---------------------------------------------------------------- 6c 夹紧到零不计热
    root = _fresh("clamp")
    _write_state(root, {"mood": 1.0, "social": 1.0}, at=T0)
    with use(root, at=T0):
        got = vec.apply_events([praise()], conv="g1", uid="u1", message_id=1300, phase="pre")
        state = _state(root)
    check("6c 已在上界时 applied 为 0", got.get("applied", {}).get("mood", 0) == 0, got.get("applied"))
    check("6c 被夹到零不涨热计数", sum(len(v) for v in state["heat"]["buckets"].values()) == 0,
          state["heat"]["buckets"])

    # ---------------------------------------------------------------- 7a 深夜+夸赞
    root = _fresh("night")
    with use(root, at=T_NIGHT):
        out = vec.observe("你好可爱", conv="g1", uid="u1",
                          verdict={"act": "夸赞", "target": "bot", "confidence": "clear",
                                   "tone": "亲昵"},
                          message_id=1400)
        state = _state(root)
    check("7a 深夜+夸赞：mood 与 social 生效",
          state["values"].get("mood", 0) > 0 and state["values"].get("social", 0) > 0,
          state["values"])
    check("7a energy 被轴选择丢弃（停在基线）",
          abs(state["values"].get("energy", 0.0)) < 1e-9 and out.get("energy") == -0.02,
          (state["values"].get("energy"), out.get("energy")))

    # ---------------------------------------------------------------- 7b 拆调用不被吞
    root = _fresh("split")
    with use(root, at=T0):
        vec.apply_events([praise()], conv="g1", uid="u1", message_id=1500, phase="pre")
        second = vec.apply_events([vec.build_event({"energy": -0.02}, kind="night", item="night")],
                                  conv="g1", uid="u1", message_id=1500, phase="pre")
        state = _state(root)
    check("7b 同 mid 的第二个事件不被吞", second["dedup"] is False and "energy" in state["values"],
          (second["dedup"], state["values"]))
    check("7b 幂等表两条", len(state["seen"]) == 2, list(state["seen"]))

    # ---------------------------------------------------------------- 7c 两项奖励
    root = _fresh("reward")
    with use(root, at=T0):
        a = vec.apply_events([vec.build_event({"mood": 0.03}, kind="intent_reward",
                                              item="reward:i:aaa")],
                             conv="g1", uid="u1", message_id=1600, phase="pre")
        b = vec.apply_events([vec.build_event({"mood": 0.03}, kind="intent_reward",
                                              item="reward:i:bbb")],
                             conv="g1", uid="u1", message_id=1600, phase="pre")
        again = vec.apply_events([vec.build_event({"mood": 0.03}, kind="intent_reward",
                                                  item="reward:i:aaa")],
                                 conv="g1", uid="u1", message_id=1600, phase="pre")
    check("7c 两个事项各自记账", a["ok"] and b["ok"] and not b["dedup"], (a["ok"], b["ok"]))
    check("7c 同一事项重试被去重", again["dedup"] is True, again["why"])

    # ---------------------------------------------------------------- 8 开关
    root = _fresh("off")
    with use(root, at=T0):
        override(vector_heat_enabled=False)
        off = [vec.apply_events([praise()], conv="g1", uid="u1",
                                message_id=1700 + i, phase="pre") for i in range(5)]
        restore()
    root_on = _fresh("on")
    with use(root_on, at=T0):
        on = [vec.apply_events([praise()], conv="g1", uid="u1",
                               message_id=1700 + i, phase="pre") for i in range(5)]
    check("8 关闭降温后因子恒为 1", all(abs(r["heat"]["factor"] - 1.0) < 1e-12 for r in off))
    check("8 关闭后每次都拿全额（不受日限额影响，5×0.04 < 0.5）",
          all(abs(r["applied"].get("mood", 0) - 0.04) < 1e-9 for r in off),
          [r["applied"].get("mood") for r in off])
    check("8 开启时第 5 次明显缩水",
          on[4]["applied"].get("mood", 0) < off[4]["applied"].get("mood", 0) - 1e-6,
          (on[4]["applied"].get("mood"), off[4]["applied"].get("mood")))

    # ---------------------------------------------------------------- 9 跨日与超窗
    root = _fresh("day")
    with use(root, at=T0):
        vec.apply_events([praise()], conv="g1", uid="u1", message_id=1800, phase="pre")
        used_today = _state(root)["daily"]["travel"].get("mood")
    with use(root, at=T0 + 86400):
        after = vec.apply_events([praise()], conv="g1", uid="u1", message_id=1801, phase="pre")
        fresh_travel = _state(root)["daily"]["travel"].get("mood")
        windowed = vec.apply_events([praise()], conv="g1", uid="u1", message_id=1802, phase="pre")
    check("9 跨日后日额度清零", abs(fresh_travel - 0.04) < 1e-9, fresh_travel)
    check("9 超窗后热度归零", after["heat"]["n"] == 0 and windowed["heat"]["n"] == 1,
          (after["heat"]["n"], windowed["heat"]["n"]))
    check("9 当天确实记过账", (used_today or 0) > 0, used_today)

    # ---------------------------------------------------------------- 10a 双进程
    restore()
    override(vector_max_step=0.02, vector_daily_limit=1.0)
    root = _fresh("proc")
    _patch_axes(root, mood={"max_step": 0.02, "max_daily": 1.0},
                social={"max_step": 0.02, "max_daily": 1.0})
    child = Path(__file__).with_name("_行为增量验证_子进程.py")
    _write_state(root, {"mood": 0.0, "social": 0.0}, at=T0)   # 从 0 起步，避免撞 ±1 上界
    procs = [subprocess.Popen([sys.executable, str(child), str(root), str(T0), axis, "0.02", "50"])
             for axis in ("mood", "social")]
    codes = [p.wait(timeout=180) for p in procs]
    state = _state(root)
    travel = state["daily"]["travel"]
    check("10a 两个子进程都正常退出", codes == [0, 0], codes)
    check("10a 两轴各记满 1.0（0.02×50，无丢写）",
          abs(travel.get("mood", 0) - 1.0) < 1e-6 and abs(travel.get("social", 0) - 1.0) < 1e-6, travel)

    # ---------------------------------------------------------------- 10b 受控交错
    root = _fresh("interleave")
    _patch_axes(root, mood={"max_step": 0.02, "max_daily": 2.0})
    override(vector_max_step=0.02, vector_daily_limit=2.0)

    def hammer(root_dir: Path, n: int, serial: bool) -> dict:
        def body() -> dict:
            with use(root_dir, at=T0):
                for i in range(n):
                    vec.apply({"mood": 0.02})
            return _state(root_dir)

        if serial:
            return body()
        out = {}

        def wrapped(tag):
            out[tag] = body()

        threads = [threading.Thread(target=wrapped, args=(f"t{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return _state(root_dir)

    parallel = hammer(root, 30, serial=False)
    root_serial = _fresh("serial")
    _patch_axes(root_serial, mood={"max_step": 0.02, "max_daily": 2.0})
    serial = hammer(root_serial, 60, serial=True)
    restore()
    check("10b 并发写入不丢：位移与串行一致",
          abs(parallel["daily"]["travel"].get("mood", 0)
              - serial["daily"]["travel"].get("mood", 0)) < 1e-9,
          (parallel["daily"]["travel"], serial["daily"]["travel"]))
    check("10b 并发写入不丢：终值一致",
          abs(parallel["values"].get("mood", 0) - serial["values"].get("mood", 0)) < 1e-9,
          (parallel["values"].get("mood"), serial["values"].get("mood")))

    # ---------------------------------------------------------------- 10c 审计恢复
    root = _fresh("audit")
    with use(root, at=T0):
        original = vec._append_audit
        vec._append_audit = lambda rows: False
        try:
            broken = vec.apply_events([praise()], conv="g1", uid="u1", message_id=1900, phase="pre")
            pending = len(_state(root).get("audit_pending") or [])
            vec._append_audit = original
            # 只重投递同一个事件：去重提前返回之前必须已经把积压补出去
            vec.apply_events([praise()], conv="g1", uid="u1", message_id=1900, phase="pre")
        finally:
            vec._append_audit = original
        rows = _audit_rows(root)
        left = len(_state(root).get("audit_pending") or [])
    check("10c 审计失败被识别", broken.get("audit_ok") is False and pending == 1, (broken.get("audit_ok"), pending))
    check("10c 重投递同事件即可补写", len(rows) == 1 and left == 0, (len(rows), left))

    # 追加成功但清除失败 → 重放不产生重复行
    root = _fresh("audit2")
    with use(root, at=T0):
        original_write = vec._write_state
        calls = {"n": 0}

        def flaky_write(state, path=None):
            calls["n"] += 1
            if calls["n"] == 2:
                return False
            return original_write(state, path)

        vec._write_state = flaky_write
        try:
            vec.apply_events([praise()], conv="g1", uid="u1", message_id=1950, phase="pre")
        finally:
            vec._write_state = original_write
        vec.apply_events([praise()], conv="g1", uid="u1", message_id=1950, phase="pre")
        rows = _audit_rows(root)
    ids = [row.get("event_id") for row in rows]
    check("10c 清除失败后重放不重复写行", len(ids) == len(set(ids)) and len(ids) >= 1, ids)

    # ---------------------------------------------------------------- 11 队列满
    root = _fresh("backlog")
    with use(root, at=T0):
        original = vec._append_audit
        vec._append_audit = lambda rows: False
        try:
            for i in range(vec._MAX_AUDIT_PENDING):
                vec.apply_events([praise()], conv="g1", uid="u1", message_id=2000 + i, phase="pre")
            before_state = _state(root)
            blocked = vec.apply_events([praise()], conv="g1", uid="u1", message_id=2100, phase="pre")
            after_state = _state(root)
        finally:
            vec._append_audit = original
    check("11 队列满拒绝新变更", blocked.get("why") == "audit_backlog", blocked.get("why"))
    check("11 拒绝时不改值、不记 seen",
          before_state["values"] == after_state["values"]
          and len(after_state.get("seen") or {}) == len(before_state.get("seen") or {}),
          (len(before_state.get("seen") or {}), len(after_state.get("seen") or {})))

    # ---------------------------------------------------------------- 12 批次同轴限幅
    root = _fresh("batch")
    with use(root, at=T0):
        two = [vec.build_event({"mood": 0.08}, kind="code", item="a"),
               vec.build_event({"mood": 0.08}, kind="code", item="b")]
        got = vec.apply_events(two, conv="g1", uid="u1", message_id=2200, phase="pre")
        parts = [r["applied"].get("mood", 0) for r in got["results"]]
        total = got["applied"].get("mood", 0)
    check("12 两项各 +0.08 不会合成 +0.16", abs(total - 0.08) < 1e-12, total)
    check("12 逐项之和等于顶层", abs(math.fsum(parts) - total) < 1e-12, (parts, total))
    root = _fresh("batch2")
    with use(root, at=T0):
        swapped = vec.apply_events(list(reversed(two)), conv="g1", uid="u1",
                                   message_id=2201, phase="pre")
    check("12 调换输入顺序结果相同", abs(swapped["applied"].get("mood", 0) - total) < 1e-12,
          swapped["applied"])

    # ---------------------------------------------------------------- 13 反向不抵消
    root = _fresh("cancel")
    with use(root, at=T0):
        got = vec.apply_events([vec.build_event({"mood": 0.05}, kind="code", item="up"),
                                vec.build_event({"mood": -0.05}, kind="code", item="down")],
                               conv="g1", uid="u1", message_id=2300, phase="pre")
        state = _state(root)
    check("13 反向事件相互抵消后 applied 为 0", abs(got["applied"].get("mood", 0)) < 1e-12,
          got["applied"])
    check("13 travel 记 gross（0.1），不因对冲归零",
          abs(state["daily"]["travel"].get("mood", 0) - 0.1) < 1e-9,
          state["daily"]["travel"])

    # ---------------------------------------------------------------- 14 部分已去重
    root = _fresh("partdup")
    with use(root, at=T0):
        vec.apply_events([vec.build_event({"mood": 0.02}, kind="code", item="x")],
                         conv="g1", uid="u1", message_id=2400, phase="pre")
        got = vec.apply_events([vec.build_event({"mood": 0.02}, kind="code", item="x"),
                                vec.build_event({"mood": 0.02}, kind="code", item="y"),
                                vec.build_event({"mood": 0.02}, kind="code", item="z")],
                               conv="g1", uid="u1", message_id=2400, phase="pre")
    flags = [r["dedup"] for r in got["results"]]
    check("14 已入账的那项被剔除", flags == [True, False, False], flags)
    check("14 其余两项各 0.02", abs(got["applied"].get("mood", 0) - 0.04) < 1e-12, got["applied"])

    # ---------------------------------------------------------------- 15 日余额与值域
    root = _fresh("daily_left")
    _write_state(root, {"mood": 0.0}, at=T0, travel={"mood": 0.49})
    with use(root, at=T0):
        got = vec.apply_events([vec.build_event({"mood": 0.08}, kind="code", item="a")],
                               conv="g1", uid="u1", message_id=2500, phase="pre")
    check("15 日余额只剩 0.01", abs(got["applied"].get("mood", 0) - 0.01) < 1e-9, got["applied"])

    root = _fresh("range")
    _write_state(root, {"mood": 0.98}, at=T0)
    with use(root, at=T0):
        got = vec.apply_events([vec.build_event({"mood": 0.08}, kind="code", item="a")],
                               conv="g1", uid="u1", message_id=2600, phase="pre")
        stages = [c.get("stage") for c in got.get("clipped") or []]
    check("15 值域夹紧到 0.02", abs(got["applied"].get("mood", 0) - 0.02) < 1e-9, got["applied"])
    check("15 记了 range 裁剪", "range" in stages, stages)

    # ---------------------------------------------------------------- 16 有效事件判据
    root = _fresh("effective")
    with use(root, at=T0):
        # 两项都落在入选轴上：跨轴有符号求和为 0，但两项都真实生效 → 应记 1 次热
        got = vec.apply_events([vec.build_event({"mood": 0.05, "defiance": -0.05},
                                                kind="praise", item="praise")],
                               conv="g1", uid="u1", message_id=2700, phase="pre")
        buckets = _state(root)["heat"]["buckets"]
    check("16 跨轴正负相抵仍算有效事件",
          sum(len(v) for v in buckets.values()) == 1, buckets)
    check("16 该事件两轴都生效", set(got["applied"]) >= {"mood", "defiance"}, got["applied"])

    restore()
    return 0


def _bootstrap():
    """用离线桩的前段拿到 stub 与 `check`/`passed`/`failed`。"""
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    marker = 'print("\\n=== 2.'
    end = source.find(marker)
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "vector_events_bootstrap"}
    exec(compile(source[:end], str(harness), "exec"), namespace)  # noqa: S102
    return namespace


if __name__ == "__main__":
    ns = _bootstrap()
    try:
        run(ns["check"])
    finally:
        restore = getattr(ns, "shutil", None) or shutil
        restore.rmtree(WORK, ignore_errors=True)
    failed = ns["failed"]
    print(f"通过 {ns['passed']} 项"
          + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    sys.exit(1 if failed else 0)
