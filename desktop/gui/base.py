"""页面基类与共用构件。

`Page` 只提供三件事：托管的刷新生命周期、忙碌状态、统一报错。
**不提供**连接管理器之外的任何入口 —— 页面拿不到"直接改配置文件"的能力，
这条边界和插件那边是同一个理由。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.apiclient import BotApiError
from ..core.connection import ConnectionManager
from ..sdk.api import CancellationToken
from .widgets import Async, PALETTE, TaskResult, label

_logger = logging.getLogger("qqbot.desktop.gui")


class Page(QtWidgets.QWidget):
    """所有内置页面的基类。"""

    title = "页面"
    subtitle = ""

    #: 页面数据就绪后再广播，用于顶部状态条同步
    refreshed = QtCore.Signal()
    busy_changed = QtCore.Signal(bool)

    def __init__(self, manager: ConnectionManager, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.async_ = Async(self)
        self._lifecycle_token = CancellationToken()
        self._pending_tasks = 0
        self._loaded = False

    @property
    def busy(self) -> bool:
        return self._pending_tasks > 0

    # -- 便捷入口 --------------------------------------------------------
    def client(self):  # noqa: ANN201 - 返回 ApiClient
        return self.manager.client()

    def toast(self, message: str, *, level: str = "info") -> None:
        window = self.window()
        if hasattr(window, "notify"):
            window.notify(message, level=level)  # type: ignore[attr-defined]

    def show_error(self, exc: BaseException | None = None, *, message: str = "") -> None:
        text = message or str(exc or "未知错误")
        hint = getattr(exc, "hint", "")
        if hint:
            text = f"{text}\n\n建议：{hint}"
        self.toast(text, level="error")

    def run_task(
        self,
        fn: Callable[[], Any],
        *,
        on_done: Callable[[TaskResult], None] | None = None,
        busy_text: str = "处理中…",
    ) -> None:
        """后台跑一件阻塞事（网络/隧道），完成后回 GUI 线程。"""
        self.toast(busy_text, level="info")
        token = self._lifecycle_token
        self._pending_tasks += 1
        self.busy_changed.emit(True)

        def complete(result: TaskResult) -> None:
            if token.cancelled:
                return
            self._pending_tasks = max(0, self._pending_tasks - 1)
            if not result.ok:
                self._loaded = False
            try:
                self._handle(result, on_done)
            except Exception as exc:  # noqa: BLE001 - 回调里的错不能静默吞掉
                # 踩过：控件范围溢出（QQ 号超过 Qt 的 32 位）让 `_apply` 抛异常，
                # 页面永远停在"正在读取…"，用户只看到空白。这里至少把原因弹出来。
                _logger.exception("页面 %s 的完成回调失败", self.title)
                self.show_error(exc)
            finally:
                self.busy_changed.emit(self.busy)

        self.async_.run(
            fn,
            on_done=complete,
        )

    def _handle(self, result: TaskResult, on_done: Callable[[TaskResult], None] | None) -> None:
        if not result.ok:
            self.show_error(result.error)
            if result.traceback_text:
                _logger.debug("后台任务失败：%s", result.traceback_text)
        if on_done is not None:
            on_done(result)

    # -- 生命周期 --------------------------------------------------------
    def load(self, *, force: bool = False) -> None:
        """首次显示或用户点刷新时调用。子类覆盖 `_load`。"""
        if self._pending_tasks or (self._loaded and not force):
            return
        self._loaded = True
        try:
            self._load()
        except Exception:
            self._loaded = False
            raise

    def invalidate(self) -> None:
        """切目标时调用：数据全部作废，下次显示重新拉。"""
        self._loaded = False
        self._lifecycle_token.cancel("切换了目标")
        self._lifecycle_token = CancellationToken()
        self._pending_tasks = 0
        self.busy_changed.emit(False)
        self._on_target_changed()

    def _load(self) -> None:  # pragma: no cover - 子类实现
        pass

    def _on_target_changed(self) -> None:  # pragma: no cover - 子类可选
        pass


class StateSlice:
    """`GET /api/state` 的取用助手。

    服务端把几乎所有东西都塞在一个聚合响应里（`webui.py:927-978`），
    于是每个页面都得从同一份数据里挖自己那一片。把它收敛在这里，
    页面就不用各自写 `state.get("memory", {}).get("facts", [])` 这种长链。
    """

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.raw: dict[str, Any] = dict(state or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def settings_groups(self) -> list[dict[str, Any]]:
        return list(self.raw.get("settings") or [])

    @property
    def models(self) -> dict[str, Any]:
        return dict(self.raw.get("models") or {})

    @property
    def models_editor(self) -> str:
        return str(self.raw.get("models_editor") or "")

    @property
    def persona(self) -> dict[str, Any]:
        return dict(self.raw.get("persona") or {})

    @property
    def memory(self) -> dict[str, Any]:
        return dict(self.raw.get("memory") or {})

    @property
    def image(self) -> dict[str, Any]:
        return dict(self.raw.get("image") or {})

    @property
    def status(self) -> dict[str, Any]:
        return dict(self.raw.get("status") or {})

    @property
    def stickers(self) -> list[dict[str, Any]]:
        return list(self.raw.get("stickers_list") or [])


@dataclass
class Field:
    """参数页的一个控件。`kind` 对应 `settings.describe()` 的 `kind` 字段。"""

    spec: dict[str, Any]
    widget: QtWidgets.QWidget
    editor: Callable[[], Any]
    setter: Callable[[Any], None]


#: Qt 的整数控件（QSpinBox/QIntValidator）只有 32 位；QQ 号会超过这个范围。
_INT32_MIN = -2_147_483_648
_INT32_MAX = 2_147_483_647


def _read_int(widget: QtWidgets.QLineEdit) -> int:
    """把大整数字段的文本转成 int；空串/坏输入按 0 处理（提交时服务端还会再夹紧）。"""
    try:
        return int(str(widget.text()).strip() or 0)
    except ValueError:
        return 0


def build_field(spec: dict[str, Any], on_commit: Callable[[str, Any], None]) -> Field:
    """按 `kind` 造控件。密钥类用密码框（**只回显掩码**）。"""
    kind = str(spec.get("kind") or "str")
    key = str(spec.get("key") or "")
    secret = bool(spec.get("secret"))
    choices = [str(x) for x in (spec.get("choices") or [])]
    minimum = spec.get("min")
    maximum = spec.get("max")

    if kind == "bool":
        widget = QtWidgets.QCheckBox()
        widget.setChecked(bool(spec.get("value")))
        widget.toggled.connect(lambda value, k=key: on_commit(k, bool(value)))
        return Field(spec, widget, lambda: bool(widget.isChecked()), lambda v: widget.setChecked(bool(v)))

    if kind == "int" and choices:
        widget = QtWidgets.QComboBox()
        widget.addItems(choices)
        _select(widget, spec.get("value"))
        widget.currentTextChanged.connect(lambda value, k=key: on_commit(k, value))
        return Field(spec, widget, lambda: widget.currentText(), lambda v: _select(widget, v))

    if kind in ("int", "float") and not choices:
        if kind == "int":
            lower = int(minimum) if isinstance(minimum, (int, float)) else -2_000_000_000
            upper = int(maximum) if isinstance(maximum, (int, float)) else 2_000_000_000
            value = int(spec.get("value") or 0)
            # QQ 号常是 10 位数（如 100000001），超出 Qt 控件的 32 位整数范围。
            # `QSpinBox.setValue()` 遇到这种值会抛 OverflowError，**整页建不出来** ——
            # 表现是「基础与身份」页空白。这类字段改用文本框，保存时再转 int。
            if not (
                _INT32_MIN <= lower <= upper <= _INT32_MAX
                and _INT32_MIN <= value <= _INT32_MAX
            ):
                widget = QtWidgets.QLineEdit(str(value))
                widget.setValidator(
                    QtGui.QRegularExpressionValidator(
                        QtCore.QRegularExpression(r"-?\d{0,19}"), widget
                    )
                )
                widget.editingFinished.connect(
                    lambda k=key, w=widget: on_commit(k, _read_int(w))
                )
                return Field(
                    spec,
                    widget,
                    lambda: _read_int(widget),
                    lambda v: widget.setText(str(int(v or 0))),
                )
            widget = QtWidgets.QSpinBox()
            widget.setRange(lower, upper)
            widget.setValue(value)
        else:
            widget = QtWidgets.QDoubleSpinBox()
            widget.setDecimals(4)
            widget.setRange(
                float(minimum) if isinstance(minimum, (int, float)) else -1e9,
                float(maximum) if isinstance(maximum, (int, float)) else 1e9,
            )
            widget.setSingleStep(0.05)
            widget.setValue(float(spec.get("value") or 0.0))
        widget.setKeyboardTracking(False)  # 边打字边触发会把服务端刷爆
        widget.editingFinished.connect(
            lambda k=key, w=widget, kd=kind: on_commit(k, w.value())
        )
        return Field(
            spec,
            widget,
            lambda: widget.value(),
            lambda v: widget.setValue(type(widget.value())(v or 0)),
        )

    if choices:
        widget = QtWidgets.QComboBox()
        widget.setEditable(True)
        widget.addItems(choices)
        _select(widget, spec.get("value"))
        widget.currentTextChanged.connect(lambda value, k=key: on_commit(k, value))
        return Field(spec, widget, lambda: widget.currentText(), lambda v: _select(widget, v))

    widget = QtWidgets.QLineEdit(str(spec.get("value") if spec.get("value") is not None else ""))
    if secret:
        widget.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        widget.setPlaceholderText("已保存（掩码）")
    widget.editingFinished.connect(lambda k=key, w=widget: on_commit(k, w.text()))
    return Field(spec, widget, lambda: widget.text(), lambda v: widget.setText(str(v)))


def _select(combo: QtWidgets.QComboBox, value: Any) -> None:
    text = "" if value is None else str(value)
    index = combo.findText(text)
    if index >= 0:
        combo.setCurrentIndex(index)
    elif combo.isEditable():
        combo.setCurrentText(text)


def secret_notice(manager: ConnectionManager) -> QtWidgets.QLabel:
    """把"令牌存在哪"这件事摆在页面上 —— 用户有权知道。"""
    backend = manager.creds.describe()
    if manager.creds.degraded:
        text = f"令牌存储：{backend}（**降级**：建议改用系统凭据或仅本次会话）"
        level = "warn"
    else:
        text = f"令牌存储：{backend}"
        level = "info"
    return label(text, level=level, wrap=True)
