"""终端页：内嵌 SSH 终端 —— 服务器命令与隧道诊断输出都落在这里。

为什么要从「连接设置」页提出来（2026-10-02 用户要求）：
  隧道一建立就要看诊断输出，而连接设置页是"配参数"的地方、不是常驻视图；
  服务器命令更是**跨目标通用**的工具，不该藏在某个目标的表单底下。
  提到与「总览」同级之后，开着终端就能一边看隧道重连、一边敲命令。

三条设计口径：

1. **目标永远是当前激活目标**（`ConnectionManager.active_target()`），不跟着连接设置页里
   "正在编辑的那一行"走 —— 终端连的是**实际在用的那条链路**。
2. **隧道输出用轮询 `tunnel.status.log_tail`**（200ms），而不是让 core 层反向依赖 Qt：
   `desktop/core/` 不许 import Qt，那条边界由自检守着（见 README 第 0 节）。
3. **命令走 `QProcess` + 系统 OpenSSH**，跑完即回收；不在应用内实现 SSH 协议。
   `QProcess` 天然不弹控制台窗口，输出直接进下面的面板。
"""

from __future__ import annotations

import re

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.connection import ConnectionManager, discover_ssh
from .base import Page
from .widgets import badge, esc, label, plain_text_edit

_SCREEN_QSS = (
    "QPlainTextEdit { background:#111827; color:#dbe7f5; border:1px solid #344054; "
    "border-radius:6px; padding:8px; font-family:Consolas, 'Cascadia Mono', monospace; "
    "font-size:12px; selection-background-color:#3568c8; }"
)


class TerminalPage(Page):
    title = "终端"
    subtitle = "内嵌 SSH 终端：服务器命令与隧道诊断输出"

    def __init__(self, manager: ConnectionManager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._last_tail: list[str] = []
        self._build()
        # 隧道日志由 core 层收集（它自己不碰 Qt），这里按固定节奏取增量。
        self._log_timer = QtCore.QTimer(self)
        self._log_timer.setInterval(200)
        self._log_timer.timeout.connect(self._poll_tunnel_log)
        self._log_timer.start()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        self.status = label("未选择目标")
        self.status.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        self.screen = plain_text_edit("")
        self.screen.setReadOnly(True)
        self.screen.setPlaceholderText("服务器命令输出与隧道诊断日志会显示在这里…")
        self.screen.document().setMaximumBlockCount(2000)
        self.screen.setMinimumHeight(220)
        self.screen.setStyleSheet(_SCREEN_QSS)
        outer.addWidget(self.screen, 1)

        row = QtWidgets.QHBoxLayout()
        self.command = QtWidgets.QLineEdit()
        self.command.setPlaceholderText("例如：docker compose ps")
        self.command.returnPressed.connect(self._run_command)
        row.addWidget(self.command, 1)
        self.btn_run = QtWidgets.QPushButton("执行")
        self.btn_run.clicked.connect(self._run_command)
        row.addWidget(self.btn_run)
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.clicked.connect(self._stop_command)
        self.btn_stop.setEnabled(False)
        row.addWidget(self.btn_stop)
        self.btn_clear = QtWidgets.QPushButton("清屏")
        self.btn_clear.clicked.connect(self.screen.clear)
        row.addWidget(self.btn_clear)
        self.btn_ssh_cmd = QtWidgets.QPushButton("复制 ssh 命令")
        self.btn_ssh_cmd.setToolTip("把当前目标的隧道命令复制到剪贴板（不含密钥内容）")
        self.btn_ssh_cmd.clicked.connect(self._copy_ssh_command)
        row.addWidget(self.btn_ssh_cmd)
        outer.addLayout(row)

        self._process = QtCore.QProcess(self)
        self._process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._read_output)
        self._process.finished.connect(self._finished)
        self._process.errorOccurred.connect(self._process_error)

    # ------------------------------------------------------------ 对外
    def append(self, text: str) -> None:
        """给主窗口/连接设置页用的入口：往终端里追加一段文字。"""
        self._append(str(text))

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self._refresh_header()

    def _on_target_changed(self) -> None:
        """切目标：旧目标的日志与命令上下文全部作废。"""
        self._last_tail = []
        target = self.manager.active_target()
        self._append(f"\n--- 已切换目标：{target.name} ---\n")
        self._refresh_header()

    def _on_connection_state(self) -> None:
        """隧道起停/断线时主窗口会调这里（见 `MainWindow._on_state_changed`）。"""
        self._refresh_header()

    def _tunnel(self):  # noqa: ANN202
        """**当前激活目标**那条隧道；不是它的一条都不认。"""
        tunnel = getattr(self.manager, "_tunnel", None)  # noqa: SLF001 - 与连接页同一读法
        if tunnel is None:
            return None
        if getattr(self.manager, "_tunnel_target_id", "") != self.manager.store.active_id:
            return None
        return tunnel

    def _refresh_header(self) -> None:
        target = self.manager.active_target()
        mode = "SSH 隧道" if target.is_tunnel else "本机直连"
        bits = [f"当前目标：<b>{esc(target.name)}</b>（{mode}）"]
        if not target.is_tunnel:
            bits.append(badge("非隧道目标：没有可连的服务器", level="warn"))
        tunnel = self._tunnel()
        if tunnel is not None:
            status = tunnel.status
            level = {"running": "ok", "starting": "warn", "failed": "err"}.get(status.state, "info")
            bits.append(badge("隧道 " + status.human(), level=level))
            if status.base_url:
                bits.append(f"<code>{esc(status.base_url)}</code>")
        self.status.setText(" · ".join(bits))
        self.status.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.status.setWordWrap(True)

    # ------------------------------------------------------------ 终端输出
    def _append(self, text: str) -> None:
        if not text:
            return
        cursor = self.screen.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self.screen.setTextCursor(cursor)
        self.screen.ensureCursorVisible()

    def _poll_tunnel_log(self) -> None:
        """把隧道 ssh 的 stderr/stdout 增量搬进终端。

        为什么是轮询而不是信号：`desktop/core/` 不许 import Qt（自检守着那条边界），
        所以 core 只往 `status.log_tail` 里存，界面自己按节奏取。
        """
        tunnel = self._tunnel()
        if tunnel is None:
            return
        tail = list(tunnel.status.log_tail)
        if tail == self._last_tail:
            return
        overlap = 0
        for size in range(min(len(self._last_tail), len(tail)), 0, -1):
            if self._last_tail[-size:] == tail[:size]:
                overlap = size
                break
        if self._last_tail and not overlap and tail:
            self._append("\n[SSH 日志缓冲已滚动]\n")
        for line in tail[overlap:]:
            self._append(line + "\n")
        self._last_tail = tail
        self._refresh_header()

    # ------------------------------------------------------------ 服务器命令
    def _run_command(self) -> None:
        target = self.manager.active_target()
        command = self.command.text().strip()
        if not target.is_tunnel:
            self.toast("终端只对 SSH 隧道目标有效：先在「连接设置」里把当前目标切成服务器", level="warn")
            return
        if not command:
            return
        if self._process.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.toast("上一条服务器命令仍在运行", level="warn")
            return
        cfg = target.ssh
        problems = cfg.validate()
        if problems:
            self.toast("SSH 配置不完整：" + "；".join(problems), level="warn")
            return
        ssh_exe = discover_ssh()
        if not ssh_exe:
            self.toast("找不到 OpenSSH 客户端，请安装 Windows 可选功能中的 OpenSSH 客户端", level="err")
            return
        args = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if cfg.use_alias:
            destination = cfg.alias.strip()
        else:
            destination = f"{cfg.user + '@' if cfg.user else ''}{cfg.host}"
            args += ["-p", str(int(cfg.port or 22))]
            if cfg.identity_file:
                args += ["-i", str(cfg.identity_file)]
            if cfg.identity_agent:
                args += ["-o", "IdentitiesOnly=no"]
            if cfg.proxy_jump:
                args += ["-J", cfg.proxy_jump]
        args.extend([destination, command])
        self._append(f"\n[{target.name}] $ {command}\n")
        self.command.clear()
        self._process.setProgram(ssh_exe)
        self._process.setArguments(args)
        self._process.start()
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)

    def _read_output(self) -> None:
        data = bytes(self._process.readAllStandardOutput())
        text = data.decode("utf-8", errors="replace")
        # ssh 会给 TTY 输出上色；这里没有终端仿真，直接剥掉 ANSI 转义。
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        self._append(text.replace("\r\n", "\n").replace("\r", "\n"))

    def _finished(self, exit_code: int, _exit_status) -> None:  # noqa: ANN001
        self._read_output()
        self._append(f"\n[SSH 命令结束，退出码 {int(exit_code)}]\n")
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)

    def _process_error(self, _error) -> None:  # noqa: ANN001
        if self._process.error() == QtCore.QProcess.ProcessError.FailedToStart:
            self._append(f"\n[无法启动 SSH：{self._process.errorString()}]\n")
            self.btn_run.setEnabled(True)
            self.btn_stop.setEnabled(False)

    def _stop_command(self) -> None:
        if self._process.state() == QtCore.QProcess.ProcessState.NotRunning:
            return
        self._append("\n[正在停止 SSH 命令…]\n")
        self._process.terminate()
        QtCore.QTimer.singleShot(
            1200,
            lambda: self._process.kill()
            if self._process.state() != QtCore.QProcess.ProcessState.NotRunning else None,
        )

    def _copy_ssh_command(self) -> None:
        target = self.manager.active_target()
        if not target.is_tunnel:
            self.toast("当前目标不是隧道模式", level="warn")
            return
        tunnel = self.manager.tunnel_for(target)
        local = tunnel.ssh.local_port_value() or 18080
        try:
            command = tunnel.command_text(local)
        except ValueError as exc:
            self.toast(str(exc), level="warn")
            return
        QtWidgets.QApplication.clipboard().setText(command)
        self._append(f"\n$ {command}\n[隧道命令已复制到剪贴板]\n")
        self.toast("隧道命令已复制", level="info")
