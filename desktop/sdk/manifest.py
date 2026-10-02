"""插件清单（`plugin.json`）的解析与校验。

**先校验、后 import**：manifest 不合法的目录到此为止，绝不去 `import` 它的代码 ——
否则"校验"就变成了"先执行再说"，等于没有边界。

校验项与方案 5.1 一致：`id / name / version / api_version / entrypoint / permissions`。
`entrypoint` 采用 `相对文件:工厂名`，例如 `plugin.py:create_plugin`：
比"包名 + import"更好审 —— 一眼能看出会执行哪个文件。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import PLUGIN_API_VERSION

logger = logging.getLogger("qqbot.desktop.plugin.manifest")

MANIFEST_NAME = "plugin.json"
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?([-.+][0-9A-Za-z.]+)?$")
_ENTRY_RE = re.compile(r"^([\w./\-]+\.py):([A-Za-z_]\w*)$")


class ManifestError(ValueError):
    """manifest 不合法（消息直接展示给用户，要写清是哪一项不对）。"""


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    api_version: str
    entrypoint: str
    directory: Path
    permissions: list[str] = field(default_factory=list)
    description: str = ""
    author: str = ""
    navigation: dict[str, Any] = field(default_factory=dict)
    enabled_by_default: bool = True
    builtin: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    # -- entrypoint ------------------------------------------------------
    @property
    def entry_file(self) -> Path:
        rel = self.entrypoint.split(":", 1)[0]
        return (self.directory / rel).resolve()

    @property
    def factory_name(self) -> str:
        return self.entrypoint.split(":", 1)[1]

    # -- 兼容性 ----------------------------------------------------------
    @property
    def major(self) -> str:
        return str(self.api_version).split(".", 1)[0]

    def compatible(self, host_version: str = PLUGIN_API_VERSION) -> tuple[bool, str]:
        """未知**主**版本拒绝加载；次版本差异只做提示（能力用 capability 判）。"""
        host_major = str(host_version).split(".", 1)[0]
        if self.major != host_major:
            return False, (
                f"插件要求 api_version={self.api_version}，宿主是 {host_version}："
                "主版本不同，拒绝加载（避免用旧接口做出错误动作）"
            )
        if str(self.api_version) != str(host_version):
            return True, f"次版本不同（插件 {self.api_version} / 宿主 {host_version}），按能力检测降级"
        return True, ""

    def nav_section(self) -> str:
        section = str((self.navigation or {}).get("section") or "").strip()
        return section or "插件"

    def nav_order(self) -> int:
        try:
            return int((self.navigation or {}).get("order") or 100)
        except (TypeError, ValueError):
            return 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "entrypoint": self.entrypoint,
            "permissions": list(self.permissions),
            "description": self.description,
            "author": self.author,
            "navigation": dict(self.navigation),
            "enabled_by_default": self.enabled_by_default,
            "builtin": self.builtin,
            "directory": str(self.directory),
        }


def load_manifest(directory: Path, *, check_entry: bool = True) -> PluginManifest:
    """读并校验一个插件目录里的 `plugin.json`。"""
    directory = Path(directory)
    path = directory / MANIFEST_NAME
    if not path.exists():
        raise ManifestError(f"缺少 {MANIFEST_NAME}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{MANIFEST_NAME} 读不了或不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{MANIFEST_NAME} 顶层必须是对象")

    def need(key: str) -> str:
        value = raw.get(key)
        if value is None or str(value).strip() == "":
            raise ManifestError(f"缺少必填字段 {key}")
        return str(value).strip()

    plugin_id = need("id")
    if not _ID_RE.match(plugin_id):
        raise ManifestError(f"id 不合法：{plugin_id!r}（要求小写字母开头，只含小写字母/数字/下划线，2–40 字符）")

    version = need("version")
    if not _VERSION_RE.match(version):
        raise ManifestError(f"version 不合法：{version!r}（形如 1.0.0）")

    api_version = need("api_version")
    if not str(api_version).split(".", 1)[0].isdigit():
        raise ManifestError(f"api_version 不合法：{api_version!r}（形如 1 或 1.2）")

    entrypoint = need("entrypoint")
    if not _ENTRY_RE.match(entrypoint):
        raise ManifestError(
            f"entrypoint 不合法：{entrypoint!r}（形如 plugin.py:create_plugin，只允许目录内的 .py）"
        )

    perms_raw = raw.get("permissions") or []
    if not isinstance(perms_raw, list):
        raise ManifestError("permissions 必须是数组")
    permissions = [str(x).strip() for x in perms_raw if str(x).strip()]

    manifest = PluginManifest(
        id=plugin_id,
        name=str(raw.get("name") or plugin_id).strip(),
        version=version,
        api_version=str(api_version),
        entrypoint=entrypoint,
        directory=directory,
        permissions=permissions,
        description=str(raw.get("description") or "").strip(),
        author=str(raw.get("author") or "").strip(),
        navigation=dict(raw.get("navigation") or {}),
        enabled_by_default=bool(raw.get("enabled_by_default", True)),
        builtin=bool(raw.get("builtin", True)),
        raw=raw,
    )

    if check_entry:
        entry_file = manifest.entry_file
        # 路径逃逸检查：entrypoint 不能跑出插件目录（`..` 也不行）。
        try:
            entry_file.relative_to(directory.resolve())
        except ValueError as exc:
            raise ManifestError("entrypoint 指向插件目录之外，拒绝加载") from exc
        if not entry_file.exists():
            raise ManifestError(f"entrypoint 文件不存在：{entry_file.name}")
        if entry_file.suffix != ".py":
            raise ManifestError(f"entrypoint 必须是 .py 文件：{entry_file.name}")

    return manifest


def discover_manifests(root: Path, *, check_entry: bool = True) -> tuple[list[PluginManifest], list[tuple[Path, str]]]:
    """扫一个目录下的所有一级子目录，返回 (合法清单, [(目录, 错误)])。

    刻意**只扫一级子目录**：插件是一个目录一个插件，不做递归 ——
    递归会把 `__pycache__`、测试夹具之类的东西也当成插件候选。
    """
    root = Path(root)
    manifests: list[PluginManifest] = []
    errors: list[tuple[Path, str]] = []
    if not root.exists():
        return manifests, errors
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if not (child / MANIFEST_NAME).exists():
            # 没有 manifest 的目录**不算插件**（也不报错）：显式注册是刻意的要求。
            logger.debug("跳过没有 %s 的目录：%s", MANIFEST_NAME, child.name)
            continue
        try:
            manifests.append(load_manifest(child, check_entry=check_entry))
        except ManifestError as exc:
            errors.append((child, str(exc)))
    seen: dict[str, Path] = {}
    unique: list[PluginManifest] = []
    for manifest in manifests:
        if manifest.id in seen:
            errors.append((manifest.directory, f"id 与 {seen[manifest.id].name} 重复：{manifest.id}"))
            continue
        seen[manifest.id] = manifest.directory
        unique.append(manifest)
    return unique, errors
