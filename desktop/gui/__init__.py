"""界面层（唯一 import Qt 的地方）。

    app.py          进程入口：参数解析、高 DPI、启动窗口
    main_window.py  导航 + 顶部状态 + 页面栈 + 插件导航
    widgets.py      配色、控件工厂、后台任务（QThreadPool）、确认框
    base.py         页面基类与 `GET /api/state` 的取用助手
    page_*.py       各内置页面

约定：**页面只通过 ConnectionManager 说话**，不读配置文件、不碰运行数据。

⚠ **`MainWindow` 必须惰性导出**：这个包一旦被 import 就会拉起 PySide6，
而 PySide6 在"没有 QApplication 却创建 QWidget"时是**致命错误**（进程直接退出，
不是抛异常）。自检/无头环境会走 `desktop.selfcheck`，它的导入链会碰到本包 ——
所以这里不能用 `from .main_window import MainWindow` 那种急切写法，否则
`python -m desktop selfcheck` 在装了 PySide6 的机器上会静默崩溃（实测踩过）。
"""

from __future__ import annotations

from typing import Any

__all__ = ["MainWindow"]


def __getattr__(name: str) -> Any:
    """按需导入（PEP 562）：只有真的要用主窗口时才碰 Qt。"""
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
