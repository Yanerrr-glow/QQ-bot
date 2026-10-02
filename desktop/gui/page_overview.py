"""总览页：一眼看清机器人现在什么状态，并给三个最常用的快捷动作。

对应 WebUI 的 `/ai/` 首页：状态徽标 + 主动发言 / 手动问候 / 刷新。
另外多一块"插件状态卡"——那是桌面端独有扩展点（插件只能注册**只读**卡片）。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from ..core.util import one_line
from .base import Page, StateSlice
from .widgets import PALETTE, badge, esc, hline, read_only_item


class OverviewPage(Page):
    title = "总览"
    subtitle = "机器人当前状态与快捷动作"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._state: StateSlice | None = None
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("刷新")
        self.btn_refresh.clicked.connect(lambda: self.load(force=True))
        self.btn_speak = QtWidgets.QPushButton("主动发言一次")
        self.btn_speak.setToolTip("让机器人在某个活跃群主动说一句（会调用模型，可能产生费用）")
        self.btn_speak.clicked.connect(self._speak)
        self.btn_greet = QtWidgets.QPushButton("手动问候")
        self.btn_greet.setToolTip("按当前钟点发一次问候（绕开总开关，不消耗当天的自动次数）")
        self.btn_greet.clicked.connect(self._greet)
        self.btn_webui = QtWidgets.QPushButton("打开旧 WebUI（回退入口）")
        self.btn_webui.clicked.connect(self._open_webui)
        for widget in (self.btn_refresh, self.btn_speak, self.btn_greet):
            bar.addWidget(widget)
        bar.addStretch(1)
        bar.addWidget(self.btn_webui)
        outer.addLayout(bar)

        self.badges = QtWidgets.QLabel("加载中…")
        # 徽标是我们自己拼的富文本；里面的**外部字符串**一律先过 esc()。
        self.badges.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.badges.setWordWrap(True)
        self.badges.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.badges)
        outer.addWidget(hline())

        self.detail = QtWidgets.QTableWidget(0, 2)
        self.detail.setHorizontalHeaderLabels(["项目", "值"])
        self.detail.verticalHeader().setVisible(False)
        self.detail.horizontalHeader().setStretchLastSection(True)
        self.detail.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        outer.addWidget(self.detail, 1)

        cards_group = QtWidgets.QGroupBox("插件状态卡（只读）")
        cards_layout = QtWidgets.QVBoxLayout(cards_group)
        self.cards = QtWidgets.QLabel("（没有启用的插件状态卡）")
        self.cards.setWordWrap(True)
        self.cards.setTextFormat(QtCore.Qt.TextFormat.RichText)
        cards_layout.addWidget(self.cards)
        outer.addWidget(cards_group)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: StateSlice(self.client().state()),
            on_done=self._apply,
            busy_text="正在读取运行状态…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.badges.setText(badge("状态读取失败", level="err"))
            return
        state: StateSlice = result.value
        self._state = state
        self.badges.setText(self._badges_html(state))
        self._fill_table(state)
        self._fill_cards()
        self.toast("状态已刷新", level="ok")
        self.refreshed.emit()

    def _badges_html(self, state: StateSlice) -> str:
        status = state.status
        stickers = dict(status.get("stickers") or {})
        groups = list(status.get("groups") or [])
        search = dict(status.get("search") or {})
        greet = dict(status.get("greet") or {})
        proactive = dict(status.get("proactive") or {})
        time_info = dict(status.get("time") or {})

        online = self.manager.state.api_ok
        chips = [
            badge("在线" if online else "离线", level="ok" if online else "err"),
            badge(f"群 {len(groups)}"),
            badge(f"表情包 {int(stickers.get('count') or 0)} 张", level="info"),
            badge(
                f"重复组 {int(stickers.get('duplicates') or 0)}",
                level="warn" if int(stickers.get("duplicates") or 0) else "info",
            ),
            badge(
                "主动发言 开" if status.get("proactive_enabled") else "主动发言 关",
                level="ok" if status.get("proactive_enabled") else "info",
            ),
            badge(
                "定时问候 开" if greet.get("enabled") else "定时问候 关",
                level="ok" if greet.get("enabled") else "info",
            ),
            badge(f"模型 {status.get('model') or '未设置'}"),
            badge(
                "联网搜索 可用" if search.get("available") else
                ("联网搜索 未配端点" if search.get("enabled") else "联网搜索 关"),
                level="ok" if search.get("available") else "warn",
            ),
            badge(
                f"时钟 {time_info.get('now') or '-'}（偏差 {time_info.get('offset', 0)}s）",
                level="info",
            ),
        ]
        if proactive.get("day_count"):
            chips.append(badge(f"今日主动发言 {sum(int(v) for v in proactive['day_count'].values())}"))
        return " &nbsp; ".join(chips)

    def _fill_table(self, state: StateSlice) -> None:
        rows: list[tuple[str, str]] = []
        status = state.status
        memory = state.memory
        stats = dict(memory.get("stats") or {})
        persona = state.persona
        pstats = dict(persona.get("stats") or {})
        piter = dict(persona.get("iter") or {})
        image = state.image
        models = dict(state.models or {})
        proactive = dict(status.get("proactive") or {})
        greet = dict(status.get("greet") or {})

        rows.append(("当前目标", f"{self.manager.state.target_name} · {self.manager.state.address_summary()}"))
        rows.append(("模型档案", f"{models.get('active') or '-'}（{len(models.get('items') or [])} 个）"))
        rows.append(("记忆", f"事实 {stats.get('facts', 0)} · 群事件 {stats.get('events', 0)} · 人物 {stats.get('profile', 0)}"))
        rows.append(("人格", f"底层 {pstats.get('base_chars', 0)} 字 · 表层 {pstats.get('surface_chars', 0)} 字 · 禁止事项 {pstats.get('forbidden', 0)} 条"))
        rows.append(("人设自动迭代", f"已跑 {piter.get('runs', 0)} 次 · 写入 {piter.get('written', 0)} 条"))
        modes = image.get("valid_modes") or []
        rows.append(("图片策略", f"全局 {image.get('global_mode') or '-'}（可选 {'/'.join(map(str, modes))}）· 会话自定义 {len(image.get('convs') or {})} 个"))
        last_spoke = proactive.get("last_spoke") or {}
        rows.append(("最近主动发言", "、".join(f"{k} {v}" for k, v in last_spoke.items()) or "还没有"))
        rows.append(("问候发送记录", f"{len(greet.get('sent') or {})} 个时段有过记录 · 今天 {greet.get('today') or '-'}"))
        rows.append(("时间校准", f"{status.get('time', {}).get('now') or '-'}（宿主 {status.get('time', {}).get('raw') or '-'}，来源 {status.get('time', {}).get('source') or '-'}）"))

        self.detail.setRowCount(len(rows))
        for index, (key, value) in enumerate(rows):
            self.detail.setItem(index, 0, read_only_item(key))
            self.detail.setItem(index, 1, read_only_item(one_line(value, 200), tip=value))
        self.detail.resizeColumnsToContents()

    def _fill_cards(self) -> None:
        window = self.window()
        host = getattr(window, "plugin_host", None)
        if host is None:
            self.cards.setText("（没有插件宿主）")
            return
        cards = host.status_cards()
        if not cards:
            self.cards.setText("（没有启用的插件状态卡）")
            return
        lines = []
        for card in cards:
            if card.get("ok"):
                lines.append(f"<b>{esc(card.get('title'))}</b>：{esc(card.get('value'))}")
            else:
                lines.append(
                    f"<b>{esc(card.get('title'))}</b>：<span style='color:{PALETTE['err']}'>"
                    f"{esc(card.get('error'))}</span>"
                )
        self.cards.setText("<br>".join(lines))

    # ------------------------------------------------------------ 动作
    def _speak(self) -> None:
        if not self._confirm_cost("主动发言", "会让机器人挑一个活跃群主动说一句。\n这会调用模型，可能产生费用。"):
            return
        self.run_task(self.client().speak, on_done=lambda r: self._report_action(r, "主动发言"))

    def _greet(self) -> None:
        if not self._confirm_cost("手动问候", "按当前钟点发一次问候（不消耗当天自动问候的次数）。\n这会调用模型，可能产生费用。"):
            return
        self.run_task(lambda: self.client().greet(), on_done=lambda r: self._report_action(r, "手动问候"))

    def _confirm_cost(self, title: str, message: str) -> bool:
        return QtWidgets.QMessageBox.question(
            self, title, message,
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok

    def _report_action(self, result, label_text: str) -> None:  # noqa: ANN001
        if not result.ok:
            return
        payload: dict[str, Any] = dict(result.value or {})
        # 这两个接口**没有** `ok` 字段：成功是 `said=True`，失败是 `said=False` + reason。
        said = bool(payload.get("said"))
        if said:
            where = payload.get("group_id") or payload.get("slot") or ""
            self.toast(f"{label_text}成功{('（' + str(where) + '）') if where else ''}", level="ok")
        else:
            self.toast(f"{label_text}没有发出：{payload.get('reason') or '未知原因'}", level="warn")
        self.load(force=True)

    def _open_webui(self) -> None:
        state = self.manager.state
        url = f"{state.base_url}{state.prefix}/"
        if self.manager.token_present():
            self.toast("旧 WebUI 需要 ?token=… 才能打开；桌面端不会把令牌写进 URL 或浏览器历史", level="warn")
        QtGui.QDesktopServices.openUrl(QtGui.QUrl(url))
