"""权限表：**把"插件只能请求相对 API 路径"升级成"只放行已知路径"**。

方案 5.3 要求"插件请求未获授权的操作时由 SDK 拒绝"。做法不能是"看插件有没有
在 manifest 里声明" —— 那个声明的可信度等于零（插件本来就是不可信代码）。
所以真正的判据是这张表：

    服务端路由（方法 + 路径） → 需要哪一个权限

`PluginAPI.http.*` 每次调用都先在这里查表：查不到 = **拒绝**（白名单而非黑名单），
查到才去看插件的 manifest 里有没有声明那个权限。这样：

- 绝对 URL 自动出局（不在表里）；
- 想借宿主客户端把凭据发到外部地址？路径都过不了；
- 服务端将来加了新路由，插件在 SDK 支持前用不了（宁可少，不可多）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# 方案 5.3 列出的 15 个权限，一个不多一个不少。
PERMISSIONS: dict[str, str] = {
    "api.read.status": "读运行状态与总览数据",
    "api.read.settings": "读全部参数",
    "api.write.settings": "改参数 / 恢复默认",
    "api.read.models": "读模型档案（**只含掩码**）",
    "api.write.models": "改模型档案（切换 / 保存 / 删除）",
    "api.read.persona": "读人格三层与改动日志",
    "api.action.persona": "触发行人设动作（撤回 / 反思 / 评估）",
    "api.read.memory": "读记忆画像、事实与群事件",
    "api.write.memory": "写记忆（新增 / 保护 / 删除）",
    "api.read.images": "读图片策略",
    "api.write.images": "改图片策略",
    "api.read.stickers": "读表情包列表与图片",
    "api.write.stickers": "删表情包",
    "api.action.speak": "触发主动发言",
    "api.action.greet": "触发手动问候",
}


class PermissionDenied(PermissionError):
    """插件越权（或请求了未登记路径）。消息会进插件管理页的错误栏。"""

    def __init__(self, message: str, *, plugin_id: str = "", permission: str = "", path: str = "") -> None:
        super().__init__(message)
        self.plugin_id = plugin_id
        self.permission = permission
        self.path = path


@dataclass(frozen=True)
class RouteRule:
    """一条白名单规则。`pattern` 里的 `{name}` 匹配单个路径段（不含 `/`）。"""

    method: str
    pattern: str
    permission: str
    note: str = ""
    _regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # 把 `{name}` 占位符换成"单个路径段"，其余字符全部转义。
        rx = "^" + re.sub(r"\\\{[a-zA-Z_]\w*\\\}", "[^/]+", re.escape(self.pattern)) + "$"
        object.__setattr__(self, "_regex", re.compile(rx))

    def matches(self, method: str, path: str) -> bool:
        return self.method == method.upper() and bool(self._regex.match(path))


# 与 `plugins/ai_chat/webui.py` 的路由一一对应（路径已去掉 /ai 前缀）。
ROUTES: tuple[RouteRule, ...] = (
    RouteRule("GET", "/api/state", "api.read.status", "总览/全部面板的初始数据"),
    RouteRule("POST", "/api/settings", "api.write.settings", "保存参数"),
    RouteRule("POST", "/api/settings/reset", "api.write.settings", "恢复 .env 默认"),
    RouteRule("POST", "/api/model/active", "api.write.models", "切换档案"),
    RouteRule("POST", "/api/model/test", "api.write.models", "探活（会花 token）"),
    RouteRule("POST", "/api/model/save", "api.write.models", "保存档案 JSON"),
    RouteRule("POST", "/api/model/delete", "api.write.models", "删除档案"),
    RouteRule("POST", "/api/persona/undo", "api.action.persona", "撤回自动改动"),
    RouteRule("POST", "/api/persona/reflect", "api.action.persona", "手动反思"),
    RouteRule("POST", "/api/persona/eval", "api.action.persona", "评估素材/基线"),
    RouteRule("POST", "/api/memory", "api.write.memory", "新增记忆"),
    RouteRule("DELETE", "/api/memory/{item_id}", "api.write.memory", "删除记忆"),
    RouteRule("POST", "/api/memory/{item_id}/protect", "api.write.memory", "保护/解除"),
    RouteRule("POST", "/api/image-policy", "api.write.images", "全局/会话图片策略"),
    RouteRule("GET", "/api/stickers", "api.read.stickers", "表情包列表与统计"),
    RouteRule("GET", "/api/stickers/{digest}", "api.read.stickers", "缩略图/原图"),
    RouteRule("DELETE", "/api/stickers/{digest}", "api.write.stickers", "删除表情包"),
    RouteRule("POST", "/api/speak", "api.action.speak", "主动发言（会花 token）"),
    RouteRule("POST", "/api/greet", "api.action.greet", "手动问候（会花 token）"),
    # `GET /api/capabilities` 是只读探测，归到"读状态"，让插件能自己判能力。
    RouteRule("GET", "/api/capabilities", "api.read.status", "后端能力探测（服务端暂未提供）"),
)

# 只读权限可以顺带从 `/api/state` 里取到对应切片，所以读操作都额外允许 state。
# **已知取舍**：`GET /api/state` 是服务端的聚合端点，它一次返回 settings/models/persona/
# memory/image/stickers/status 全部内容。也就是说：声明任意一个 `api.read.*` 的插件，
# 都能从这个响应里看到其它切片。这是服务端聚合端点的固有性质，不是这里的疏漏 ——
# 真要按切片隔离，得先让服务端把 state 拆成多个只读端点（方案第 4.3 节的方向）。
_STATE_READERS: dict[str, tuple[str, ...]] = {
    "api.read.settings": ("GET /api/state",),
    "api.read.models": ("GET /api/state",),
    "api.read.persona": ("GET /api/state",),
    "api.read.memory": ("GET /api/state",),
    "api.read.images": ("GET /api/state",),
    "api.read.stickers": ("GET /api/state",),
}


def strip_prefix(path: str, prefix: str = "/ai") -> str:
    """去掉 API 前缀，得到用于查表的规范路径。"""
    raw = str(path or "").split("?", 1)[0]
    if not raw.startswith("/"):
        raw = "/" + raw
    pre = str(prefix or "").strip()
    if pre and pre != "/":
        if not pre.startswith("/"):
            pre = "/" + pre
        pre = pre.rstrip("/")
        if raw == pre:
            return "/"
        if raw.startswith(pre + "/"):
            raw = raw[len(pre):]
    return raw or "/"


def required_permission(method: str, path: str, *, prefix: str = "/ai") -> str | None:
    """查表：这个调用需要什么权限。返回 None = **拒绝**（未登记路径）。"""
    norm = strip_prefix(path, prefix)
    for rule in ROUTES:
        if rule.matches(method, norm):
            return rule.permission
    return None


def describe_routes() -> Iterable[tuple[str, str, str]]:
    for rule in ROUTES:
        yield rule.method, rule.pattern, f"{rule.permission}｜{rule.note}"


def unknown_permissions(declared: Iterable[str]) -> list[str]:
    """manifest 里声明了但 SDK 不认识的权限名（多半是手写拼错）。"""
    return sorted({str(x) for x in declared if str(x) not in PERMISSIONS})


def check(
    plugin_id: str,
    declared: Iterable[str],
    method: str,
    path: str,
    *,
    prefix: str = "/ai",
) -> str:
    """核心判定：返回被用到的权限名；任何一步不通过就抛 `PermissionDenied`。"""
    norm = strip_prefix(path, prefix)
    if "://" in str(path):
        raise PermissionDenied(
            f"拒绝绝对 URL：{path}", plugin_id=plugin_id, path=norm
        )
    needed = required_permission(method, norm, prefix="/")
    if needed is None:
        raise PermissionDenied(
            f"未登记的 API 路径：{method.upper()} {norm}（SDK 白名单里没有它）",
            plugin_id=plugin_id,
            path=norm,
        )
    declared_set = {str(x) for x in declared}
    if needed in declared_set:
        return needed
    # 只读权限：`/api/state` 自身允许被任何 read.* 权限使用（数据就是从那来的）。
    if needed == "api.read.status" and method.upper() == "GET":
        readers = [perm for perm in declared_set if perm in _STATE_READERS]
        if readers:
            return readers[0]
    raise PermissionDenied(
        f"插件未声明权限 {needed}（{PERMISSIONS.get(needed, '')}），拒绝 {method.upper()} {norm}",
        plugin_id=plugin_id,
        permission=needed,
        path=norm,
    )
