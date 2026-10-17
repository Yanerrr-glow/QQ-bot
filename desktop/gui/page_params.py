"""参数页：全部设置项由 `settings.describe()` 动态生成，页面上没有一个硬编码的键。

这一点很关键：WebUI 加一个参数（`settings.py` 的 `_SPECS` 加一行）桌面端要自动跟上，
否则两边迟早对不上。所以这里的每一个控件都是"按 `kind` 现造"的。

**渲染逻辑在 `params_view.ParamsGroupsView` 里**（2026-10-03 抽出）：「功能」目录下的
每个子菜单用的是同一份 —— 那边只把组过滤成"属于本功能的那几组"。
本页保持"全量 + 搜索 + 恢复默认"的定位。

密钥类（`secret=True`）的行为与 WebUI 一致：服务端**只回掩码**，
桌面端也把掩码原样回写 —— `settings.py:688` 会识别 `***` 并保留原值。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtWidgets

from . import theme
from .base import Page
from .params_view import ParamsGroupsView, show_value
from .widgets import badge, busy_button, danger_button, esc, label, toolbar


class ParamsPage(Page):
    title = "参数"
    subtitle = "全部运行参数（动态读取；单项保存，改完立即生效）"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        # 视图**先建**：下面工具栏里的"展开/收起/过滤"回调都指向它（顺序反了就是 AttributeError）。
        self.view = ParamsGroupsView(self._commit)

        # 用统一工具条助手：它已经放了左侧说明 + stretch；
        # 手搓 QHBoxLayout 会把四个按钮均分拉伸成大方块（2026-10-18 实拍）。
        bar = toolbar("恢复默认不影响人格与记忆")
        self.btn_refresh = busy_button("刷新", lambda: self.load(force=True))
        self.btn_expand = busy_button("全部展开", lambda: self.view.set_all_groups(True))
        self.btn_collapse = busy_button("全部收起", lambda: self.view.set_all_groups(False))
        self.btn_reset = danger_button(
            "恢复 .env 默认", self._reset_all,
            tip="清掉控制台写入的覆盖值，全部回到 .env（不影响人格与记忆）",
        )
        for widget in (self.btn_refresh, self.btn_expand, self.btn_collapse):
            bar.addWidget(widget)
        bar.addSpacing(theme.SPACE["lg"])
        bar.addWidget(self.btn_reset)
        outer.addLayout(bar)

        # 过滤框单独一行：和四个按钮挤在同一行时，窗口缩到最小宽度会把
        # 「恢复 .env 默认」切掉（940px 实测）。搜索框本来也更适合占整行。
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("过滤参数名/说明…")
        self.search.textChanged.connect(lambda text: self.view.apply_filter(text))
        outer.addWidget(self.search)

        self.hint = label("正在读取参数…")
        outer.addWidget(self.hint)

        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.view)
        outer.addWidget(area, 1)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: list(self.client().state().get("settings") or []),
            on_done=self._apply,
            busy_text="正在读取参数…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("参数读取失败", level="err"))
            return
        groups: list[dict[str, Any]] = list(result.value or [])
        total = self.view.rebuild(groups)
        self.hint.setText(
            f"{esc(len(groups))} 组 / {esc(total)} 个参数 · "
            "修改后立即保存到服务端的 <code>data/runtime/settings.json</code>"
        )
        if self.search.text().strip():
            self.view.apply_filter(self.search.text())
        self.toast(f"已载入 {total} 个参数", level="ok")
        self.refreshed.emit()

    # ------------------------------------------------------------ 动作
    def _commit(self, key: str, value: Any) -> None:
        spec = self.view.spec_of(key)

        def _save() -> dict[str, Any]:
            return self.client().set_settings(**{key: value})

        def _done(result) -> None:  # noqa: ANN001
            if not result.ok:
                # 失败要**把界面改回服务端的真实值**，否则界面会撒谎。
                self.view.set_value(key, spec.get("value"))
                return
            applied = dict((result.value or {}).get("applied") or {})
            saved = applied.get(key, value)
            self.view.apply_saved(key, saved)
            self.toast(f"{spec.get('label') or key} = {show_value(saved)}", level="ok")
            self.refreshed.emit()

        self.run_task(_save, on_done=_done, busy_text=f"保存 {spec.get('label') or key}…")

    def _reset_all(self) -> None:
        if not QtWidgets.QMessageBox.question(
            self, "恢复全部默认",
            "把所有参数恢复为 .env 默认值？\n\n"
            "这会把控制台写入的覆盖值全部清掉（含模型档案相关的设置项）。\n"
            "**不会**动人格三层文件，也**不会**动记忆库。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            self.client().reset_settings,
            on_done=lambda result: self._after_reset(result),
            busy_text="正在恢复默认…",
        )

    def _after_reset(self, result) -> None:  # noqa: ANN001
        if result.ok:
            self.toast("已恢复 .env 默认", level="ok")
            self.load(force=True)
