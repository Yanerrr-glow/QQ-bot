"""#0015 M5 候选收敛：只改示例、越界整批拒绝、提案元数据、locked 与回滚。

跑法：`python 验证\\_评估迭代验证.py`（退出码 0 = 全部通过）。

用桩模型跑完整链路（不联网）；提案目录落临时 workspace。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "temp" / "_eval_iter"
SEED = ROOT / "persona/packs/whale_v2"
T0 = 1_000_000.0


def _fresh(name: str) -> Path:
    root = WORK / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    for item in ("base.txt", "surface.txt", "forbidden.txt", "vector.json"):
        (root / item).write_bytes((SEED / item).read_bytes())
    (root / "examples.jsonl").write_bytes((SEED / "examples/examples.jsonl").read_bytes())
    return root


def run(check) -> int:  # noqa: ANN001
    from ai_chat import persona_eval as ev, persona_examples as pex, persona_revise as pr
    from ai_chat import persona_revision_batch as batch, settings
    from ai_chat.persona_workspace import use

    settings.store.load()

    # ---------------------------------------------------------- 白名单
    check("允许 add examples", pr._check_ops([{"op": "add", "target": "examples"}]) == [])
    check("允许 delete examples", pr._check_ops([{"op": "delete", "target": "examples"}]) == [])
    bad = pr._check_ops([{"op": "replace", "target": "surface"}])
    check("拒 replace surface", bool(bad) and "不允许" in bad[0], bad)
    bad = pr._check_ops([{"op": "add", "target": "vector"}])
    check("拒 vector", bool(bad), bad)
    bad = pr._check_ops(["不是对象"])
    check("拒非法元素", bool(bad), bad)
    check("混合里有一条越界就整批拒",
          bool(pr._check_ops([{"op": "add", "target": "examples"},
                              {"op": "delete", "target": "forbidden"}])))

    # ---------------------------------------------------------- 端到端：越界整批拒绝
    root = _fresh("reject")
    with use(root, at=T0):
        settings.store._runtime["eval_enabled"] = True
        settings.store._runtime["eval_propose_only"] = True
        settings.store._runtime["eval_iter_enabled"] = True
        orig_targets = ev.window_targets
        orig_ask = pr._ask
        orig_verify = ev.verify_revision
        try:
            ev.window_targets = lambda **kw: [{"why": "测试问题"}]
            ev.verify_revision = lambda plan: asyncio.sleep(0, result={"passed": True})

            async def _ask_bad(prompt):
                return json.dumps({"ops": [{"op": "replace", "target": "surface",
                                            "find": "x", "replace": "y", "why": "越界"}]})

            pr._ask = _ask_bad
            got = asyncio.run(pr.revise_from_eval(force=True))
            check("越界 ops 整批拒绝", not got.get("ok"), got)
            check("拒绝时说明哪条不允许",
                  "rejected_ops" in got and got["rejected_ops"], got.get("rejected_ops"))

            async def _ask_good(prompt):
                return json.dumps({"ops": [{
                    "op": "add", "target": "examples", "id": "iter-add-1",
                    "polarity": "good", "tags": ["scene:闲聊"],
                    "ctx": ["对方：测试"], "say": ["你：收到。"], "why": "测试"}]})

            pr._ask = _ask_good

            async def _verify_ok(plan):
                return {"passed": True, "why": "测试放行"}

            ev.verify_revision = _verify_ok
            got = asyncio.run(pr.revise_from_eval(force=True))
            check("合法 ops 生成提案", bool(got.get("proposed")), str(got)[:200])
            check("提案未写线上人格",
                  not any(str(x.get("id")) == "iter-add-1" for x in pex._load()))

            rev = str(got.get("rev") or "")

            # ---------------- D4：提案通知的取数与标记
            pend = batch.pending_notice()
            check("新提案进入待通知", any(str(x.get("rev")) == rev for x in pend),
                  [x.get("rev") for x in pend])
            check("待通知项都还没有 notified_at",
                  all(not x.get("notified_at") for x in pend))
            marked = batch.mark_notified([rev], at=T0)
            check("标记通知成功", marked.get("ok") and rev in marked["notified"], marked)
            check("标记后不再待通知",
                  all(str(x.get("rev")) != rev for x in batch.pending_notice()))
            meta_after = batch._read_batch(rev) or {}
            check("通知不改变提案状态", meta_after.get("status") == "awaiting_review",
                  meta_after.get("status"))
            check("记下通知时间", meta_after.get("notified_at") == T0, meta_after.get("notified_at"))
            settings.store._runtime["eval_proposal_notify"] = False
            res = asyncio.run(ev.notify_pending())
            check("通知关着时不发", res.get("ok") is False and "关着" in str(res.get("why")), res)
            settings.store._runtime["eval_proposal_notify"] = True
            res = asyncio.run(ev.notify_pending())
            check("没有待通知时明确说没有",
                  res.get("ok") is True and res.get("sent") == 0, res)
            pend2 = batch.pending_notice()
            check("标记过的不重复通知",
                  all(str(x.get("rev")) != rev for x in pend2), [x.get("rev") for x in pend2])

            detail = batch.proposal_detail(rev)
            check("提案详情带 verification", isinstance(detail.get("verification"), dict))
            meta = dict(detail.get("verification") or {}).get("eval") or {}
            check("详情带评估依据", meta.get("cases") is not None, meta)
            check("未评分如实标 graded=False", meta.get("graded") is False, meta)
            check("目标类型是 expression", meta.get("target_kind") == "expression", meta)

            committed = batch.commit_proposal(rev)
            check("采纳后示例才落盘", bool(committed.get("ok")), str(committed)[:160])
            check("采纳后能在示例库看到",
                  any(str(x.get("id")) == "iter-add-1" for x in pex._load()))
            persona_undo = __import__("ai_chat.persona", fromlist=["x"]).undo_revision()
            check("整版回滚成功", bool(persona_undo.get("ok")), str(persona_undo)[:120])
            check("回滚后示例消失",
                  not any(str(x.get("id")) == "iter-add-1" for x in pex._load()))

            # ------------------------------------------------------ locked 不能删
            locked = [x for x in pex._load() if x.get("locked")]
            if locked:
                lead = locked[0]
                plan = batch.prepare([{"op": "delete", "target": "examples",
                                       "id": str(lead.get("id")), "why": "测试"}])
                check("删 locked 示例被拒", not plan.get("ok"), str(plan)[:160])
            else:
                check("示例库里没有 locked 条目（本条跳过）", True)

            # ------------------------------------------------------ 越界不进 prepare
            # codex P2-1：`prepare()` 只认 examples 的 add/delete —— replace examples 会报
            # 「不支持的操作」。所以白名单里也就不该出现它。
            plan = batch.prepare([{"op": "replace", "target": "examples",
                                   "id": "whatever", "find": "旧", "replace": "新", "why": "测试"}])
            check("prepare 不支持 replace examples",
                  not plan.get("ok") and "不支持" in str(plan.get("why") or ""), str(plan)[:120])
        finally:
            ev.window_targets = orig_targets
            ev.verify_revision = orig_verify
            pr._ask = orig_ask
            for key in ("eval_enabled", "eval_propose_only", "eval_iter_enabled"):
                settings.store._runtime.pop(key, None)
    return 0


def _bootstrap():
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    end = source.find('print("\\n=== 2.')
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "eval_iter_bootstrap"}
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
