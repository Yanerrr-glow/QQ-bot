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
import re
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.util import one_line
from . import theme

logger = logging.getLogger("qqbot.desktop.gui")

# ---------------------------------------------------------------- 配色 / 样式
# **令牌与 QSS 的唯一来源是 `theme.py`**。这里保留旧名字做兼容别名：
# 页面里已有大量 `PALETTE['dim']` / `from .widgets import STYLESHEET` 的引用，
# 改名会直接 ImportError。新代码请直接 `from . import theme`。
PALETTE = theme.PALETTE
STYLESHEET = theme.STYLESHEET

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


def esc(text: Any) -> str:
    """任何要拼进富文本的外部字符串都先过这里（默认按纯文本处理）。"""
    return html.escape(str(text if text is not None else ""), quote=True)


def badge(text: str, *, level: str = "info") -> str:
    color = LEVEL_COLOR.get(level, PALETTE["fg"])
    return f"<span style='color:{color};font-weight:600'>{esc(text)}</span>"


# 只给**本地固定的说明文案**用（`features/catalog.py` 里的 description 是带
# `**粗体**` / `` `键名` `` 的 Markdown）。服务端数据一律仍走 `esc()` / `label()`，
# 绝不在这里解释富文本 —— 那条边界由 `widgets.label` 与各页面自己守着。
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE = re.compile(r"`([^`\n]+)`")
_CODE_SPAN = (
    "<span style='font-family:Consolas,\"Cascadia Mono\",monospace;"
    f"color:{PALETTE['dim']}'>\\1</span>"
)


def local_rich(text: str) -> str:
    """本地说明文案的**最小** Markdown 处理：先转义，再只认 `**粗体**` 与反引号代码。"""
    out = esc(text)
    out = _MD_BOLD.sub(r"<b>\1</b>", out)
    out = _MD_CODE.sub(_CODE_SPAN, out)
    return out


def description_label(text: str, *, wrap: bool = True) -> QtWidgets.QLabel:
    """功能说明这类**本地固定文案**的富文本标签（外部字符串不要用这个）。"""
    widget = QtWidgets.QLabel(local_rich(text))
    widget.setObjectName("descriptionText")
    widget.setTextFormat(QtCore.Qt.TextFormat.RichText)
    widget.setWordWrap(wrap)
    widget.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


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


#: 按钮角色。QSS 用 `QPushButton[variant="…"]` 选择器上色，**不逐页写 inline stylesheet**。
BUTTON_VARIANTS = ("", "primary", "danger", "ghost")


def set_variant(button: QtWidgets.QPushButton, kind: str = "secondary") -> QtWidgets.QPushButton:
    """给已有按钮换角色（`primary` / `danger` / `ghost` / `secondary`）。

    QSS 的动态属性选择器不会在属性变化时自动重绘，所以这里显式 unpolish/polish。
    """
    value = "" if kind in ("", "secondary", "default") else str(kind)
    if value not in BUTTON_VARIANTS:
        raise ValueError(f"未知按钮角色：{kind!r}")
    button.setProperty("variant", value)
    style = button.style()
    style.unpolish(button)
    style.polish(button)
    button.update()
    return button


def _action_button(
    text: str,
    on_click: Callable[[], None],
    *,
    tip: str = "",
    variant: str = "",
) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton(text)
    button.clicked.connect(lambda: on_click())
    button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    # QSS 的 min-width 是内容盒（42 + padding 28 + 边框 2 = 72）；这里再钉一次实际宽度，
    # 页面代码就算误设了更小的 maxWidth 也不会把文字挤掉（按钮文字截断是踩过的）。
    button.setMinimumWidth(theme.BUTTON_MIN_WIDTH)
    if variant:
        set_variant(button, variant)
    if tip:
        button.setToolTip(tip)
    return button


def primary_button(text: str, on_click: Callable[[], None], *, tip: str = "") -> QtWidgets.QPushButton:
    """**每页唯一的主操作**：强调色实底 + 白字。"""
    return _action_button(text, on_click, tip=tip, variant="primary")


def danger_button(text: str, on_click: Callable[[], None], *, tip: str = "") -> QtWidgets.QPushButton:
    """危险操作：删除 / 清除 / 重置 / 花 token。错误色描边 + 文字。"""
    return _action_button(text, on_click, tip=tip, variant="danger")


def ghost_button(text: str, on_click: Callable[[], None], *, tip: str = "") -> QtWidgets.QPushButton:
    """低强调操作（跳转、辅助入口）：透明底、无边框，hover 才出现浅底。"""
    return _action_button(text, on_click, tip=tip, variant="ghost")


def busy_button(text: str, on_click: Callable[[], None], *, tip: str = "") -> QtWidgets.QPushButton:
    return _action_button(text, on_click, tip=tip)


def toolbar_note(text: str) -> QtWidgets.QLabel:
    """工具条左侧的弱色说明（12px、次要色）。"""
    note = QtWidgets.QLabel(str(text))
    note.setObjectName("toolbarNote")
    note.setWordWrap(False)
    return note


def toolbar(description: str = "") -> QtWidgets.QHBoxLayout:
    """统一工具条：**左说明、右操作**，组内间距 8。

    返回的布局已经放了左侧说明与一个 stretch；调用方只需继续 `addWidget`
    右对齐的操作按钮。危险按钮组前面自己 `addSpacing(theme.SPACE["lg"])` 隔开。
    """
    bar = QtWidgets.QHBoxLayout()
    bar.setContentsMargins(0, 0, 0, 0)
    bar.setSpacing(theme.SPACE["sm"])
    if description:
        bar.addWidget(toolbar_note(description))
    bar.addStretch(1)
    return bar


def section_separator() -> QtWidgets.QFrame:
    """竖分隔线（危险操作与普通操作之间用；不需要时用 `addSpacing(16)` 也行）。"""
    line = QtWidgets.QFrame()
    line.setFrameShape(QtWidgets.QFrame.Shape.VLine)
    line.setFixedWidth(1)
    line.setStyleSheet(f"color:{PALETTE['border']}; background:{PALETTE['border']}; border:none;")
    return line


def style_form(form: QtWidgets.QFormLayout, *, label_width: int = theme.FORM_LABEL_WIDTH) -> None:
    """表单统一：label 右对齐、固定宽 110、行距 10、字段可伸展。"""
    form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
    form.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
    form.setHorizontalSpacing(theme.SPACE["md"])
    form.setVerticalSpacing(10)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.DontWrapRows)
    for row in range(form.rowCount()):
        item = form.itemAt(row, QtWidgets.QFormLayout.ItemRole.LabelRole)
        widget = item.widget() if item is not None else None
        if widget is not None:
            widget.setMinimumWidth(label_width)
            widget.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)


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
    widget.verticalHeader().setDefaultSectionSize(theme.TABLE_ROW_HEIGHT)
    widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    header = widget.horizontalHeader()
    header.setFixedHeight(theme.TABLE_HEADER_HEIGHT)
    header.setDefaultAlignment(
        QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
    )
    header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
    if stretch_last and headers:
        # `setStretchLastSection(True)` 在「所有列都是 ResizeToContents」时不会真的拉伸
        # （实测：表宽 824、最后一列仍按内容 411，右侧留下大片空白）。必须把最后一列
        # 的 resize mode 也改成 Stretch，内容才会横向铺满。
        header.setStretchLastSection(True)
        header.setSectionResizeMode(
            len(headers) - 1, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
    return widget


def fit_table(widget: QtWidgets.QTableWidget) -> None:
    """按内容定列宽，并**重新下令**让最后一列拉伸铺满。

    踩过的坑：`resizeColumnsToContents()` 会把最后一列也按内容定宽，把
    `Stretch` 的实际宽度打回去；必须在 resize 之后再设一次 Stretch，
    否则表格右侧会留一条几百像素的空白（用户截图里报过）。
    """
    header = widget.horizontalHeader()
    widget.resizeColumnsToContents()
    if widget.columnCount() and header.stretchLastSection():
        header.setSectionResizeMode(
            widget.columnCount() - 1, QtWidgets.QHeaderView.ResizeMode.Stretch
        )


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
    line.setFixedHeight(1)
    line.setStyleSheet(f"color:{PALETTE['border']}; background:{PALETTE['border']}; border:none;")
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
