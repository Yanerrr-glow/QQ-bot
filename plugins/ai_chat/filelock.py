"""跨进程文件锁：只依赖标准库（Windows `msvcrt` / POSIX `fcntl`）。

## 为什么需要

`vector_state.json` 是「读 - 改 - 写」：插件里原有写盘只有 `x.tmp` + `replace`
（原子替换），**没有互斥**。两个进程同时更新时，后写者会覆盖先写者的结果，
表现是"情绪偶尔不涨"。原子替换保证的只是"不会读到半截文件"，不是"不会丢写"。

## 约定

* 锁文件是 `<目标文件>.lock`，**用后不删**——删掉会引入"删了但还有人持有"的竞争。
* `exclusive()` 是上下文管理器，`yield` 的值表示**是否拿到锁**：
  `True` 可以安全写入；`False`（超时/打不开）时调用方应当**放弃这次写入**，
  而不是无锁硬写。丢一轮状态比写坏一份状态便宜。
* 同一进程内再加一把 `threading.RLock`：不同线程不会互相等操作系统的锁到超时，
  而且重入不会自锁。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("ai_chat.filelock")

_RLOCK = threading.RLock()
# 本进程已经持有的锁文件：Windows 上 `msvcrt.locking` 是**按进程**生效的，
# 同一进程里的第二个句柄去锁同一文件会一直失败（表现为"一律 busy"）。
# 进程内本来就有 `_RLOCK` 串行化，所以这里记一张表：已经持有就跳过系统锁。
_HELD: set[str] = set()


def _try_lock(handle) -> None:  # noqa: ANN001
    """非阻塞地拿一把排他锁；拿不到就抛 OSError（`msvcrt` 与 `fcntl` 都是这样）。"""
    handle.seek(0)
    try:
        import msvcrt  # type: ignore[import-not-found]
    except ImportError:  # POSIX
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(handle) -> None:  # noqa: ANN001
    handle.seek(0)
    try:
        import msvcrt  # type: ignore[import-not-found]
    except ImportError:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def exclusive(path: Path, *, timeout: float = 3.0) -> Iterator[bool]:
    """独占 `path`（建议传 `<数据文件>.lock`）。拿不到锁时 `yield False`。"""
    lock_path = Path(path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.debug("锁目录建不出来：%s", lock_path.parent, exc_info=True)
    with _RLOCK:
        try:
            handle = lock_path.open("a+b")
        except OSError:
            logger.warning("锁文件打不开，按拿不到锁处理：%s", lock_path)
            yield False
            return
        got = False
        skip_os = str(lock_path) in _HELD
        try:
            if skip_os:
                got = True                     # 同进程重入：_RLOCK 已经串行化
            else:
                deadline = time.monotonic() + max(0.0, float(timeout))
                while True:
                    try:
                        _try_lock(handle)
                        got = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.05)
                if got:
                    _HELD.add(str(lock_path))
            if not got:
                logger.warning("等锁超时（%.1fs）：%s", timeout, lock_path.name)
            yield got
        finally:
            if got and not skip_os:
                try:
                    _unlock(handle)
                except OSError:
                    logger.debug("解锁失败：%s", lock_path, exc_info=True)
                _HELD.discard(str(lock_path))
            try:
                handle.close()
            except OSError:
                pass
