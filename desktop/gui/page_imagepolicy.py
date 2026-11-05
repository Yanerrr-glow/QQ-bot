"""图片策略页：全局策略（一行按钮）+ 会话级有效策略（一张表）。

服务端的形状很简单（`webui.py:438-458`）：

    POST /ai/api/image-policy {"global_mode": "normal|ignore|only|off"}
    POST /ai/api/image-policy {"conv": "g123", "mode": "…"}    ← mode 空串 = 清除该会话的自定义

"有效策略"≠"全局策略"：会话有自己的设置时以会话为准。所以页面上**同时**显示两者，
不然用户会疑惑"我明明禁存了，怎么这个群还在收"。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtWidgets

from . import theme
from .base import Page, StateSlice
from .widgets import (
    badge,
    busy_button,
    danger_button,
    elide,
    esc,
    fit_table,
    ghost_button,
    label,
    primary_button,
    table,
    toolbar,
)

#: 与服务端 `state.VALID_MODES` 一一对应。**不要在这里另造模式**：
#: 曾经写过一个服务端不存在的 `ask`，选中就 400，而且 `only` / `off` 反而漏掉了。
MODE_LABELS = {
    "normal": "正常（会看图、会入库）",
    "ignore": "不再保存（会看图、不入库）",
    "only": "只看不存（会看图、不入库）",
    "off": "完全不看（不下载、不送模型）",
}
_FALLBACK_MODES = ("normal", "ignore", "only", "off")


def _mode_of(entry: Any) -> str:
    """取会话条目里的自定义模式。

    `state.snapshot()` 的 `convs` 是 `conv -> {mode, effective, …}`；
    旧桩/旧版本可能是裸字符串，两种都认，避免把 dict 的 repr 直接显示到表格里。
    """
    if isinstance(entry, dict):
        return str(entry.get("mode") or "")
    return str(entry or "")


def _effective_of(entry: Any, global_mode: str) -> str:
    if isinstance(entry, dict) and entry.get("effective"):
        return str(entry["effective"])
    return _mode_of(entry) or global_mode


class ImagePolicyPage(Page):
    #: ⚠ 标题**不能**叫「图片策略」：那个标题归「功能」目录下的图片策略子菜单
    #: （策略开关在那边）。本页是**会话级数据**（哪个群什么策略、怎么改），
    #: 两个同名页会让按标题跳转二义（`goto_page` 按标题查）。
    title = "图片策略（会话）"
    subtitle = "全局策略与会话级有效策略"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._image: dict[str, Any] = {}
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = toolbar("全局策略影响所有没有自定义策略的会话")
        self.btn_refresh = busy_button("刷新", lambda: self.load(force=True))
        bar.addWidget(self.btn_refresh)
        outer.addLayout(bar)

        # ---- 全局
        global_box = QtWidgets.QGroupBox("全局策略")
        global_layout = QtWidgets.QHBoxLayout(global_box)
        global_layout.setContentsMargins(0, 4, 0, 0)
        global_layout.setSpacing(theme.SPACE["sm"])
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setMinimumWidth(180)
        global_layout.addWidget(QtWidgets.QLabel("模式："))
        global_layout.addWidget(self.mode_combo)
        self.btn_apply_global = primary_button("应用", self._apply_global)
        global_layout.addWidget(self.btn_apply_global)
        global_layout.addStretch(1)
        global_layout.addSpacing(theme.SPACE["lg"])
        self.btn_restore_all = ghost_button(
            "撤销禁存（恢复正常）", lambda: self._set_global("normal")
        )
        global_layout.addWidget(self.btn_restore_all)
        global_layout.addSpacing(theme.SPACE["lg"])
        self.btn_forbid_all = danger_button(
            "全部禁存", lambda: self._set_global("ignore"),
            tip="把全局策略设为「禁存」——机器人不再记住任何新图",
        )
        global_layout.addWidget(self.btn_forbid_all)
        outer.addWidget(global_box)

        self.hint = label("正在读取图片策略…")
        outer.addWidget(self.hint)

        # ---- 会话
        session_box = QtWidgets.QGroupBox("会话级有效策略")
        session_layout = QtWidgets.QVBoxLayout(session_box)

        # 服务端不再下发"已知群列表"（那是主动发言时代的 `status.groups`），
        # 所以必须能手动指定会话 id，否则只能改那些**已经设过**策略的会话。
        add_row = QtWidgets.QHBoxLayout()
        add_row.setSpacing(theme.SPACE["sm"])
        add_row.addWidget(QtWidgets.QLabel("新增/修改会话："))
        self.conv_input = QtWidgets.QLineEdit()
        self.conv_input.setPlaceholderText("会话 id：群 g123456789 / 私聊 u10001")
        self.conv_input.setToolTip(
            "群会话是 g + 群号，私聊是 u + QQ 号。设完会出现在下面的表格里。"
        )
        self.conv_input.setMinimumWidth(220)
        add_row.addWidget(self.conv_input, 1)
        self.conv_mode = QtWidgets.QComboBox()
        self.conv_mode.setMinimumWidth(180)
        add_row.addWidget(self.conv_mode)
        self.btn_add_conv = primary_button("设置", self._add_conv)
        add_row.addWidget(self.btn_add_conv)
        session_layout.addLayout(add_row)

        self.table = table(["会话", "有效策略", "自定义", "操作"])
        session_layout.addWidget(self.table, 1)
        outer.addWidget(session_box, 1)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: StateSlice(self.client().state()),
            on_done=self._apply,
            busy_text="正在读取图片策略…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("读取失败", level="err"))
            return
        state: StateSlice = result.value
        self._image = state.image
        modes = [str(x) for x in (self._image.get("valid_modes") or _FALLBACK_MODES)]
        current = str(self._image.get("global_mode") or "normal")

        if self.mode_combo.count() != len(modes):
            self.mode_combo.clear()
            for mode in modes:
                self.mode_combo.addItem(MODE_LABELS.get(mode, mode), mode)
        index = self.mode_combo.findData(current)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

        if self.conv_mode.count() != len(modes):
            self.conv_mode.clear()
            for mode in modes:
                self.conv_mode.addItem(MODE_LABELS.get(mode, mode), mode)
        default_index = self.conv_mode.findData("ignore")
        if default_index >= 0:
            self.conv_mode.setCurrentIndex(default_index)

        # `__global__` 是服务端给 WebUI 的占位条目（全局非 normal 时出现），不是真会话。
        convs = {
            str(conv): item
            for conv, item in dict(self._image.get("convs") or {}).items()
            if not str(conv).startswith("__")
        }
        # 旧服务端还会给 `status.groups`；有就并进来（现在通常没有）。
        groups = list(state.status.get("groups") or [])
        all_convs = sorted(set(convs) | set(groups))
        self.table.setRowCount(len(all_convs))
        for row, conv in enumerate(all_convs):
            entry = convs.get(conv)
            custom = _mode_of(entry) if entry is not None else ""
            effective = _effective_of(entry, current) if entry is not None else current
            conv_item = QtWidgets.QTableWidgetItem(str(conv))
            conv_item.setToolTip(str(conv))
            effective_item = QtWidgets.QTableWidgetItem(
                MODE_LABELS.get(str(effective), str(effective))
            )
            effective_item.setToolTip(str(effective))
            self.table.setItem(row, 0, conv_item)
            self.table.setItem(row, 1, effective_item)
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(MODE_LABELS.get(custom, custom) if custom else ""))

            holder = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, theme.SPACE["sm"], 0)
            layout.setSpacing(theme.SPACE["sm"])
            combo = QtWidgets.QComboBox()
            combo.setMinimumWidth(150)
            for mode in modes:
                combo.addItem(MODE_LABELS.get(mode, mode), mode)
            index = combo.findData(str(effective))
            if index >= 0:
                combo.setCurrentIndex(index)
            apply_button = busy_button(
                "设置",
                lambda c=str(conv), w=combo: self._set_conv(c, str(w.currentData())),
            )
            clear_button = danger_button(
                "清除自定义", lambda c=str(conv): self._clear_conv(c)
            )
            layout.addWidget(combo)
            layout.addWidget(apply_button)
            layout.addSpacing(theme.SPACE["lg"])
            layout.addWidget(clear_button)
            layout.addStretch(1)
            self.table.setCellWidget(row, 3, holder)
        fit_table(self.table)
        # 三列内容都短，给固定上限；把剩余宽度让给「操作」列 ——
        # 否则 940px 窗口下 combo + 两个按钮放不下，「清除自定义」会切字。
        header = self.table.horizontalHeader()
        for column, width in ((0, 70), (1, 150), (2, 90)):
            header.setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)

        self.hint.setText(
            f"全局：<b>{esc(MODE_LABELS.get(current, current))}</b> · 有自定义策略的会话 "
            f"{esc(len(convs))} 个 · 表格列出 {esc(len(all_convs))} 个 ｜ "
            "有效策略 = 会话自定义优先，没有则跟全局"
        )
        self.toast("图片策略已载入", level="ok")
        self.refreshed.emit()

    # ------------------------------------------------------------ 动作
    def _add_conv(self) -> None:
        """手动给一个会话设策略（表格里通常只会出现"已经设过"的会话）。"""
        conv = self.conv_input.text().strip()
        if not conv:
            self.toast("先填会话 id：群 g+群号，私聊 u+QQ号", level="warn")
            return
        if not (conv.startswith("g") or conv.startswith("u")):
            self.toast("会话 id 应以 g（群）或 u（私聊）开头，例如 g123456789 / u10001", level="warn")
            return
        mode = str(self.conv_mode.currentData() or "normal")
        self._set_conv(conv, mode)

    def _apply_global(self) -> None:
        self._set_global(str(self.mode_combo.currentData() or "normal"))

    def _set_global(self, mode: str) -> None:
        if not QtWidgets.QMessageBox.question(
            self, "设置全局图片策略",
            f"把全局图片策略设为「{MODE_LABELS.get(mode, mode)}」？\n\n"
            "影响所有**没有**自定义策略的会话。已有的图片不会被删除。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            lambda: self.client().image_policy_global(mode),
            on_done=lambda r: self._after_write(r, "全局策略"),
            busy_text="正在设置全局策略…",
        )

    def _set_conv(self, conv: str, mode: str) -> None:
        self.run_task(
            lambda: self.client().image_policy_conv(conv, mode),
            on_done=lambda r: self._after_write(r, f"{conv} 的策略"),
            busy_text=f"正在设置 {conv} 的策略…",
        )

    def _clear_conv(self, conv: str) -> None:
        self.run_task(
            lambda: self.client().image_policy_conv(conv, ""),
            on_done=lambda r: self._after_write(r, f"清除 {conv} 的自定义"),
            busy_text=f"正在清除 {conv} 的自定义策略…",
        )

    def _after_write(self, result, what: str) -> None:  # noqa: ANN001
        if not result.ok:
            return
        payload = dict(result.value or {})
        if payload.get("ok"):
            self.toast(f"{what} 已更新", level="ok")
            self.load(force=True)
        else:
            self.toast(f"{what} 未更新：{elide(payload.get('error') or payload.get('detail') or '', 160)}", level="warn")
