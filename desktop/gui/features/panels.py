"""「功能」目录两处共用的控件：状态 chip 行、功能的参数设置面板、功能卡片。

三样东西都**不自己发请求**：数据由页面取一次 `/api/state` 之后灌进来。
这样「功能」中心页一次请求就能把 19 个功能的状态全填上，而不是 19 次往返。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from PySide6 import QtCore, QtWidgets

from .. import theme
from ..params_view import ParamsGroupsView
from ..widgets import LEVEL_COLOR, PALETTE, badge, description_label, esc, set_variant

#: `(标签, 值, 级别)`；级别为 `info` / `ok` / `warn` / `err`。
Chip = tuple[str, Any, str]


def chips_html(chips: Sequence[Chip], *, empty: str = "（暂无运行数据）") -> str:
    """把 chip 列表拼成一行富文本。**所有外部字符串先过 esc()**。"""
    if not chips:
        return f"<span style='color:{PALETTE['dim']}'>{esc(empty)}</span>"
    return " &nbsp; ".join(
        f"{badge(f'{label} {value}', level=level)}" for label, value, level in chips
    )


def chip_line(chips: Sequence[Chip], *, empty: str = "（暂无运行数据）") -> QtWidgets.QLabel:
    widget = QtWidgets.QLabel(chips_html(chips, empty=empty))
    widget.setTextFormat(QtCore.Qt.TextFormat.RichText)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class SettingsPanel(QtWidgets.QWidget):
    """一个功能的设置区：过滤框 + 展开/收起 + 参数分组视图。"""

    def __init__(
        self,
        on_commit: Callable[[str, Any], None],
        parent: QtWidgets.QWidget | None = None,
        *,
        title: str = "设置",
        key_order: tuple[str, ...] = (),
        note: str = "",
    ) -> None:
        super().__init__(parent)
        self._key_order = key_order
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.box = QtWidgets.QGroupBox(title)
        box_layout = QtWidgets.QVBoxLayout(self.box)
        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(theme.SPACE["sm"])
        self.btn_expand = QtWidgets.QPushButton("全部展开")
        self.btn_collapse = QtWidgets.QPushButton("全部收起")
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("过滤本功能的参数名/说明…")
        if note:
            label = QtWidgets.QLabel(esc(note))
            label.setTextFormat(QtCore.Qt.TextFormat.RichText)
            label.setStyleSheet(f"color:{PALETTE['muted']}")
            toolbar.addWidget(label)
        toolbar.addStretch(1)
        toolbar.addWidget(self.btn_expand)
        toolbar.addWidget(self.btn_collapse)
        box_layout.addLayout(toolbar)
        # 过滤框单独一行：与说明 + 两个按钮同排时，940px 宽度会把「全部收起」切掉。
        box_layout.addWidget(self.search)

        self.view = ParamsGroupsView(on_commit, key_order=key_order)
        self.btn_expand.clicked.connect(lambda: self.view.set_all_groups(True))
        self.btn_collapse.clicked.connect(lambda: self.view.set_all_groups(False))
        self.search.textChanged.connect(lambda text: self.view.apply_filter(text))
        box_layout.addWidget(self.view)
        outer.addWidget(self.box)

    @property
    def count(self) -> int:
        return self.view.field_count

    def load_groups(self, groups: list[dict[str, Any]]) -> int:
        """重建分组并**重新套一遍过滤词**（过滤词是用户输入，不该被重建清掉）。"""
        total = self.view.rebuild(groups)
        if self.search.text().strip():
            self.view.apply_filter(self.search.text())
        return total


class FeatureCard(QtWidgets.QGroupBox):
    """「功能」中心页里的一张功能卡：标题 + 开关状态 + 运行细节 + 进子菜单。"""

    def __init__(
        self,
        feature_id: str,
        title: str,
        description: str,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self.feature_id = feature_id
        self.setCheckable(False)

        layout = QtWidgets.QVBoxLayout(self)
        if description:
            # 本地固定说明文案（`catalog.description`）：最小 Markdown 处理，先转义。
            layout.addWidget(description_label(description, wrap=True))

        self.enable_label = QtWidgets.QLabel("")
        self.enable_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        layout.addWidget(self.enable_label)

        self.chips = QtWidgets.QLabel("正在读取…")
        self.chips.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.chips.setWordWrap(True)
        self.chips.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.chips)

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACE["sm"])
        # 17 张卡片各挂一颗主按钮会满屏蓝色：卡片的"打开设置"用默认 secondary，
        # 页面级主操作只留给顶部的「刷新全部」。
        self.btn_open = QtWidgets.QPushButton("打开设置")
        self.btn_open.setToolTip(f"在左侧「功能」目录下打开「{title}」子菜单")
        self.btn_params = QtWidgets.QPushButton("在「参数」页里找")
        set_variant(self.btn_params, "ghost")
        self.btn_params.setToolTip("跳到全量参数列表（那里能按名字搜索、恢复 .env 默认）")
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_params)
        row.addStretch(1)
        layout.addLayout(row)

    def set_enabled(self, enabled: bool | None) -> None:
        if enabled is None:
            self.enable_label.setText(f"<span style='color:{PALETTE['dim']}'>没有单一开关（按参数生效）</span>")
            return
        color = LEVEL_COLOR["ok"] if enabled else PALETTE["dim"]
        self.enable_label.setText(
            f"<span style='color:{color};font-weight:600'>"
            f"{'● 已启用' if enabled else '○ 已关闭'}</span>"
        )

    def set_chips(self, chips: Sequence[Chip], *, empty: str = "（暂无运行数据）") -> None:
        self.chips.setText(chips_html(chips, empty=empty))
