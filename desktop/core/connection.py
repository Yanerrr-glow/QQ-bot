"""连接管理：把"目标 + 令牌 + 隧道 + API 客户端"绑成一个可切换的当前连接。

这是桌面端所有页面的唯一入口。三件事必须做对：

1. **切目标 = 换客户端实例**：旧客户端 `close()`（清 cookie），新客户端重新探测。
   绝不"改同一个实例的 base_url" —— cookie 会串味到别的目标。
2. **服务器目标必须先有隧道**：隧道没起来时 `base_url` 为空，客户端拿不到地址，
   写操作会被 `require_writable()` 拦住。**不存在"隧道失败就改走公网直连"的分支。**
3. **令牌只在内存里经过一次**：从凭据存储读出来 → 交给 `ApiClient`；
   任何时候往外报信息（日志、界面文字、错误体）都不带它。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from . import credentials, tunnel as tunnel_mod
from .apiclient import ApiClient, BotApiError, ProbeResult
from .targets import MODE_SSH, Target, TargetStore
from .tunnel import SshTunnel, TunnelStatus
from .util import one_line

logger = logging.getLogger("qqbot.desktop.connection")


@dataclass
class ConnectionState:
    """当前连接的可展示状态（界面顶部状态条用的就是它）。"""

    target_id: str = ""
    target_name: str = ""
    mode: str = ""
    base_url: str = ""
    prefix: str = ""
    api_ok: bool = False
    api_detail: str = ""
    tunnel: TunnelStatus | None = None
    token_saved: bool = False
    token_backend: str = ""

    @property
    def writable(self) -> bool:
        return self.api_ok

    def address_summary(self) -> str:
        if not self.base_url:
            return "（地址未就绪）"
        return f"{self.base_url}{self.prefix}"

    def human(self) -> str:
        bits = [f"目标：{self.target_name or '-'}"]
        bits.append(("在线" if self.api_ok else "离线") + f"（{self.address_summary()}）")
        if self.tunnel is not None and self.mode == MODE_SSH:
            bits.append("隧道：" + self.tunnel.human())
        return " · ".join(bits)


class ConnectionManager:
    """持有 `TargetStore` / `CredentialStore` / 当前 `ApiClient` / 当前隧道。"""

    def __init__(
        self,
        store: TargetStore | None = None,
        creds: credentials.CredentialStore | None = None,
        *,
        on_target_changed: Callable[[str], None] | None = None,
        on_state_changed: Callable[[ConnectionState], None] | None = None,
    ) -> None:
        self.store = store or TargetStore()
        self.creds = creds or credentials.CredentialStore()
        self._client: ApiClient | None = None
        self._tunnel: SshTunnel | None = None
        self._tunnel_target_id = ""
        self.state = ConnectionState()
        self._lock = threading.RLock()
        self._on_target_changed = on_target_changed
        self._on_state_changed = on_state_changed
        self.store.ensure()

    # ------------------------------------------------------------ 目标
    def targets(self) -> list[Target]:
        return list(self.store.targets)

    def active_target(self) -> Target:
        return self.store.active()

    def client(self) -> ApiClient:
        """拿当前 API 客户端（懒建）。切目标后一定是新实例。"""
        with self._lock:
            if self._client is None or self._client.target_id != self.store.active_id:
                self._build_client()
            assert self._client is not None
            return self._client

    def _token_for(self, target: Target) -> str:
        return self.creds.get(target.http.token_ref) if target.http.token_ref else ""

    def _build_client(self) -> ApiClient:
        if self._client is not None:
            self._client.close()
        target = self.store.active()
        tunnel_base = ""
        if target.is_tunnel:
            if self._tunnel is not None and self._tunnel_target_id == target.id:
                tunnel_base = self._tunnel.status.base_url
            if not tunnel_base:
                logger.warning("目标 %s 是隧道目标，但隧道未就绪 —— 客户端地址为空", target.id)
        token = self._token_for(target)
        self._client = ApiClient(
            target.effective_base_url(tunnel_base=tunnel_base),
            prefix=target.http.normalized_prefix(),
            token=token,
            timeout=target.http.timeout,
            verify_tls=target.http.verify_tls,
            target_id=target.id,
        )
        self.state = ConnectionState(
            target_id=target.id,
            target_name=target.name,
            mode=target.mode,
            base_url=self._client.base_url,
            prefix=self._client.prefix,
            tunnel=self._tunnel.status if (self._tunnel and self._tunnel_target_id == target.id) else None,
            token_saved=bool(token),
            token_backend=self.creds.backend,
        )
        return self._client

    def switch_target(self, target_id: str) -> ConnectionState:
        """切目标：关闭旧客户端 → 换新实例 → 通知插件清缓存。"""
        with self._lock:
            old = self._client
            self.store.set_active(target_id)
            self._stop_tunnel_if_other_target(target_id)
            self._client = None
            self._build_client()
        if old is not None:
            old.close()
            logger.info("已关闭旧目标的连接（%s）", old.target_id)
        logger.info("切换目标 → %s", self.state.target_name)
        if self._on_target_changed is not None:
            try:
                self._on_target_changed(self.store.active_id)
            except Exception:  # noqa: BLE001 - 插件回调出错不该阻断切换
                logger.exception("on_target_changed 回调出错")
        self._emit()
        return self.state

    # ------------------------------------------------------------ 令牌
    def token_present(self, target: Target | None = None) -> bool:
        target = target or self.store.active()
        return bool(self._token_for(target))

    def set_token(self, token: str, *, target: Target | None = None, session_only: bool = False) -> str:
        """写令牌并重建客户端。返回实际生效的存储后端名。

        落盘失败（配置目录不可写）时**自动退成"仅本次会话"**并说清楚 ——
        令牌这次能用，但退出就没了。这比"报个错什么都不做"更接近用户的意图，
        也比"假装存好了"诚实。
        """
        target = target or self.store.active()
        if not target.http.token_ref:
            target.http.token_ref = TargetStore.suggest_token_ref(target)
            self.store.update(target)
        try:
            backend = self.creds.set(target.http.token_ref, str(token or ""), session_only=session_only)
        except credentials.CredentialError as exc:
            logger.warning("令牌落盘失败，改为仅本次会话：%s", exc)
            self.creds = credentials.CredentialStore("session")
            backend = self.creds.set(target.http.token_ref, str(token or ""), session_only=True)
            backend = f"session（落盘失败已降级：{one_line(exc, 120)}）"
        with self._lock:
            self._build_client()
        self._emit()
        logger.info("目标 %s 的令牌已更新（后端：%s）", target.id, backend)
        return backend

    def clear_token(self, *, target: Target | None = None) -> None:
        target = target or self.store.active()
        if target.http.token_ref:
            self.creds.delete(target.http.token_ref)
        with self._lock:
            self._build_client()
        self._emit()

    # ------------------------------------------------------------ 隧道
    def tunnel_for(self, target: Target | None = None) -> SshTunnel:
        """取（必要时新建）某个目标的隧道实例。"""
        target = target or self.store.active()
        if self._tunnel is None or self._tunnel_target_id != target.id:
            if self._tunnel is not None:
                self._tunnel.stop()
                logger.info("上一目标的隧道已关闭（%s）", self._tunnel_target_id)
            self._tunnel = SshTunnel(target.ssh)
            self._tunnel_target_id = target.id
        return self._tunnel

    def start_tunnel(self, target: Target | None = None) -> TunnelStatus:
        target = target or self.store.active()
        if not target.is_tunnel:
            raise BotApiError("当前目标不是隧道模式", hint="在「连接设置」里把模式改成 SSH 隧道")
        tun = self.tunnel_for(target)
        # The selected target may differ from the active target while being configured.
        # Probe the endpoint being started instead of store.active().
        status = tun.start(health=lambda local_port: self._tunnel_health(local_port, target))
        with self._lock:
            self._build_client()
        self._emit()
        return status

    def stop_tunnel(self, target: Target | None = None) -> TunnelStatus:
        target = target or self.store.active()
        tun = self.tunnel_for(target)
        status = tun.stop()
        with self._lock:
            self._build_client()
        self._emit()
        return status

    def _tunnel_health(self, local_port: int, target: Target | None = None) -> bool:
        """隧道可用 = 本地转发端口能拿到 API 响应（不只是端口在监听）。"""
        return port_api_ok(local_port, target or self.store.active())

    def _stop_tunnel_if_other_target(self, target_id: str) -> None:
        if self._tunnel is not None and self._tunnel_target_id != target_id:
            self._tunnel.stop()
            logger.info("切换目标时关闭了旧隧道（%s）", self._tunnel_target_id)
            self._tunnel = None
            self._tunnel_target_id = ""

    # ------------------------------------------------------------ 探测
    def probe(self, *, timeout: float = 8.0) -> ProbeResult:
        """探测当前目标。隧道目标必须先起隧道（这里不自动起 —— 那是用户的动作）。"""
        target = self.store.active()
        if target.is_tunnel:
            tun = self._tunnel
            ready = bool(tun and tun.status.running and tun.status.base_url)
            if not ready:
                result = ProbeResult(
                    ok=False,
                    status=0,
                    detail="隧道未就绪：请先点「启动隧道」",
                    hint="服务器目标必须经隧道访问；本应用不会回退到公网直连",
                    server_prefix=target.http.normalized_prefix(),
                )
            else:
                result = self.client().probe()
        else:
            result = self.client().probe()
        self.state.api_ok = result.ok
        self.state.api_detail = result.detail if result.ok else f"{result.detail}（{result.hint}）"
        self._emit()
        logger.info("连接探测：%s → %s", self.state.target_name, self.state.api_detail)
        return result

    def require_writable(self) -> None:
        """写操作前的闸门。离线/认证失败时明确拒绝，而不是"点了没反应"。"""
        if not self.state.api_ok:
            raise BotApiError(
                "当前目标不可写：还没有成功连接",
                hint=(
                    "先点「测试连接」；服务器目标确认隧道已启动"
                    if self.store.active().is_tunnel
                    else "确认机器人进程在跑、端口正确"
                ),
            )

    # ------------------------------------------------------------ 关闭
    def shutdown(self) -> None:
        """退出应用时调用：只关自己启动的隧道，不碰系统里其它 ssh。"""
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None
            self._tunnel_target_id = ""

    def _emit(self) -> None:
        if self._on_state_changed is not None:
            try:
                self._on_state_changed(self.state)
            except Exception:  # noqa: BLE001
                logger.exception("连接状态回调出错")


def port_api_ok(local_port: int, target: Target) -> bool:
    """隧道健康检查：经本地转发端口发一次最轻的 API 调用。"""
    client = ApiClient(
        f"http://127.0.0.1:{int(local_port)}",
        prefix=target.http.normalized_prefix(),
        token="",  # 健康检查不带令牌：401 也说明服务在跑，认证由后面的正式请求管
        timeout=6.0,
        verify_tls=True,
        max_retries=0,
        target_id=target.id,
    )
    try:
        result = client.probe()
        # 401 = 服务在跑只是要认证；对"隧道是否通"来说足够。
        return result.ok or result.status in (401, 403)
    finally:
        client.close()


def discover_ssh() -> str:
    return tunnel_mod.find_ssh()
