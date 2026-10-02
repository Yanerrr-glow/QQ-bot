"""示范插件：表情包体检。

它存在的意义是**证明接口够用**，不是提供功能。具体证明了四件事：

1. **能注册独立页面**：`register_page(...)`，工厂收到 `PluginContext`（拿不到主窗口）。
2. **能受控地调服务端**：`api.http.get("/api/stickers")` —— 只给相对路径，
   绝对 URL、未登记路径、未声明的权限都会被 SDK 拒掉。
3. **能注册只读状态卡与操作**：卡只返回数据；操作可以带确认与"会花 token"标记。
4. **能优雅退化**：没装 PySide6 时页面工厂返回纯数据，插件照样加载成功
   （看起来是小事，但这正是"插件加载失败"与"环境没 GUI"的分界线）。

权限只声明了两个读权限。故意**不**声明 `api.write.stickers` ——
这样"越权被拒"是可以在自检里被验证的事实，而不是文档里的承诺。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 插件要么只依赖标准库，要么用宿主 SDK 的**公开**类型。这里选后者，
# 是为了让动作返回值就是宿主认识的那个类型，而不是鸭子类型猜出来的形状。
# 注意路径推导：plugin.py → sticker_health → plugins → desktop → 项目根。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from desktop.sdk.api import ActionResult  # noqa: E402 - 依赖上面的 sys.path 调整
from qt_compat import gui_ready, qt_available, qtwidgets  # noqa: E402

PLUGIN_ID = "sticker_health"


def _human_bytes(size: Any) -> str:
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


class StickerHealthPlugin:
    """无参构造 —— 这一点是刻意的：插件不该在构造期就拿到宿主的东西。"""

    def __init__(self) -> None:
        self._last_report = "还没有体检过"

    # ------------------------------------------------------------ 协议
    def manifest(self) -> dict[str, Any]:
        return {"id": PLUGIN_ID, "name": "表情包体检", "version": "1.0.0", "api_version": "1"}

    def register(self, api: Any) -> None:
        self.api = api
        api.register_page(
            "overview",
            "表情包体检",
            self.build_page,
            icon="health",
            order=20,
        )
        api.register_status_card("summary", "表情包体检", self.card_value, refresh_interval=60)
        api.register_action(
            "report",
            "生成表情包体检报告",
            self.report,
            placement="toolbar",
            confirm="将读取表情包列表并统计，可能耗时几秒（不花 token）。继续？",
        )
        self.api.logger.info("示范插件已登记：1 页面 / 1 状态卡 / 1 操作")

    def on_activate(self) -> None:
        # 只有到了这里才允许碰界面：主窗口与导航已经就绪。
        self.api.logger.info("示范插件已激活（系统主题：Qt 可用=%s）", qt_available())

    def on_deactivate(self) -> None:
        self.api.logger.info("示范插件已停用")
        self._last_report = "还没有体检过"

    # ------------------------------------------------------------ 数据
    def _stats(self) -> dict[str, Any]:
        got = self.api.http.get("/api/stickers")
        return dict(got.get("stats") or {}), list(got.get("items") or [])

    def card_value(self, ctx: Any) -> str:
        stats, _items = self._stats()
        count = int(stats.get("count") or 0)
        dups = int(stats.get("duplicates") or 0)
        used = int(stats.get("used") or 0)
        return f"{count} 张 · {_human_bytes(stats.get('total_bytes'))} · 重复组 {dups} · 被用过 {used} 次"

    def report(self, ctx: Any) -> Any:
        stats, items = self._stats()
        count = int(stats.get("count") or 0)
        dups = int(stats.get("duplicates") or 0)
        no_phash = int(stats.get("no_phash") or 0)
        never_used = [x for x in items if not int(x.get("uses") or 0)]
        top = sorted(items, key=lambda x: float(x.get("score") or 0), reverse=True)[:3]
        lines = [
            f"共 {count} 张，占用 {_human_bytes(stats.get('total_bytes'))}",
            f"疑似重复组 {dups} 组；没有感知哈希的 {no_phash} 张（旧数据）",
            f"从未被用过的 {len(never_used)} 张",
        ]
        if top:
            lines.append("喜好分前三：" + "、".join(
                f"{str(x.get('hash'))[:8]}（{float(x.get('score') or 0):.2f}）" for x in top
            ))
        self._last_report = "\n".join(lines)
        return ActionResult(ok=True, message="体检完成", detail=self._last_report)

    # ------------------------------------------------------------ 界面
    def build_page(self, ctx: Any) -> Any:
        """返回一个页面控件；**没有可用的 GUI 时退化成纯数据**。

        注意判据是 `gui_ready()`（装了 Qt **且**已有 QApplication），不是 `qt_available()`：
        Qt 在无 QApplication 时创建 QWidget 会直接让进程死掉，不是异常。

        **取数必须异步**：`http.get` 是同步的，写在工厂里就是让 GUI 线程干等一次超时
        （隧道没起来时十几秒，用户实测报回"点插件页卡住"）。所以先给骨架，数据用
        `ctx.api.run_async` 在后台线程拉、回 GUI 线程填。
        """
        if not gui_ready():
            # 无 GUI 环境（自检/服务器）：同步取数，返回数据由宿主包成只读文本。
            stats, items = self._stats()
            return {
                "title": "表情包体检",
                "text": self.report_text(stats, items),
                "items": len(items),
                "headless": True,
            }

        widgets = qtwidgets()
        box = widgets.QWidget()
        layout = widgets.QVBoxLayout(box)
        title = widgets.QLabel("表情包体检")
        title.setStyleSheet("font-size:16px;font-weight:600;")
        layout.addWidget(title)
        text = widgets.QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("正在读取表情包统计…")
        layout.addWidget(text, 1)
        hint = widgets.QLabel(
            "只读页面：本插件只声明了 api.read.stickers / api.read.status，"
            "删除表情包之类写操作会被 SDK 拒绝。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        def fetch() -> str:
            # ⚠ 这段跑在后台线程：只许算数据，不许碰上面的任何控件。
            stats, items = self._stats()
            return self.report_text(stats, items)

        def fill(body: str) -> None:
            if ctx.token.cancelled:
                return
            text.setPlainText(body)

        def failed(message: str) -> None:
            if ctx.token.cancelled:
                return
            text.setPlainText(
                f"读取失败：{message}\n\n"
                "（连接恢复后点顶部「刷新当前页」重试；隧道状态见「终端」页）"
            )

        ctx.api.run_async(fetch, on_done=fill, on_error=failed)
        return box

    def report_text(self, stats: dict[str, Any], items: list[dict[str, Any]]) -> str:
        return (
            f"共 {int(stats.get('count') or 0)} 张，占用 {_human_bytes(stats.get('total_bytes'))}\n"
            f"其中被用过 {int(stats.get('used') or 0)} 次，"
            f"以文件形式发来的 {int(stats.get('file_sent') or 0)} 张\n"
            f"疑似重复组：{int(stats.get('duplicates') or 0)}\n"
            f"当前目标缓存里的条目：{len(items)}\n\n"
            "（数据来自 GET /ai/api/stickers，与 WebUI 表情包页同一个来源）"
        )


def create_plugin() -> StickerHealthPlugin:
    """manifest 里 `entrypoint` 指向的工厂。**必须无参**。"""
    return StickerHealthPlugin()
