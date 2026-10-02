"""第二个示范插件：记忆概览（只读）。

它和 `sticker_health` 是两个独立目录、两个独立 manifest、各自一份权限声明。
存在的意义是证明三件"多插件"相关的事：

1. 同一目录下多个插件各自登记、互不覆盖（注册 id 会带插件前缀）。
2. 一个插件只声明读权限时，哪怕另一个插件能写，它自己的写操作照样被拒。
3. 插件**不 import Qt** 也能加载 —— 自检环境没有 PySide6，两个插件都必须成功。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from desktop.sdk.api import ActionResult  # noqa: E402 - 依赖上面的 sys.path 调整

PLUGIN_ID = "memory_stats"


def _gui_ready() -> bool:
    """能不能真的建控件（装了 PySide6 **且**已有 QApplication）。

    与 `sticker_health/qt_compat.py` 里那份是同一个判据；这里内联一份，
    是为了让这个插件**只依赖 SDK 与标准库**（示范"最简插件"长什么样）。
    """
    try:
        from PySide6 import QtWidgets  # noqa: PLC0415
    except ImportError:
        return False
    return QtWidgets.QApplication.instance() is not None


class MemoryStatsPlugin:
    def __init__(self) -> None:
        self._last_summary = "还没有统计过"

    def manifest(self) -> dict[str, Any]:
        return {"id": PLUGIN_ID, "name": "记忆概览", "version": "1.0.0", "api_version": "1"}

    def register(self, api: Any) -> None:
        self.api = api
        api.register_page("overview", "记忆概览", self.build_page, order=30)
        api.register_status_card("summary", "记忆概览", self.card_value, refresh_interval=120)
        api.register_action(
            "summarize",
            "统计记忆与群事件",
            self.summarize,
            placement="menu",
            confirm="将读取记忆库统计（只读，不花 token）。继续？",
        )

    def on_activate(self) -> None:
        self.api.logger.info("记忆概览插件已激活")

    def on_deactivate(self) -> None:
        self.api.logger.info("记忆概览插件已停用")

    # ------------------------------------------------------------ 数据
    def _memory(self) -> dict[str, Any]:
        state = self.api.http.get("/api/state")
        return dict(state.get("memory") or {})

    def card_value(self, ctx: Any) -> str:
        memory = self._memory()
        stats = dict(memory.get("stats") or {})
        facts = len(memory.get("facts") or [])
        events = len(memory.get("events") or [])
        people = len(memory.get("profile") or [])
        locked = int(stats.get("protected") or 0)
        return f"事实 {facts} · 群事件 {events} · 人物 {people} · 受保护 {locked}"

    def summarize(self, ctx: Any) -> Any:
        memory = self._memory()
        facts = list(memory.get("facts") or [])
        events = list(memory.get("events") or [])
        people = list(memory.get("profile") or [])
        locked = [x for x in facts if x.get("locked") or x.get("protected")]
        self._last_summary = (
            f"事实 {len(facts)} 条（其中受保护 {len(locked)} 条）、"
            f"群事件 {len(events)} 条、人物画像 {len(people)} 个"
        )
        return ActionResult(ok=True, message="统计完成", detail=self._last_summary)

    # ------------------------------------------------------------ 界面
    def build_page(self, ctx: Any) -> Any:
        # 判据是"能不能真的建控件"：Qt 装着但没有 QApplication 时建 QWidget 会让进程直接死。
        if not _gui_ready():
            # 无 GUI：同步取数，返回纯数据（自检正是走这条路）。
            return {"title": "记忆概览", "text": self._render(self._memory()), "headless": True}

        from PySide6 import QtWidgets  # noqa: PLC0415

        box = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(box)
        label = QtWidgets.QLabel("记忆概览（只读）")
        label.setStyleSheet("font-size:16px;font-weight:600;")
        layout.addWidget(label)
        text = QtWidgets.QPlainTextEdit("正在读取记忆统计…")
        text.setReadOnly(True)
        layout.addWidget(text, 1)

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

        # 取数放后台：本插件的 http.get 是同步的，写在工厂里会卡住 GUI 线程。
        # lambda 跑在后台线程，只做取数与拼文本，不碰控件。
        ctx.api.run_async(lambda: self._render(self._memory()), on_done=fill, on_error=failed)
        return box

    def _render(self, memory: dict[str, Any]) -> str:
        facts = list(memory.get("facts") or [])
        lines = [self._text(memory)]
        lines.append("")
        lines.append("最近 10 条事实：")
        for item in sorted(facts, key=lambda x: float(x.get("ts") or 0), reverse=True)[:10]:
            flag = "[保护]" if (item.get("locked") or item.get("protected")) else "      "
            lines.append(f"{flag} {str(item.get('text') or '')[:60]}")
        return "\n".join(lines)

    def _text(self, memory: dict[str, Any]) -> str:
        stats = dict(memory.get("stats") or {})
        return (
            f"事实 {len(memory.get('facts') or [])} 条 · "
            f"群事件 {len(memory.get('events') or [])} 条 · "
            f"人物画像 {len(memory.get('profile') or [])} 个\n"
            f"服务端统计：{stats}\n"
            "（只读：本插件未声明任何写权限，新增/删除记忆会被 SDK 拒绝）"
        )


def create_plugin() -> MemoryStatsPlugin:
    return MemoryStatsPlugin()
