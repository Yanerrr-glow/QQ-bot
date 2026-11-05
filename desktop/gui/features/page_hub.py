"""「功能」中心页：一眼看清每个功能是开是关、现在什么状态，并点进它的子菜单设置。

这是「功能」目录的入口（导航顶层，与「总览」「终端」同级）。它自己**不编辑任何参数** ——
每个功能一个子菜单，设置与细节都在子菜单里。这里做的是"索引 + 体检"：

* 按类别列全部功能（类别与顺序来自 `catalog.FEATURES`，加功能只改目录那一处）；
* 每个功能显示开关状态与几条实时 chip（一次 `GET /api/state` 填满全部卡片）；
* 搜索过滤，以及一键跳到「功能」目录下的对应子菜单 / 全量「参数」页。

**一次请求填满全部卡片**是刻意的：全部功能各发一次请求既慢又会在服务端忙时刷屏，
而 `/api/state` 本来就是聚合端点（服务端把一切都塞在同一个响应里）。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtWidgets

from ..base import Page, StateSlice
from ..widgets import PALETTE, badge, esc, label, primary_button, toolbar
from . import catalog
from .panels import FeatureCard


class FeatureHubPage(Page):
    title = "功能"
    subtitle = "全部功能：开关状态、运行细节与各自的设置入口"
    #: 它**不在**「功能」目录下面 —— 它是那个目录的顶层入口（与总览、终端同级）。
    in_features = False
    nav_section = "功能"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._groups: list[dict[str, Any]] = []
        self._cards: dict[str, FeatureCard] = {}
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = toolbar("功能设置按类别分在左侧「功能」目录的子菜单里；这里也能直接点进去")
        self.btn_refresh = primary_button("刷新全部", lambda: self.load(force=True))
        bar.addWidget(self.btn_refresh)
        outer.addLayout(bar)

        # 过滤框单独一行：说明 + 搜索 + 按钮排一行时，940px 宽度会把「刷新全部」切掉。
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("过滤功能名/说明…")
        self.search.textChanged.connect(self._apply_filter)
        outer.addWidget(self.search)

        self.summary = label("正在读取全部功能…", wrap=True)
        outer.addWidget(self.summary)

        self.container = QtWidgets.QWidget()
        self.form = QtWidgets.QVBoxLayout(self.container)
        self.form.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.container)
        outer.addWidget(area, 1)

        self._build_cards()

    def _build_cards(self) -> None:
        for category in catalog.CATEGORY_ORDER:
            box = QtWidgets.QGroupBox(f"{category}（{len(catalog.features_of(category))}）")
            layout = QtWidgets.QVBoxLayout(box)
            for feature in catalog.features_of(category):
                card = FeatureCard(feature.id, feature.title, feature.description)
                card.btn_open.clicked.connect(
                    lambda _=False, t=feature.title: self._goto(t)
                )
                card.btn_params.clicked.connect(lambda _=False: self._goto("参数"))
                layout.addWidget(card)
                self._cards[feature.id] = card
            self.form.addWidget(box)
        self.form.addStretch(1)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: StateSlice(self.client().state()),
            on_done=self._apply,
            busy_text="正在读取全部功能…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.summary.setText(badge("读取失败（看状态条里的原因）", level="err"))
            return
        state: StateSlice = result.value
        self._groups = list(state.settings_groups or [])

        enabled = 0
        switchable = 0
        total_params = 0
        for feature in catalog.FEATURES:
            card = self._cards[feature.id]
            groups = list(state.settings_groups or [])
            total_params += catalog.param_total(feature, groups)
            if feature.enable_key:
                switchable += 1
                on = bool(catalog.setting_value(groups, feature.enable_key))
                enabled += 1 if on else 0
                card.set_enabled(on)
            else:
                card.set_enabled(None)
            chips: list[tuple[str, Any, str]] = []
            for probe in feature.state:
                try:
                    chips.extend(probe(state))
                except Exception as exc:  # noqa: BLE001 - 单个探针出错不该打掉整页
                    chips.append(("细节读取失败", f"{type(exc).__name__}", "err"))
            card.set_chips(chips)

        self.summary.setText(
            f"{esc(len(catalog.FEATURES))} 个功能 / {esc(total_params)} 个参数 · "
            f"带开关的 {esc(enabled)} / {esc(switchable)} 个已启用 ｜ "
            f"{esc(len(state.status.get('groups') or []))} 个已知群 · "
            f"模型 {esc(state.status.get('model') or '未设置')} ｜ "
            "功能设置在各子菜单里，这里只做索引与体检（全量参数在「参数」页）"
        )
        self.toast(f"已载入 {len(catalog.FEATURES)} 个功能", level="ok")
        self.refreshed.emit()

    # ------------------------------------------------------------ 过滤与跳转
    def _apply_filter(self, text: str) -> None:
        needle = str(text or "").strip().lower()
        for feature in catalog.FEATURES:
            card = self._cards[feature.id]
            blob = f"{feature.title} {feature.subtitle} {feature.description} {feature.category}".lower()
            matched = (not needle) or (needle in blob)
            # 参数名也参与匹配：用户常按 `sticker_chance` 这种键名找功能。
            if not matched and needle:
                matched = needle in " ".join(feature.groups).lower() or any(
                    needle in key.lower() for key in catalog.feature_key_order(feature)
                )
            card.setVisible(matched)
        # 类别框整组都空了就收起来，否则会留下一排空标题。
        for index in range(self.form.count()):
            box = self.form.itemAt(index).widget()
            if not isinstance(box, QtWidgets.QGroupBox):
                continue
            visible = any(
                box.layout().itemAt(i).widget().isVisible()
                for i in range(box.layout().count())
                if box.layout().itemAt(i).widget() is not None
            )
            box.setVisible(visible or not needle)

    def _goto(self, title: str) -> None:
        window = self.window()
        goto = getattr(window, "goto_page", None)
        if callable(goto) and goto(title):
            return
        self.toast(f"找不到「{title}」页", level="warn")
