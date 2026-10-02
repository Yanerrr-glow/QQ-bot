"""脱敏与文本工具。

桌面端唯一允许"打印敏感信息"的地方就是本模块 —— 别处一律先过 `redact()`。
控制台日志会被用户截图、贴进 issue，token 一旦进去就是既成事实。
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable

# 键名里出现这些片段，值一律按秘密处理（大小写不敏感）。
SECRET_HINTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "auth",
)

MASK = "***"
# 短于此长度的秘密连前缀都不露（露 2 个字符等于泄露一半）。
_SHORT_SECRET_LEN = 8
_KEEP_PREFIX = 4

# 值里形如 `?token=xxxx` 的查询串 —— 服务端接受这种写法，但绝不能进日志。
_QUERY_SECRET_RE = re.compile(
    r"(?i)\b(token|api_key|apikey|access_token|password|secret)=([^&\s\"']+)"
)


def is_secret_key(key: str) -> bool:
    """键名是否属于秘密类。"""
    low = str(key).lower()
    return any(hint in low for hint in SECRET_HINTS)


def mask_value(value: Any) -> str:
    """把秘密值压成可安全展示的形式：长值露前 4 位，短值全掩。"""
    text = "" if value is None else str(value)
    if not text:
        return ""
    if len(text) < _SHORT_SECRET_LEN:
        return MASK
    return text[:_KEEP_PREFIX] + MASK


def redact_text(text: str) -> str:
    """清掉文本里形如 `token=...` 的片段。"""
    if not text:
        return ""
    return _QUERY_SECRET_RE.sub(lambda m: m.group(1) + "=" + MASK, str(text))


def redact(data: Any, *, depth: int = 0) -> Any:
    """递归脱敏：秘密键 → 掩码；其余字符串 → 清查询串。

    只处理 dict/list/tuple/标量；遇到不认识的对象退回 `repr` 并同样清一遍，
    保证"任何输入都不会原样漏出去"。
    """
    if depth > 8:  # 防御自引用结构
        return "<深>"
    if isinstance(data, dict):
        out: dict[Any, Any] = {}
        for key, value in data.items():
            if is_secret_key(str(key)):
                out[key] = mask_value(value)
            else:
                out[key] = redact(value, depth=depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in data]
    if isinstance(data, str):
        return redact_text(data)
    if isinstance(data, (int, float, bool)) or data is None:
        return data
    if dataclasses.is_dataclass(data) and not isinstance(data, type):
        return redact(dataclasses.asdict(data), depth=depth + 1)
    return redact_text(repr(data))


def truncate(text: Any, limit: int = 300) -> str:
    """日志里长文本截断（带省略标记），避免一条日志糊满屏。"""
    out = "" if text is None else str(text)
    if len(out) <= limit:
        return out
    return out[:limit] + f"…(+{len(out) - limit})"


def one_line(text: Any, limit: int = 300) -> str:
    """折成一行再截断 —— 服务端错误体常带换行，日志里会串行。"""
    out = " ".join(str(text if text is not None else "").split())
    return truncate(out, limit)


def summarize_lines(lines: Iterable[str], limit: int = 6) -> str:
    """取若干行拼成摘要（SSH stderr 用）。"""
    picked = [one_line(x, 200) for x in list(lines)[:limit] if str(x).strip()]
    return " | ".join(picked)
