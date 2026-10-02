"""桌面 UI 插件 SDK（与 NoneBot 服务端插件没有任何关系）。

分界要说清：这里是**桌面端进程内**的 UI 插件 —— 注册页面、操作、状态卡，
通过受限的宿主 API 调服务端 HTTP。它不是"可以动态加载任意 NoneBot 插件"，
`desktop/plugins/` 与 `plugins/` 之间也没有任何 import 关系。

四块：

    manifest.py     插件清单的解析与校验（**先校验，后 import**）
    permissions.py  路由 → 权限的映射表（白名单，未登记一律拒绝）
    api.py          PluginAPI / PluginContext / 各注册表
    loader.py       发现、加载、生命周期、错误隔离
"""

from __future__ import annotations

from .api import (
    ActionSpec,
    ActionResult,
    CancellationToken,
    NullUiBridge,
    PageSpec,
    PluginAPI,
    PluginContext,
    PluginHttp,
    Registry,
    SettingsSectionSpec,
    StatusCardSpec,
    UiBridge,
)
from .loader import LoadedPlugin, PluginHost, PluginRecord, PluginState
from .manifest import ManifestError, PluginManifest, discover_manifests, load_manifest
from .permissions import PERMISSIONS, PermissionDenied, required_permission

__all__ = [
    "ActionSpec",
    "ActionResult",
    "CancellationToken",
    "LoadedPlugin",
    "ManifestError",
    "NullUiBridge",
    "PERMISSIONS",
    "PageSpec",
    "PermissionDenied",
    "PluginAPI",
    "PluginContext",
    "PluginHost",
    "PluginHttp",
    "PluginManifest",
    "PluginRecord",
    "PluginState",
    "Registry",
    "SettingsSectionSpec",
    "StatusCardSpec",
    "UiBridge",
    "discover_manifests",
    "load_manifest",
    "required_permission",
]
