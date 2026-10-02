"""受管 SSH 隧道（用系统 OpenSSH，不在应用内实现 SSH 协议）。

为什么不做"在 Python 里实现 SSH"：那是把一件成熟的事重做一遍，还得自己保证
密钥、算法、代理这些细节不出错。Windows 10/11 自带 OpenSSH 客户端
（本机实测 `C:\\WINDOWS\\System32\\OpenSSH\\ssh.exe`，OpenSSH_for_Windows_9.5p2），
直接把它当受管子进程用即可。

命令行里那几个开关**不是装饰**（少一个就会表现成"隧道永远在连"）：

    -N              只要转发，不要远程 shell
    -T              不分配伪终端
    -o BatchMode=yes            认证失败**立刻退出**，而不是挂住等输密码
    -o ExitOnForwardFailure=yes 本地端口被占时立刻失败，而不是"看起来起来了"
    -o ServerAliveInterval=30   保活
    -o ConnectTimeout=10        连不上时不无限等

安全边界（对应方案 3.1）：

- **只回收自己启动的进程**：不是"杀掉所有 ssh.exe"。用户可能自己有隧道/别的会话。
- **不回退公网直连**：隧道失败就是失败，上层把服务器目标标成离线并禁止写操作。
- **不接收私钥内容**：只传 `-i <路径>`，让 OpenSSH 自己读文件 / 走密钥代理。
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .targets import SshConfig
from .util import one_line, redact_text, summarize_lines

logger = logging.getLogger("qqbot.desktop.tunnel")

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_FAILED = "failed"

# 启动一个隧道最多容忍几次"起来又断"的重连（方案要求"有限次数"）。
DEFAULT_MAX_RESTARTS = 3
# 等端口/健康检查的总预算。
DEFAULT_READY_TIMEOUT = 20.0
_TAIL_LINES = 200


def find_ssh() -> str:
    """找 OpenSSH 客户端。找不到返回空串（上层提示去"可选功能"里装）。"""
    found = shutil.which("ssh")
    if found:
        return found
    fallback = Path(os.environ.get("WINDIR", r"C:\WINDOWS")) / "System32" / "OpenSSH" / "ssh.exe"
    return str(fallback) if fallback.exists() else ""


def pick_free_port() -> int:
    """让系统挑一个空闲本地端口。

    注意这是"挑完就释放"的经典竞态：真正的占用判定还要靠 `ExitOnForwardFailure`
    与端口探测（所以 `start()` 会等端口真的可连才算成功）。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class TunnelStatus:
    state: str = STATE_STOPPED
    pid: int = 0
    local_port: int = 0
    restarts: int = 0
    error: str = ""
    detail: str = ""
    started_at: float = 0.0
    log_tail: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}" if self.local_port else ""

    @property
    def running(self) -> bool:
        return self.state == STATE_RUNNING

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "pid": self.pid,
            "local_port": self.local_port,
            "restarts": self.restarts,
            "error": self.error,
            "detail": self.detail,
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "base_url": self.base_url,
        }

    def human(self) -> str:
        return {
            STATE_STOPPED: "未启动",
            STATE_STARTING: "正在建立…",
            STATE_RUNNING: f"运行中（本地端口 {self.local_port}）",
            STATE_FAILED: f"失败：{self.error or '未知原因'}",
        }[self.state]


class SshTunnel:
    """一个目标的隧道。每个目标一个实例，互不干扰。"""

    def __init__(
        self,
        ssh: SshConfig,
        *,
        ssh_exe: str = "",
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
    ) -> None:
        self.ssh = ssh
        self.ssh_exe = ssh_exe or find_ssh()
        self.max_restarts = max(0, int(max_restarts))
        self.ready_timeout = float(ready_timeout)
        self.status = TunnelStatus()
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._log_tail: list[str] = []

    # ------------------------------------------------------------ 命令行
    def build_command(self, local_port: int) -> list[str]:
        """拼 ssh 命令行。

        两种模式，**互斥**：

        * **别名模式**（`cfg.use_alias`）：目标就是 `~/.ssh/config` 里的 Host 别名，
          主机/用户/端口/私钥全交给 OpenSSH 的配置决定，我们**不再传 `-p` / `-i` / `-J`** ——
          传了就会与配置打架（这是配 SSH 别名最常见的翻车点）。
          项目里 `启动隧道.ps1` 早就写好了 `Host qqbot` 这类别名，桌面端直接复用即可，
          不必让用户把服务器 IP 与私钥路径再抄一遍。
        * **手工模式**：字段全部来自 GUI。

        两种模式下 `-L` 都是**我们显式给的**。SSH 别名中已有的 `LocalForward` 仍会由
        OpenSSH 读取；不能用 `ClearAllForwardings` 清除，因为它也会清掉这里传入的 `-L`，
        导致 ssh 进程存活但桌面端监听的本地端口始终未就绪。
        """
        cfg = self.ssh
        args = [
            self.ssh_exe or "ssh",
            "-N",
            "-T",
            "-v",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", f"ConnectTimeout={int(cfg.connect_timeout or 10)}",
            "-o", f"ServerAliveInterval={int(cfg.server_alive_interval or 30)}",
            "-o", f"ServerAliveCountMax={int(cfg.server_alive_count_max or 3)}",
        ]
        if cfg.use_alias:
            target = cfg.alias.strip()
            if not target:
                raise ValueError("别名模式需要填 SSH 别名（`~/.ssh/config` 里的 Host 名）")
        else:
            target = f"{cfg.user + '@' if cfg.user else ''}{cfg.host}"
            args += ["-p", str(int(cfg.port or 22))]
            if cfg.identity_file:
                args += ["-i", str(cfg.identity_file)]
            if cfg.identity_agent:
                # 显式指定用系统密钥代理（否则 ssh 可能忽略 agent 里的密钥）。
                args += ["-o", "IdentitiesOnly=no"]
            if cfg.proxy_jump:
                args += ["-J", str(cfg.proxy_jump)]
        args += ["-L", f"127.0.0.1:{local_port}:{cfg.remote_host}:{int(cfg.remote_port)}"]
        args.append(target)
        return args

    def command_text(self, local_port: int = 0) -> str:
        """可展示/可复制的命令行（**不含密钥内容，不含密码**）。"""
        return " ".join(self.build_command(local_port or self.status.local_port or 0))

    # ------------------------------------------------------------ 生命周期
    def start(self, *, health: Callable[[int], bool] | None = None,
              on_change: Callable[[TunnelStatus], None] | None = None,
              allow_restart: bool = True) -> TunnelStatus:
        """启动隧道并等它真的可用。

        `health(local_port)` 由调用方给（一般是"发一次 API 探测"），
        返回 True 才算可用 —— 端口在监听不等于后端在跑。
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self.status
            problems = self.ssh.validate()
            if problems:
                return self._fail("；".join(problems), on_change)
            if not self.ssh_exe or not Path(self.ssh_exe).exists():
                return self._fail(
                    "找不到 ssh 客户端：Windows 可在「设置 → 应用 → 可选功能」里装 OpenSSH 客户端",
                    on_change,
                )
            local_port = self.ssh.local_port_value() or pick_free_port()
            self.status = TunnelStatus(state=STATE_STARTING, local_port=local_port)
            self.status.started_at = time.time()
            self._notify(on_change)

            attempts = self.max_restarts + 1 if allow_restart else 1
            for attempt in range(attempts):
                if attempt:
                    self.status.restarts = attempt
                    self.status.state = STATE_STARTING
                    self.status.error = ""
                    self._notify(on_change)
                    time.sleep(min(3.0, 0.5 * (2 ** attempt)))
                ok, why = self._spawn_and_wait(local_port, health)
                if ok:
                    self.status.state = STATE_RUNNING
                    self.status.error = ""
                    self.status.detail = f"{self.ssh.remote_host}:{self.ssh.remote_port}"
                    self._notify(on_change)
                    logger.info(
                        "隧道已就绪：127.0.0.1:%s → %s:%s（pid %s）",
                        local_port, self.ssh.remote_host, self.ssh.remote_port, self.status.pid,
                    )
                    return self.status
                logger.warning("隧道第 %s 次尝试失败：%s", attempt + 1, why)
            return self._fail(why or "隧道未能建立", on_change)

    def _spawn_and_wait(
        self, local_port: int, health: Callable[[int], bool] | None
    ) -> tuple[bool, str]:
        try:
            cmd = self.build_command(local_port)
        except ValueError as exc:
            # 别名模式没填别名之类：给一句人话，不要把它当成"ssh 进程退出"。
            return False, str(exc)
        logger.info("启动隧道：%s", redact_text(" ".join(cmd)))
        creation = 0
        if os.name == "nt":
            # 新进程组：这样 Ctrl+C 关掉 GUI 时不会连带把 ssh 变成孤儿。
            creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            # **必须同时禁止新建控制台窗口**：桌面端的父进程是 pythonw.exe（没有控制台），
            # 而 ssh.exe 是控制台程序 —— 不给这个标志时 Windows 会为它新建一个黑窗口，
            # 于是"启动隧道"每次都弹一个 cmd 窗口出来（用户实测报回）。
            # ssh 的输出本来就走下面的管道收进桌面端终端，不需要那个窗口。
            creation |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation,
            )
        except OSError as exc:
            return False, f"启动 ssh 失败：{type(exc).__name__}: {one_line(exc, 200)}"
        self.status.pid = int(self._proc.pid or 0)
        # 收集输出：ssh 的报错（权限被拒、端口占用）只会打在 stderr 上，必须留证。
        self._log_tail: list[str] = []
        threading.Thread(target=self._drain, args=(self._proc,), daemon=True).start()

        deadline = time.time() + self.ready_timeout
        port_seen = False
        while time.time() < deadline:
            if self._proc.poll() is not None:
                return False, self._ended_reason()
            if port_open("127.0.0.1", local_port):
                port_seen = True
                if health is None:
                    return True, ""
                try:
                    if health(local_port):
                        return True, ""
                except Exception as exc:  # noqa: BLE001 - 健康检查失败当失败处理
                    return False, f"健康检查异常：{type(exc).__name__}: {one_line(exc, 160)}"
            time.sleep(0.25)
        if port_seen:
            return False, (
                f"本地转发端口 {local_port} 已监听，但远端 API 健康检查未通过；"
                f"请确认服务 {self.ssh.remote_host}:{self.ssh.remote_port} 正在运行，"
                "且 API 前缀配置正确"
            )
        return False, (
            f"等待 {self.ready_timeout:g}s 后本地转发端口 {local_port} 仍未监听；"
            "请检查 SSH 转发参数与本地端口占用"
        )

    def _ended_reason(self) -> str:
        tail = summarize_lines(getattr(self, "_log_tail", []), 4)
        code = self._proc.returncode if self._proc else None
        return f"ssh 进程提前退出（退出码 {code}）" + (f"：{tail}" if tail else "")

    def _drain(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            return
        tail: list[str] = []
        try:
            for line in proc.stdout:
                tail.append(line.rstrip())
                if len(tail) > _TAIL_LINES:
                    del tail[0]
                self.status.log_tail = list(tail)
        except (OSError, ValueError):
            pass
        finally:
            self._log_tail = tail

    def stop(self, *, timeout: float = 3.0) -> TunnelStatus:
        """停掉**本实例启动的**进程。系统里其它 ssh 一律不碰。"""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            logger.info("关闭隧道进程 pid=%s", proc.pid)
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=timeout)
            except OSError as exc:  # noqa: PERF203 - 进程可能已经自己没了
                logger.warning("关闭隧道进程出错：%s", exc)
        self.status.state = STATE_STOPPED
        self.status.pid = 0
        self.status.error = ""
        self._notify(None)
        return self.status

    def alive(self) -> bool:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        return port_open("127.0.0.1", self.status.local_port)

    def check(self, *, health: Callable[[int], bool] | None = None) -> TunnelStatus:
        """断线检测：进程没了或端口不可连就标记失败（**不自动改走公网**）。"""
        if self.status.state in (STATE_STOPPED, STATE_STARTING):
            return self.status
        if not self.alive():
            self.status.state = STATE_FAILED
            self.status.error = "隧道已断开（ssh 进程退出或本地端口不可连）"
            self._notify(None)
            return self.status
        if health is not None and not health(self.status.local_port):
            self.status.state = STATE_FAILED
            self.status.error = "隧道在转，但 API 健康检查不过（服务端没跑或过期）"
            self._notify(None)
        return self.status

    # ------------------------------------------------------------ 内部
    def _fail(self, why: str, on_change: Callable[[TunnelStatus], None] | None) -> TunnelStatus:
        self.status.state = STATE_FAILED
        self.status.error = redact_text(one_line(why, 400))
        logger.error("隧道不可用：%s", self.status.error)
        self._notify(on_change)
        return self.status

    def _notify(self, on_change: Callable[[TunnelStatus], None] | None) -> None:
        if on_change is not None:
            try:
                on_change(self.status)
            except Exception:  # noqa: BLE001 - UI 回调出错不该影响隧道状态机
                logger.exception("隧道状态回调出错")

    def audit_own_processes(self, pids: Iterable[int]) -> str:
        """给"退出时只回收自己启动的进程"留一份可核对的说明。"""
        return "本应用启动的隧道进程：" + (", ".join(str(x) for x in pids) or "无")
