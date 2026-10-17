"""功能子菜单页：由 `catalog.FeatureEntry` 生成 —— 上面是细节，下面是设置。

一个功能页只做三件事（不需要各自手写页面）：

1. **细节** —— 从 `/api/state` 的 `status` / `memory` / `image` / `persona` 切片读出来的
   实时状态（`feature.state` 里的探针函数），以及功能说明。
2. **设置** —— 本功能归属的参数组（`catalog.feature_groups()` 裁剪/排序后）交给
   `SettingsPanel`；保存走 `POST /api/settings`，**改完立即生效**。
3. **去别处** —— 数据型功能（记忆库、表情包库、图片策略、参数）由两个跳转按钮送过去。

⚠ **本页不重复实现那些专用页面**：记忆条目、表情包缩略图、会话级图片策略都在各自页面里，
本页只给入口。否则同一件事两个入口、两套行为，迟早对不上。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtWidgets

from .. import theme
from ..base import Page, StateSlice
from ..params_view import show_value
from ..widgets import (
    PALETTE,
    badge,
    busy_button,
    description_label,
    esc,
    ghost_button,
    label,
    toolbar,
)
from . import catalog
from .panels import SettingsPanel, chips_html, chip_line


class FeaturePage(Page):
    """`catalog.FEATURES` 里的一个功能。`feature_id` 由 `build_feature_pages()` 注入。"""

    #: 挂在「功能」目录**下面**（`page_hub` 是同一个目录的顶层入口，值不同）。
    in_features = True
    #: 导航顶层目录名（`main_window.FEATURES_SECTION` 的**字面量副本**）。
    #: ⚠ 这里**故意**不 import `main_window`：那是反向依赖（主窗口 import 本模块），
    #: 成环之后 PySide6 的加载顺序会变得不可预测。字面量与主窗口同名不冲突 ——
    #: `gui_smoke` 有断言守着"子菜单必须挂在同名目录下"。
    nav_section = "功能"
    feature_id = ""

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self.feature = catalog.FEATURE_BY_ID[self.feature_id]
        # 页面标题就是目录里的标题：导航、窗口标题、自检都靠它对齐。
        self.title = self.feature.title
        self.subtitle = self.feature.subtitle
        self._groups: list[dict[str, Any]] = []
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        feature = self.feature
        outer = QtWidgets.QVBoxLayout(self)

        bar = toolbar("本功能的设置改完立即生效")
        self.btn_refresh = busy_button(
            "刷新", lambda: self.load(force=True),
            tip="重新读取本功能的参数与运行状态",
        )
        bar.addWidget(self.btn_refresh)
        bar.addSpacing(theme.SPACE["lg"])
        # 数据型功能的入口：那些页面才是"看数据/改数据"的地方，本页只管策略。
        # 每个按钮 = (页面标题, 按钮文案)；`always` 里的永远显示，「全量参数页」是设置总出口。
        self._jump_buttons: dict[str, QtWidgets.QPushButton] = {}
        for target, text in (
            ("记忆库", "打开记忆库"),
            ("表情包库", "打开表情包库"),
            ("图片策略（会话）", "打开会话级策略"),
            ("人格评估台", "打开评估台"),
            ("参数", "全量参数页"),
        ):
            button = ghost_button(text, lambda t=target: self._goto(t))
            bar.addWidget(button)
            self._jump_buttons[target] = button
        self.btn_memory = self._jump_buttons["记忆库"]
        self.btn_stickers = self._jump_buttons["表情包库"]
        self.btn_imagepolicy = self._jump_buttons["图片策略（会话）"]
        self.btn_eval = self._jump_buttons["人格评估台"]
        self.btn_params = self._jump_buttons["参数"]
        self.btn_params.setToolTip("所有参数在一页里，可搜索、可恢复 .env 默认")
        # 与本功能相邻的页面留入口，剩下的收起来 —— 一页上挂五个跳转按钮没人看。
        # 「全量参数页」永远保留：它是设置的总出口（能搜索、能恢复 .env 默认）。
        related = {
            "memory": ("记忆库", "表情包库"),
            "stickers": ("表情包库", "记忆库"),
            "image_policy": ("图片策略（会话）", "记忆库"),
            "persona": ("人格评估台", "记忆库"),
            "persona_iter": ("人格评估台", "记忆库"),
        }.get(feature.id, ("记忆库", "表情包库", "图片策略（会话）"))
        for target, button in self._jump_buttons.items():
            if target != "参数" and target not in related:
                button.setVisible(False)
        outer.addLayout(bar)

        # `catalog` 的说明是本地固定文案、带 `**粗体**` / 反引号键名：
        # 走 `description_label` 做最小 Markdown 处理（内部先 esc，服务端数据不经过它）。
        outer.addWidget(description_label(feature.description, wrap=True))

        detail_box = QtWidgets.QGroupBox("运行细节（只读）")
        detail_layout = QtWidgets.QVBoxLayout(detail_box)
        self.chips = chip_line([], empty="正在读取运行状态…")
        detail_layout.addWidget(self.chips)
        if feature.enable_key:
            self.enable_hint = label(
                f"开关参数：`{feature.enable_key}` —— 下面「设置」区里就能改，改完立即生效",
                level="info",
                wrap=True,
            )
        else:
            self.enable_hint = label("本功能没有单一总开关，按各自参数生效", level="info", wrap=True)
        detail_layout.addWidget(self.enable_hint)
        outer.addWidget(detail_box)

        self.panel = SettingsPanel(
            self._commit,
            title="设置（属于本功能的参数）",
            key_order=catalog.feature_key_order(feature),
            note="改完立即生效并写到服务端的 data/runtime/settings.json",
        )
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.panel)
        outer.addWidget(area, 1)

        self.hint = label("正在读取本功能的参数…")
        outer.addWidget(self.hint)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: StateSlice(self.client().state()),
            on_done=self._apply,
            busy_text=f"正在读取「{self.feature.title}」…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("读取失败", level="err"))
            self.chips.setText(badge("读取失败（看状态条里的原因）", level="err"))
            return
        state: StateSlice = result.value
        all_groups = list(state.settings_groups or [])
        self._groups = all_groups
        mine = catalog.feature_groups(self.feature, all_groups)
        total = self.panel.load_groups(mine)

        chips: list[tuple[str, Any, str]] = []
        for probe in self.feature.state:
            try:
                chips.extend(probe(state))
            except Exception as exc:  # noqa: BLE001 - 单个探针出错不该把整页打掉
                chips.append(("细节读取失败", f"{type(exc).__name__}", "err"))
        if self.feature.enable_key:
            chips.insert(0, (
                "总开关",
                "开" if catalog.setting_value(all_groups, self.feature.enable_key) else "关",
                "ok" if catalog.setting_value(all_groups, self.feature.enable_key) else "info",
            ))
        self.chips.setText(chips_html(chips, empty="本功能没有可展示的运行细节（都是静态参数）"))

        missing = catalog.missing_groups(self.feature, all_groups)
        note = f"本功能参数 {total} 个 · 共 {len(mine)} 组"
        if missing:
            # 服务端改了分组名：说清是哪一组，而不是安静地少一块参数。
            note += f" ｜ ⚠ 服务端没有这些分组：{'、'.join(missing)}（分组名可能改过）"
        self.hint.setText(
            f"<span style='color:{PALETTE['dim']}'>{esc(note)}</span>"
        )
        self.toast(f"「{self.feature.title}」已载入", level="ok")
        self.refreshed.emit()

    # ------------------------------------------------------------ 动作
    def _commit(self, key: str, value: Any) -> None:
        spec = self.panel.view.spec_of(key)

        def _save() -> dict[str, Any]:
            return self.client().set_settings(**{key: value})

        def _done(result) -> None:  # noqa: ANN001
            if not result.ok:
                # 保存失败要把界面改回服务端的真实值，否则界面会撒谎。
                self.panel.view.set_value(key, spec.get("value"))
                return
            applied = dict((result.value or {}).get("applied") or {})
            saved = applied.get(key, value)
            self.panel.view.apply_saved(key, saved)
            self.toast(f"{spec.get('label') or key} = {show_value(saved)}", level="ok")
            self.load(force=True)   # 开关一动，上面的细节 chip 立刻要跟着变

        self.run_task(_save, on_done=_done, busy_text=f"保存 {spec.get('label') or key}…")

    def _goto(self, title: str) -> None:
        window = self.window()
        goto = getattr(window, "goto_page", None)
        if callable(goto) and goto(title):
            return
        self.toast(f"找不到「{title}」页", level="warn")


def build_feature_pages() -> list[type[FeaturePage]]:
    """为目录里每个功能生成一个页面类（`feature_id` 是唯一差异）。"""
    pages: list[type[FeaturePage]] = []
    for feature in catalog.FEATURES:
        pages.append(type(
            f"FeaturePage_{feature.id}",
            (FeaturePage,),
            {"feature_id": feature.id, "__doc__": f"「{feature.title}」功能页（由目录生成）。"},
        ))
    return pages
