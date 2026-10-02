"""宿主给插件用的"能不能安全建控件"小工具。

两个层次，**不要混用**：

* `qt_available()` —— PySide6 **装没装**；
* `gui_ready()` —— 现在**能不能真的建控件**（装了 Qt 且已有 `QApplication`）。

为什么要分开：Qt 在"没有 QApplication 却创建 QWidget"时是**致命错误**
（进程直接退出，`QProcess` 退出码 `0xC0000409`，C++ 层 `qFatal`），
**不是可以 try/except 的异常**。所以"装了 PySide6"绝不等于"能建界面"：

* 无头自检 / 服务端环境：Qt 可能装着，但没有 QApplication；
* 用户桌面：两者都有。

插件的页面工厂必须按 `gui_ready()` 决定是建真界面还是**返回纯数据**（宿主会把纯数据
包成只读文本显示）。这样同一份插件在两种环境下都能加载成功 —— 而"插件加载失败"
和"这个环境没有 GUI"是两件完全不同的事，不该混为一谈。

放 `desktop/plugins/` 而不是 `desktop/sdk/`：它只是给示范插件看的样例，
**SDK 本身不依赖 Qt**（`_桌面控制台自检.py` 会验证这条）。第三方插件可以照抄本文件。
"""

from __future__ import annotations

import importlib.util

_CACHE: bool | None = None


def qt_available() -> bool:
    """PySide6 装了吗。"""
    global _CACHE
    if _CACHE is None:
        _CACHE = importlib.util.find_spec("PySide6") is not None
    return bool(_CACHE)


def qtwidgets():  # noqa: ANN201 - 返回模块或 None，避免在这里 import 类型
    """返回 `PySide6.QtWidgets` 模块；没装就返回 None（**不抛异常**）。"""
    if not qt_available():
        return None
    from PySide6 import QtWidgets  # noqa: PLC0415 - 故意延迟到确认可用之后

    return QtWidgets


def gui_ready() -> bool:
    """现在能不能真的建控件（装了 Qt **且**已有 QApplication）。"""
    widgets = qtwidgets()
    return widgets is not None and widgets.QApplication.instance() is not None
