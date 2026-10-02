"""连接设置页：多目标（本机直连 / SSH 隧道）+ 令牌保管 + 隧道进程管理。

这一页是整个桌面端的"命门"，四条硬约束都落在这里：

1. **令牌不进 `targets.json`**：表单里写的是引用名，真值进凭据存储。
   页面上只显示"已保存 / 未保存"和用的是哪条后端（DPAPI / 降级文件 / 仅会话）。
2. **不给"隧道失败就改走公网直连"留任何口子**：模式是 ssh_tunnel 时，
   地址框直接禁用并显示"来自隧道转发端口"。
3. **只回收自己启动的隧道**：停止按钮只作用于本应用启动的那个 ssh 子进程。
4. **切目标后销毁旧会话**：由 `ConnectionManager.switch_target` 负责，
   这里只负责把界面切过去并让所有页面作废重载。

内嵌 SSH 终端**已独立成「终端」页**（与「总览」同级，见 `page_terminal.py`）：
隧道诊断输出与服务器命令是跨目标通用的工具，不该埋在某一个目标的表单下面。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtWidgets

from ..core import paths
from ..core.connection import ConnectionManager, discover_ssh
from ..core.targets import (
    MODE_LOCAL,
    MODE_SSH,
    ConfigWriteError,
    SshConfig,
    Target,
    TargetStore,
    duplicate,
)
from ..core.tunnel import STATE_FAILED, STATE_RUNNING, STATE_STARTING
from .base import Page, secret_notice
from .widgets import badge, esc, label


class ConnectionsPage(Page):
    title = "连接设置"
    subtitle = "本机直连 / 服务器 SSH 隧道（多目标、按目标隔离凭证）"

    def __init__(self, manager: ConnectionManager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._target: Target | None = None
        self._loading = False
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        # ---- 左：目标列表
        left = QtWidgets.QWidget()
        left.setMinimumWidth(190)
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.addWidget(QtWidgets.QLabel("目标"))
        self.target_list = QtWidgets.QListWidget()
        self.target_list.currentRowChanged.connect(self._on_select)
        left_layout.addWidget(self.target_list, 1)
        buttons = QtWidgets.QGridLayout()
        for index, (text, slot, tip) in enumerate((
            ("添加本机目标", lambda: self._add(MODE_LOCAL), "直连 127.0.0.1:8080（默认）"),
            ("添加服务器目标", lambda: self._add(MODE_SSH), "经 OpenSSH 本地端口转发访问"),
            ("复制选中", self._copy, "复制一份（新 id、新令牌引用）"),
            ("删除选中", self._remove, "删除目标（会确认）"),
        )):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(slot)
            button.setToolTip(tip)
            buttons.addWidget(button, index // 2, index % 2)
        left_layout.addLayout(buttons)
        splitter.addWidget(left)

        # ---- 右：表单
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        self.form_host = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.form_host)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

        self.name = QtWidgets.QLineEdit()
        self.name.editingFinished.connect(self._collect)
        form.addRow("名称", self.name)

        self.mode_label = QtWidgets.QLabel("-")
        form.addRow("模式", self.mode_label)

        self.base_url = QtWidgets.QLineEdit()
        self.base_url.setPlaceholderText("http://127.0.0.1:8080")
        self.base_url.editingFinished.connect(self._collect)
        form.addRow("服务地址", self.base_url)

        self.prefix = QtWidgets.QLineEdit()
        self.prefix.setPlaceholderText("/ai")
        self.prefix.editingFinished.connect(self._collect)
        form.addRow("API 前缀", self.prefix)

        token_row = QtWidgets.QWidget()
        token_layout = QtWidgets.QHBoxLayout(token_row)
        token_layout.setContentsMargins(0, 0, 0, 0)
        self.token = QtWidgets.QLineEdit()
        self.token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("留空 = 服务端不校验令牌")
        token_layout.addWidget(self.token, 1)
        self.btn_save_token = QtWidgets.QPushButton("保存令牌")
        self.btn_save_token.clicked.connect(lambda: self._save_token(session_only=False))
        self.btn_session_token = QtWidgets.QPushButton("仅本次会话")
        self.btn_session_token.setToolTip("只放内存，退出即失效（比降级文件更安全）")
        self.btn_session_token.clicked.connect(lambda: self._save_token(session_only=True))
        self.btn_clear_token = QtWidgets.QPushButton("清除")
        self.btn_clear_token.clicked.connect(self._clear_token)
        for widget in (self.btn_save_token, self.btn_session_token, self.btn_clear_token):
            token_layout.addWidget(widget)
        form.addRow("令牌", token_row)

        self.token_state = QtWidgets.QLabel("-")
        form.addRow("令牌状态", self.token_state)

        self.timeout = QtWidgets.QDoubleSpinBox()
        self.timeout.setRange(1.0, 600.0)
        self.timeout.setValue(15.0)
        self.timeout.setSuffix(" 秒")
        self.timeout.editingFinished.connect(self._collect)
        form.addRow("请求超时", self.timeout)

        self.verify_tls = QtWidgets.QCheckBox("校验 TLS 证书")
        self.verify_tls.setChecked(True)
        self.verify_tls.toggled.connect(lambda _v: self._collect())
        form.addRow("", self.verify_tls)

        right_layout.addWidget(self.form_host)

        # ---- SSH 段（只有隧道目标才显示）
        self.ssh_box = QtWidgets.QGroupBox("SSH 隧道")
        ssh_form = QtWidgets.QFormLayout(self.ssh_box)

        # 别名模式排在最前：项目里早就有 `启动隧道.ps1` 写好的 `Host qqbot`，
        # 复用它就不用在 GUI 里再抄一遍服务器 IP 与私钥路径。
        self.ssh_use_alias = QtWidgets.QCheckBox("用 ~/.ssh/config 里已有的 Host 别名（推荐）")
        self.ssh_use_alias.setToolTip(
            "勾上后主机/端口/用户/私钥全部交给 OpenSSH 的配置，桌面端不再传 -p/-i/-J，避免与配置打架"
        )
        self.ssh_alias = QtWidgets.QLineEdit()
        self.ssh_alias.setPlaceholderText("例如 qqbot（就是 `ssh qqbot` 里的那个名字）")
        self.ssh_alias.editingFinished.connect(self._collect)
        self.ssh_use_alias.toggled.connect(self._on_alias_toggled)

        alias_row = QtWidgets.QWidget()
        alias_layout = QtWidgets.QHBoxLayout(alias_row)
        alias_layout.setContentsMargins(0, 0, 0, 0)
        alias_layout.addWidget(self.ssh_alias, 1)
        self.btn_scan_aliases = QtWidgets.QPushButton("读 ~/.ssh/config")
        self.btn_scan_aliases.setToolTip("列出本机 ssh 配置里的 Host 别名，点一个就填上")
        self.btn_scan_aliases.clicked.connect(self._pick_alias)
        alias_layout.addWidget(self.btn_scan_aliases)

        self.ssh_host = QtWidgets.QLineEdit()
        self.ssh_port = QtWidgets.QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(22)
        self.ssh_user = QtWidgets.QLineEdit()
        self.ssh_key = QtWidgets.QLineEdit()
        self.ssh_key.setPlaceholderText(r"C:\Users\你\.ssh\id_ed25519")
        self.ssh_agent = QtWidgets.QCheckBox("使用系统密钥代理（Windows 的 ssh-agent）")
        self.ssh_jump = QtWidgets.QLineEdit()
        self.ssh_jump.setPlaceholderText("user@jump-host（不保存密码）")
        self.ssh_remote_host = QtWidgets.QLineEdit("127.0.0.1")
        self.ssh_remote_port = QtWidgets.QSpinBox()
        self.ssh_remote_port.setRange(1, 65535)
        self.ssh_remote_port.setValue(8080)
        self.ssh_local_port = QtWidgets.QLineEdit("auto")
        self.ssh_alive = QtWidgets.QSpinBox()
        self.ssh_alive.setRange(0, 600)
        self.ssh_alive.setValue(30)
        self.ssh_alive.setSuffix(" 秒")

        for widget in (self.ssh_host, self.ssh_key, self.ssh_jump, self.ssh_remote_host, self.ssh_local_port):
            widget.editingFinished.connect(self._collect)
        for spin in (self.ssh_port, self.ssh_remote_port, self.ssh_alive):
            spin.editingFinished.connect(self._collect)
        self.ssh_agent.toggled.connect(lambda _v: self._collect())

        ssh_form.addRow("", self.ssh_use_alias)
        ssh_form.addRow("SSH 别名", alias_row)
        ssh_form.addRow("主机", self.ssh_host)
        ssh_form.addRow("端口", self.ssh_port)
        ssh_form.addRow("用户", self.ssh_user)
        ssh_form.addRow("私钥文件", self.ssh_key)
        ssh_form.addRow("", self.ssh_agent)
        ssh_form.addRow("跳板机", self.ssh_jump)
        ssh_form.addRow("远端主机", self.ssh_remote_host)
        ssh_form.addRow("远端端口", self.ssh_remote_port)
        ssh_form.addRow("本地端口", self.ssh_local_port)
        ssh_form.addRow("保活间隔", self.ssh_alive)

        tunnel_buttons = QtWidgets.QHBoxLayout()
        self.btn_tunnel_start = QtWidgets.QPushButton("启动隧道")
        self.btn_tunnel_start.clicked.connect(self._start_tunnel)
        self.btn_tunnel_stop = QtWidgets.QPushButton("停止隧道")
        self.btn_tunnel_stop.clicked.connect(self._stop_tunnel)
        self.btn_tunnel_check = QtWidgets.QPushButton("检查状态")
        self.btn_tunnel_check.clicked.connect(self._check_tunnel)
        self.btn_ssh_cmd = QtWidgets.QPushButton("显示 ssh 命令")
        self.btn_ssh_cmd.setToolTip("复制隧道命令，并显示在下方内嵌终端")
        self.btn_ssh_cmd.clicked.connect(self._show_command)
        for widget in (self.btn_tunnel_start, self.btn_tunnel_stop, self.btn_tunnel_check, self.btn_ssh_cmd):
            tunnel_buttons.addWidget(widget)
        tunnel_buttons.addStretch(1)
        ssh_form.addRow("", self._wrap(tunnel_buttons))
        right_layout.addWidget(self.ssh_box)

        # 内嵌终端已独立成「终端」页（与「总览」同级，见 `page_terminal.py`）：
        # 隧道诊断输出与服务器命令是跨目标通用的工具，不该埋在某一个目标的表单下面。

        # ---- 底部：连接动作与状态
        actions = QtWidgets.QHBoxLayout()
        self.btn_test = QtWidgets.QPushButton("测试连接")
        self.btn_test.clicked.connect(self._probe)
        self.btn_activate = QtWidgets.QPushButton("切换到此目标")
        self.btn_activate.clicked.connect(self._activate)
        self.btn_save = QtWidgets.QPushButton("保存配置")
        self.btn_save.clicked.connect(self._save)
        for widget in (self.btn_test, self.btn_activate, self.btn_save):
            actions.addWidget(widget)
        actions.addStretch(1)
        right_layout.addLayout(actions)

        self.status = label("未选择目标")
        right_layout.addWidget(self.status)
        right_layout.addWidget(secret_notice(self.manager))
        # 配置落点摆在页面上：用户报"存不了目标"时，第一眼就该看到东西存在哪。
        self.where = label(f"配置落点：{paths.config_home()}", wrap=True)
        right_layout.addWidget(self.where)
        self.ssh_missing = label(
            "未找到 ssh 客户端：Windows 可在「设置 → 应用 → 可选功能」里安装「OpenSSH 客户端」",
            level="warn", wrap=True,
        )
        right_layout.addWidget(self.ssh_missing)
        self.ssh_missing.setVisible(not bool(discover_ssh()))
        right_layout.addStretch(1)

        right_scroll = QtWidgets.QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        outer.addWidget(splitter, 1)

    @staticmethod
    def _wrap(layout: QtWidgets.QLayout) -> QtWidgets.QWidget:
        holder = QtWidgets.QWidget()
        holder.setLayout(layout)
        return holder

    # ------------------------------------------------------------ 别名辅助
    def _on_alias_toggled(self, on: bool) -> None:
        """勾上别名模式就把手工字段禁掉 —— 让"哪些字段此刻有意义"一眼可见。"""
        for widget in (self.ssh_host, self.ssh_port, self.ssh_user, self.ssh_key,
                       self.ssh_agent, self.ssh_jump):
            widget.setEnabled(not on)
        self.ssh_alias.setEnabled(on)
        self.btn_scan_aliases.setEnabled(on)
        self._collect()

    @staticmethod
    def read_ssh_aliases() -> list[str]:
        """读 `~/.ssh/config` 里的 Host 别名（只读，不改文件）。

        `Host *` 这类通配条目跳过：它不是"一个可用的别名"，
        填进 `ssh <别名>` 里只会得到一个奇怪的结果。
        """
        path = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".ssh" / "config"
        if not path.exists():
            return []
        aliases: list[str] = []
        try:
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"(?i)^host\s+(.+)$", line)
                if not match:
                    continue
                for name in match.group(1).split():
                    if "*" in name or "?" in name or name.startswith("!"):
                        continue
                    if name not in aliases:
                        aliases.append(name)
        except OSError:
            return []
        return aliases

    def _pick_alias(self) -> None:
        found = self.read_ssh_aliases()
        if not found:
            self.toast(
                r"没读到别名：确认 %USERPROFILE%\.ssh\config 存在，且里面有 `Host xxx` 这样的条目",
                level="warn",
            )
            return
        current = self.ssh_alias.text().strip()
        index = found.index(current) if current in found else 0
        name, ok = QtWidgets.QInputDialog.getItem(
            self, "选择 SSH 别名", "本机 ~/.ssh/config 里的 Host 别名：", found, index, False
        )
        if ok and name:
            self.ssh_alias.setText(name)
            self._collect()

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self._refresh_list()
        # 兜底：列表里没有可选项时（目标被删干净之类）别让整页静默失效。
        if self._target is None:
            self.toast("没有可用的连接目标：点左下「添加本机目标」或「添加服务器目标」", level="warn")
        self.refreshed.emit()

    def _refresh_list(self) -> None:
        self._loading = True
        try:
            self.target_list.clear()
            active_id = self.manager.store.active_id
            for target in self.manager.targets():
                mark = "★ " if target.id == active_id else "   "
                item = QtWidgets.QListWidgetItem(f"{mark}{target.name}")
                item.setToolTip(target.summary())
                self.target_list.addItem(item)
            row = next(
                (i for i, t in enumerate(self.manager.targets()) if t.id == active_id), 0
            )
            self.target_list.setCurrentRow(row)
        finally:
            self._loading = False
        # 这里直接调一次，**不依赖 `currentRowChanged` 信号**：
        # 若当前行本来就是目标行，`setCurrentRow` 不会发信号，`_target` 会一直是 None
        # （表现就是"页面有了目标、点启动隧道却没反应"）。
        self._on_select(self.target_list.currentRow())

    def _on_select(self, row: int) -> None:
        if self._loading:
            return
        targets = self.manager.targets()
        if not targets:
            self._target = None
            self._update_status()
            return
        # 行号无效（-1 或越界）时退回当前激活目标，而不是把 `_target` 留成 None ——
        # 设置页一旦"静默失效"，用户只会看到按钮点了没反应。
        if row < 0 or row >= len(targets):
            row = next(
                (i for i, t in enumerate(targets) if t.id == self.manager.store.active_id), 0
            )
            if self.target_list.currentRow() != row:
                self.target_list.setCurrentRow(row)
        self._target = targets[row]
        self._fill(self._target)
        # `_fill()` 走的是"文件里那份"，而地址框要显示的是**当前生效**的地址
        # （可能是刚启动的隧道端口）。落到行号没变时 `currentRowChanged` 不发信号，
        # 所以这里显式再同步一次。
        self._sync_address_field()

    def _fill(self, target: Target) -> None:
        self._loading = True
        try:
            self.name.setText(target.name)
            self.mode_label.setText("SSH 隧道" if target.is_tunnel else "本机直连")
            self.prefix.setText(target.http.normalized_prefix())
            self.timeout.setValue(float(target.http.timeout or 15.0))
            self.verify_tls.setChecked(bool(target.http.verify_tls))
            self.token.clear()
            saved = self.manager.creds.get(target.http.token_ref) if target.http.token_ref else ""
            self.token_state.setText(
                ("已保存（" + self.manager.creds.describe() + "）") if saved else "未保存"
            )

            self.ssh_box.setVisible(target.is_tunnel)
            self.ssh_use_alias.setChecked(bool(target.ssh.use_alias))
            self.ssh_alias.setText(target.ssh.alias)
            self.ssh_host.setText(target.ssh.host)
            self.ssh_port.setValue(int(target.ssh.port or 22))
            self.ssh_user.setText(target.ssh.user)
            self.ssh_key.setText(target.ssh.identity_file)
            self.ssh_agent.setChecked(bool(target.ssh.identity_agent))
            self.ssh_jump.setText(target.ssh.proxy_jump)
            self.ssh_remote_host.setText(target.ssh.remote_host)
            self.ssh_remote_port.setValue(int(target.ssh.remote_port or 8080))
            self.ssh_local_port.setText(str(target.ssh.local_port or "auto"))
            self.ssh_alive.setValue(int(target.ssh.server_alive_interval or 30))
            # 手工字段的启用状态跟着勾选框走。**必须留在 `_loading` 还立着的时候调**：
            # 它内部会 `_collect()`，而 `_collect()` 会拿地址框的现值覆盖
            # `target.http.base_url` —— 首屏/切目标时地址框还没被下面那行填上，
            # 放它出去就等于用空串覆盖掉配置里真实的地址（本机目标地址框变空，实测踩到）。
            self._on_alias_toggled(bool(self.ssh_use_alias.isChecked()))
        finally:
            self._loading = False
        self._sync_address_field()
        self._update_status()

    # ------------------------------------------------------------ 地址框
    def _tunnel_base_url(self) -> str:
        """当前目标那条隧道的本地转发地址；没有就返回空串。"""
        if self._target is None or not self._target.is_tunnel:
            return ""
        tunnel = getattr(self.manager, "_tunnel", None)
        if tunnel is None or getattr(self.manager, "_tunnel_target_id", "") != self._target.id:
            return ""
        return tunnel.status.base_url or ""

    def _sync_address_field(self) -> None:
        """把"这个目标此刻该访问哪个地址"反映到服务地址框里。

        **隧道目标每次状态变化都要同步一次** —— 端口是启动时才分配的，
        只在 `_fill()` 里填一次的话，用户会一直看到一个空框（或者一个失效的旧端口），
        然后以为"启动隧道没生效"（用户实测报回）。
        """
        if self._target is None:
            self.base_url.clear()
            self.base_url.setEnabled(False)
            self.base_url.setPlaceholderText("先选一个目标")
            return
        if self._target.is_tunnel:
            base = self._tunnel_base_url()
            self.base_url.setText(base)
            self.base_url.setEnabled(False)   # 派生值：手改它会绕过隧道，一律只读
            self.base_url.setPlaceholderText("由隧道转发端口决定（点「启动隧道」后自动填入）")
        else:
            self.base_url.setText(self._target.http.base_url)
            self.base_url.setEnabled(True)
            self.base_url.setPlaceholderText("http://127.0.0.1:8080")

    def _on_connection_state(self) -> None:
        """连接状态（探测成功/失败、隧道起停、切目标）变化时刷新本页。

        由主窗口在 `ConnectionState` 变化时调用。**必须挂上**：否则"目标已经切到
        本机、地址框却还空着"这类不同步会一直存在（实测踩过）。
        """
        if self._target is None:
            # 页面还没选过目标（比如首屏还没轮到本页加载）：顺手同步一次列表，
            # 免得用户点进来看见空列表。
            self._refresh_list()
            return
        self._sync_address_field()
        self._update_status()

    def _update_status(self) -> None:
        tune = self.manager.state
        parts = [f"当前目标：<b>{esc(tune.target_name)}</b>", esc(tune.address_summary())]
        if tune.api_ok:
            parts.append(badge("在线", level="ok"))
        else:
            parts.append(badge("离线", level="err"))
        tunnel = getattr(self.manager, "_tunnel", None)
        if tunnel is not None and self._target is not None and getattr(self.manager, "_tunnel_target_id", "") == self._target.id:
            status = tunnel.status
            level = {"running": "ok", "starting": "warn", "failed": "err"}.get(status.state, "info")
            parts.append(badge("隧道 " + status.human(), level=level))
            if status.log_tail:
                parts.append("<br><code>" + esc(" / ".join(status.log_tail[-3:])) + "</code>")
        if self._target is not None and self._target.is_tunnel:
            parts.append("<br>" + esc(self._target.summary()))
        # 状态每次变都同步地址框：隧道起停、断线、切目标都会走到这里。
        self._sync_address_field()
        self.status.setText(" · ".join(parts))
        self.status.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.status.setWordWrap(True)
        self.btn_tunnel_start.setEnabled(bool(self._target and self._target.is_tunnel))
        self.btn_tunnel_stop.setEnabled(bool(self._target and self._target.is_tunnel))

    # ------------------------------------------------------------ 收集表单
    def _collect(self) -> bool:
        """把表单写回内存里的目标对象。

        返回是否**落盘成功**。写不进去（配置目录只读）时不抛给用户一句 WinError，
        而是给出落点、原因和三条处理办法 —— 见 `ConfigWriteError`。
        """
        if self._loading or self._target is None:
            return False
        target = self._target
        target.name = self.name.text().strip() or target.name
        target.http.prefix = self.prefix.text().strip() or "/ai"
        target.http.timeout = float(self.timeout.value())
        target.http.verify_tls = bool(self.verify_tls.isChecked())
        if not target.is_tunnel:
            target.http.base_url = self.base_url.text().strip()
        else:
            use_alias = bool(self.ssh_use_alias.isChecked())
            target.ssh = SshConfig(
                host=self.ssh_host.text().strip(),
                port=int(self.ssh_port.value()),
                user=self.ssh_user.text().strip(),
                identity_file=self.ssh_key.text().strip(),
                identity_agent=bool(self.ssh_agent.isChecked()),
                proxy_jump=self.ssh_jump.text().strip(),
                remote_host=self.ssh_remote_host.text().strip() or "127.0.0.1",
                remote_port=int(self.ssh_remote_port.value()),
                local_port=self.ssh_local_port.text().strip() or "auto",
                server_alive_interval=int(self.ssh_alive.value()),
                alias=self.ssh_alias.text().strip(),
                use_alias=use_alias,
            )
        return self._store_update(target)

    def _store_update(self, target: Target) -> bool:
        try:
            self.manager.store.update(target)
            return True
        except ConfigWriteError as exc:
            self.toast(str(exc), level="error")
            return False

    def _save(self) -> None:
        if self._target is None or not self._collect():
            if self._target is None:
                self.toast("先选一个目标", level="warn")
            return
        self.toast("配置已保存（令牌不在这个文件里）", level="ok")
        self._refresh_list()

    # ------------------------------------------------------------ 目标增删
    def _add(self, mode: str) -> None:
        target = Target(name="本机（直连）" if mode == MODE_LOCAL else "服务器（SSH 隧道）", mode=mode)
        if mode == MODE_LOCAL:
            target.http.base_url = "http://127.0.0.1:8080"
            target.http.prefix = "/ai"
        else:
            target.ssh = SshConfig(remote_host="127.0.0.1", remote_port=8080, local_port="auto")
        target.http.token_ref = TargetStore.suggest_token_ref(target)
        try:
            self.manager.store.add(target)
        except ConfigWriteError as exc:
            self.toast(str(exc), level="error")
            return
        self._refresh_list()
        self.toast("已添加目标，记得保存配置并填 SSH 参数", level="info")

    def _copy(self) -> None:
        if self._target is None:
            return
        self._collect()
        clone = duplicate(self._target)
        try:
            self.manager.store.add(clone)
        except ConfigWriteError as exc:
            self.toast(str(exc), level="error")
            return
        self._refresh_list()
        self.toast("已复制目标（新 id、新令牌引用：不会共用同一个令牌）", level="info")

    def _remove(self) -> None:
        if self._target is None:
            return
        if not QtWidgets.QMessageBox.question(
            self, "删除目标",
            f"删除目标「{self._target.name}」？\n\n"
            "该目标的令牌引用也会从配置里移除（凭据存储里的条目保留，可在系统凭据管理器里自行清理）。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        try:
            self.manager.store.remove(self._target.id)
        except ConfigWriteError as exc:
            self.toast(str(exc), level="error")
            return
        self._target = None
        self._refresh_list()

    def _activate(self) -> None:
        if self._target is None:
            return
        if not self._collect():
            # 配置没落盘也允许切换（本次会话内有效），但必须说清楚"重启就没了"。
            self.toast("配置没写进磁盘，只在本次会话内有效（重启桌面程序后会丢）", level="warn")
        self.manager.switch_target(self._target.id)
        self._refresh_list()
        self.toast(f"已切换到「{self._target.name}」；其它页面数据已作废，将重新加载", level="ok")

    # ------------------------------------------------------------ 令牌
    def _save_token(self, *, session_only: bool) -> None:
        if self._target is None:
            return
        value = self.token.text()
        if not value and not session_only:
            self.toast("令牌为空：留空 = 服务端不校验（服务端 token 为空时才是正常的）", level="warn")
        backend = self.manager.set_token(value, target=self._target, session_only=session_only)
        self.token.clear()
        self.token_state.setText(("已保存（" + backend + "）") if value else "未保存")
        self.toast(f"令牌已写入：{backend}", level="ok")

    def _clear_token(self) -> None:
        if self._target is None:
            return
        try:
            self.manager.clear_token(target=self._target)
        except Exception as exc:  # noqa: BLE001 - 清令牌失败要让用户知道，别静默
            self.toast(f"清除令牌失败：{exc}", level="error")
            return
        self.token_state.setText("未保存")
        self.toast("令牌已清除", level="ok")

    # ------------------------------------------------------------ 连接动作
    def _probe(self) -> None:
        self._collect()
        if self._target is not None and self._target.is_tunnel:
            tunnel = getattr(self.manager, "_tunnel", None)
            ready = bool(tunnel and tunnel.status.running and getattr(self.manager, "_tunnel_target_id", "") == self._target.id)
            if not ready:
                self.toast("服务器目标必须先点「启动隧道」；桌面端不会回退到公网直连", level="warn")
                return

        def _run():  # noqa: ANN202
            return self.manager.probe()

        self.run_task(
            _run,
            on_done=self._after_probe,
            busy_text="正在测试连接…",
        )

    def _after_probe(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self._update_status()
            return
        probe = result.value
        self._update_status()
        if probe.ok:
            self.toast(f"连接正常（{probe.elapsed_ms} ms，{probe.detail}）", level="ok")
            window = self.window()
            if hasattr(window, "on_connection_changed"):
                window.on_connection_changed()  # type: ignore[attr-defined]
        else:
            self.toast(f"{probe.detail}｜{probe.hint}", level="err")

    def _start_tunnel(self) -> None:
        if self._target is None or not self._target.is_tunnel:
            self.toast("当前目标不是隧道模式", level="warn")
            return
        self._collect()  # 落盘失败已在内部提示；建隧道本身不依赖它成功
        target = self._target

        def _run():  # noqa: ANN202
            return self.manager.start_tunnel(target)

        # ssh 的诊断输出会实时出现在「终端」页；这里只在状态条上给一句进度。
        self.run_task(_run, on_done=self._after_tunnel, busy_text="正在建立隧道（最长等 20 秒）…")

    def _stop_tunnel(self) -> None:
        if self._target is None:
            return
        target = self._target
        self.run_task(lambda: self.manager.stop_tunnel(target), on_done=self._after_tunnel, busy_text="正在关闭隧道…")

    def _check_tunnel(self) -> None:
        tunnel = getattr(self.manager, "_tunnel", None)
        if tunnel is None:
            self.toast("还没有启动过隧道", level="info")
            return
        target = self._target
        status = tunnel.check(health=lambda port: self.manager._tunnel_health(port, target))  # noqa: SLF001
        self._update_status()
        self.toast("隧道状态：" + status.human(), level="ok" if status.running else "warn")

    def _after_tunnel(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self._update_status()
            return
        status = result.value
        self._update_status()   # 内部会调 `_sync_address_field()`：地址框在这里被自动填入
        if status.state == STATE_RUNNING:
            self.toast(f"隧道已就绪：{status.base_url} → {status.detail}；服务地址已填入", level="ok")
            if self._target is not None:
                self._fill(self._target)
        elif status.state == STATE_STARTING:
            self.toast("隧道正在建立…", level="info")
        else:
            self.toast(
                f"隧道不可用：{status.error or '未知原因'}\n"
                "服务器目标将保持离线并禁止写操作（不会改走公网直连）",
                level="err",
            )

    def _show_command(self) -> None:
        if self._target is None or not self._target.is_tunnel:
            self.toast("当前目标不是隧道模式", level="warn")
            return
        self._collect()
        tunnel = self.manager.tunnel_for(self._target)
        local = tunnel.ssh.local_port_value() or 18080
        try:
            command = tunnel.command_text(local)
        except ValueError as exc:
            self.toast(str(exc), level="warn")
            return
        QtWidgets.QApplication.clipboard().setText(command)
        self.toast("隧道命令已复制；诊断输出请看「终端」页", level="info")
