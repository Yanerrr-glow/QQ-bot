r"""后台任务的**统一生命周期**：命名、异常日志、并发上限、计数、停机收尾。

## 为什么要它（改造前的四个洞）

`__init__._spawn()` 原来只有 4 行：`create_task` + 强引用 set + `discard`：

1. **不命名** → 日志里看不出是哪个任务出的问题；
2. **不取 `task.exception()`** → 协程里抛的异常要么被自己 try 掉，要么变成 GC 时那句
   无任务名的 "Task exception was never retrieved"。实测会**静默失败**的有三类：
   `proactive.on_group_message`（内部无 try）、`_remember_definition`（落盘异常无人取回）、
   `memory.extract_and_store`（`_apply_extraction`/`_persist` 未捕获）；
3. **无背压** → 一条消息带 N 张图就起 N 个 `_ingest_image`，没有上限；
4. **无停机收尾** → 全项目没有 `on_shutdown`：进程退出时后台任务被硬切，
   而 `msgindex.close()` 与 `render.shutdown()` 两个**为关闭准备的函数从来没人调**。

## 设计取舍

* **只依赖标准库**（asyncio / logging / time）—— 这样 `验证\_任务验证.py` 能按文件直接加载它，
  不需要 nonebot，也不需要桩。
* **背压用"排队"不用"丢弃"**：每类给一个信号量，超了就在里面等 —— 语义不变（一条都不会丢），
  只是不再无限并发。图片入库这类"多了也没用"的除外，它按上限**拒绝多余的**（见 `reject_over`）。
* **停机顺序**：先停止接收新任务 → 等一小段时间让在跑的收尾 → 超时取消并逐个记名。
  必须用 `asyncio.wait(timeout=…)`，**不能 `gather` 全等** ——
  后台里有 DSH 轮询这种 deadline 可达一分多钟的任务，等它等于拖住停机。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable

logger = logging.getLogger("ai_chat.tasks")

# 每类任务的并发上限（**按用途分**，而不是一个全局信号量）。
# 语义：超出就在信号量上排队等，不丢弃 —— 所以调小只是"变慢"，不会"丢活"。
#
# ⚠ `loop` 这一档是**按任务名**限实例数（不是按 kind 共享）：7 个常驻循环各是一个 name，
#    共享一把信号量会让后 6 个永远排队（2026-09-28 写这版时踩过，`_任务验证.py` 现在守着它）。
KIND_LIMITS: dict[str, int] = {
    "image": 3,       # 图片入库：下载 + 感知哈希 + 打分，重活
    "extract": 1,     # 记忆抽取：一次一批，天然串行
    "definition": 1,  # 释义沉淀
    "loop": 1,        # 常驻循环：**每个名字**一个实例
    "once": 8,        # 其它一次性任务
}

_tasks: set[asyncio.Task] = set()
_names: dict[asyncio.Task, str] = {}
_inner: dict[asyncio.Task, Awaitable[Any]] = {}   # 外层 Task → 内层协程（兜底 close 用）
_entered: set[asyncio.Task] = set()               # 已经进入执行体的 Task
_started: dict[str, int] = {}
_inflight: dict[str, int] = {}              # 同类在跑几个（`reject_over` 用它判）
_failed: list[dict[str, Any]] = []          # 最近几次失败（给停机日志与诊断用）
_sems: dict[str, asyncio.Semaphore] = {}
_accepting = True                            # 停机后置 False：不再接收新任务
_MAX_FAILED_KEEP = 20


def _sem(kind: str, name: str) -> asyncio.Semaphore:
    """取这一档的信号量。

    `loop` 特殊：**按任务名**各一把（同一名字只允许一个实例，
    不同循环互不阻塞）—— 按 kind 共享会让 7 个常驻循环只跑得起 1 个。
    """
    if kind == "loop":
        key = "loop:" + name
        lim = 1
    else:
        key = kind
        lim = max(1, int(KIND_LIMITS.get(kind, KIND_LIMITS["once"])))
    sem = _sems.get(key)
    if sem is None:
        sem = asyncio.Semaphore(lim)
        _sems[key] = sem
    return sem


def name_of(coro: Awaitable[Any]) -> str:
    """从协程推一个可读的任务名（调用点不必逐个改）。"""
    q = getattr(coro, "__qualname__", None) or getattr(coro, "__name__", None)
    return str(q or type(coro).__name__)


def _done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    name = _names.pop(task, "?")
    left = _inflight.get(name, 0) - 1
    if left > 0:
        _inflight[name] = left
    else:
        _inflight.pop(name, None)
    if task.cancelled():
        # **还没进入执行体就被取消**（任务刚 create 就被 cancel）：`_runner` 的 finally 不会跑，
        # 内层协程会漏掉 → 这里兜底 close，免得报 "coroutine ... was never awaited"。
        if task not in _entered:
            _close(_inner.pop(task, None))
        _entered.discard(task)
        logger.info("后台任务已取消：%s", name)
        return
    _entered.discard(task)
    _inner.pop(task, None)
    exc = task.exception()
    if exc is not None:
        # **这就是改造前丢掉的那条信息**：以前异常只在 GC 时以无任务名的形式冒出来
        _failed.append({"name": name, "error": f"{type(exc).__name__}: {exc}", "at": time.time()})
        del _failed[: max(0, len(_failed) - _MAX_FAILED_KEEP)]
        logger.error("后台任务失败：%s —— %s: %s", name, type(exc).__name__, exc,
                     exc_info=exc)


def spawn(coro: Awaitable[Any], *, name: str = "", kind: str = "once",
          reject_over: int = 0) -> asyncio.Task | None:
    """起一个后台任务。返回 Task（调用方要取消/等待时用得上；旧调用点忽略返回值即可）。

    * `name` 留空就从协程函数名推一个；
    * `kind` 决定并发上限（见 `KIND_LIMITS`，超出**排队**，不丢活）；
    * `reject_over > 0` 时改为**丢弃**超出者并返回 None（图片入库这类"多了也没用"的用它）。
      丢弃时会把协程 `close()` 掉 —— 否则 Python 会报 "coroutine was never awaited"。
    """
    global _accepting
    label = name or name_of(coro)
    if not _accepting:
        logger.info("已在停机流程中，拒绝新任务：%s", label)
        _close(coro)
        return None
    if reject_over > 0 and _inflight.get(label, 0) >= reject_over:
        logger.info("同类任务已达上限 %d，跳过：%s", reject_over, label)
        _close(coro)
        return None

    # `acquired` 是为了**排队期间被取消**的情况：那时内层协程一次都没被 await，
    # 不 close 掉 Python 会报 "coroutine ... was never awaited"（2026-09-28 实测）。
    async def _runner() -> Any:
        me = asyncio.current_task()
        if me is not None:
            _entered.add(me)
        acquired = False
        try:
            async with _sem(kind, label):
                acquired = True
                if me is not None:
                    _inner.pop(me, None)   # 已经真的 await 上了，不再需要兜底
                return await coro
        finally:
            if not acquired:
                _close(coro)

    task = asyncio.create_task(_runner(), name=label)
    _tasks.add(task)
    _names[task] = label
    _inner[task] = coro
    _started[label] = _started.get(label, 0) + 1
    _inflight[label] = _inflight.get(label, 0) + 1
    task.add_done_callback(_done)
    return task


def _close(coro: Awaitable[Any] | None) -> None:
    """丢弃一个协程时把它关掉（避免 "coroutine was never awaited" 警告）。"""
    if coro is None:
        return
    close = getattr(coro, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - 关不掉也不该影响主流程
            pass


def stats() -> dict[str, Any]:
    """给诊断/控制台用：在跑几个、累计起过几个、最近失败。"""
    return {
        "running": len(_tasks),
        "names": sorted(_names.values()),
        "started": dict(sorted(_started.items())),
        "failed": list(_failed[-5:]),
        "accepting": _accepting,
        "limits": dict(KIND_LIMITS),
    }


def pending() -> list[str]:
    return sorted(_names.values())


async def shutdown(timeout: float = 5.0) -> dict[str, Any]:
    """停机收尾：停止接收 → 等一小段时间 → 超时取消并**逐个记名**。

    返回一份结果（给日志与诊断用）。幂等：重复调用不会出错。
    """
    global _accepting
    _accepting = False
    tasks = [t for t in _tasks if not t.done()]
    if not tasks:
        return {"waited": 0, "cancelled": [], "timeout": timeout}
    done, still = await asyncio.wait(tasks, timeout=timeout)
    cancelled: list[str] = []
    for task in still:
        cancelled.append(_names.get(task, "?"))
        task.cancel()
    if cancelled:
        # 取消后再给一小口气，让 CancelledError 传播完（不吞：协程里的 except Exception 不接它）
        await asyncio.wait(still, timeout=1.0)
    logger.info(
        "后台任务收尾：%d 个在 %s 秒内自然结束，%d 个被取消%s",
        len(done), timeout, len(cancelled),
        ("（" + "、".join(cancelled) + "）") if cancelled else "",
    )
    return {"waited": len(done), "cancelled": cancelled, "timeout": timeout}
