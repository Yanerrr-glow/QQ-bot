"""插件宿主到 Qt 的适配层。"""
from __future__ import annotations

from typing import Any, Callable, TYPE_CHECKING
from PySide6 import QtWidgets
from ..sdk.api import UiBridge
from .widgets import TaskResult

if TYPE_CHECKING:
    from .main_window import MainWindow


class QtUiBridge(UiBridge):
    """把宿主的 UI 能力交给插件（插件拿不到主窗口对象，只能调这几个方法）。"""

    def __init__(self, window: "MainWindow") -> None:
        self._window = window

    def toast(self, message: str, *, level: str = "info") -> None:
        self._window.notify(str(message), level=level)

    def confirm(self, title: str, message: str) -> bool:
        return QtWidgets.QMessageBox.question(
            self._window, str(title), str(message),
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok

    def open_dialog(self, title: str, content: object) -> None:
        window = QtWidgets.QDialog(self._window)
        window.setWindowTitle(str(title))
        window.resize(720, 520)
        layout = QtWidgets.QVBoxLayout(window)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        if hasattr(content, "toPlainText"):
            text.setPlainText(content.toPlainText())  # type: ignore[attr-defined]
        else:
            text.setPlainText(str(content))
        layout.addWidget(text)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(window.reject)
        buttons.accepted.connect(window.accept)
        layout.addWidget(buttons)
        window.exec()

    def run_async(
        self,
        work: Callable[[], Any],
        *,
        on_done: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """后台跑一件阻塞事（插件页面取数、批量请求），完成后回 GUI 线程。

        插件手里没有 QThreadPool，这是它唯一"不卡界面"的正路。
        `work` 里**禁止碰控件** —— Qt 要求碰 widget 的代码只在 GUI 线程跑。
        """
        def _done(result: TaskResult) -> None:
            if result.ok:
                on_done(result.value)
            elif on_error is not None:
                on_error(result.message)
            else:
                self.toast(f"后台任务失败：{result.message}", level="error")

        self._window._async.run(work, on_done=_done)


