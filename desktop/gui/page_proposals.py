"""人格修订提案：看清单、审完整改动、采纳或拒绝。

## 为什么单开一页（#0006）

提案能改 `forbidden` / `surface` / `vector`。列表只给 6 项、每项 60 字的摘要，
**不足以"确认整批修改"** —— 所以这一页的重点是详情面板：把每个文件的
`before` 与 `after` 摆出来，人对着看。

## 三个容易踩的地方

1. **接口恒返回 200**，成败只能看 `ok`；失败原因在 `why` 里，直接展示；
2. **过期由后端判**（`commit_proposal` 里查 TTL 并顺手标 `expired`），
   这一页**不自己算时间** —— 否则指令 / Web / 桌面端三处行为会不一致；
3. 采纳失败时**要刷新列表**：后端可能已经把状态改成 `expired` 了。
"""

from __future__ import annotations

from typing import Any

from PySide6 import QtWidgets

from . import theme
from .base import Page
from ..core.apiclient import BotApiError  # noqa: F401  （异常类型在提示里用到）
from .widgets import (
    badge,
    busy_button,
    danger_button,
    esc,
    fit_table,
    ghost_button,
    label,
    table,
)


class ProposalsPage(Page):
    """待确认的修订提案。"""

    title = "修订提案"
    subtitle = "评估给出的待确认改动：看清楚再采纳"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._revs: list[str] = []
        self._current = ""

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(theme.SPACE["lg"], theme.SPACE["md"],
                                 theme.SPACE["lg"], theme.SPACE["md"])
        outer.setSpacing(theme.SPACE["md"])

        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(theme.SPACE["sm"])
        self.btn_refresh = ghost_button("刷新", self._reload)
        self.btn_commit = busy_button("采纳", self._commit)
        self.btn_reject = danger_button("拒绝", self._reject,
                                       tip="拒绝后这条提案不会再出现在列表里")
        self.btn_commit.setEnabled(False)
        self.btn_reject.setEnabled(False)
        bar.addWidget(self.btn_refresh)
        bar.addWidget(self.btn_commit)
        bar.addWidget(self.btn_reject)
        bar.addStretch(1)
        self.hint = label("正在读取提案…")
        bar.addWidget(self.hint)
        outer.addLayout(bar)

        self.table = table(["编号", "时间", "改了什么", "条数", "校验"])
        self.table.itemSelectionChanged.connect(self._on_pick)
        outer.addWidget(self.table, 2)

        self.detail = QtWidgets.QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText("选中一条提案，这里显示完整的 before / after")
        outer.addWidget(self.detail, 3)

    # ------------------------------------------------------------ 数据
    def _load(self) -> None:
        self.run_task(self.client().persona_proposals, on_done=self._apply_list,
                      busy_text="正在读取提案…")

    def _reload(self) -> None:
        self._current = ""
        self.table.setRowCount(0)
        self.detail.clear()
        self.btn_commit.setEnabled(False)
        self.btn_reject.setEnabled(False)
        self.load(force=True)

    def _apply_list(self, result: Any) -> None:  # noqa: ANN001
        if not result.ok:
            self.hint.setText(badge("读取失败", level="err"))
            self.show_error(result.error)
            return
        items = list((result.value or {}).get("items") or [])
        self._revs = [str(x.get("rev") or "") for x in items]
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            rev = str(item.get("rev") or "")
            what = "；".join(str(x) for x in (item.get("items") or [])[:2])
            cells = [rev, str(item.get("at") or "")[:19], what,
                     str(item.get("ops") or 0),
                     "通过" if item.get("passed") else "未通过"]
            for column, text in enumerate(cells):
                cell = QtWidgets.QTableWidgetItem(text)
                cell.setToolTip(text)
                self.table.setItem(row, column, cell)
        fit_table(self.table)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.hint.setText(
            f"<b>{esc(len(items))}</b> 条待确认" if items
            else "没有待确认的提案（评估每天定时跑，也可以在「人设」页手动触发）")
        self.refreshed.emit()

    def _on_pick(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        rev_item = self.table.item(rows[0].row(), 0)
        self._current = rev_item.text() if rev_item else ""
        has = bool(self._current)
        self.btn_commit.setEnabled(has)
        self.btn_reject.setEnabled(has)
        if not has:
            return
        rev = self._current
        self.run_task(lambda: self.client().persona_proposal_detail(rev),
                      on_done=self._apply_detail, busy_text="正在读取提案详情…")

    def _apply_detail(self, result: Any) -> None:  # noqa: ANN001
        if not result.ok:
            self.detail.setPlainText(f"读不到详情：{result.message}")
            return
        data = dict(result.value or {})
        lines = [
            f"提案 {data.get('rev')}　状态 {data.get('status')}　"
            f"已存在 {data.get('age_days')} 天" + ("　**已过期**" if data.get("expired") else ""),
            "",
        ]
        # #0015 M5：把「这条提案依据什么」摊开。`graded=False` 要如实显示 ——
        # 没有评分就说没有，不能让人误以为它通过了筛查。
        ver = dict(data.get("verification") or {})
        ev = dict(ver.get("eval") or {})
        if ev:
            graded = "已评分" if ev.get("graded") else "未评分（只登记依据）"
            lines.append(f"评估依据：{ev.get('cases', 0)} 条案例　目标类型 {ev.get('target_kind')}　{graded}")
            if ev.get("why"):
                lines.append(f"　{ev.get('why')}")
            lines.append("")
        for op in data.get("ops") or []:
            lines.append(f"[{op.get('op')}] {op.get('target')}：{op.get('why') or ''}")
        lines.append("")
        for row in data.get("files") or []:
            lines.append(f"===== {row.get('name')} =====")
            lines.append("--- 改动前 ---")
            lines.append(str(row.get("before") or ""))
            lines.append("--- 改动后 ---")
            lines.append(str(row.get("after") or ""))
            if row.get("truncated"):
                lines.append("（内容过长已截断）")
            lines.append("")
        self.detail.setPlainText("\n".join(lines))
        if data.get("expired"):
            # 过期的不让点采纳：后端也会拒，但先把按钮关掉，省一次往返
            self.btn_commit.setEnabled(False)

    # ------------------------------------------------------------ 动作
    def _commit(self) -> None:
        self._act("commit", "确认采纳这条提案？\n\n改动会**立刻**写进线上人格资产，"
                            "采纳前请确认详情里的 before / after。")

    def _reject(self) -> None:
        self._act("reject", "拒绝这条提案？拒绝后它不会再出现在列表里。")

    def _act(self, action: str, question: str) -> None:
        if not self._current:
            return
        if QtWidgets.QMessageBox.question(self, "确认", question) != QtWidgets.QMessageBox.Yes:
            return
        rev = self._current
        self.run_task(lambda: self.client().persona_proposal(rev, action),
                      on_done=self._after_act, busy_text="正在提交…")

    def _after_act(self, result: Any) -> None:  # noqa: ANN001
        if not result.ok:
            self.show_error(result.error)
            return
        data = dict(result.value or {})
        if not data.get("ok"):
            # **200 也可能失败**：原因在 why；而且后端可能已经把它标成 expired，
            # 所以失败也要刷新列表，别让界面停在旧状态上。
            self.show_error(BotApiError(str(data.get("why") or "失败")))
        else:
            self.toast("提案已处理", level="ok")
        self._reload()
