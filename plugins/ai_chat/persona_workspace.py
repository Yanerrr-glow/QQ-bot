"""任务局部的人格沙盒：评估读取冻结副本，不切换线上人格或写入在线状态。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import time

_workspace: ContextVar[tuple[Path, float] | None] = ContextVar("persona_workspace", default=None)


def root() -> Path | None:
    value = _workspace.get()
    return value[0] if value else None


def now() -> float:
    value = _workspace.get()
    return value[1] if value else time.time()


def today() -> str:
    """日历日 key（`YYYY-MM-DD`），**与 `now()` 同一时钟**。

    为什么不能各自调 `time.strftime`（#0010）：
    `persona_vector` 的日限额与热窗口都按"哪一天"分账，而衰减用的是可冻结的
    `now()`。冻结时钟的离线验证里两者各走各的，用例就不可复现；
    跨日重载的用例也会因为"真实日期没变"而永远测不出跨日。
    """
    return time.strftime("%Y-%m-%d", time.localtime(now()))


@contextmanager
def use(path: Path, *, at: float):
    token = _workspace.set((Path(path), at))
    try:
        yield
    finally:
        _workspace.reset(token)
