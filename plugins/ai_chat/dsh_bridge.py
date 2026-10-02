"""DSH 桥接：把主人发来的 `/dsh run <任务>` 下发到本机，再把结果取回来。

## 它是「转发站」，不是「执行器」

这条链路刻意做成两半，缺一不可：

```
QQ 群/私聊  /dsh run <任务>
   │  bot（服务器容器）只做三件事：认前缀 → 校验是不是主人 → 落一个任务文件
   ▼
/data/runtime/dsh_bridge/task-<id>.json          ← 服务器侧只写**数据**，从不执行任何命令
   │  本机 agent 每 2 秒轮询取走（SSH 方向是「本机→服务器」，不需要开任何入站端口）
   ▼
本机执行 `dsh --profile headless "<任务>"`，把 stdout 写成 result-<id>.json
   ▲
   │  bot 轮询 /data/runtime/dsh_bridge/result-<id>.json，取回后**原样转发**（不改写、不解释）
```

**为什么不能让 bot 直接执行**：bot 的输出与它读到的群消息都是不可信输入
（群里一句话就可能是一次 prompt injection）。让它直接驱动本机 DSH 等于把电脑交出去。
所以服务器侧只负责"放一张写着任务的纸"，真正动手的永远是本机那个 agent，
而它按**固定动作表**（目前只有 `dsh.run`）执行，不做通用 shell。

## 超时是**两条**，别混

* `WAIT_SECONDS`：bot 在回复里**同步等**多久（超了就回"已下发，稍后推结果"，任务不取消）；
* `TIMEOUT_SECONDS`：写进任务、由本机 agent 执行的**执行上限**（DSH 任务跑太久要掐掉）。

两个数都会随任务一起落盘，本机 agent 按任务里的 `timeout_seconds` 执行。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger("ai_chat.dsh_bridge")

# 只允许一个动作。加动作必须同时改本机 agent 的白名单 —— 两边都不认识的动作会被拒。
ALLOWED_ACTIONS = frozenset({"dsh.run"})

WAIT_SECONDS = 60.0        # bot 同步等待结果的上限（超时 → 回"已下发"）
TIMEOUT_SECONDS = 300      # 本机执行上限
_MAX_TASK_CHARS = 800      # 任务文本上限
_MAX_RESULT_CHARS = 1500   # 转发回 QQ 的结果上限（超了截断并注明）


def bridge_dir() -> Path:
    """桥接目录。放在 data/ 下 —— 它有卷挂载，容器重建也不会丢。"""
    return config.LOG_DIR / "dsh_bridge"


def _ensure() -> Path:
    d = bridge_dir()
    (d / "out").mkdir(parents=True, exist_ok=True)
    return d


def clean_task(text: str) -> str:
    """规范化任务文本。

    只做**保守**的清洗：压空白、去控制字符、限长。
    刻意**不**做任何"命令解析" —— 任务文本会作为**一个 argv 元素**交给
    `dsh --profile headless`，不经 shell，所以这里不需要防注入。
    """
    flat = " ".join(str(text or "").split())
    flat = "".join(ch for ch in flat if ch == " " or ord(ch) >= 32)
    return flat[:_MAX_TASK_CHARS].strip()


def enqueue(task: str, *, from_user: str = "", wait_seconds: float = WAIT_SECONDS) -> dict[str, Any]:
    """落一个任务文件。返回任务字典（含 id）。

    **写入是原子的**（先写 `.tmp` 再 replace）：本机 agent 可能在任意时刻轮询，
    半截 JSON 会让它解析失败。
    """
    d = _ensure()
    clean = clean_task(task)
    if not clean:
        raise ValueError("空任务")
    task_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    payload = {
        "id": task_id,
        "action": "dsh.run",
        "task": clean,
        "from": str(from_user or "")[:32],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "timeout_seconds": TIMEOUT_SECONDS,
        "wait_seconds": wait_seconds,
    }
    tmp = d / f".task-{task_id}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(d / f"task-{task_id}.json")
    # 顺手清理过期文件：轮询 agent 只删任务、不删结果，结果目录会一直长。
    # 挂在这里是因为它本来就稀疏（每次下发才一次），不需要定时器。
    cleanup(keep=20)
    logger.info("DSH 任务已下发 id=%s from=%s task=%r", task_id, from_user, clean[:60])
    return payload


def _result_path(task_id: str) -> Path:
    return bridge_dir() / "out" / f"result-{task_id}.json"


def read_result(task_id: str) -> dict[str, Any] | None:
    """读一条结果。读不到或坏掉都返回 None（**不抛**）。"""
    path = _result_path(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("DSH 结果文件读不动：%s", path)
        return None
    return data if isinstance(data, dict) else None


def wait_result(task_id: str, timeout: float = WAIT_SECONDS) -> dict[str, Any] | None:
    """同步等结果，最长 `timeout` 秒。超时返回 None（**任务不取消**）。"""
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        got = read_result(task_id)
        if got is not None:
            return got
        # 本机 agent 的轮询周期是 2 秒，这里 0.5 秒查一次足够，也不会白烧 CPU
        time.sleep(0.5)
    return read_result(task_id)


def cleanup(keep: int = 50) -> int:
    """删掉过期的任务与结果文件（只保留最近 `keep` 对），避免目录无限长。"""
    d = bridge_dir()
    if not d.exists():
        return 0
    removed = 0
    for pattern, folder in (("task-*.json", d), ("result-*.json", d / "out")):
        files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            try:
                old.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def render_result(payload: dict[str, Any]) -> str:
    """把结果渲染成**原样转发**的文本。

    这里刻意**不加工、不解释、不浓缩** —— 主人要的是 DSH 的原始回话，
    中间加一层"人设化改写"就会失真（这条需求写得很明确：bot 只做转发站）。
    只做一件事：超长截断，并明确注明截断了。
    """
    status = str(payload.get("status") or "?")
    text = str(payload.get("stdout") or payload.get("text") or "").strip()
    err = str(payload.get("stderr") or payload.get("error") or "").strip()
    exit_code = payload.get("exit_code")

    head = f"【DSH 结果】exit={exit_code} status={status}"
    if not text and err:
        text = err
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + f"\n…（已截断，原文 {len(text)} 字）"
    return f"{head}\n{text}" if text else f"{head}（DSH 没有输出）"


def stats() -> dict[str, Any]:
    d = bridge_dir()
    return {
        "dir": str(d),
        "exists": d.exists(),
        "pending": len(list(d.glob("task-*.json"))) if d.exists() else 0,
        "results": len(list((d / "out").glob("result-*.json"))) if d.exists() else 0,
        "actions": sorted(ALLOWED_ACTIONS),
        "wait_seconds": WAIT_SECONDS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "pid_hint": os.getpid(),
    }
