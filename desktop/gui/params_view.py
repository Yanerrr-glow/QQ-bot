"""参数分组视图：按 `settings.describe()` 的组渲染可折叠的参数表单。

**「参数」页与「功能」目录下的每个子菜单共用这一份**（2026-10-03 抽出）。

为什么值得单独一个模块：参数控件是"按 `kind` 现造"的（见 `base.build_field`），
而"服务端加一个参数、桌面端要自动跟上"这条要求对**两处**都成立 ——
「参数」页是全量列表，功能子菜单是同一批参数里属于该功能的那几组。
两处各写一份渲染逻辑的后果不是"多写点代码"，而是**迟早只有一处跟上**。

用法：

    view = ParamsGroupsView(on_commit=self._commit)
    view.rebuild(feature_groups)      # feature_groups 已由 catalog 过滤/排序好

约定（与「参数」页一致）：密钥类显示为掩码 `***`，**掩码没动过就不发请求**
（服务端见 `***` 会保留原值，但少一次往返总是好的）。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6 import QtCore, QtWidgets

from . import theme
from .base import Field, build_field
from .widgets import PALETTE, esc as _esc


class ParamsGroupsView(QtWidgets.QWidget):
    """一组可折叠的参数分组框。`on_commit(key, value)` 由调用方负责保存。"""

    def __init__(
        self,
        on_commit: Callable[[str, Any], None],
        parent: QtWidgets.QWidget | None = None,
        *,
        key_order: tuple[str, ...] = (),
    ) -> None:
        super().__init__(parent)
        self._on_commit = on_commit
        #: 同一组内部的键序（功能子菜单按目录里声明的顺序排；空表示按服务端顺序）
        self._key_order = tuple(key_order)
        self._fields: dict[str, Field] = {}
        self._groups: list[QtWidgets.QGroupBox] = []
        #: 当前过滤词。折叠/展开后要按它重新算可见性，否则一收一展会漏出不该显示的参数。
        self._filter = ""
        #: 程序内部改 `setChecked` 时防止 `toggled` 递归重入。
        self._syncing = False

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.form = QtWidgets.QVBoxLayout()
        self.form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        outer.addLayout(self.form)

    # ------------------------------------------------------------ 渲染
    @property
    def field_count(self) -> int:
        return len(self._fields)

    def rebuild(self, groups: list[dict[str, Any]]) -> int:
        """按分组数据重建表单，返回参数总数。"""
        while self.form.count():
            item = self.form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._fields.clear()
        self._groups.clear()
        self._syncing = False

        for group in groups or []:
            items = list(group.get("items") or [])
            if not items:
                continue
            items = self._sorted_items(items)
            box = QtWidgets.QGroupBox(f"{group.get('group')}（{len(items)}）")
            box.setCheckable(True)
            # 默认展开：勾选框同时是"展开/收起"开关。之前默认 `False` 会让
            # QGroupBox 把子控件**禁用**（不是隐藏），用户看到的是满屏灰掉的参数，
            # 还以为设置被重置成了默认值 —— 值其实一直是服务端那份。
            box.setChecked(True)
            grid = QtWidgets.QGridLayout(box)
            grid.setContentsMargins(2, 4, 2, 2)
            grid.setHorizontalSpacing(theme.SPACE["md"])
            grid.setVerticalSpacing(10)
            for row, spec in enumerate(items):
                self._add_field(grid, row, spec)
            box.toggled.connect(lambda _on, b=box: self._on_group_toggled(b))
            self.form.addWidget(box)
            self._groups.append(box)
        self.form.addStretch(1)
        self._apply_visibility()
        return len(self._fields)

    def _sorted_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._key_order:
            return items
        rank = {key: index for index, key in enumerate(self._key_order)}
        return sorted(items, key=lambda spec: rank.get(str(spec.get("key") or ""), len(rank)))

    def _add_field(self, grid: QtWidgets.QGridLayout, row: int, spec: dict[str, Any]) -> None:
        key = str(spec.get("key") or "")
        field = build_field(spec, self._commit)
        self._fields[key] = field

        title = QtWidgets.QLabel(
            f"<b>{_esc(spec.get('label') or key)}</b> "
            f"<span style='color:{PALETTE['muted']}'>{_esc(key)}</span>"
        )
        title.setTextFormat(QtCore.Qt.TextFormat.RichText)
        grid.addWidget(title, row, 0)

        holder = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(holder)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(field.widget)
        row_layout.addStretch(1)
        grid.addWidget(holder, row, 1)

        hint_bits: list[str] = []
        if spec.get("hint"):
            hint_bits.append(str(spec["hint"]))
        rng = range_text(spec)
        if rng:
            hint_bits.append(rng)
        if spec.get("secret"):
            hint_bits.append("密钥类：显示为掩码，原样保存不会覆盖服务端原值")
        if spec.get("choices"):
            hint_bits.append("可选：" + "/".join(map(str, spec["choices"])))
        hint = QtWidgets.QLabel(_esc(" · ".join(hint_bits)) if hint_bits else "")
        hint.setTextFormat(QtCore.Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{PALETTE['muted']}")
        grid.addWidget(hint, row, 2)
        # 字段列拿大头：密钥类的长占位文字（「已保存（显示为掩码…）」）在 1:2 时会被切。
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(2, 2)
        title.setToolTip(key)

    # ------------------------------------------------------------ 操作
    def commit(self, key: str, value: Any) -> None:
        """外部（如"恢复默认"按钮）改完控件后，按同一套判据提交。"""
        self._commit(key, value)

    def default_of(self, key: str) -> Any:
        field = self._fields.get(key)
        if field is None:
            return None
        default = field.spec.get("env_value")
        if default is None:
            default = field.spec.get("default")
        return default

    def value_of(self, key: str) -> Any:
        field = self._fields.get(key)
        return field.editor() if field is not None else None

    def spec_of(self, key: str) -> dict[str, Any]:
        """参数的 spec（保存失败时要用它把界面改回服务端的真实值）。"""
        field = self._fields.get(key)
        return dict(field.spec) if field is not None else {}

    def set_value(self, key: str, value: Any) -> None:
        field = self._fields.get(key)
        if field is not None:
            field.setter(value)

    def apply_saved(self, key: str, value: Any) -> None:
        """保存成功后把 spec 里的"当前值"同步掉（下次判断"值没变"才准）。"""
        field = self._fields.get(key)
        if field is not None:
            field.spec["value"] = value

    def set_all_groups(self, expanded: bool) -> None:
        for box in self._groups:
            box.setChecked(bool(expanded))

    def apply_filter(self, text: str) -> None:
        """按参数名/说明过滤；有命中的组自动展开。"""
        self._filter = str(text or "")
        self._apply_visibility()

    def _on_group_toggled(self, box: QtWidgets.QGroupBox) -> None:
        if not self._syncing:
            self._apply_visibility()

    def _apply_visibility(self) -> None:
        """统一算一次"每个控件该不该显示"。

        折叠**只隐藏内容、不禁用控件**：`QGroupBox` 自带的行为是取消勾选就禁用子控件，
        那会把参数显示成灰色，看起来像"设置丢了"。这里反过来：勾选 = 展开且可编辑，
        取消勾选 = 收起（隐藏）。同时把过滤词一起算进去，避免展开时漏出过滤掉的参数。
        """
        if self._syncing:
            return
        self._syncing = True
        try:
            needle = self._filter.strip().lower()
            for box in self._groups:
                grid = box.layout()
                if not isinstance(grid, QtWidgets.QGridLayout):
                    continue
                matches: list[bool] = []
                for row in range(grid.rowCount()):
                    blob = ""
                    for column in (0, 2):
                        item = grid.itemAtPosition(row, column)
                        widget = item.widget() if item else None
                        if widget is not None:
                            blob += " " + (widget.text() or "")
                    matches.append((not needle) or (needle in blob.lower()))
                any_match = any(matches)

                # 过滤命中而组是收起的：自动展开（这是原来的行为，保留）。
                if needle and any_match and not box.isChecked():
                    box.setChecked(True)
                opened = box.isChecked()
                for row, matched in enumerate(matches):
                    for column in (0, 1, 2):
                        item = grid.itemAtPosition(row, column)
                        widget = item.widget() if item else None
                        if widget is not None:
                            widget.setEnabled(True)
                            widget.setVisible(opened and matched)
                box.setVisible(any_match or not needle)
        finally:
            self._syncing = False

    # ------------------------------------------------------------ 提交
    def _commit(self, key: str, value: Any) -> None:
        field = self._fields.get(key)
        if field is None:
            return
        spec = field.spec
        if spec.get("secret") and str(value) == "***":
            return  # 掩码没动过：不发请求（服务端也会忽略，但少一次往返）
        if value == spec.get("value"):
            return
        self._on_commit(key, value)


def range_text(spec: dict[str, Any]) -> str:
    minimum, maximum = spec.get("min"), spec.get("max")
    if minimum is None and maximum is None:
        return ""
    return f"范围 {minimum if minimum is not None else '-∞'} ~ {maximum if maximum is not None else '+∞'}"


def show_value(value: Any) -> str:
    if value is None:
        return "（空）"
    if isinstance(value, bool):
        return "开" if value else "关"
    return str(value)
