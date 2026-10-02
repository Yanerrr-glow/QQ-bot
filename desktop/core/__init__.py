"""桌面控制台的非 GUI 核心。

这一层的硬约束：**只 import 标准库**。

原因很实际：本机没有 PySide6 时，依然要能跑 `验证\\_桌面控制台自检.py`，
把 API 客户端、认证、错误分类、目标隔离、SSH 隧道参数、插件权限判定全部验一遍。
GUI 那点渲染没法自动验，但"会不会把 token 发错目标""401 会不会被当成空数据"
这类真正致命的问题，全在这一层。
"""

from __future__ import annotations

__all__ = [
    "apiclient",
    "connection",
    "credentials",
    "logsetup",
    "paths",
    "targets",
    "tunnel",
    "util",
]
