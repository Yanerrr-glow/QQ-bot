"""模型页：上半是档案表（切换/探活/新增/删除），下半是整份 JSON 编辑。

为什么不把"改密钥"做成单个输入框：服务端给出的就是**掩码**
（`webui.py:933-936`，`llm.editor_text()` 里 `api_key` 显示成 `***`），
而保存接口 `POST /api/model/save` 收的是**整份 JSON 文本**（`webui.py:1008`）。
所以这里照原样提供 JSON 编辑器 —— 高级但诚实，而且服务端自己会保留掩码位置的原密钥。

**探活会真的调一次模型**（花 token），所以按钮上标出来、并先确认。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtWidgets

from ..core.util import one_line
from .base import Page
from .widgets import badge, esc, label, plain_text_edit, table

NEW_PROFILE_TEMPLATE = """{
  "new-profile": {
    "label": "新档案",
    "model": "deepseek-chat",
    "base_url": "https://api.deepseek.com",
    "api_key": ""
  }
}"""


class ModelsPage(Page):
    title = "模型"
    subtitle = "档案列表、切换、探活与 JSON 编辑"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._models: dict[str, Any] = {}
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        for text, slot, tip in (
            ("刷新", lambda: self.load(force=True), "重新读取档案列表"),
            ("切换为当前", self._activate, "把选中的档案设为当前使用的模型"),
            ("探活（花 token）", self._probe, "向该档案发一次最小请求，确认密钥与网络可用"),
            ("新增模板", self._new_template, "在 JSON 编辑器里插入一份新档案模板"),
            ("删除", self._delete, "删除选中的档案（会确认）"),
        ):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(slot)
            button.setToolTip(tip)
            bar.addWidget(button)
        bar.addStretch(1)
        outer.addLayout(bar)

        self.hint = label("正在读取模型档案…")
        outer.addWidget(self.hint)

        self.table = table(["档案 id", "名称", "模型", "接口地址", "密钥", "当前"])
        self.table.setMaximumHeight(200)
        outer.addWidget(self.table)

        outer.addWidget(QtWidgets.QLabel("整份档案 JSON（保存时服务端会保留掩码位置的原密钥）"))
        self.editor = plain_text_edit("", readonly=False)
        outer.addWidget(self.editor, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("保存整份 JSON")
        self.btn_save.clicked.connect(self._save)
        self.btn_reload_editor = QtWidgets.QPushButton("放弃修改，重新读取")
        self.btn_reload_editor.clicked.connect(lambda: self.load(force=True))
        bottom.addWidget(self.btn_save)
        bottom.addWidget(self.btn_reload_editor)
        bottom.addStretch(1)
        outer.addLayout(bottom)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        def _fetch() -> tuple[dict[str, Any], str]:
            state = self.client().state()
            return dict(state.get("models") or {}), str(state.get("models_editor") or "")

        self.run_task(_fetch, on_done=self._apply, busy_text="正在读取模型档案…")

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("读取失败", level="err"))
            return
        models, editor = result.value
        self._models = models
        items = list(models.get("items") or [])
        active = str(models.get("active") or "")

        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            cells = [
                str(item.get("id") or ""),
                str(item.get("label") or ""),
                str(item.get("model") or ""),
                str(item.get("base_url") or ""),
                str(item.get("api_key") or "(未设置)"),
                "当前" if str(item.get("id")) == active else "",
            ]
            for column, text in enumerate(cells):
                widget = QtWidgets.QTableWidgetItem(text)
                widget.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(row, column, widget)
        self.table.resizeColumnsToContents()

        self.editor.setPlainText(editor)
        self.hint.setText(
            f"{esc(len(items))} 个档案 · 当前 <b>{esc(active or '未设置')}</b> · "
            "密钥一律显示为掩码，明文不会出现在桌面端"
        )
        self.toast("模型档案已载入", level="ok")
        self.refreshed.emit()

    def _selected_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return item.text() if item is not None else ""

    # ------------------------------------------------------------ 动作
    def _activate(self) -> None:
        profile_id = self._selected_id()
        if not profile_id:
            self.toast("先选一个档案", level="warn")
            return
        self.run_task(
            lambda: self.client().model_active(profile_id),
            on_done=lambda r: self._report(r, "切换", then_reload=True),
            busy_text=f"正在切换到 {profile_id}…",
        )

    def _probe(self) -> None:
        profile_id = self._selected_id()
        target = profile_id or "当前档案"
        if not QtWidgets.QMessageBox.question(
            self, "探活",
            f"向「{target}」发一次最小请求以确认可用。\n\n这会调用模型，可能产生少量费用。继续？",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            lambda: self.client().model_test(profile_id),
            on_done=lambda r: self._report(r, "探活", then_reload=True),
            busy_text=f"正在探活 {target}…",
        )

    def _delete(self) -> None:
        profile_id = self._selected_id()
        if not profile_id:
            self.toast("先选一个档案", level="warn")
            return
        if not QtWidgets.QMessageBox.question(
            self, "删除档案", f"删除模型档案「{profile_id}」？此操作不可撤销。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            lambda: self.client().model_delete(profile_id),
            on_done=lambda r: self._report(r, "删除", then_reload=True),
            busy_text="正在删除…",
        )

    def _new_template(self) -> None:
        self.editor.setPlainText(NEW_PROFILE_TEMPLATE)
        self.toast("已在编辑器里放入新档案模板，改完点「保存整份 JSON」", level="info")

    def _save(self) -> None:
        text = self.editor.toPlainText()
        if not QtWidgets.QMessageBox.question(
            self, "保存整份档案 JSON",
            "用编辑器里的内容**整体替换**服务端的模型档案？\n\n"
            "掩码处的原密钥会被服务端保留（不要把 *** 改成别的值，除非确实要换密钥）。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            lambda: self.client().model_save(text),
            on_done=lambda r: self._report(r, "保存", then_reload=True),
            busy_text="正在保存档案 JSON…",
        )

    def _report(self, result, action: str, *, then_reload: bool = False) -> None:  # noqa: ANN001
        if not result.ok:
            return
        payload = dict(result.value or {})
        ok = bool(payload.get("ok"))
        detail = payload.get("detail") or payload.get("error") or ""
        if ok:
            self.toast(f"{action}成功{('：' + one_line(detail, 80)) if detail else ''}", level="ok")
        else:
            self.toast(f"{action}失败：{one_line(detail, 200)}", level="err")
        if then_reload:
            self.load(force=True)
