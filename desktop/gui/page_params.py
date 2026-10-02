"""参数页：全部设置项由 `settings.describe()` 动态生成，页面上没有一个硬编码的键。

这一点很关键：WebUI 加一个参数（`settings.py` 的 `_SPECS` 加一行）桌面端要自动跟上，
否则两边迟早对不上。所以这里的每一个控件都是"按 `kind` 现造"的。

密钥类（`secret=True`）的行为与 WebUI 一致：服务端**只回掩码**，
桌面端也把掩码原样回写 —— `settings.py:688` 会识别 `***` 并保留原值。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtWidgets

from .base import Field, Page, build_field
from .widgets import badge, esc, label


class ParamsPage(Page):
    title = "参数"
    subtitle = "全部运行参数（动态读取；单项保存，改完立即生效）"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._fields: dict[str, Field] = {}
        self._groups: list[QtWidgets.QGroupBox] = []
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("刷新")
        self.btn_refresh.clicked.connect(lambda: self.load(force=True))
        self.btn_expand = QtWidgets.QPushButton("全部展开")
        self.btn_expand.clicked.connect(lambda: self._set_all_groups(True))
        self.btn_collapse = QtWidgets.QPushButton("全部收起")
        self.btn_collapse.clicked.connect(lambda: self._set_all_groups(False))
        self.btn_reset = QtWidgets.QPushButton("恢复 .env 默认")
        self.btn_reset.setToolTip("清掉控制台写入的覆盖值，全部回到 .env（不影响人格与记忆）")
        self.btn_reset.clicked.connect(self._reset_all)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("过滤参数名/说明…")
        self.search.textChanged.connect(self._apply_filter)
        for widget in (self.btn_refresh, self.btn_expand, self.btn_collapse, self.btn_reset):
            bar.addWidget(widget)
        bar.addWidget(label("恢复默认不影响人格与记忆"))
        bar.addStretch(1)
        bar.addWidget(self.search, 2)
        outer.addLayout(bar)

        self.hint = label("正在读取参数…")
        outer.addWidget(self.hint)

        self.container = QtWidgets.QWidget()
        self.form = QtWidgets.QVBoxLayout(self.container)
        self.form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.container)
        outer.addWidget(area, 1)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: list(self.client().state().get("settings") or []),
            on_done=self._apply,
            busy_text="正在读取参数…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("参数读取失败", level="err"))
            return
        groups: list[dict[str, Any]] = list(result.value or [])
        self._rebuild(groups)
        total = sum(len(g.get("items") or []) for g in groups)
        self.hint.setText(
            f"{esc(len(groups))} 组 / {esc(total)} 个参数 · "
            "修改后立即保存到服务端的 <code>data/runtime/settings.json</code>"
        )
        self.toast(f"已载入 {total} 个参数", level="ok")
        self.refreshed.emit()

    def _rebuild(self, groups: list[dict[str, Any]]) -> None:
        while self.form.count():
            item = self.form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._fields.clear()
        self._groups.clear()

        for group in groups:
            box = QtWidgets.QGroupBox(f"{group.get('group')}（{len(group.get('items') or [])}）")
            box.setCheckable(True)
            box.setChecked(False)  # 默认收起：上百个参数全展开没人找得到
            grid = QtWidgets.QGridLayout(box)
            for row, spec in enumerate(group.get("items") or []):
                self._add_field(grid, row, spec)
            self.form.addWidget(box)
            self._groups.append(box)
        self.form.addStretch(1)

    def _add_field(self, grid: QtWidgets.QGridLayout, row: int, spec: dict[str, Any]) -> None:
        key = str(spec.get("key") or "")
        field = build_field(spec, self._commit)
        self._fields[key] = field

        title = QtWidgets.QLabel(f"<b>{esc(spec.get('label') or key)}</b> <span style='color:#8b949e'>{esc(key)}</span>")
        title.setTextFormat(QtCore.Qt.TextFormat.RichText)
        grid.addWidget(title, row, 0)

        holder = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(holder)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(field.widget)
        reset = QtWidgets.QPushButton("默认")
        reset.setToolTip(
            f"恢复 .env 默认值：{spec.get('env_value') if spec.get('env_value') is not None else spec.get('default')}"
        )
        reset.clicked.connect(lambda _=False, s=spec: self._reset_one(s))
        row_layout.addWidget(reset)
        row_layout.addStretch(1)
        grid.addWidget(holder, row, 1)

        hint_bits: list[str] = []
        if spec.get("hint"):
            hint_bits.append(str(spec["hint"]))
        rng = _range_text(spec)
        if rng:
            hint_bits.append(rng)
        if spec.get("secret"):
            hint_bits.append("密钥类：显示为掩码，原样保存不会覆盖服务端原值")
        if spec.get("choices"):
            hint_bits.append("可选：" + "/".join(map(str, spec["choices"])))
        hint = QtWidgets.QLabel(esc(" · ".join(hint_bits)) if hint_bits else "")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b949e")
        grid.addWidget(hint, row, 2)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 2)
        title.setToolTip(key)

    # ------------------------------------------------------------ 动作
    def _commit(self, key: str, value: Any) -> None:
        field = self._fields.get(key)
        if field is None:
            return
        spec = field.spec
        if spec.get("secret") and str(value) == "***":
            return  # 掩码没动过：不发请求（服务端也会忽略，但少一次往返）
        if value == spec.get("value"):
            return

        def _save() -> dict[str, Any]:
            return self.client().set_settings(**{key: value})

        def _done(result) -> None:  # noqa: ANN001
            if not result.ok:
                # 失败要**把界面改回服务端的真实值**，否则界面会撒谎。
                field.setter(spec.get("value"))
                return
            applied = dict((result.value or {}).get("applied") or {})
            spec["value"] = applied.get(key, value)
            self.toast(f"{spec.get('label') or key} = {_show(applied.get(key, value))}", level="ok")
            self.refreshed.emit()

        self.run_task(_save, on_done=_done, busy_text=f"保存 {spec.get('label') or key}…")

    def _reset_one(self, spec: dict[str, Any]) -> None:
        key = str(spec.get("key") or "")
        default = spec.get("env_value")
        if default is None:
            default = spec.get("default")
        if not QtWidgets.QMessageBox.question(
            self, "恢复默认",
            f"把「{spec.get('label') or key}」恢复为 .env 默认值？\n"
            f"默认值：{_show(default)}\n\n（只影响这一个参数，不动人格与记忆）",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        field = self._fields.get(key)
        if field is None:
            return
        field.setter(default)
        self._commit(key, default)

    def _reset_all(self) -> None:
        if not QtWidgets.QMessageBox.question(
            self, "恢复全部默认",
            "把所有参数恢复为 .env 默认值？\n\n"
            "这会把控制台写入的覆盖值全部清掉（含模型档案相关的设置项）。\n"
            "**不会**动人格三层文件，也**不会**动记忆库。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            self.client().reset_settings,
            on_done=lambda result: self._after_reset(result),
            busy_text="正在恢复默认…",
        )

    def _after_reset(self, result) -> None:  # noqa: ANN001
        if result.ok:
            self.toast("已恢复 .env 默认", level="ok")
            self.load(force=True)

    # ------------------------------------------------------------ 过滤
    def _set_all_groups(self, expanded: bool) -> None:
        for box in self._groups:
            box.setChecked(expanded)

    def _apply_filter(self, text: str) -> None:
        needle = str(text or "").strip().lower()
        for box in self._groups:
            grid = box.layout()
            visible_any = False
            for row in range(grid.rowCount()):
                title = grid.itemAtPosition(row, 0)
                hint = grid.itemAtPosition(row, 2)
                blob = ""
                for item in (title, hint):
                    widget = item.widget() if item else None
                    if widget is not None:
                        blob += " " + (widget.text() or "")
                match = (not needle) or (needle in blob.lower())
                for column in (0, 1, 2):
                    entry = grid.itemAtPosition(row, column)
                    widget = entry.widget() if entry else None
                    if widget is not None:
                        widget.setVisible(match)
                visible_any = visible_any or match
            box.setVisible(visible_any)
            if needle and visible_any:
                box.setChecked(True)


def _range_text(spec: dict[str, Any]) -> str:
    minimum, maximum = spec.get("min"), spec.get("max")
    if minimum is None and maximum is None:
        return ""
    return f"范围 {minimum if minimum is not None else '-∞'} ~ {maximum if maximum is not None else '+∞'}"


def _show(value: Any) -> str:
    if value is None:
        return "（空）"
    if isinstance(value, bool):
        return "开" if value else "关"
    return str(value)
