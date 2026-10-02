"""插件看得到的全部东西：`PluginAPI` / `PluginContext` / 注册表 / 取消令牌。

这是**功能边界**，不是沙箱（方案第 6 节说得很清楚：插件在桌面进程里跑的是任意
Python，权限声明只约束"它通过宿主 API 能做什么"）。所以这里的取舍是：

- 只暴露**能力**，不暴露对象：没有 `main_window`、没有 `store`、没有 `client`、
  没有数据库连接、没有凭据读取入口。插件拿到的是"注册一个页面""发一次受控请求"。
- `http.*` 一律先过 `permissions.check()`：绝对 URL、未登记路径、没声明的权限，
  三种情况全部拒绝。
- `logger` 自动带插件 ID，输出再过宿主的脱敏 handler。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..core.apiclient import ApiClient, BotApiError
from ..core.logsetup import plugin_logger
from ..core.util import one_line
from . import permissions
from .manifest import PluginManifest
from .permissions import PermissionDenied


class CancellationToken:
    """生命周期取消令牌：切目标、卸载页面、停用插件时置位。

    长任务必须在下一次循环里 `if token.cancelled: return`，
    否则会出现"页面没了、请求还在跑、回来了往死控件上写"的经典崩溃。
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "") -> None:
        self._reason = reason or "已取消"
        self._event.set()

    def reset(self) -> None:
        self._reason = ""
        self._event.clear()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise RuntimeError(f"操作已取消：{self._reason}")


@dataclass
class PageSpec:
    id: str
    title: str
    factory: Callable[["PluginContext"], Any]
    plugin_id: str = ""
    icon: str = ""
    order: int = 100
    permission: str = ""


@dataclass
class ActionSpec:
    id: str
    title: str
    callback: Callable[["PluginContext"], Any]
    placement: str = "toolbar"          # toolbar | menu
    permission: str = ""
    confirm: str = ""                   # 非空 → 宿主先弹确认框
    plugin_id: str = ""
    costly: bool = False                # 会花 token 的动作，界面上要标出来


@dataclass
class StatusCardSpec:
    id: str
    title: str
    provider: Callable[["PluginContext"], Any]
    refresh_interval: int = 30
    plugin_id: str = ""


@dataclass
class SettingsSectionSpec:
    id: str
    title: str
    schema: Any = None
    on_save: Callable[[dict[str, Any]], Any] | None = None
    plugin_id: str = ""


@dataclass
class ActionResult:
    """动作执行结果，宿主据此弹提示。"""

    ok: bool = True
    message: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "detail": self.detail}


@dataclass
class Registry:
    """插件注册内容的集合。宿主读它来搭界面。"""

    pages: list[PageSpec] = field(default_factory=list)
    actions: list[ActionSpec] = field(default_factory=list)
    status_cards: list[StatusCardSpec] = field(default_factory=list)
    settings_sections: list[SettingsSectionSpec] = field(default_factory=list)

    def by_plugin(self, plugin_id: str) -> dict[str, list[Any]]:
        return {
            "pages": [x for x in self.pages if x.plugin_id == plugin_id],
            "actions": [x for x in self.actions if x.plugin_id == plugin_id],
            "status_cards": [x for x in self.status_cards if x.plugin_id == plugin_id],
            "settings_sections": [x for x in self.settings_sections if x.plugin_id == plugin_id],
        }


@runtime_checkable
class UiBridge(Protocol):
    """宿主 UI 服务。注册阶段只是登记，真正显示由主窗口实现。"""

    def toast(self, message: str, *, level: str = "info") -> None: ...
    def confirm(self, title: str, message: str) -> bool: ...
    def open_dialog(self, title: str, content: Any) -> None: ...

    def run_async(
        self,
        work: Callable[[], Any],
        *,
        on_done: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """把阻塞活丢到后台线程跑，回调回 UI 线程。

        **页面工厂里该用它**：`http.*` 是同步的，直接写在 `build_page` 里就是让
        GUI 线程干等一次超时（表现就是"点插件页卡住"）。
        `work` 在后台线程执行，**里面禁止碰任何控件**。
        """
        ...


class NullUiBridge:
    """没有 UI（自检/无头模式）时的桥：把请求记下来，不假装成功。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def toast(self, message: str, *, level: str = "info") -> None:
        self.records.append((level, str(message)))

    def confirm(self, title: str, message: str) -> bool:
        # 无人确认 → 一律不通过。宁可"动作没执行"，也不要"没问就执行了"。
        self.records.append(("confirm-denied", f"{title}: {message}"))
        return False

    def open_dialog(self, title: str, content: Any) -> None:
        self.records.append(("dialog", f"{title}: {one_line(content, 120)}"))

    def run_async(
        self,
        work: Callable[[], Any],
        *,
        on_done: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """无 UI 环境（自检/服务器）：不存在"GUI 线程被阻塞"，**直接同步跑完**。

        这里刻意不搞"假装排队"：自检要断言的是数据本身，同步执行最诚实。
        """
        try:
            value = work()
        except BaseException as exc:  # noqa: BLE001 - 与宿主 Async 同一口径：失败转回调
            if on_error is not None:
                on_error(f"{type(exc).__name__}: {exc}")
            return
        on_done(value)


class PluginHttp:
    """受控 HTTP：插件唯一能碰服务端的入口。"""

    def __init__(
        self,
        plugin_id: str,
        manifest: PluginManifest,
        client_provider: Callable[[], ApiClient],
        *,
        prefix_provider: Callable[[], str],
        on_call: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._manifest = manifest
        self._client_provider = client_provider
        self._prefix_provider = prefix_provider
        self._on_call = on_call

    # -- 判定 ------------------------------------------------------------
    def check(self, method: str, path: str) -> str:
        prefix = self._prefix_provider() or "/ai"
        used = permissions.check(
            self._plugin_id, self._manifest.permissions, method, path, prefix=prefix
        )
        if self._on_call is not None:
            self._on_call(method.upper(), path, used)
        return used

    # -- 调用 ------------------------------------------------------------
    def request(self, method: str, path: str, *, body: Any = None) -> Any:
        self.check(method, path)
        prefix = self._prefix_provider() or "/ai"
        rel = permissions.strip_prefix(path, prefix).lstrip("/")
        client = self._client_provider()
        try:
            return client._request(  # noqa: SLF001 - SDK 与客户端同属宿主，用内部入口避免重复实现
                method.upper(), rel, body=body
            )
        except BotApiError as exc:
            raise PermissionError(f"服务端调用失败：{exc}") from exc

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, *, body: Any = None) -> Any:
        return self.request("POST", path, body=body if body is not None else {})

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def sticker_bytes(self, digest: str) -> bytes:
        """取图片要单独走：它返回二进制、且必须过同一会话（含 cookie）。"""
        path = f"/api/stickers/{digest}"
        self.check("GET", path)
        client = self._client_provider()
        return client.sticker_bytes(digest)


@dataclass
class PluginContext:
    """页面工厂拿到的上下文。

    刻意**只有**这些：api、主题、语言、取消令牌、能力查询。
    这么做是为了让"插件不该拿到主窗口全局对象"成为**类型层面的事实**，
    而不是靠评审去发现。
    """

    plugin_id: str
    api: "PluginAPI"
    theme: str = "dark"
    locale: str = "zh_CN"
    token: CancellationToken = field(default_factory=CancellationToken)

    def capabilities(self) -> dict[str, Any]:
        return self.api.capabilities()

    def supports(self, feature: str) -> bool:
        """按服务端 capability 判断功能是否可用（拿不到就返回 False，宁可藏起来）。"""
        caps = self.capabilities()
        features = (caps or {}).get("features") or {}
        return bool(features.get(feature))


class PluginAPI:
    """一个插件一个实例。所有注册方法都会盖 `plugin_id` 章，防止互相覆盖。"""

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        client_provider: Callable[[], ApiClient],
        prefix_provider: Callable[[], str],
        registry: Registry | None = None,
        ui: UiBridge | None = None,
        token: CancellationToken | None = None,
        capability_probe: Callable[[], dict[str, Any] | None] | None = None,
        on_http_call: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.manifest = manifest
        self.plugin_id = manifest.id
        self.registry = registry if registry is not None else Registry()
        self.ui: UiBridge = ui if ui is not None else NullUiBridge()
        self.token = token if token is not None else CancellationToken()
        self.logger = plugin_logger(manifest.id)
        self.http = PluginHttp(
            manifest.id,
            manifest,
            client_provider,
            prefix_provider=prefix_provider,
            on_call=on_http_call,
        )
        self._capability_probe = capability_probe
        self._caps_cache: dict[str, Any] | None = None
        self._caps_loaded = False
        self._warn_unknown_permissions()

    # ------------------------------------------------------------ 注册
    def register_page(
        self,
        page_id: str,
        title: str,
        factory: Callable[[PluginContext], Any],
        *,
        icon: str = "",
        order: int = 100,
        permission: str = "",
    ) -> PageSpec:
        """注册一个独立页面（`factory(context)` 返回 QWidget）。

        **只登记**，不要在这里 new 控件 —— 主窗口可能还没建好。
        真正建页面发生在 `on_activate` 之后。
        """
        spec = PageSpec(
            id=f"{self.plugin_id}:{page_id}",
            title=str(title),
            factory=factory,
            plugin_id=self.plugin_id,
            icon=str(icon),
            order=int(order),
            permission=str(permission),
        )
        if any(x.id == spec.id for x in self.registry.pages):
            raise ValueError(f"页面 id 重复：{spec.id}")
        self.registry.pages.append(spec)
        self.logger.info("已登记页面：%s（%s）", spec.title, spec.id)
        return spec

    def register_action(
        self,
        action_id: str,
        title: str,
        callback: Callable[[PluginContext], Any],
        *,
        placement: str = "toolbar",
        permission: str = "",
        confirm: str = "",
        costly: bool = False,
    ) -> ActionSpec:
        if placement not in ("toolbar", "menu"):
            raise ValueError("placement 只能是 toolbar 或 menu")
        spec = ActionSpec(
            id=f"{self.plugin_id}:{action_id}",
            title=str(title),
            callback=callback,
            placement=placement,
            permission=str(permission),
            confirm=str(confirm),
            plugin_id=self.plugin_id,
            costly=bool(costly),
        )
        self.registry.actions.append(spec)
        self.logger.info("已登记操作：%s（%s）", spec.title, spec.id)
        return spec

    def register_status_card(
        self,
        card_id: str,
        title: str,
        provider: Callable[[PluginContext], Any],
        *,
        refresh_interval: int = 30,
    ) -> StatusCardSpec:
        spec = StatusCardSpec(
            id=f"{self.plugin_id}:{card_id}",
            title=str(title),
            provider=provider,
            refresh_interval=max(0, int(refresh_interval)),
            plugin_id=self.plugin_id,
        )
        self.registry.status_cards.append(spec)
        self.logger.info("已登记状态卡：%s（%s）", spec.title, spec.id)
        return spec

    def register_settings_section(
        self,
        section_id: str,
        title: str,
        schema: Any = None,
        on_save: Callable[[dict[str, Any]], Any] | None = None,
    ) -> SettingsSectionSpec:
        """注册**插件本地偏好**。

        如果这个偏好其实是机器人运行参数，必须改走 `http.post('/api/settings')` ——
        桌面端不写机器人配置文件（方案 6 的硬约束）。
        """
        spec = SettingsSectionSpec(
            id=f"{self.plugin_id}:{section_id}",
            title=str(title),
            schema=schema,
            on_save=on_save,
            plugin_id=self.plugin_id,
        )
        self.registry.settings_sections.append(spec)
        return spec

    # ------------------------------------------------------------ 能力
    def capabilities(self, *, refresh: bool = False) -> dict[str, Any]:
        """服务端能力表。**服务端当前没有这个端点**，拿不到就返回空 dict。"""
        if self._caps_loaded and not refresh:
            return self._caps_cache or {}
        self._caps_loaded = True
        if self._capability_probe is None:
            self._caps_cache = {}
            return {}
        try:
            self._caps_cache = self._capability_probe() or {}
        except Exception as exc:  # noqa: BLE001 - 能力探测失败不该让插件加载失败
            self.logger.warning("能力探测失败：%s", type(exc).__name__)
            self._caps_cache = {}
        return self._caps_cache

    # ------------------------------------------------------------ UI 便捷方法
    def toast(self, message: str, *, level: str = "info") -> None:
        self.ui.toast(str(message), level=level)

    def confirm(self, title: str, message: str) -> bool:
        return bool(self.ui.confirm(str(title), str(message)))

    def open_dialog(self, title: str, content: Any) -> None:
        self.ui.open_dialog(str(title), content)

    def run_async(
        self,
        work: Callable[[], Any],
        *,
        on_done: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """把阻塞活（同步 HTTP、批量取图）丢到后台线程，结果回 UI 线程。

        **建页面时该用它**：见 `UiBridge.run_async` 的说明。
        无 UI 环境下会退化成同步执行（`NullUiBridge.run_async`）。
        """
        self.ui.run_async(work, on_done=on_done, on_error=on_error)

    def context(self, *, token: CancellationToken | None = None) -> PluginContext:
        return PluginContext(
            plugin_id=self.plugin_id,
            api=self,
            token=token or self.token,
        )

    # ------------------------------------------------------------ 内部
    def _warn_unknown_permissions(self) -> None:
        unknown = permissions.unknown_permissions(self.manifest.permissions)
        if unknown:
            self.logger.warning(
                "manifest 里声明了 SDK 不认识的权限（会被忽略）：%s", "、".join(unknown)
            )

    def stats(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "pages": len(self.registry.by_plugin(self.plugin_id)["pages"]),
            "actions": len(self.registry.by_plugin(self.plugin_id)["actions"]),
            "status_cards": len(self.registry.by_plugin(self.plugin_id)["status_cards"]),
            "settings_sections": len(self.registry.by_plugin(self.plugin_id)["settings_sections"]),
            "permissions": list(self.manifest.permissions),
        }


__all__ = [
    "ActionResult",
    "CancellationToken",
    "NullUiBridge",
    "PageSpec",
    "ActionSpec",
    "StatusCardSpec",
    "SettingsSectionSpec",
    "PermissionDenied",
    "PluginAPI",
    "PluginContext",
    "PluginHttp",
    "Registry",
    "UiBridge",
]
