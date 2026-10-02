"""页面基类与共用构件。

`Page` 只提供三件事：托管的刷新生命周期、忙碌状态、统一报错。
**不提供**连接管理器之外的任何入口 —— 页面拿不到"直接改配置文件"的能力，
这条边界和插件那边是同一个理由。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from PySide6 import QtCore, QtWidgets

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

    def __init__(self, manager: ConnectionManager, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.async_ = Async(self)
        self.token = CancellationToken()
        self._loaded = False

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
        self.async_.run(
            fn,
            on_done=lambda result: self._handle(result, on_done),
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
        if self._loaded and not force:
            return
        self._loaded = True
        self._load()

    def invalidate(self) -> None:
        """切目标时调用：数据全部作废，下次显示重新拉。"""
        self._loaded = False
        self.token.cancel("切换了目标")
        self.token = CancellationToken()
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
            widget = QtWidgets.QSpinBox()
            widget.setRange(
                int(minimum) if isinstance(minimum, (int, float)) else -2_000_000_000,
                int(maximum) if isinstance(maximum, (int, float)) else 2_000_000_000,
            )
            widget.setValue(int(spec.get("value") or 0))
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
        widget.setPlaceholderText("已保存（显示为掩码；改这里才会覆盖）")
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
