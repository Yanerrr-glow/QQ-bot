"""插件管理页：看得见每个插件的来源、权限、状态与错误，并能启停。

按方案 5.3 的要求，这一页必须显示：名称、版本、来源目录、启用状态、**权限**、
加载错误与兼容状态。两条额外的诚实说明也要摆在界面上：

- **权限声明不是沙箱**：插件在桌面进程里跑的是任意 Python，manifest 里的
  `permissions` 只是"它通过宿主 API 能做什么"的边界。
- **停用需要重启**：不热重载，避免留下幽灵信号/定时器/线程。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtWidgets

from ..core.util import one_line
from ..sdk.loader import PluginState
from ..sdk.permissions import PERMISSIONS
from .base import Page
from .widgets import badge, esc, label, plain_text_edit, table


class PluginsPage(Page):
    title = "插件"
    subtitle = "桌面 UI 插件（manifest 驱动、窄权限、失败隔离）"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("重新扫描")
        self.btn_refresh.setToolTip("重新读 desktop/plugins/ 下的 manifest（不热重载已加载的插件）")
        self.btn_refresh.clicked.connect(self._rescan)
        self.btn_toggle = QtWidgets.QPushButton("启用 / 禁用")
        self.btn_toggle.clicked.connect(self._toggle)
        self.btn_details = QtWidgets.QPushButton("查看详情")
        self.btn_details.clicked.connect(self._details)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_toggle)
        bar.addWidget(self.btn_details)
        bar.addStretch(1)
        outer.addLayout(bar)

        self.hint = label("正在读取插件列表…")
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)

        self.table = table(["插件", "版本", "API", "状态", "权限", "来源目录", "说明"])
        self.table.itemSelectionChanged.connect(self._update_buttons)
        outer.addWidget(self.table, 3)

        self.detail = plain_text_edit("")
        outer.addWidget(self.detail, 2)

    # ------------------------------------------------------------ 数据
    def _host(self):  # noqa: ANN202
        return getattr(self.window(), "plugin_host", None)

    def _load(self) -> None:
        host = self._host()
        if host is None:
            self.hint.setText(badge("没有插件宿主", level="warn"))
            return
        summary = host.summary()
        records = sorted(host.records.values(), key=lambda r: (r.manifest.nav_order(), r.id))
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            permissions = list(record.manifest.permissions)
            labels = [PERMISSIONS.get(p, f"{p}（未知）") for p in permissions]
            cells = [
                record.manifest.name,
                record.manifest.version,
                record.manifest.api_version,
                record.state.label,
                "、".join(labels) or "（无）",
                str(record.manifest.directory),
                one_line(record.error or record.note, 80),
            ]
            for column, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(str(text))
                item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
                if record.state == PluginState.FAILED and column == 3:
                    item.setForeground(QtCore.Qt.GlobalColor.red)
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.hint.setText(
            f"根目录 <code>{esc(summary['root'])}</code> · 共 {esc(summary['total'])} 个 · "
            f"已激活 {esc(summary['states'].get('active', 0))} 个 · "
            f"失败 {esc(summary['states'].get('failed', 0))} 个 · "
            f"登记页面 {esc(summary['pages'])} / 操作 {esc(summary['actions'])} / 状态卡 {esc(summary['status_cards'])}"
            f"<br><b>权限声明不是沙箱</b>：插件在桌面进程里执行的是任意 Python，"
            f"manifest 的 permissions 只约束它通过宿主 API 能做什么。只加载你信任的来源。"
        )
        self._update_buttons()
        self.refreshed.emit()

    def _current(self):  # noqa: ANN202
        host = self._host()
        if host is None:
            return None
        row = self.table.currentRow()
        if row < 0:
            return None
        records = sorted(host.records.values(), key=lambda r: (r.manifest.nav_order(), r.id))
        return records[row] if row < len(records) else None

    def _update_buttons(self) -> None:
        record = self._current()
        self.btn_toggle.setEnabled(bool(record) and record.state != PluginState.INCOMPATIBLE)
        self.btn_details.setEnabled(bool(record))
        self.btn_toggle.setText("禁用" if record and record.enabled else "启用")

    def _details(self) -> None:
        record = self._current()
        if record is None:
            return
        host = self._host()
        lines = [
            f"id            : {record.manifest.id}",
            f"名称          : {record.manifest.name}",
            f"版本          : {record.manifest.version}",
            f"api_version   : {record.manifest.api_version}",
            f"入口          : {record.manifest.entrypoint}",
            f"来源目录      : {record.manifest.directory}",
            f"状态          : {record.state.label}",
            f"启用          : {record.enabled}",
            "",
            "权限（声明 → 含义）：",
        ]
        for perm in record.manifest.permissions:
            lines.append(f"  - {perm}：{PERMISSIONS.get(perm, 'SDK 不认识这个权限名')}")
        if record.registered:
            lines.append("")
            lines.append("登记内容：" + ", ".join(f"{k}={v}" for k, v in record.registered.items()))
        if host is not None:
            per_plugin = host.registry.by_plugin(record.manifest.id)
            if per_plugin["pages"]:
                lines.append("页面：" + "、".join(p.title for p in per_plugin["pages"]))
            if per_plugin["actions"]:
                lines.append("操作：" + "、".join(a.title for a in per_plugin["actions"]))
            if per_plugin["status_cards"]:
                lines.append("状态卡：" + "、".join(c.title for c in per_plugin["status_cards"]))
            if record.http_calls:
                lines.append("")
                lines.append("最近的受控 API 调用：")
                lines.extend(f"  {call}" for call in record.http_calls[-10:])
        if record.note:
            lines += ["", f"提示：{record.note}"]
        if record.error:
            lines += ["", "错误：", record.error]
        self.detail.setPlainText("\n".join(lines))

    # ------------------------------------------------------------ 动作
    def _rescan(self) -> None:
        """重新扫 manifest。

        刻意**不**在这里激活新插件：`on_activate` 可能创建控件、订阅信号，
        在主窗口已经建好之后再激活，很容易留下"半激活"状态。所以只做到登记，
        新插件显示为「已加载（未激活）」，重启桌面程序后才真正生效。
        """
        host = self._host()
        if host is None:
            return
        host.discover()
        host.load_all()
        window = self.window()
        if hasattr(window, "_rebuild_plugin_nav"):
            window._rebuild_plugin_nav()  # type: ignore[attr-defined]
        self.load(force=True)
        self.toast("已重新扫描 manifest（新插件需重启桌面程序才会激活）", level="info")

    def _toggle(self) -> None:
        record = self._current()
        host = self._host()
        if record is None or host is None:
            return
        want_enabled = not record.enabled
        if not want_enabled and not QtWidgets.QMessageBox.question(
            self, "禁用插件",
            f"禁用「{record.manifest.name}」？\n\n"
            "它的页面会从导航里移除、后台任务会被取消。\n"
            "**完全卸载需要重启桌面程序**（不做热重载，避免留下幽灵信号/定时器）。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        host.set_enabled(record.manifest.id, want_enabled)
        self.toast(
            f"「{record.manifest.name}」已{'启用' if want_enabled else '禁用'}；重启桌面程序后完全生效",
            level="ok",
        )
        self.load(force=True)
