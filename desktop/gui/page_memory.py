"""记忆页：人物画像 / 事实 / 群事件，分类过滤 + 本地搜索 + 新增/保护/删除。

**搜索是本地过滤，这一条要说清**：服务端目前没有搜索接口
（`memory.search()` 只存在于 Python 层，`webui.py` 没暴露），
而 `/ai/api/state` 本来就把全量 `facts/events/profile` 一起发过来了。
所以第一阶段在客户端过滤这三份已取到的数据 —— 不新增任何服务端接口，
等记忆量真的大到拉取都卡，再按方案 4.3 去加只读搜索/分页端点。

删除与保护都先确认；服务端的 `locked` 字段对应"受保护"。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtCore, QtWidgets

from ..core.util import one_line
from . import theme
from .base import Page, StateSlice
from .widgets import (
    badge,
    busy_button,
    danger_button,
    esc,
    fit_table,
    label,
    primary_button,
    table,
    toolbar,
)

CATEGORIES = ("人物画像", "事实", "群事件")


class MemoryPage(Page):
    title = "记忆库"
    subtitle = "画像 / 事实 / 群事件（服务端无搜索接口，这里是本地过滤）"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._memory: dict[str, Any] = {}
        self._rows: list[dict[str, Any]] = []
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = toolbar("条目按类别与搜索词在本地过滤；写入与删除都会先确认")
        self.btn_refresh = busy_button("刷新", lambda: self.load(force=True))
        self.btn_add = primary_button("手工记一条事实", self._add_fact)
        self.btn_protect = busy_button(
            "保护 / 解除", self._toggle_protect,
            tip="受保护的条目不会被自动整理删除",
        )
        self.btn_delete = danger_button("删除", self._delete, tip="删除选中的条目（会确认）")
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_add)
        bar.addWidget(self.btn_protect)
        bar.addSpacing(theme.SPACE["lg"])
        bar.addWidget(self.btn_delete)
        outer.addLayout(bar)

        filter_row = QtWidgets.QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(theme.SPACE["sm"])
        filter_row.addWidget(QtWidgets.QLabel("类别："))
        self.category = QtWidgets.QComboBox()
        self.category.addItems(list(CATEGORIES))
        self.category.currentTextChanged.connect(lambda _text: self._fill())
        filter_row.addWidget(self.category)
        filter_row.addWidget(QtWidgets.QLabel("搜索："))
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("按内容 / 主体 / 会话过滤（本地）")
        self.search.textChanged.connect(lambda _text: self._fill())
        filter_row.addWidget(self.search, 1)
        self.only_locked = QtWidgets.QCheckBox("只看受保护")
        self.only_locked.toggled.connect(lambda _state: self._fill())
        filter_row.addWidget(self.only_locked)
        outer.addLayout(filter_row)

        self.hint = label("正在读取记忆库…")
        outer.addWidget(self.hint)

        self.table = table(["#", "内容", "主体/来源", "重要度", "保护", "时间"])
        self.table.itemSelectionChanged.connect(self._update_buttons)
        outer.addWidget(self.table, 1)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(
            lambda: StateSlice(self.client().state()),
            on_done=self._apply,
            busy_text="正在读取记忆库…",
        )

    def _apply(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("读取失败", level="err"))
            return
        state: StateSlice = result.value
        self._memory = state.memory
        self._fill()
        self.toast("记忆库已载入", level="ok")
        self.refreshed.emit()

    def _fill(self) -> None:
        memory = self._memory
        stats = dict(memory.get("stats") or {})
        category = self.category.currentText()
        needle = self.search.text().strip().lower()
        only_locked = self.only_locked.isChecked()

        if category == "人物画像":
            all_profiles = list(memory.get("profile") or [])
            # 旧版写入路径留下过一批"只有 key、没有任何字段"的空画像（`updated_at` 也是空）。
            # 它们在服务端不会被自动删，但列出来只会让人以为"画像没写进去"。
            profiles = [x for x in all_profiles if not _is_empty_profile(x)]
            empty_profiles = len(all_profiles) - len(profiles)
            source = profiles
            self._rows = [self._profile_row(x) for x in source]
            headers = ["人物", "UID", "喜欢", "不喜欢", "习惯 / 备注", "更新时间"]
        elif category == "事实":
            source = list(memory.get("facts") or [])
            self._rows = [self._fact_row(x) for x in source]
            headers = ["#", "内容", "主体 / 来源", "重要度", "保护", "时间"]
        else:
            source = list(memory.get("events") or [])
            self._rows = [self._event_row(x) for x in source]
            headers = ["#", "内容", "会话", "重要度", "保护", "时间"]

        filtered = []
        for row in self._rows:
            if only_locked and not row.get("locked"):
                continue
            if needle and needle not in str(row.get("blob") or "").lower():
                continue
            filtered.append(row)

        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setRowCount(len(filtered))
        for index, row in enumerate(filtered):
            for column, text in enumerate(row["cells"]):
                self.table.setItem(index, column, QtWidgets.QTableWidgetItem(str(text)))
        fit_table(self.table)

        if category == "人物画像":
            extra_hint = f"（另有 {esc(empty_profiles)} 条空画像，旧数据，未列出）" if empty_profiles else ""
        else:
            extra_hint = ""
        people = stats.get("profile", stats.get("people", 0))
        self.hint.setText(
            f"事实 {esc(stats.get('facts', 0))} · 群事件 {esc(stats.get('events', 0))} · "
            f"人物 {esc(people)} · 受保护 {esc(stats.get('protected', 0))} ｜ "
            f"当前显示 {esc(len(filtered))} / {esc(len(self._rows))} 条"
            + ("（本地过滤）" if needle else "")
            + extra_hint
        )
        self._update_buttons()

    # ------------------------------------------------------------ 行构造
    @staticmethod
    def _fact_row(item: dict[str, Any]) -> dict[str, Any]:
        locked = bool(item.get("locked") or item.get("protected"))
        return {
            "id": int(item.get("id") or 0),
            "locked": locked,
            "kind": "fact",
            "blob": f"{item.get('text')} {item.get('subject')} {item.get('source')}",
            "cells": [
                item.get("id"),
                one_line(item.get("text"), 160),
                f"{item.get('subject') or '-'} / {item.get('source') or '-'}",
                _score(item.get("importance")),
                "已保护" if locked else "",
                _ts(item.get("ts")),
            ],
        }

    @staticmethod
    def _event_row(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(item.get("id") or 0),
            "locked": bool(item.get("locked") or item.get("protected")),
            "kind": "event",
            "blob": f"{item.get('text')} {item.get('conv')}",
            "cells": [
                item.get("id"),
                one_line(item.get("text"), 160),
                item.get("conv") or "-",
                _score(item.get("importance")),
                "",
                _ts(item.get("ts")),
            ],
        }

    @staticmethod
    def _profile_row(item: dict[str, Any]) -> dict[str, Any]:
        """人物画像一行。

        ⚠ 字段名跟着服务端走过一次重构：旧版给的是 `summary/facts/updated/uid`，
        现在 `settings` 那边给的是画像快照 `display/love/dislike/habit/note/updated_at`，
        uid 编码在 key 里（`uid:<QQ号>`）。还用旧字段的话，整张表每格都是 `-` ——
        看起来就像"画像一条都没写进去"（2026-10-24 用户报的就是这个）。
        """
        key = str(item.get("key") or "")
        uid = key[4:] if key.startswith("uid:") else ""
        display = str(item.get("display") or key)

        def _join(name: str) -> str:
            return "、".join(str(x) for x in (item.get(name) or []) if str(x).strip())

        love = _join("love")
        dislike = _join("dislike")
        habit = _join("habit")
        note = str(item.get("note") or "").strip()
        extra = "；".join(x for x in (habit, note) if x)
        return {
            "id": 0,
            "locked": True,  # 画像条目不支持单独保护/删除（服务端没有这个接口）
            "kind": "profile",
            "blob": " ".join(x for x in (key, display, love, dislike, habit, note) if x),
            "cells": [
                one_line(display, 80) or "-",
                uid or "-",
                one_line(love, 160) or "—",
                one_line(dislike, 160) or "—",
                one_line(extra, 200) or "—",
                item.get("updated_at") or "-",
            ],
        }

    # ------------------------------------------------------------ 选择
    def _selected(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        id_item = self.table.item(row, 0)
        if id_item is None:
            return None
        try:
            wanted = int(id_item.text())
        except ValueError:
            return None
        kind = "fact" if self.category.currentText() == "事实" else (
            "event" if self.category.currentText() == "群事件" else "profile"
        )
        for item in self._rows:
            if item["kind"] == kind and item["id"] == wanted and wanted:
                return item
        return None

    def _update_buttons(self) -> None:
        selected = self._selected()
        editable = bool(selected) and selected.get("kind") in ("fact", "event")
        self.btn_protect.setEnabled(editable)
        self.btn_delete.setEnabled(editable)
        if selected and selected.get("kind") == "profile":
            self.btn_protect.setToolTip("人物画像不支持单独保护（服务端没有该接口）")
            self.btn_delete.setToolTip("人物画像不支持单独删除（服务端没有该接口）")
        else:
            self.btn_protect.setToolTip("受保护的条目不会被自动整理删除")
            self.btn_delete.setToolTip("删除选中的条目（会确认）")

    # ------------------------------------------------------------ 动作
    def _add_fact(self) -> None:
        text, ok = QtWidgets.QInputDialog.getMultiLineText(
            self, "手工记一条事实", "内容（会写入长期记忆，并可能被后续对话引用）："
        )
        if not ok or not text.strip():
            return
        subject, ok2 = QtWidgets.QInputDialog.getText(self, "归属", "这条事实关于谁？", text="主人")
        if not ok2:
            return
        self.run_task(
            lambda: self.client().memory_add(text.strip(), subject=subject.strip() or "主人"),
            on_done=lambda r: self._after_write(r, "新增事实"),
            busy_text="正在写入记忆…",
        )

    def _toggle_protect(self) -> None:
        selected = self._selected()
        if not selected:
            self.toast("先选一条记忆", level="warn")
            return
        target = not bool(selected.get("locked"))
        action = "保护" if target else "解除保护"
        self.run_task(
            lambda: self.client().memory_protect(int(selected["id"]), target),
            on_done=lambda r: self._after_write(r, action),
            busy_text=f"正在{action}…",
        )

    def _delete(self) -> None:
        selected = self._selected()
        if not selected:
            self.toast("先选一条记忆", level="warn")
            return
        if not QtWidgets.QMessageBox.question(
            self, "删除记忆",
            f"删除这条记忆（id={selected['id']}）？\n\n{one_line(selected['blob'], 160)}\n\n此操作不可撤销。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            lambda: self.client().memory_delete(int(selected["id"])),
            on_done=lambda r: self._after_write(r, "删除"),
            busy_text="正在删除…",
        )

    def _after_write(self, result, action: str) -> None:  # noqa: ANN001
        if not result.ok:
            return
        payload = dict(result.value or {})
        if payload.get("ok"):
            self.toast(f"{action}成功", level="ok")
            self.load(force=True)
        else:
            self.toast(f"{action}未成功：{one_line(payload.get('error') or payload.get('detail') or '', 160)}", level="warn")


def _is_empty_profile(item: dict[str, Any]) -> bool:
    """只有 key、没有任何内容的旧画像（2026-10 之前的占位写入留下的）。"""
    if str(item.get("display") or "").strip():
        return False
    if str(item.get("note") or "").strip():
        return False
    return not any(item.get(field) for field in ("love", "dislike", "habit"))


def _score(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _ts(value: Any) -> str:
    """时间戳 → 可读时间。服务端给的是秒级浮点（`time.time()`）。"""
    try:
        import time as _time

        stamp = float(value)
    except (TypeError, ValueError):
        return str(value or "-")
    if stamp <= 0:
        return "-"
    return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(stamp))
