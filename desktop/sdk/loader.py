"""插件加载器：发现 → 校验 → 登记 → 激活 → 停用，**每个失败都只影响它自己**。

调用时序在方案 5.2.1 里定死了，这里就是那份约定的实现：

    discovery   只读 manifest，不 import 任何插件代码
    register    import + 构造实例 + register(api)（**不得建 widget / 起线程 / 发请求**）
    activate    主窗口就绪后才调 on_activate（这里才允许建页面、订阅、起定时器）
    deactivate  先断信号/停定时器/取消请求，再让宿主销毁页面

任何一步抛异常都只把那个插件标成 `failed`，其余插件照常加载 —— 插件把主窗口
带崩是最不能接受的失败模式（方案 7 的验收条件之一）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import traceback
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable

from ..core.apiclient import ApiClient
from ..core.util import one_line
from .api import CancellationToken, PluginAPI, PluginContext, Registry, UiBridge
from .manifest import ManifestError, PluginManifest, discover_manifests
from .permissions import PermissionDenied

logger = logging.getLogger("qqbot.desktop.plugin.loader")

# 第一阶段只加载随包内置/用户明确安装的插件；不做远程市场（方案 5.1）。
TRUSTED_ROOTS_HINT = "只加载 desktop/plugins/ 下、manifest 合法且用户启用的插件"


class PluginState(str, Enum):
    DISCOVERED = "discovered"
    LOADED = "loaded"        # register 完成，等激活
    ACTIVE = "active"
    INACTIVE = "inactive"    # 用户禁用
    FAILED = "failed"
    INCOMPATIBLE = "incompatible"

    @property
    def label(self) -> str:
        return {
            "discovered": "已发现",
            "loaded": "已加载（未激活）",
            "active": "已激活",
            "inactive": "已禁用",
            "failed": "加载失败",
            "incompatible": "版本不兼容",
        }[self.value]


@dataclass
class PluginRecord:
    """插件管理页显示的就是这个。"""

    manifest: PluginManifest
    state: PluginState = PluginState.DISCOVERED
    error: str = ""
    traceback_text: str = ""
    enabled: bool = True
    note: str = ""
    registered: dict[str, int] = field(default_factory=dict)
    http_calls: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.manifest.id

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.manifest.to_dict(),
            "state": self.state.value,
            "state_label": self.state.label,
            "error": self.error,
            "note": self.note,
            "enabled": self.enabled,
            "registered": dict(self.registered),
        }


@dataclass
class LoadedPlugin:
    """一个已实例化的插件。"""

    manifest: PluginManifest
    instance: Any
    api: PluginAPI
    module: ModuleType | None = None

    def register(self) -> None:
        """调 `register(api)`。**此阶段禁止 UI 副作用**（写进插件开发说明）。"""
        self.instance.register(self.api)

    def activate(self) -> None:
        hook = getattr(self.instance, "on_activate", None)
        if callable(hook):
            hook()

    def deactivate(self) -> None:
        hook = getattr(self.instance, "on_deactivate", None)
        if callable(hook):
            hook()
        # 无论插件自己有没有清干净，宿主都把它的取消令牌置位 ——
        # 这是"停用后不留幽灵任务"的兜底。
        self.api.token.cancel("插件已停用")


class PluginHost:
    """插件宿主：一个目录、一份启用状态、一份注册表。"""

    def __init__(
        self,
        plugin_root: Path,
        *,
        client_provider: Callable[[], ApiClient],
        prefix_provider: Callable[[], str],
        ui: UiBridge | None = None,
        capability_probe: Callable[[], dict[str, Any] | None] | None = None,
        enabled_ids: Iterable[str] | None = None,
        host_api_version: str | None = None,
    ) -> None:
        self.plugin_root = Path(plugin_root)
        self.client_provider = client_provider
        self.prefix_provider = prefix_provider
        self.ui = ui
        self.capability_probe = capability_probe
        self.host_api_version = host_api_version
        self.registry = Registry()
        self.records: dict[str, PluginRecord] = {}
        self.loaded: dict[str, LoadedPlugin] = {}
        self._enabled: set[str] | None = set(enabled_ids) if enabled_ids is not None else None
        self._imported_modules: list[str] = []

    # ------------------------------------------------------------ 发现
    def discover(self) -> list[PluginRecord]:
        """只读 manifest。**这一步不 import 插件代码。**

        ⚠ **重扫时不能把"已经加载过"的插件忘成"刚发现"**（2026-10-02 用户实测报回
        `ValueError: 页面 id 重复：memory_stats:overview`）：`Registry` 是**宿主那一份**，
        一旦让 `load_all()` 再走一遍 `_load_one`，就会二次 `register()`，而 `register_page`
        见到重复 id 直接抛错 —— 插件被标成"加载失败"、导航里的页面也跟着消失。
        所以这里把已加载插件的**状态与启用标记**沿下来（`load_all()` 也会跳过它们）。
        """
        from .. import PLUGIN_API_VERSION

        host_version = self.host_api_version or PLUGIN_API_VERSION
        settled = {
            plugin_id: (record.state, record.enabled)
            for plugin_id, record in self.records.items()
            if plugin_id in self.loaded
        }
        manifests, errors = discover_manifests(self.plugin_root)
        self.records = {}
        for manifest in manifests:
            record = PluginRecord(manifest=manifest)
            ok, reason = manifest.compatible(host_version)
            if not ok:
                record.state = PluginState.INCOMPATIBLE
                record.error = reason
                record.enabled = False
            elif manifest.id in settled:
                # 已经加载过的：状态与启用标记原样沿下来，别退回 DISCOVERED
                state, enabled = settled[manifest.id]
                record.enabled = enabled
                record.state = state
                record.registered = self.loaded[manifest.id].api.stats()
                if reason:
                    record.note = reason
            else:
                if reason:
                    record.note = reason
                record.enabled = self._is_enabled(manifest)
                if not record.enabled:
                    record.state = PluginState.INACTIVE
            self.records[manifest.id] = record
        for directory, reason in errors:
            # manifest 不合法的目录也进列表：用户需要看到"这里有个插件是坏的"，
            # 而不是"它悄悄不见了"。
            fake_id = f"__invalid__{directory.name}"
            self.records[fake_id] = PluginRecord(
                manifest=PluginManifest(
                    id=fake_id,
                    name=directory.name,
                    version="-",
                    api_version="-",
                    entrypoint="-",
                    directory=directory,
                    builtin=True,
                ),
                state=PluginState.FAILED,
                error=reason,
                enabled=False,
            )
        logger.info(
            "插件发现：%s 个合法、%s 个 manifest 有问题（目录 %s）",
            len(manifests), len(errors), self.plugin_root,
        )
        return list(self.records.values())

    def _is_enabled(self, manifest: PluginManifest) -> bool:
        if manifest.id in self.records:  # 已加载过：沿用既有启用状态
            return self.records[manifest.id].enabled
        if self._enabled is not None:
            return manifest.id in self._enabled
        return bool(manifest.enabled_by_default)

    def set_enabled(self, plugin_id: str, enabled: bool) -> PluginRecord | None:
        """启用/禁用。**禁用后必须重启桌面程序**才能真正卸载干净（方案 5.3）。"""
        record = self.records.get(plugin_id)
        if record is None:
            return None
        if not enabled and record.state == PluginState.ACTIVE:
            self.deactivate(plugin_id)
        record.enabled = bool(enabled)
        if not enabled:
            record.state = PluginState.INACTIVE
        elif record.state in (PluginState.INACTIVE,):
            record.state = PluginState.DISCOVERED
            record.note = "重启桌面程序后生效"
        return record

    def enabled_ids(self) -> list[str]:
        return sorted(pid for pid, rec in self.records.items() if rec.enabled)

    # ------------------------------------------------------------ 加载
    def load_all(self) -> list[PluginRecord]:
        """按调用序加载所有"启用且合法"的插件（discovery → register）。

        **已经加载过的直接跳过**：`Registry` 是宿主那一份，重复 `register()` 会让插件的
        `register_page()` 撞上"页面 id 重复"（见 `discover()` 的说明）。正常路径上
        `discover()` 已经把它们的状态还原了，这里的判断是第二道保险。
        """
        if not self.records:
            self.discover()
        for plugin_id, record in list(self.records.items()):
            if record.state in (PluginState.INACTIVE, PluginState.INCOMPATIBLE, PluginState.FAILED):
                continue
            if plugin_id in self.loaded:
                continue
            self._load_one(plugin_id)
        return list(self.records.values())

    def _load_one(self, plugin_id: str) -> PluginRecord:
        record = self.records[plugin_id]
        manifest = record.manifest
        try:
            module = self._import_plugin(manifest)
            factory = getattr(module, manifest.factory_name, None)
            if factory is None:
                raise ManifestError(f"{manifest.entrypoint} 里没有 {manifest.factory_name}")
            instance = factory() if callable(factory) else factory
            missing = self._check_protocol(instance)
            if missing:
                raise ManifestError("插件实例缺少必需方法：" + "、".join(missing))
            api = PluginAPI(
                manifest,
                client_provider=self.client_provider,
                prefix_provider=self.prefix_provider,
                registry=self.registry,   # ← 必须是宿主那一份，否则插件注册的内容没人看得到
                ui=self.ui,
                token=CancellationToken(),
                capability_probe=self.capability_probe,
                on_http_call=lambda m, p, perm, pid=plugin_id: self._record_call(pid, m, p, perm),
            )
            loaded = LoadedPlugin(manifest=manifest, instance=instance, api=api, module=module)
            loaded.register()  # ← 只登记，不建界面
            self.loaded[plugin_id] = loaded
            record.state = PluginState.LOADED
            record.registered = api.stats()
            record.error = ""
            logger.info(
                "插件已加载：%s（页面 %s / 操作 %s / 状态卡 %s）",
                record.manifest.name,
                record.registered.get("pages", 0),
                record.registered.get("actions", 0),
                record.registered.get("status_cards", 0),
            )
        except BaseException as exc:  # noqa: BLE001 - 插件加载失败必须被隔离
            # `register()` 可能"登记了一半"才抛错（比如两个页面里第二个撞了 id）。
            # 残留在宿主注册表里的条目会让**下一次** `load_all()`（重扫）撞上
            # "页面 id 重复"，把一次偶然失败变成永久失败 —— 这里先摘干净。
            self._discard_registry_entries(plugin_id)
            self._mark_failed(plugin_id, exc)
        return record

    def _discard_registry_entries(self, plugin_id: str) -> None:
        """把某个插件"登记到一半"的内容从宿主注册表里摘掉（只在加载失败时用）。"""
        reg = self.registry
        reg.pages = [x for x in reg.pages if x.plugin_id != plugin_id]
        reg.actions = [x for x in reg.actions if x.plugin_id != plugin_id]
        reg.status_cards = [x for x in reg.status_cards if x.plugin_id != plugin_id]
        reg.settings_sections = [x for x in reg.settings_sections if x.plugin_id != plugin_id]

    def _check_protocol(self, instance: Any) -> list[str]:
        missing: list[str] = []
        for name in ("manifest", "register"):
            if not callable(getattr(instance, name, None)):
                missing.append(name)
        return missing

    def _import_plugin(self, manifest: PluginManifest) -> ModuleType:
        """从文件路径 import 插件入口。

        模块名加宿主前缀（`qqbot_desktop_plugin_<id>`），避免插件之间、
        以及插件与项目其它包之间重名互踩 sys.modules。
        """
        entry = manifest.entry_file
        module_name = f"qqbot_desktop_plugin_{manifest.id}"
        if module_name in sys.modules:
            return sys.modules[module_name]
        parent = str(entry.parent)
        added = parent not in sys.path
        if added:
            # 让插件能 import 同目录的兄弟模块（`from helpers import x`）。
            sys.path.insert(0, parent)
        try:
            spec = importlib.util.spec_from_file_location(module_name, entry)
            if spec is None or spec.loader is None:
                raise ManifestError(f"无法加载入口文件：{entry}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            self._imported_modules.append(module_name)
            return module
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        finally:
            if added:
                try:
                    sys.path.remove(parent)
                except ValueError:
                    pass

    def _mark_failed(self, plugin_id: str, exc: BaseException) -> None:
        record = self.records[plugin_id]
        record.state = PluginState.FAILED
        record.error = f"{type(exc).__name__}: {one_line(exc, 300)}"
        record.traceback_text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[-4000:]
        logger.error("插件 %s 加载失败（已隔离，不影响其它插件）：%s", plugin_id, record.error)

    def _record_call(self, plugin_id: str, method: str, path: str, permission: str) -> None:
        record = self.records.get(plugin_id)
        if record is None:
            return
        record.http_calls.append(f"{method} {path} → {permission}")
        del record.http_calls[:-40]  # 只留最近 40 条，界面够看

    # ------------------------------------------------------------ 生命周期
    def activate_all(self) -> list[PluginRecord]:
        """主窗口就绪后调用。**建页面/起定时器只能发生在这之后。**"""
        for plugin_id, loaded in list(self.loaded.items()):
            record = self.records[plugin_id]
            if record.state != PluginState.LOADED:
                continue
            try:
                loaded.activate()
                record.state = PluginState.ACTIVE
            except BaseException as exc:  # noqa: BLE001
                self._mark_failed(plugin_id, exc)
        return list(self.records.values())

    def deactivate(self, plugin_id: str) -> bool:
        loaded = self.loaded.get(plugin_id)
        if loaded is None:
            return False
        record = self.records[plugin_id]
        try:
            loaded.deactivate()
            record.state = PluginState.INACTIVE
            logger.info("插件已停用：%s", record.manifest.name)
            return True
        except BaseException as exc:  # noqa: BLE001
            self._mark_failed(plugin_id, exc)
            return False

    def deactivate_all(self) -> None:
        for plugin_id in list(self.loaded):
            self.deactivate(plugin_id)

    def call_action(self, action_id: str, context: PluginContext | None = None) -> Any:
        """执行一个插件操作（宿主统一做确认与异常兜底）。"""
        spec = next((x for x in self.registry.actions if x.id == action_id), None)
        if spec is None:
            raise KeyError(f"没有这个操作：{action_id}")
        loaded = self.loaded.get(spec.plugin_id)
        if loaded is None:
            raise RuntimeError(f"插件未加载：{spec.plugin_id}")
        ctx = context or loaded.api.context()
        return spec.callback(ctx)

    def call_page_factory(self, page_id: str) -> Any:
        """建页面。只能在 activate 之后调用。"""
        spec = next((x for x in self.registry.pages if x.id == page_id), None)
        if spec is None:
            raise KeyError(f"没有这个页面：{page_id}")
        loaded = self.loaded.get(spec.plugin_id)
        if loaded is None:
            raise RuntimeError(f"插件未加载：{spec.plugin_id}")
        record = self.records[spec.plugin_id]
        if record.state != PluginState.ACTIVE:
            raise RuntimeError(f"插件未激活：{spec.plugin_id}（当前 {record.state.label}）")
        return spec.factory(loaded.api.context())

    def status_cards(self, refresh: bool = False) -> list[dict[str, Any]]:
        """收状态卡数据（只读）。provider 抛异常只影响该卡片。"""
        out: list[dict[str, Any]] = []
        for spec in self.registry.status_cards:
            loaded = self.loaded.get(spec.plugin_id)
            if loaded is None or self.records[spec.plugin_id].state != PluginState.ACTIVE:
                continue
            item: dict[str, Any] = {"id": spec.id, "title": spec.title, "ok": True, "value": None}
            try:
                item["value"] = spec.provider(loaded.api.context())
            except PermissionDenied as exc:
                item["ok"] = False
                item["error"] = f"权限不足：{exc}"
            except BaseException as exc:  # noqa: BLE001 - 单张卡出错不能拖垮面板
                item["ok"] = False
                item["error"] = f"{type(exc).__name__}: {one_line(exc, 200)}"
            out.append(item)
        return out

    # ------------------------------------------------------------ 汇总
    def summary(self) -> dict[str, Any]:
        states: dict[str, int] = {}
        for record in self.records.values():
            states[record.state.value] = states.get(record.state.value, 0) + 1
        return {
            "root": str(self.plugin_root),
            "hint": TRUSTED_ROOTS_HINT,
            "total": len(self.records),
            "states": states,
            "loaded": sorted(self.loaded),
            "pages": len(self.registry.pages),
            "actions": len(self.registry.actions),
            "status_cards": len(self.registry.status_cards),
            "settings_sections": len(self.registry.settings_sections),
        }
