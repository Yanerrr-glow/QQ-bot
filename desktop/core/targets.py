"""连接目标（多目标、按目标隔离凭证）与持久化。

一个"目标"= 一套"怎么连上某个机器人"的配置：本机直连，或经应用的 SSH 隧道。

关键设计（对应方案 3.1 的"目标隔离"）：

- 每个目标持有**自己的 token 引用**（`token_ref`），真实令牌存在凭据存储里，
  **不写进 `targets.json`**。所以这个 JSON 可以随便备份/贴给别人看。
- 每个目标在运行时对应**一个独立的 `ApiClient` 实例**（独立 cookie jar）。
  切目标 = 换实例并 `close()` 旧的；不做"改同一个实例的 base_url"那种做法 ——
  那样 cookie 会跨目标串味，等于把 A 的会话带去 B。
- SSH 参数与 HTTP 参数分开存，`base_url` 对隧道目标由本地转发端口派生。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .apiclient import DEFAULT_PREFIX, DEFAULT_TIMEOUT, normalize_base_url, normalize_prefix

logger = logging.getLogger("qqbot.desktop.targets")

MODE_LOCAL = "local"
MODE_SSH = "ssh_tunnel"
VALID_MODES = (MODE_LOCAL, MODE_SSH)

DEFAULT_LOCAL_BASE = "http://127.0.0.1:8080"


class ConfigWriteError(RuntimeError):
    """配置写不进去（权限/只读盘/被托管策略拦）。消息要能直接给用户看。"""

    def __init__(self, path: Path, exc: OSError) -> None:
        self.path = Path(path)
        self.original = exc
        super().__init__(
            f"写配置失败：{self.path}\n"
            f"原因：{type(exc).__name__}: {exc}\n\n"
            "怎么处理：\n"
            "  1) 跑 `python -m desktop paths` 看落点体检（它会列出哪个候选目录可写）；\n"
            "  2) 若系统配置目录不可写，桌面端会自动退到项目内的 `.console\\`；\n"
            "  3) 也可以用环境变量显式指定，例如：\n"
            "     $env:QQBOT_CONSOLE_HOME = 'D:\\qqbot-console'"
        )


def new_id(prefix: str = "target") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class SshConfig:
    """SSH 转发参数。私钥只由 OpenSSH 使用，**内容不导入应用**。

    两种填法，二选一：

    * **别名模式**（`use_alias=True`）：只填 `alias`，其余交给 `~/.ssh/config`。
      项目里的 `启动隧道.ps1` 已经写过 `Host qqbot` 这类别名，复用它可以省掉
      "服务器 IP + 私钥路径"这些个人环境信息，也不必在 GUI 里再抄一遍。
    * **手工模式**：主机/端口/用户/私钥逐个填。
    """

    host: str = ""
    port: int = 22
    user: str = ""
    identity_file: str = ""
    identity_agent: bool = False  # 用系统密钥代理（Windows 上即 OpenSSH Authentication Agent）
    proxy_jump: str = ""         # 跳板机；**不保存任何密码**
    remote_host: str = "127.0.0.1"
    remote_port: int = 8080
    local_port: str = "auto"     # "auto" 或具体端口号
    server_alive_interval: int = 30
    server_alive_count_max: int = 3
    connect_timeout: int = 10
    #: 用 `~/.ssh/config` 里的 Host 别名（推荐：服务器地址不落在 GUI 配置里）
    alias: str = ""
    use_alias: bool = False

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.use_alias:
            if not self.alias.strip():
                problems.append("别名模式需要填 SSH 别名（~/.ssh/config 里的 Host 名）")
        else:
            if not self.host.strip():
                problems.append("SSH 主机为空")
            if not (0 < int(self.port) < 65536):
                problems.append(f"SSH 端口不合法：{self.port}")
        if not self.remote_host.strip():
            problems.append("远端主机为空")
        if not (0 < int(self.remote_port) < 65536):
            problems.append(f"远端端口不合法：{self.remote_port}")
        return problems

    def where(self) -> str:
        """给界面/日志用的一句话描述（别名模式下不泄露 IP 与私钥路径）。"""
        if self.use_alias:
            return f"{self.alias.strip() or '（未填别名）'}（~/.ssh/config）"
        return f"{self.user + '@' if self.user else ''}{self.host}:{self.port}"

    def local_port_value(self) -> int:
        """解析 local_port：'auto' 或空 → 0（表示让系统挑）。"""
        raw = str(self.local_port or "auto").strip().lower()
        if raw in ("", "auto", "0"):
            return 0
        try:
            value = int(raw)
        except ValueError:
            return 0
        return value if 0 < value < 65536 else 0


@dataclass
class HttpConfig:
    base_url: str = DEFAULT_LOCAL_BASE
    prefix: str = DEFAULT_PREFIX
    token_ref: str = ""
    timeout: float = DEFAULT_TIMEOUT
    verify_tls: bool = True

    def normalized_base(self) -> str:
        return normalize_base_url(self.base_url)

    def normalized_prefix(self) -> str:
        return normalize_prefix(self.prefix)


@dataclass
class Target:
    id: str = field(default_factory=new_id)
    name: str = "本机"
    mode: str = MODE_LOCAL
    ssh: SshConfig = field(default_factory=SshConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    enabled: bool = True

    # -- 序列化 ----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # 隧道目标的 base_url 是"派生值"（本地转发端口），存盘时留个占位，
        # 免得下次启动时拿着一个早已失效的旧端口当真实地址。
        if self.mode == MODE_SSH:
            data["http"]["base_url"] = ""
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Target":
        raw = dict(raw or {})
        ssh = SshConfig(**(raw.get("ssh") or {}))
        http = HttpConfig(**(raw.get("http") or {}))
        mode = str(raw.get("mode") or MODE_LOCAL)
        if mode not in VALID_MODES:
            logger.warning("目标 %s 的模式不认识（%s），按本机处理", raw.get("id"), mode)
            mode = MODE_LOCAL
        return cls(
            id=str(raw.get("id") or new_id()),
            name=str(raw.get("name") or "未命名"),
            mode=mode,
            ssh=ssh,
            http=http,
            enabled=bool(raw.get("enabled", True)),
        )

    # -- 派生值 ----------------------------------------------------------
    @property
    def is_tunnel(self) -> bool:
        return self.mode == MODE_SSH

    def effective_base_url(self, *, tunnel_base: str = "") -> str:
        """这个目标**实际**该访问的地址。

        隧道目标必须指向本地转发地址 —— 这是"防止 UI 请求绕过隧道直连公网服务器"
        那条约束的落点：隧道没起来就返回空，调用方只能报离线，不能偷偷回退。
        """
        if self.is_tunnel:
            return normalize_base_url(tunnel_base)
        return normalize_base_url(self.http.base_url)

    def summary(self) -> str:
        if self.is_tunnel:
            return f"SSH 隧道 → {self.ssh.where()} → {self.ssh.remote_host}:{self.ssh.remote_port}"
        return f"直连 {self.http.normalized_base()}{self.http.normalized_prefix()}"


class TargetStore:
    """目标列表的读写（`targets.json`，**不含任何令牌**）。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.targets_file()
        self.targets: list[Target] = []
        self.active_id: str = ""
        self._loaded = False

    def load(self) -> None:
        self._loaded = True
        if not self.path.exists():
            self.targets = [self._default_local()]
            self.active_id = self.targets[0].id
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("目标配置损坏，改用默认本机目标：%s", self.path)
            self.targets = [self._default_local()]
            self.active_id = self.targets[0].id
            return
        items = raw.get("targets") if isinstance(raw, dict) else None
        self.targets = [Target.from_dict(x) for x in (items or []) if isinstance(x, dict)]
        if not self.targets:
            self.targets = [self._default_local()]
        active = str((raw or {}).get("active_id") or "")
        self.active_id = active if any(t.id == active for t in self.targets) else self.targets[0].id

    def ensure(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> None:
        self.ensure()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "active_id": self.active_id,
                "targets": [t.to_dict() for t in self.targets],
            }
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            # **不要**把原始 WinError 直接甩给用户（"拒绝访问"看不出该干什么）。
            raise ConfigWriteError(self.path, exc) from exc
        # 只给当前用户读：里面没有令牌，但服务器地址也算敏感信息。
        if os.name == "posix":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    # -- 查询 ------------------------------------------------------------
    def get(self, target_id: str) -> Target | None:
        self.ensure()
        for item in self.targets:
            if item.id == target_id:
                return item
        return None

    def active(self) -> Target:
        self.ensure()
        target = self.get(self.active_id)
        return target or self.targets[0]

    def set_active(self, target_id: str) -> Target:
        self.ensure()
        target = self.get(target_id)
        if target is None:
            raise KeyError(f"没有这个目标：{target_id}")
        self.active_id = target.id
        self.save()
        return target

    # -- 变更 ------------------------------------------------------------
    def add(self, target: Target) -> Target:
        self.ensure()
        if not target.id:
            target.id = new_id()
        if any(t.id == target.id for t in self.targets):
            target.id = new_id()
        # 令牌引用按目标隔离，且要互相看不出内容（引用名不含令牌）。
        if not target.http.token_ref:
            target.http.token_ref = self.suggest_token_ref(target)
        self.targets.append(target)
        self.save()
        return target

    def update(self, target: Target) -> Target:
        self.ensure()
        for idx, item in enumerate(self.targets):
            if item.id == target.id:
                self.targets[idx] = target
                self.save()
                return target
        raise KeyError(f"没有这个目标：{target.id}")

    def remove(self, target_id: str) -> bool:
        self.ensure()
        before = len(self.targets)
        self.targets = [t for t in self.targets if t.id != target_id]
        if len(self.targets) == before:
            return False
        if not self.targets:
            self.targets = [self._default_local()]
        if self.active_id == target_id:
            self.active_id = self.targets[0].id
        self.save()
        return True

    @staticmethod
    def suggest_token_ref(target: Target) -> str:
        slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (target.id or "target"))
        return f"qqbot/{slug}/webui-token"

    @staticmethod
    def _default_local() -> Target:
        target = Target(name="本机（直连）", mode=MODE_LOCAL)
        target.http.base_url = DEFAULT_LOCAL_BASE
        target.http.prefix = DEFAULT_PREFIX
        target.http.token_ref = TargetStore.suggest_token_ref(target)
        return target


def duplicate(target: Target, *, name_suffix: str = "（副本）") -> Target:
    """复制一个目标：**新 id、新令牌引用**，避免两个目标共用一个令牌。"""
    clone = Target.from_dict(dataclasses.asdict(target))
    clone.id = new_id()
    clone.name = f"{target.name}{name_suffix}"
    clone.http.token_ref = TargetStore.suggest_token_ref(clone)
    return clone
