"""会话级运行时状态：图片策略与「哪张图刚发过」。

## 为什么单独一个模块

有两类状态**既不属于聊天记录，也不属于长期记忆**：

1. **图片处理策略**：`/图 忽略` 或「不要保存这张图片」之后，这个会话该怎么处理图片。
   它必须**立刻**影响行为（在下载/入库/送模型之前就查），所以不能等落进 prompt 里
   绕一圈让模型"记住别存图" —— 模型答应了也没用，真正存图的是 `stickers.py`。
2. **最近图片的内容哈希**：用来做「这张图」的指代。

`chatlog` 不适合放这些（它是纯文本记录、按字符预算丢弃），`memory` 也不适合
（它是跨会话长期事实、有淘汰）。所以在两者之间放一个轻量的会话状态，
落盘到 `data/conv_state.json`，**进程重启后仍然生效** —— 「以后别存图」不该因为
一次重启就失效。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger("ai_chat.state")

_FILE = "conv_state.json"

# 最近图片哈希保留几个：够覆盖"发了三张再问这张别存"的场景
_RECENT_HASHES = 8
# 单会话忽略名单的上限，避免无限增长
_MAX_IGNORE = 200

VALID_MODES = ("normal", "ignore", "only", "off")

MODE_LABEL: dict[str, str] = {
    "normal": "正常（会看图，符合条件就收进表情库）",
    "ignore": "不再保存图片（还会看图，但一张都不入库）",
    "only": "只看不存（会读图回应，不入库）",
    "off": "完全不看图（不下载、不送模型）",
}


def _path() -> Path:
    return config.LOG_DIR / _FILE


class _State:
    def __init__(self) -> None:
        # conv -> {"mode": str, "ignore": [hash...], "note": str, "updated_at": str}
        self.convs: dict[str, dict[str, Any]] = {}
        self.global_mode: str = "normal"
        self.recent: dict[str, list[str]] = {}
        self.loaded = False

    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    def load(self) -> None:
        self.loaded = True
        path = _path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("会话状态文件损坏，按默认继续：%s", path)
            return
        if not isinstance(raw, dict):
            return
        gm = str(raw.get("global_mode") or "normal")
        self.global_mode = gm if gm in VALID_MODES else "normal"
        for conv, item in (raw.get("convs") or {}).items():
            if not isinstance(item, dict):
                continue
            mode = str(item.get("mode") or "")
            self.convs[str(conv)] = {
                "mode": mode if mode in VALID_MODES else "",
                "ignore": [str(h) for h in (item.get("ignore") or [])][-_MAX_IGNORE:],
                "note": str(item.get("note") or "")[:120],
                "updated_at": str(item.get("updated_at") or ""),
            }

    def save(self) -> None:
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "global_mode": self.global_mode,
                "convs": self.convs,
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            # 带上异常类型与原文：只写"写盘失败"的话，排查时完全看不出是
            # 权限、路径不存在、还是磁盘满（实测在受限环境里就吃过这个亏）。
            logger.warning("会话状态写盘失败：%s（%s: %s）", path, type(exc).__name__, exc)


_state = _State()
_lock = threading.Lock()


def _entry(conv: str) -> dict[str, Any]:
    """取（必要时创建）一个会话的状态条目。调用方负责持锁。"""
    return _state.convs.setdefault(
        conv, {"mode": "", "ignore": [], "note": "", "updated_at": ""}
    )


# --------------------------------------------------------------------- 图片策略
def set_image_policy(conv: str, mode: str, note: str = "") -> str:
    """设这个会话的图片策略。返回生效后的模式。"""
    if mode not in VALID_MODES:
        raise ValueError(f"未知图片策略：{mode}")
    with _lock:
        _state.ensure()
        item = _entry(conv)
        item["mode"] = mode
        if note:
            item["note"] = note[:120]
        item["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _state.save()
    logger.info("图片策略 conv=%s → %s", conv, mode)
    return mode


def clear_image_policy(conv: str) -> bool:
    """撤掉会话级策略（回落到全局默认）。"""
    with _lock:
        _state.ensure()
        item = _state.convs.get(conv)
        if item is None or (not item.get("mode") and not item.get("ignore")):
            return False
        _state.convs.pop(conv, None)
        _state.save()
    logger.info("图片策略已撤销 conv=%s", conv)
    return True


def set_global_image_policy(mode: str) -> str:
    if mode not in VALID_MODES:
        raise ValueError(f"未知图片策略：{mode}")
    with _lock:
        _state.ensure()
        _state.global_mode = mode
        _state.save()
    logger.info("全局图片策略 → %s", mode)
    return mode


def clear_global_image_policy() -> int:
    """撤销全局策略，并把所有"只是跟着全局"的会话设置一并清掉。返回清理处数。"""
    with _lock:
        _state.ensure()
        _state.global_mode = "normal"
        dropped = 0
        for conv in list(_state.convs):
            item = _state.convs[conv]
            if not item.get("mode") and not item.get("ignore"):
                _state.convs.pop(conv, None)
                dropped += 1
        _state.save()
    return dropped


def image_mode(conv: str) -> str:
    """当前会话真正生效的图片模式：会话设置 > 全局设置 > normal。"""
    _state.ensure()
    item = _state.convs.get(conv) or {}
    mode = str(item.get("mode") or "")
    if mode in VALID_MODES:
        return mode
    return _state.global_mode


def may_download(conv: str) -> bool:
    """要不要下载这张图（下载是入库、读图、记住"最近一张"的共同前提）。"""
    return image_mode(conv) != "off"


def may_store(conv: str) -> bool:
    """要不要把图收进表情包库。"""
    return image_mode(conv) not in ("ignore", "only", "off")


def may_view(conv: str) -> bool:
    """要不要把图送给模型看。"""
    return image_mode(conv) != "off"


# --------------------------------------------------------------------- 单张豁免
def remember_image(conv: str, digest: str) -> None:
    """记下"这个会话刚发过的图的哈希"，供「这张图」指代。"""
    with _lock:
        _state.ensure()
        bucket = _state.recent.setdefault(conv, [])
        if digest in bucket:
            bucket.remove(digest)
        bucket.append(digest)
        del bucket[:-_RECENT_HASHES]


def latest_image(conv: str) -> str:
    _state.ensure()
    bucket = _state.recent.get(conv) or []
    return bucket[-1] if bucket else ""


def has_recent_image(conv: str) -> bool:
    return bool(latest_image(conv))


def ignore_image(conv: str, digest: str) -> bool:
    """把某张图加进忽略名单（不会入库）。返回是否是新增。"""
    if not digest:
        return False
    with _lock:
        _state.ensure()
        item = _entry(conv)
        bucket = item.setdefault("ignore", [])
        if digest in bucket:
            return False
        bucket.append(digest)
        del bucket[:-_MAX_IGNORE]
        item["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _state.save()
    logger.info("图片已进忽略名单 conv=%s hash=%s", conv, digest[:8])
    return True


def ignore_latest_image(conv: str) -> str:
    """把"最近那张图"加进忽略名单。返回被忽略的哈希（没有则空串）。"""
    digest = latest_image(conv)
    if digest:
        ignore_image(conv, digest)
    return digest


def is_ignored(conv: str, digest: str) -> bool:
    _state.ensure()
    item = _state.convs.get(conv) or {}
    return digest in (item.get("ignore") or [])


def clear_ignored(conv: str) -> int:
    with _lock:
        _state.ensure()
        item = _state.convs.get(conv)
        if not item:
            return 0
        n = len(item.get("ignore") or [])
        item["ignore"] = []
        _state.save()
    return n


# --------------------------------------------------------------------- 只读视图
def describe_image_policy(conv: str) -> str:
    """给 /图 状态 与 Web 控制台看的人话描述。"""
    _state.ensure()
    item = _state.convs.get(conv) or {}
    own = str(item.get("mode") or "")
    effective = image_mode(conv)
    lines = [f"这个会话当前：{MODE_LABEL.get(effective, effective)}"]
    if own:
        lines.append(f"（是本会话单独设的：{MODE_LABEL.get(own, own)}）")
    elif _state.global_mode != "normal":
        lines.append(f"（来自全局设置：{MODE_LABEL.get(_state.global_mode, _state.global_mode)}）")
    ignored = len(item.get("ignore") or [])
    if ignored:
        lines.append(f"另有 {ignored} 张图被单独点名不保存。")
    return "\n".join(lines)


def snapshot() -> dict[str, Any]:
    _state.ensure()
    # 会话级设置为空、但全局被改过时也要出现在快照里，所以这里补一个占位条目 ——
    # 否则 Web 控制台会显示"全局已改"却列不出任何会话，看起来像没生效。
    if _state.global_mode != "normal":
        _state.convs.setdefault("__global__", {"mode": "", "ignore": [], "note": "", "updated_at": ""})
    return {
        "global_mode": _state.global_mode,
        "convs": {
            conv: {
                "mode": item.get("mode") or "",
                "effective": image_mode(conv),
                "label": MODE_LABEL.get(image_mode(conv), ""),
                "ignored": len(item.get("ignore") or []),
                "note": item.get("note") or "",
                "updated_at": item.get("updated_at") or "",
            }
            for conv, item in _state.convs.items()
        },
    }


def set_from_web(conv: str, mode: str) -> str:
    """Web 控制台入口：mode 为 "" 表示撤销。"""
    if not mode:
        clear_image_policy(conv)
        return image_mode(conv)
    return set_image_policy(conv, mode, note="webui")
