"""`task_manager`（后台任务生命周期）的离线验证：**纯逻辑，不联网、不调模型**。

它守的是改造前的四个洞（见 `task_manager.py` 开头的说明）：
不命名、异常静默、无背压、无停机收尾。另外守着一条**写这一版时踩过的坑**：
`loop` 那一档必须按**任务名**限实例数 —— 按 kind 共享信号量会让 7 个常驻循环只跑得起 1 个。

`task_manager.py` 只依赖标准库，所以这里按文件直接加载即可（不需要 nonebot、不需要桩）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PKG = os.path.join(PROJ, "plugins", "ai_chat")

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  [OK] " + name)
    else:
        FAILED.append(name)
        print("  [FAIL] " + name + ((" —— " + str(detail)) if detail else ""))


# ---- 按文件加载（只设 __path__，不执行包的 __init__.py）--------------------
_pkg = types.ModuleType("ai_chat")
_pkg.__path__ = [PKG]
sys.modules["ai_chat"] = _pkg
spec = importlib.util.spec_from_file_location("ai_chat.task_manager", os.path.join(PKG, "task_manager.py"))
tm = importlib.util.module_from_spec(spec)
sys.modules["ai_chat.task_manager"] = tm
spec.loader.exec_module(tm)

# 记下日志，验证"异常不再静默"
_logged: list[tuple[str, str]] = []


class _Capture:
    def info(self, msg, *a, **k):
        _logged.append(("info", str(msg) % a if a else str(msg)))

    def error(self, msg, *a, **k):
        _logged.append(("error", str(msg) % a if a else str(msg)))

    def warning(self, msg, *a, **k):
        _logged.append(("warning", str(msg) % a if a else str(msg)))

    def exception(self, msg, *a, **k):
        _logged.append(("exception", str(msg) % a if a else str(msg)))


tm.logger = _Capture()


async def _ok(tag="done"):
    await asyncio.sleep(0.01)
    return tag


async def _boom():
    await asyncio.sleep(0)
    raise ValueError("故意炸一个")


async def _forever():
    while True:
        await asyncio.sleep(0.05)


def run(coro):
    return asyncio.run(coro)


print("-- 1. spawn：命名 + 计数 --")
async def _t1():
    t = tm.spawn(_ok(), name="unit.ok", kind="once")
    check("spawn 返回 Task", t is not None)
    check("任务有名（日志里能认出来）", tm._names.get(t) == "unit.ok", str(tm._names))
    await t
    await asyncio.sleep(0)          # 让 done_callback 跑完
    st = tm.stats()
    check("跑完之后不再计入在跑", st["running"] == 0, str(st))
    check("累计起过次数被记下", st["started"].get("unit.ok") == 1, str(st["started"]))
run(_t1())

print("\n-- 2. 名字缺省时从协程推（旧调用点不必改）--")
async def _t2():
    t = tm.spawn(_ok(), kind="once")
    nm = tm._names.get(t) or ""
    check("自动推出来的名字含协程名", "_ok" in nm, nm)
    await t
run(_t2())

print("\n-- 3. 异常不再静默：带任务名进日志 + 进 stats --")
async def _t3():
    t = tm.spawn(_boom(), name="unit.boom", kind="once")
    await asyncio.sleep(0.05)
    errs = [m for lvl, m in _logged if lvl == "error"]
    check("失败被 logger.error 记下（带任务名）", any("unit.boom" in m for m in errs), str(errs[-2:]))
    fails = tm.stats()["failed"]
    check("失败进 stats（能追溯）", any("unit.boom" == f["name"] for f in fails), str(fails))
    check("错误类型保留在记录里", any("ValueError" in f["error"] for f in fails), str(fails))
run(_t3())

print("\n-- 4. 背压：同类排队（不丢活）--")
async def _t4():
    order = []

    def mk(tag):
        async def _job():
            await asyncio.sleep(0.02)
            order.append(tag)
        return _job()
    a = tm.spawn(mk("a"), name="unit.q", kind="extract")
    b = tm.spawn(mk("b"), name="unit.q2", kind="extract")
    await asyncio.gather(a, b)
    check("两条都跑完了（排队而不是丢弃）", sorted(order) == ["a", "b"], str(order))
run(_t4())

print("\n-- 5. loop 那一档按**任务名**限实例（写这一版时踩过的坑）--")
async def _t5():
    l1 = tm.spawn(_forever(), name="loop.one", kind="loop")
    l2 = tm.spawn(_forever(), name="loop.two", kind="loop")
    await asyncio.sleep(0.02)
    check("两个**不同**常驻循环能同时跑（不能共享一把信号量）",
          not l1.done() and not l2.done(), "done=%s,%s" % (l1.done(), l2.done()))
    # 同一个名字再来一个：会排在后面（同一名字只允许一个实例在跑）
    l1b = tm.spawn(_forever(), name="loop.one", kind="loop")
    await asyncio.sleep(0.02)
    check("同名循环的第二个实例在排队（没有并发第二份）",
          not l1b.done() and tm._inflight.get("loop.one", 0) == 2,
          "inflight=%s" % tm._inflight.get("loop.one"))
    for t in (l1, l2, l1b):
        t.cancel()
    await asyncio.sleep(0.05)
run(_t5())

print("\n-- 6. reject_over：超出直接跳过（图片入库那种「多了没用」的）--")
async def _t6():
    kept = []
    for i in range(5):
        t = tm.spawn(_forever(), name="image", kind="image", reject_over=2)
        if t is not None:
            kept.append(t)
    check("超过上限的被跳过（只留 2 个）", len(kept) == 2, len(kept))
    check("跳过的协程被 close 掉（否则报 coroutine was never awaited）",
          any("已达上限" in m for lvl, m in _logged if lvl == "info"), str(_logged[-3:]))
    for t in kept:
        t.cancel()
    await asyncio.sleep(0.05)
run(_t6())

print("\n-- 7. 停机收尾：等短窗口 → 超时取消并逐个记名 --")
async def _t7():
    tm.spawn(_forever(), name="loop.long", kind="loop")
    tm.spawn(_ok(), name="unit.quick", kind="once")
    await asyncio.sleep(0.02)
    res = await tm.shutdown(timeout=0.3)
    check("停机结果里只报还没结束的那个（早跑完的不算）",
          res["cancelled"] == ["loop.long"], str(res))
    check("超时没结束的被取消，且**记了名字**", "loop.long" in res["cancelled"], str(res))
    check("停机后不再接收新任务", tm.spawn(_ok(), name="unit.after", kind="once") is None)
    st = tm.stats()
    check("stats 里 accepting 已置 False", st["accepting"] is False, str(st))
run(_t7())

print("\n=== 结果：通过 %d 项，失败 %d 项 ===" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  失败：", f)
sys.exit(1 if FAILED else 0)
