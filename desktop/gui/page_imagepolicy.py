"""图片策略页：全局策略（一行按钮）+ 会话级有效策略（一张表）。

服务端的形状很简单（`webui.py:1122-1141`）：

    POST /ai/api/image-policy {"global_mode": "normal|ignore|ask"}
    POST /ai/api/image-policy {"conv": "g123", "mode": "…"}    ← mode 空串 = 清除该会话的自定义

"有效策略"≠"全局策略"：会话有自己的设置时以会话为准。所以页面上**同时**显示两者，
不然用户会疑惑"我明明禁存了，怎么这个群还在收"。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtWidgets

from .base import Page, StateSlice
from .widgets import badge, elide, esc, label, table

MODE_LABELS = {
    "normal": "正常（按全局）",
    "ignore": "禁存（不记这类图）",
    "ask": "询问（问过再决定）",
}


class ImagePolicyPage(Page):
    title = "图片策略"
    subtitle = "全局策略与会话级有效策略"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._image: dict[str, Any] = {}
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("刷新")
        self.btn_refresh.clicked.connect(lambda: self.load(force=True))
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)
        outer.addLayout(bar)

        # ---- 全局
        global_box = QtWidgets.QGroupBox("全局策略")
        global_layout = QtWidgets.QHBoxLayout(global_box)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setMinimumWidth(180)
        global_layout.addWidget(QtWidgets.QLabel("模式："))
        global_layout.addWidget(self.mode_combo)
        self.btn_apply_global = QtWidgets.QPushButton("应用")
        self.btn_apply_global.clicked.connect(self._apply_global)
        global_layout.addWidget(self.btn_apply_global)
        self.btn_forbid_all = QtWidgets.QPushButton("全部禁存")
        self.btn_forbid_all.setToolTip("把全局策略设为「禁存」——机器人不再记住任何新图")
        self.btn_forbid_all.clicked.connect(lambda: self._set_global("ignore"))
        global_layout.addWidget(self.btn_forbid_all)
        self.btn_restore_all = QtWidgets.QPushButton("撤销禁存（恢复正常）")
        self.btn_restore_all.clicked.connect(lambda: self._set_global("normal"))
        global_layout.addWidget(self.btn_restore_all)
        global_layout.addStretch(1)
        outer.addWidget(global_box)

        self.hint = label("正在读取图片策略…")
        outer.addWidget(self.hint)

        # ---- 会话
        session_box = QtWidgets.QGroupBox("会话级有效策略")
        session_layout = QtWidgets.QVBoxLayout(session_box)
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
        modes = [str(x) for x in (self._image.get("valid_modes") or ["normal", "ignore", "ask"])]
        current = str(self._image.get("global_mode") or "normal")

        if self.mode_combo.count() != len(modes):
            self.mode_combo.clear()
            for mode in modes:
                self.mode_combo.addItem(MODE_LABELS.get(mode, mode), mode)
        index = self.mode_combo.findData(current)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

        convs = dict(self._image.get("convs") or {})
        groups = list(state.status.get("groups") or [])
        # 群列表来自主动发言的已知群；把有自定义策略的会话也并进来，避免"设置了却看不见"。
        all_convs = sorted(set(convs) | set(groups))
        self.table.setRowCount(len(all_convs))
        for row, conv in enumerate(all_convs):
            custom = convs.get(conv)
            effective = custom or current
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(str(conv)))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(MODE_LABELS.get(str(effective), str(effective))))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(MODE_LABELS.get(str(custom), str(custom)) if custom else ""))

            holder = QtWidgets.QWidget()
            layout = QtWidgets.QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            combo = QtWidgets.QComboBox()
            for mode in modes:
                combo.addItem(MODE_LABELS.get(mode, mode), mode)
            index = combo.findData(str(effective))
            if index >= 0:
                combo.setCurrentIndex(index)
            apply_button = QtWidgets.QPushButton("设置")
            apply_button.clicked.connect(
                lambda _=False, c=str(conv), w=combo: self._set_conv(c, str(w.currentData()))
            )
            clear_button = QtWidgets.QPushButton("清除自定义")
            clear_button.clicked.connect(lambda _=False, c=str(conv): self._clear_conv(c))
            layout.addWidget(combo)
            layout.addWidget(apply_button)
            layout.addWidget(clear_button)
            layout.addStretch(1)
            self.table.setCellWidget(row, 3, holder)
        self.table.resizeColumnsToContents()

        self.hint.setText(
            f"全局：<b>{esc(MODE_LABELS.get(current, current))}</b> · 有自定义策略的会话 "
            f"{esc(len(convs))} 个 · 已知会话 {esc(len(all_convs))} 个 ｜ "
            "有效策略 = 会话自定义优先，没有则跟全局"
        )
        self.toast("图片策略已载入", level="ok")
        self.refreshed.emit()

    # ------------------------------------------------------------ 动作
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
