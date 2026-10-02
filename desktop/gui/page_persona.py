"""人格页：三层只读信息 + 自动改动日志 + 四个明示动作。

**"只读"这件事由服务端保证**：改人设的接口已经删掉，只剩两个恒返回 410 的桩
（`webui.py:1049-1067`），要改只能直接编辑那三个文件。所以本页没有任何输入框，
只有四个动作：

    撤回最近自动改动（`/api/persona/undo`）
    立即反思一次（`/api/persona/reflect`，**会花 token**）
    生成测评素材（`/api/persona/eval` mode=artifacts，**会花 token**）
    跑一轮基线分（`/api/persona/eval` mode=round，**会花 token**）

后三个前面都要确认 —— 花的是主人的钱，不是 UI 的。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtWidgets

from ..core.util import one_line
from .base import Page, StateSlice
from .widgets import badge, esc, label, plain_text_edit, table


class PersonaPage(Page):
    title = "人格"
    subtitle = "三层结构（只读）、评估台与自动改动日志"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._persona: dict[str, Any] = {}
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("刷新")
        self.btn_refresh.clicked.connect(lambda: self.load(force=True))
        self.btn_undo = QtWidgets.QPushButton("撤回最近自动改动")
        self.btn_undo.setToolTip("只回退自动迭代写入的那一条（表层），并记入改动日志")
        self.btn_undo.clicked.connect(self._undo)
        self.btn_reflect = QtWidgets.QPushButton("立即反思一次（花 token）")
        self.btn_reflect.clicked.connect(self._reflect)
        self.btn_artifacts = QtWidgets.QPushButton("生成测评素材（花 token）")
        self.btn_artifacts.clicked.connect(lambda: self._eval("artifacts"))
        self.btn_round = QtWidgets.QPushButton("跑一轮基线分（花 token）")
        self.btn_round.clicked.connect(lambda: self._eval("round"))
        for widget in (self.btn_refresh, self.btn_undo, self.btn_reflect, self.btn_artifacts, self.btn_round):
            bar.addWidget(widget)
        bar.addStretch(1)
        outer.addLayout(bar)

        self.hint = label("正在读取人格信息…")
        outer.addWidget(self.hint)

        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs, 1)

        # ---- 三层
        layers = QtWidgets.QWidget()
        layers_layout = QtWidgets.QVBoxLayout(layers)
        layers_layout.addWidget(
            label(
                "三个文件都是**直接编辑文件**才生效（控制台不能改，这是刻意的边界）："
                "底层人设 persona/active/base.txt / 表层人设 persona/active/surface.txt / 禁止事项 persona/active/forbidden.txt",
                wrap=True,
            )
        )
        self.layer_table = table(["层", "文件", "字数", "说明"], stretch_last=True)
        self.layer_table.setMaximumHeight(160)
        layers_layout.addWidget(self.layer_table)
        self.layer_view = plain_text_edit("")
        layers_layout.addWidget(self.layer_view, 1)
        self.layer_selector = QtWidgets.QComboBox()
        self.layer_selector.currentTextChanged.connect(self._show_layer)
        selector_row = QtWidgets.QHBoxLayout()
        selector_row.addWidget(QtWidgets.QLabel("查看："))
        selector_row.addWidget(self.layer_selector)
        selector_row.addStretch(1)
        layers_layout.insertLayout(2, selector_row)
        self.tabs.addTab(layers, "三层（只读）")

        # ---- 评估台
        evaluation = QtWidgets.QWidget()
        eval_layout = QtWidgets.QVBoxLayout(evaluation)
        self.eval_hint = label("")
        eval_layout.addWidget(self.eval_hint)
        self.eval_table = table(["特质", "分数", "题数", "备注"])
        eval_layout.addWidget(self.eval_table, 1)
        self.tabs.addTab(evaluation, "评估台")

        # ---- 日志
        changelog = QtWidgets.QWidget()
        log_layout = QtWidgets.QVBoxLayout(changelog)
        log_layout.addWidget(
            label("服务端只保留最近若干条（`persona.changelog(30)`，上限见 capabilities.limits）", wrap=True)
        )
        self.log_table = table(["时间", "动作", "内容"])
        log_layout.addWidget(self.log_table, 1)
        self.tabs.addTab(changelog, "自动改动日志")

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: StateSlice(self.client().state()),
            on_done=self._apply,
            busy_text="正在读取人格信息…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("读取失败", level="err"))
            return
        state: StateSlice = result.value
        self._persona = state.persona
        persona = self._persona
        stats = dict(persona.get("stats") or {})
        iter_stats = dict(persona.get("iter") or {})
        evaluation = dict(persona.get("eval") or {})

        self.hint.setText(
            f"底层 {esc(stats.get('base_chars', 0))} 字 · 表层 {esc(stats.get('surface_chars', 0))} 字 · "
            f"禁止事项 {esc(stats.get('forbidden', 0))} 条 · 自动迭代已跑 {esc(iter_stats.get('runs', 0))} 次"
        )

        layers = dict(persona.get("layers") or {})
        forbidden = list(persona.get("forbidden") or [])
        rows = [
            ("底层人设", "persona/active/base.txt", len(str(layers.get("base") or "")), "只有主人直接编辑文件才能改"),
            ("表层人设", "persona/active/surface.txt", len(str(layers.get("surface") or "")), "自动迭代会写这里，可撤回"),
            ("禁止事项", "persona/active/forbidden.txt", len(forbidden), "只有主人直接编辑文件才能改"),
        ]
        self.layer_table.setRowCount(len(rows))
        for row, (name, filename, size, note) in enumerate(rows):
            for column, text in enumerate((name, filename, str(size), note)):
                self.layer_table.setItem(row, column, QtWidgets.QTableWidgetItem(str(text)))
        self.layer_table.resizeColumnsToContents()

        current = self.layer_selector.currentText()
        items = ["底层人设", "表层人设", "禁止事项"]
        if [self.layer_selector.itemText(i) for i in range(self.layer_selector.count())] != items:
            self.layer_selector.blockSignals(True)
            self.layer_selector.clear()
            self.layer_selector.addItems(items)
            self.layer_selector.setCurrentText(current if current in items else items[0])
            self.layer_selector.blockSignals(False)
        self._show_layer(self.layer_selector.currentText())

        self._fill_eval(evaluation)
        self._fill_log(list(persona.get("changelog") or []))
        self.toast("人格信息已载入", level="ok")
        self.refreshed.emit()

    def _show_layer(self, name: str) -> None:
        layers = dict(self._persona.get("layers") or {})
        if name == "底层人设":
            body = str(layers.get("base") or "")
        elif name == "表层人设":
            body = str(layers.get("surface") or "")
        else:
            forbidden = list(self._persona.get("forbidden") or [])
            body = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(forbidden))
        self.layer_view.setPlainText(body or "（空）")

    def _fill_eval(self, evaluation: dict[str, Any]) -> None:
        enabled = bool(evaluation.get("enabled"))
        traits = list(evaluation.get("traits") or [])
        artifacts = evaluation.get("artifacts")
        self.eval_hint.setText(
            badge("评估台已启用", level="ok") if enabled else badge("评估台已关闭（参数 → 人设评估 → eval_enabled）", level="warn")
        )
        self.eval_hint.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.eval_table.setRowCount(len(traits))
        for row, trait in enumerate(traits):
            score = trait.get("score")
            note = "未评" if score is None else ""
            if artifacts is not None:
                note = f"素材 {artifacts}"
            cells = [
                str(trait.get("key") or trait.get("name") or ""),
                "-" if score is None else f"{float(score):.3f}",
                str(trait.get("questions") or trait.get("count") or ""),
                str(trait.get("note") or note),
            ]
            for column, text in enumerate(cells):
                self.eval_table.setItem(row, column, QtWidgets.QTableWidgetItem(text))
        self.eval_table.resizeColumnsToContents()

    def _fill_log(self, entries: list[dict[str, Any]]) -> None:
        self.log_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            cells = [
                str(entry.get("ts") or entry.get("time") or ""),
                str(entry.get("action") or entry.get("kind") or ""),
                one_line(entry.get("text") or entry.get("note") or "", 200),
            ]
            for column, text in enumerate(cells):
                self.log_table.setItem(row, column, QtWidgets.QTableWidgetItem(text))
        self.log_table.resizeColumnsToContents()

    # ------------------------------------------------------------ 动作
    def _undo(self) -> None:
        if not QtWidgets.QMessageBox.question(
            self, "撤回自动改动",
            "回退最近一次**自动迭代**写入的表层人设？\n\n只会动表层人设文件，底层人设与禁止事项不受影响。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            self.client().persona_undo,
            on_done=lambda r: self._report(r, "撤回"),
            busy_text="正在撤回…",
        )

    def _reflect(self) -> None:
        if not self._confirm_cost("立即反思", "让机器人现在做一次自我反思（可能写入表层人设）。"):
            return
        self.run_task(
            self.client().persona_reflect,
            on_done=lambda r: self._report(r, "反思", detail_key="text"),
            busy_text="正在反思（可能要几十秒）…",
        )

    def _eval(self, mode: str) -> None:
        title = "生成测评素材" if mode == "artifacts" else "跑一轮基线分"
        what = "生成一批测评素材（题目/对话样例）" if mode == "artifacts" else "让裁判模型给各特质打一轮基线分"
        if not self._confirm_cost(title, f"{what}。"):
            return
        self.run_task(
            lambda: self.client().persona_eval(mode),
            on_done=lambda r: self._report(r, title, detail_key="text"),
            busy_text=f"正在{title}…",
        )

    def _confirm_cost(self, title: str, message: str) -> bool:
        return QtWidgets.QMessageBox.question(
            self, title, message + "\n\n这会调用模型，可能产生费用。继续？",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok

    def _report(self, result, action: str, *, detail_key: str = "note") -> None:  # noqa: ANN001
        if not result.ok:
            return
        payload = dict(result.value or {})
        ok = bool(payload.get("ok", True))
        detail = payload.get(detail_key) or payload.get("note") or payload.get("detail") or payload.get("error") or ""
        if ok:
            self.toast(f"{action}完成{('：' + one_line(detail, 100)) if detail else ''}", level="ok")
        else:
            self.toast(f"{action}未完成：{one_line(detail, 200)}", level="warn")
        self.load(force=True)
