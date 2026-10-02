"""QQ_bot 桌面管理端（独立进程，与 NoneBot 服务端解耦）。

为什么单独放一个顶层目录，而不是塞进 `plugins/`：

    bot.py 里是 `nonebot.load_plugins("plugins")` —— 它会**递归扫** plugins 下的
    所有模块并 import。PySide6 一旦出现在那条路径上，机器人运行环境就被迫背上
    GUI 依赖（服务器上装不上 Qt，一装就是几百 MB）。所以桌面端必须在
    `plugins/` 之外，这个目录的存在本身就是那条边界。

本包分三层：

    desktop/core/   非 GUI：API 客户端、连接目标、凭证、SSH 隧道、脱敏日志
                    —— **只用标准库**，因此可以在没有 PySide6 的机器上跑自检
    desktop/sdk/    桌面 UI 插件 SDK：manifest、权限、PluginAPI、加载器
    desktop/gui/    PySide6 界面（唯一 import Qt 的地方）

约定：`desktop/` 与 `plugins/` 之间**没有代码依赖**，两边只通过 HTTP API 说话。
"""

from __future__ import annotations

APP_NAME = "QQ_bot 桌面控制台"
APP_VERSION = "1.0.0"

# 插件协议版本：与插件 manifest 的 `api_version` 比较，只认主版本。
PLUGIN_API_VERSION = "1"

__all__ = ["APP_NAME", "APP_VERSION", "PLUGIN_API_VERSION", "__version__"]

__version__ = APP_VERSION
