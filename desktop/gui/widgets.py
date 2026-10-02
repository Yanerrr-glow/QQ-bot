"""界面层的公共约定：颜色、控件工厂、异步任务、状态条。

**这里是桌面端唯一 import Qt 的地方**（`desktop/gui/`），
`desktop/core/` 与 `desktop/sdk/` 都不许碰 Qt —— 那条边界由自检守着。

三条来自踩坑的硬规矩，写在这里给所有页面共用：

1. **网络一律不在 GUI 线程里等**（`Async`）。同步 HTTP 直接写在按钮回调里，
   服务端慢一次界面就假死，用户会以为程序崩了。
2. **所有外部字符串按纯文本显示**：`setPlainText` / `QLabel.setText` + 转义，
   绝不把服务端返回的内容当富文本渲染。
3. **写操作先确认**（`confirm_then`）：删除、恢复默认、花 token 的动作都要问一句。
"""

from __future__ import annotations

import html
import logging
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.util import one_line

logger = logging.getLogger("qqbot.desktop.gui")

# ---------------------------------------------------------------- 配色

PALETTE = {
    "bg": "#f3f6fb",
    "panel": "#ffffff",
    "panel2": "#e8edf5",
    "fg": "#1d2939",
    "dim": "#667085",
    "accent": "#3568c8",
    "ok": "#16845b",
    "warn": "#a96100",
    "err": "#c53945",
}
# 状态级别 → 颜色。两种写法都收：`err` 与 `error` 指的是同一件事，
# 但页面里两种都出现过，与其到处改成一种，不如在这里收敛成别名。
LEVEL_COLOR = {
    "info": PALETTE["fg"],
    "ok": PALETTE["ok"],
    "warn": PALETTE["warn"],
    "warning": PALETTE["warn"],
    "err": PALETTE["err"],
    "error": PALETTE["err"],
}

STYLESHEET = f"""
QWidget {{ background: {PALETTE['bg']}; color: {PALETTE['fg']}; font-size: 13px;
           font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; }}
QMainWindow {{ background: {PALETTE['bg']}; }}
QFrame {{ border-color: {PALETTE['panel2']}; }}
QListWidget {{ background: {PALETTE['panel']}; border: 1px solid {PALETTE['panel2']};
               outline: none; padding: 6px; border-radius: 8px; }}
QListWidget::item {{ padding: 8px 10px; border-radius: 6px; margin: 1px 0; }}
QListWidget::item:hover {{ background: {PALETTE['panel2']}; }}
QListWidget::item:selected {{ background: {PALETTE['accent']}; color: #ffffff; }}
/* 左侧导航是 QTreeWidget（插件页面挂在「插件」节点下、目录可收起）。
   这几条要盖住下面那条通用的 `QTableWidget, QTreeWidget, ...` 表格样式 —— `#nav` 更具体。 */
QTreeWidget#nav {{ background: {PALETTE['panel']}; border: 1px solid {PALETTE['panel2']};
                   outline: none; padding: 4px; border-radius: 8px; }}
QTreeWidget#nav::item {{ padding: 7px 6px; border-radius: 6px; margin: 1px 0; }}
QTreeWidget#nav::item:hover {{ background: {PALETTE['panel2']}; }}
QTreeWidget#nav::item:selected {{ background: {PALETTE['accent']}; color: #ffffff; }}
QTreeWidget#nav::branch {{ background: transparent; }}
QTabWidget::pane {{ background: {PALETTE['panel']}; border: 1px solid {PALETTE['panel2']};
                     border-radius: 8px; top: -1px; }}
QTabBar::tab {{ padding: 8px 14px; background: {PALETTE['panel2']};
                border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 3px; }}
QTabBar::tab:selected {{ background: {PALETTE['panel']}; color: {PALETTE['accent']};
                         font-weight: 600; }}
QPushButton {{ background: {PALETTE['panel']}; border: 1px solid #d0d7e2;
               border-radius: 6px; padding: 7px 12px; min-height: 18px; }}
QPushButton:hover {{ background: #edf3ff; border-color: {PALETTE['accent']}; }}
QPushButton:pressed {{ background: #dce8ff; }}
QPushButton:disabled {{ color: #98a2b3; background: #f2f4f7; border-color: #eaecf0; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {PALETTE['panel']}; border: 1px solid #d0d7e2;
    border-radius: 6px; padding: 6px 8px; selection-background-color: {PALETTE['accent']}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus,
QTextEdit:focus, QComboBox:focus {{ border: 1px solid {PALETTE['accent']}; }}
QComboBox QAbstractItemView {{ background: {PALETTE['panel']}; border: 1px solid {PALETTE['panel2']};
                               selection-background-color: #e4edff; selection-color: {PALETTE['fg']}; }}
QTableWidget, QTreeWidget, QListWidget#plain {{ background: {PALETTE['panel']};
    border: 1px solid {PALETTE['panel2']}; gridline-color: #edf0f5; alternate-background-color: #f8faff; }}
QTableWidget::item:selected, QTreeWidget::item:selected {{ background: #e4edff; color: {PALETTE['fg']}; }}
QHeaderView::section {{ background: #f1f4f9; color: #475467; padding: 7px 8px;
                         border: none; border-bottom: 1px solid {PALETTE['panel2']}; font-weight: 600; }}
QGroupBox {{ background: {PALETTE['panel']}; border: 1px solid {PALETTE['panel2']};
             border-radius: 8px; margin-top: 12px; padding: 12px 10px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px;
                    color: {PALETTE['dim']}; font-weight: 600; }}
QStatusBar {{ background: {PALETTE['panel']}; color: {PALETTE['dim']};
              border-top: 1px solid {PALETTE['panel2']}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #c5ceda; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #98a6ba; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QCheckBox {{ spacing: 7px; }}
QToolTip {{ background: #1d2939; color: #ffffff; border: 1px solid #344054;
            padding: 5px 7px; border-radius: 4px; }}
QSplitter::handle {{ background: {PALETTE['bg']}; }}
QSplitter::handle:hover {{ background: #dbe6f6; }}
QProgressBar {{ background: {PALETTE['panel2']}; border: none; border-radius: 4px;
                text-align: center; min-height: 8px; }}
QProgressBar::chunk {{ background: {PALETTE['accent']}; border-radius: 4px; }}
"""


def esc(text: Any) -> str:
    """任何要拼进富文本的外部字符串都先过这里（默认按纯文本处理）。"""
    return html.escape(str(text if text is not None else ""), quote=True)


def badge(text: str, *, level: str = "info") -> str:
    color = LEVEL_COLOR.get(level, PALETTE["fg"])
    return f"<span style='color:{color};font-weight:600'>{esc(text)}</span>"


# 折行 + 截断的实现只有一份，在 `core/util.py`（那一层不依赖 Qt，所以能被两边共用）。
elide = one_line


# ---------------------------------------------------------------- 异步


@dataclass
class TaskResult:
    ok: bool
    value: Any = None
    #: 成功时为 None；失败时是**在 GUI 线程重建**的异常，文本形如
    #: `"BotApiError: 连接被拒绝"`（worker 只送文本过来，见 `Async.run`）。
    error: BaseException | None = None
    traceback_text: str = ""

    @property
    def message(self) -> str:
        if self.error is None:
            return ""
        return elide(self.error, 400)


class _TaskSignals(QtCore.QObject):
    """后台任务 → GUI 线程的信号。

    签名只用 Qt 已注册的类型（`bool` / `object` / `str`），并且**显式声明参数类型**：
    `Signal(object)` 这种不带参数的写法在跨线程队列投递里更容易出意外，
    显式写清楚没有代价。
    """

    done = QtCore.Signal(bool, object, str)
    failed = QtCore.Signal(bool, object, str, str)
    progress = QtCore.Signal(str)


class _Task(QtCore.QRunnable):
    """在 QThreadPool 里跑一个阻塞函数，结果用信号投回 GUI 线程。

    为什么不用 `asyncio`/线程池外边直接调：Qt 要求"碰 widget 的代码在 GUI 线程"，
    所以后台线程**只允许**算数据、发信号，一律不许碰任何控件。

    ⚠ **必须由调用方持有引用直到任务结束**（`Async` 用 `self._running` 集合做这件事）。
    踩过的坑（2026-10-02，用户实测报回"启动隧道无法自动填入"）：
    `QThreadPool.start(task)` **不会**在 Python 侧持有 `task` 的强引用，
    于是 `run()` 一返回、`task` 离开局部作用域就被 GC —— `QRunnable` 与其
    `signals` 一起消失，槽函数**一次都不会被调用**，而且**没有任何报错**。
    表现是"后台请求明明发出去了，界面却永远停在旧状态"，极具误导性。
    """

    def __init__(self, fn: Callable[[], Any], *, label: str = "") -> None:
        super().__init__()
        self.fn = fn
        self.label = label
        self.signals = _TaskSignals()
        self.setAutoDelete(True)

    @QtCore.Slot()
    def run(self) -> None:  # noqa: D102 - QRunnable 的接口
        if self.label:
            try:
                self.signals.progress.emit(self.label)
            except RuntimeError:
                pass
        try:
            value = self.fn()
        except BaseException as exc:  # noqa: BLE001 - 后台异常必须转成信号，不能吞
            self.signals.failed.emit(
                False, None, f"{type(exc).__name__}: {exc}",
                "".join(traceback.format_exception(exc))[-2000:],
            )
        else:
            self.signals.done.emit(True, value, "")


class Async(QtCore.QObject):
    """页面用来跑后台任务的混入小工具。

    **持有正在运行的任务引用**：`QThreadPool.start()` 不会在 Python 侧保活 `QRunnable`，
    不自己收着就会被 GC（见 `_Task` 的说明）。任务结束时从集合里摘掉。
    """

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QtCore.QThreadPool.globalInstance()
        self._busy = 0
        self._running: set[Any] = set()

    @property
    def busy(self) -> bool:
        return self._busy > 0

    def run(
        self,
        fn: Callable[[], Any],
        *,
        on_done: Callable[[TaskResult], None],
        on_progress: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        task = _Task(fn)
        self._running.add(task)          # ← 保活，直到回调跑完
        self._busy += 1
        if on_start is not None:
            on_start()

        def _release() -> None:
            self._running.discard(task)
            if on_finish is not None:
                on_finish()

        def _done(ok: bool, value: Any, _err: str) -> None:
            self._busy = max(0, self._busy - 1)
            try:
                on_done(TaskResult(True, value))
            finally:
                _release()

        def _failed(ok: bool, value: Any, err: str, tb: str) -> None:
            self._busy = max(0, self._busy - 1)
            # worker 那边只把异常送成 `"类型: 消息"` 文本（跨线程队列不保证自定义对象），
            # 这里在 GUI 线程重建异常对象，让 `TaskResult.error` 依旧可读可抛，
            # 上层（`Page._handle` / `show_error`）不用改。
            result = TaskResult(False, value, RuntimeError(err), traceback_text=tb)
            try:
                on_done(result)
            finally:
                _release()

        task.signals.done.connect(_done)
        task.signals.failed.connect(_failed)
        if on_progress is not None:
            task.signals.progress.connect(on_progress)
        self._pool.start(task)


# ---------------------------------------------------------------- 控件工厂


def busy_button(text: str, on_click: Callable[[], None], *, tip: str = "") -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text)
    button.clicked.connect(lambda: on_click())
    if tip:
        button.setToolTip(tip)
    return button


def plain_text_edit(text: str = "", *, readonly: bool = True) -> QtWidgets.QPlainTextEdit:
    widget = QtWidgets.QPlainTextEdit(text)
    widget.setReadOnly(readonly)
    widget.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
    font = QtGui.QFont("Consolas")
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    widget.setFont(font)
    return widget


def table(headers: list[str], *, stretch_last: bool = True) -> QtWidgets.QTableWidget:
    widget = QtWidgets.QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.verticalHeader().setVisible(False)
    widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    header = widget.horizontalHeader()
    header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
    if stretch_last:
        header.setStretchLastSection(True)
    return widget


def read_only_item(text: Any, *, tip: str = "") -> QtWidgets.QTableWidgetItem:
    item = QtWidgets.QTableWidgetItem(str(text if text is not None else ""))
    item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
    if tip:
        item.setToolTip(tip)
    return item


def scrollable(inner: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(inner)
    return area


def label(text: str, *, level: str = "info", wrap: bool = False) -> QtWidgets.QLabel:
    widget = QtWidgets.QLabel(f"<span style='color:{LEVEL_COLOR.get(level, PALETTE['fg'])}'>{esc(text)}</span>")
    # 外部字符串一律按纯文本渲染（`setTextFormat` 明确关掉富文本解释）。
    widget.setTextFormat(QtCore.Qt.TextFormat.RichText)
    widget.setWordWrap(wrap)
    return widget


def hline() -> QtWidgets.QFrame:
    line = QtWidgets.QFrame()
    line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
    line.setStyleSheet(f"color:{PALETTE['panel2']}")
    return line


def confirm(parent: QtWidgets.QWidget, title: str, message: str, *, danger: bool = False) -> bool:
    """统一确认框。**默认按钮是"取消"** —— 手快连按回车不该删掉东西。"""
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QtWidgets.QMessageBox.Icon.Warning if danger else QtWidgets.QMessageBox.Icon.Question)
    yes = box.addButton("确定", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton("取消", QtWidgets.QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(no)
    box.exec()
    return box.clickedButton() is yes


@dataclass
class Toast:
    """给状态条用的简短反馈（避免到处弹模态框）。"""

    level: str = "info"
    text: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
