"""人格页：**人格包（可切换）** + 三层只读信息 + 自动改动日志 + 四个明示动作。

## 两类动作要分清

* **切换人格包** —— 这是本页唯一会改变"它是谁"的动作，也是包化（2026-10）新增的。
  服务端是**热切换**（下一次回复就是新人格），所以不需要重启机器人。
  切过去之前会先确认：这是全局可见的效果，误点一下的代价比多点一次高。
* **改人设内容**（三层正文）—— **服务端保证改不了**：旧接口恒返回 410
  （`webui.py` 的 `/api/persona`）。所以本页没有任何编辑框，包里的
  `base.txt` / `forbidden.txt` 只能直接编辑文件。

剩下四个动作（都会花 token 或改变运行状态）：

    撤回最近自动改动（`/api/persona/undo`）
    立即反思一次（`/api/persona/reflect`，**会花 token**）
    生成测评素材（`/api/persona/eval` mode=artifacts，**会花 token**）
    跑一轮基线分（`/api/persona/eval` mode=round，**会花 token**）

## 本文件现在有两个类（2026-10-03）

`PersonaPage` 是完整版（**没有挂进导航**）。挂进导航的是 `PersonaEvalPage`
（标题「人格评估台」）：它复用完整版的**三层查看 / 评估台 / 改动日志**，但把四个动作按钮藏起来
—— 那些动作与「人格评估与自动修订」参数在同一个「功能」目录下的子菜单里，
两个入口各存一份行为迟早会不一致。

⚠ **标题不能叫「人格」**：那个标题归「功能」目录下的人格子菜单（`MainWindow.goto_page`
按标题跳转，同名会二义）。这也是本页当初从导航撤下的原因（见 `desktop/README.md` 4.1）。
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
    description_label,
    esc,
    fit_table,
    label,
    plain_text_edit,
    primary_button,
    table,
    toolbar,
)


class PersonaPage(Page):
    title = "人格"
    subtitle = "人格包（可切换）、三层结构（只读）、评估台与自动改动日志"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        self._persona: dict[str, Any] = {}
        self._packs: list[dict[str, Any]] = []
        self._build()

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)

        bar = toolbar("四个动作会花 token 或改变运行状态，执行前都会确认")
        self.btn_refresh = busy_button("刷新", lambda: self.load(force=True))
        self.btn_undo = busy_button(
            "回滚最近一次修订", self._undo,
            tip="用修订前的备份恢复三层人设，并记入改动日志",
        )
        self.btn_reflect = danger_button("立即评估并修订（花 token）", self._reflect)
        self.btn_artifacts = danger_button(
            "生成测评素材（花 token）", lambda: self._eval("artifacts")
        )
        self.btn_round = danger_button(
            "跑一轮基线分（花 token）", lambda: self._eval("round")
        )
        for widget in (self.btn_refresh, self.btn_undo):
            bar.addWidget(widget)
        bar.addSpacing(theme.SPACE["lg"])
        for widget in (self.btn_reflect, self.btn_artifacts, self.btn_round):
            bar.addWidget(widget)
        outer.addLayout(bar)
        # 动作按钮收进 dict：`PersonaEvalPage` 只留「刷新」，其余藏起来
        # （那些动作的设置在功能子菜单里，别有两个各存一份行为的入口）。
        self._action_buttons = {
            "刷新": self.btn_refresh,
            "回滚最近一次修订": self.btn_undo,
            "立即评估并修订（花 token）": self.btn_reflect,
            "生成测评素材（花 token）": self.btn_artifacts,
            "跑一轮基线分（花 token）": self.btn_round,
        }

        self.hint = label("正在读取人格信息…")
        outer.addWidget(self.hint)

        # ---- 人格包（唯一会改变"它是谁"的动作）----
        packs_box = QtWidgets.QGroupBox("人格包")
        packs_layout = QtWidgets.QVBoxLayout(packs_box)
        packs_layout.addWidget(
            description_label(
                "一个包 = 一个角色需要的全部文件（`persona/packs/<id>/`）。"
                "切换**立即生效**，不用重启；每个包的自我学习成果按包分开存，切回来还在。",
                wrap=True,
            )
        )
        pick_row = QtWidgets.QHBoxLayout()
        self.pack_selector = QtWidgets.QComboBox()
        self.pack_selector.setMinimumWidth(260)
        pick_row.addWidget(self.pack_selector, 1)
        self.btn_switch = primary_button("切到这个包", self._switch)
        pick_row.addWidget(self.btn_switch)
        packs_layout.addLayout(pick_row)
        self.pack_info = label("")
        self.pack_info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.pack_info.setWordWrap(True)
        packs_layout.addWidget(self.pack_info)
        outer.addWidget(packs_box)

        self.tabs = QtWidgets.QTabWidget()
        outer.addWidget(self.tabs, 1)

        # ---- 三层
        layers = QtWidgets.QWidget()
        layers_layout = QtWidgets.QVBoxLayout(layers)
        layers_layout.addWidget(
            description_label(
                "三层正文都可以被**评估驱动的修订**改写（每次先整份备份，可整版回滚）；"
                "也可以直接编辑文件：`base.txt`（它是谁）/ `forbidden.txt`（铁律）/ `surface.txt`"
                "（表层，实际生效的是 `data/runtime/persona/<包>/surface.txt`）。"
                "底层与禁止事项的运行时改动写在包运行目录的 `base.txt` / `forbidden.txt` 覆盖文件里。",
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
        self.eval_summary = label("")
        self.eval_summary.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.eval_summary.setWordWrap(True)
        eval_layout.addWidget(self.eval_summary)
        self.vector_table = table(["轴", "当前", "基线", "状态"])
        self.vector_table.setMaximumHeight(150)
        eval_layout.addWidget(self.vector_table)
        self.relation_table = table(["对象", "play", "亲密度", "能开的玩笑"])
        self.relation_table.setMaximumHeight(150)
        eval_layout.addWidget(self.relation_table)
        # 解释链（#0011）：**只读**。给操作者看"最近为什么这样"，
        # 与注入给模型的 `explain(uid, conv)` 是两条路径（那条严格按 uid+conv 过滤）。
        self.vector_explain = description_label("")
        self.vector_explain.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.vector_explain.setWordWrap(True)
        eval_layout.addWidget(self.vector_explain)
        eval_layout.addWidget(label("诊断项（旧特质，不参与否决）", wrap=True))
        self.eval_table = table(["特质", "极性", "分数", "状态"])
        eval_layout.addWidget(self.eval_table, 1)
        self.tabs.addTab(evaluation, "评估台")

        # ---- 像人样本库（v2）
        samples = QtWidgets.QWidget()
        samples_layout = QtWidgets.QVBoxLayout(samples)
        samples_layout.addWidget(
            description_label(
                "像人判别的对照样本：一半来自真实聊天候选，一半由模型生成。"
                "勾选后点「保存勾选」，评估只读 `likeness_samples.jsonl`；"
                "候选存在 `likeness_candidates.jsonl`，不会动人格文件。",
                wrap=True,
            )
        )
        self.sample_hint = label("正在读取样本…", wrap=True)
        samples_layout.addWidget(self.sample_hint)
        sample_bar = QtWidgets.QHBoxLayout()
        self.btn_sample_refresh = busy_button("刷新样本", self._load_samples)
        self.btn_sample_generate = danger_button(
            "生成 10 条模型样本", self._generate_samples)
        self.btn_sample_save = primary_button("保存勾选", self._save_samples)
        sample_bar.addWidget(self.btn_sample_refresh)
        sample_bar.addWidget(self.btn_sample_generate)
        sample_bar.addWidget(self.btn_sample_save)
        sample_bar.addStretch(1)
        samples_layout.addLayout(sample_bar)
        self.sample_table = table(["选", "来源", "上下文", "真人回复"], stretch_last=True)
        self.sample_table.setColumnWidth(0, 44)
        samples_layout.addWidget(self.sample_table, 1)
        self.tabs.addTab(samples, "样本库")

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
        evaluation = dict(persona.get("eval") or {})

        self.hint.setText(
            f"当前人格包 {esc(stats.get('pack_name') or '（无）')} · "
            f"底层 {esc(stats.get('base_chars', 0))} 字 · 表层 {esc(stats.get('surface_chars', 0))} 字 · "
            f"禁止事项 {esc(stats.get('forbidden', 0))} 条 · "
            f"待修特质 {esc(len(evaluation.get('targets') or []))} 个 · "
            f"历史修订 {esc(evaluation.get('revisions', 0))} 次"
        )

        self._fill_packs(dict(persona.get("pack") or {}), list(persona.get("packs") or []))

        layers = dict(persona.get("layers") or {})
        forbidden = list(persona.get("forbidden") or [])
        # 文件列报**实际路径**（服务端给了就照它显示）：写死文件名在包化之后会骗人 ——
        # "我正在编辑哪个包"恰恰是这个页面最该回答的问题。
        base_file = str(stats.get("base_file") or "")
        forbidden_file = str(stats.get("forbidden_file") or "")
        surface_file = str(stats.get("surface_file") or "")
        rows = [
            ("底层人设", base_file, len(str(layers.get("base") or "")), "只允许人工修改"),
            ("表层人设", surface_file, len(str(layers.get("surface") or "")), "评估修订会写这里，可回滚"),
            ("禁止事项", forbidden_file, len(forbidden), "评估修订可写（备份可回滚）"),
        ]
        self.layer_table.setRowCount(len(rows))
        for row, (name, filename, size, note) in enumerate(rows):
            for column, text in enumerate((name, filename, str(size), note)):
                self.layer_table.setItem(row, column, QtWidgets.QTableWidgetItem(str(text)))
        fit_table(self.layer_table)

        current = self.layer_selector.currentText()
        items = ["底层人设", "表层人设", "禁止事项"]
        if [self.layer_selector.itemText(i) for i in range(self.layer_selector.count())] != items:
            self.layer_selector.blockSignals(True)
            self.layer_selector.clear()
            self.layer_selector.addItems(items)
            self.layer_selector.setCurrentText(current if current in items else items[0])
            self.layer_selector.blockSignals(False)
        self._show_layer(self.layer_selector.currentText())

        self._fill_eval(evaluation, persona)
        self._load_samples()
        self._fill_log(list(persona.get("changelog") or []))
        self.toast("人格信息已载入", level="ok")
        self.refreshed.emit()

    def _fill_packs(self, cur: dict[str, Any], items: list[dict[str, Any]]) -> None:
        """填「人格包」那一块：下拉可选 + 当前包的信息。"""
        self._packs = items
        active = str(cur.get("id") or "")
        labels = []
        for item in items:
            pid = str(item.get("id") or "")
            name = str(item.get("name") or pid)
            mark = "（当前）" if pid == active else ""
            labels.append(f"{name} — {pid}{mark}")
        if [self.pack_selector.itemText(i) for i in range(self.pack_selector.count())] != labels:
            self.pack_selector.blockSignals(True)
            self.pack_selector.clear()
            self.pack_selector.addItems(labels)
            self.pack_selector.blockSignals(False)
        # 默认选中当前那个：不选中的话"切到这个包"会被误当成"切到列表第一项"。
        for index, item in enumerate(items):
            if str(item.get("id") or "") == active:
                self.pack_selector.setCurrentIndex(index)
                break
        self.btn_switch.setEnabled(bool(items) and self.pack_selector.count() > 1)

        if not active:
            self.pack_info.setText(
                badge("没有可用的人格包", level="warn")
                + " 把人格文件放进 <code>persona/packs/&lt;id&gt;/</code>"
                  "（可照抄 <code>persona/_TEMPLATE/</code>）"
            )
            return
        desc = str(cur.get("description") or "")
        bits = [
            f"<b>{esc(cur.get('name') or active)}</b>（id={esc(active)}）",
            f"包目录：<code>{esc(cur.get('dir') or '')}</code>",
            f"运行数据：<code>{esc(cur.get('stage') or '')}</code>",
        ]
        if desc:
            bits.append(esc(desc))
        if cur.get("bot_name"):
            bits.append(f"角色名：{esc(cur['bot_name'])}"
                        + (f"（别名：{esc('、'.join(cur.get('aliases') or []))}）" if cur.get("aliases") else ""))
        if cur.get("wake_words"):
            bits.append(f"唤醒词：{esc(cur['wake_words'])}")
        if cur.get("manifest_error"):
            bits.append(f"⚠ {esc(cur['manifest_error'])}")
        if cur.get("frozen"):
            bits.append(f"⚠ 已被 freeze({esc(cur['frozen'])}) 钉死：切换不会改变实际加载的包")
        self.pack_info.setText("<br>".join(bits))

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

    def _fill_eval(self, evaluation: dict[str, Any], persona: dict[str, Any]) -> None:
        enabled = bool(evaluation.get("enabled"))
        traits = list(evaluation.get("traits") or [])
        targets = {str(x.get("slug")) for x in (evaluation.get("targets") or [])}
        extra = ""
        if enabled:
            extra = (
                f" · 自动修订 {'开' if evaluation.get('iter_enabled') else '关'}"
                f" · 每天 {int(evaluation.get('daily_hour') or 3):02d}:00"
                f" · 待修 {len(targets)} 个 · 历史修订 {evaluation.get('revisions', 0)} 次"
            )
        self.eval_hint.setText(
            (badge("评估台已启用", level="ok") if enabled
             else badge("评估台已关闭（参数 → 人设评估 → eval_enabled）", level="warn")) + esc(extra)
        )
        self.eval_hint.setTextFormat(QtCore.Qt.TextFormat.RichText)

        # ---- v2 摘要：调度 + 模仿度 + 边界 + 示例 ----
        lk = dict(evaluation.get("likeness") or {})
        bd = dict(evaluation.get("boundary") or {})
        ex = dict(persona.get("examples") or {})
        school = (
            f"小评估：每天 {'开' if evaluation.get('small_enabled') else '关'}"
            f"（边界 + {evaluation.get('small_samples', 0)} 条像人） · "
            f"大评估：每 {evaluation.get('big_every_days', 3)} 天"
            f"（{evaluation.get('big_samples', 0)} 条像人"
            f"{' + 特质分' if evaluation.get('traits_on_big') else ''}） · "
            f"上次 {evaluation.get('last_big') or '还没跑'} / 下次 {evaluation.get('next_big') or ''}"
        )
        win = dict(evaluation.get("window") or {})
        score_line = (
            f"<b>语料</b> {win.get('scored', 0)} 段 / 均分 {win.get('avg', '-')} / "
            f"低分 {len(win.get('low') or [])} 段 · "
            f"<b>口语偏好 L</b> {lk.get('L', '-')}"
            f"（n={lk.get('n', 0)}，对照偏好 {round(float(lk.get('acc') or 0) * 100, 1)}%） · "
            f"<b>边界</b> 检查 {bd.get('checked', 0)} 条，越界 {len(bd.get('violations') or [])}，"
            f"拿不准 {len(bd.get('borderline') or [])} · "
            f"<b>示例库</b> {ex.get('total', 0)} 条（good {ex.get('good', 0)} / bad {ex.get('bad', 0)}）"
        )
        self.eval_summary.setText(esc(school) + "<br>" + score_line)

        vec = dict(persona.get("vector") or {})
        axes = list(vec.get("axes") or [])
        values = dict(vec.get("values") or {})
        self.vector_table.setRowCount(len(axes))
        for row_index, axis in enumerate(axes):
            axis_id = str(axis.get("id") or "")
            value = values.get(axis_id)
            base = axis.get("baseline")
            diff = ((float(value) - float(base))
                    if isinstance(value, (int, float)) and isinstance(base, (int, float)) else 0.0)
            state = "正常" if abs(diff) < 0.2 else ("偏高" if diff > 0 else "偏低")
            cells = [
                str(axis.get("name") or axis_id),
                f"{float(value):.2f}" if isinstance(value, (int, float)) else "-",
                f"{float(base):.2f}" if isinstance(base, (int, float)) else "-",
                state,
            ]
            for column, text in enumerate(cells):
                self.vector_table.setItem(row_index, column, QtWidgets.QTableWidgetItem(text))
        fit_table(self.vector_table)

        parts = []
        samples = list(vec.get("effective_samples") or [])
        if samples and axes:
            first = samples[0]
            who = str(first.get("who") or first.get("uid") or "")
            eff = dict(first.get("values") or {})
            cells = []
            for axis in axes:
                axis_id = str(axis.get("id") or "")
                if axis_id in eff:
                    cells.append(f"{axis.get('name') or axis_id}={float(eff[axis_id]):.2f}")
            if cells:
                parts.append(f"有效值（{who}{'·主人' if first.get('is_master') else ''}）：" + " / ".join(cells))
        recent = list(vec.get("recent") or [])
        if recent:
            lines = []
            for row in recent[:5]:
                kind = str(row.get("kind") or row.get("source") or "?")
                applied = dict(row.get("applied") or {})
                text = "，".join(f"{k}{v:+.2f}" for k, v in applied.items()) or "无变化"
                stamp = str(row.get("at") or "")[-8:]
                flag = "（已过期）" if row.get("stale") else ""
                lines.append(f"{stamp} {kind}：{text}{flag}")
            parts.append("最近的状态变化：" + chr(10) + chr(10).join(lines))
        self.vector_explain.setText(chr(10).join(parts) if parts else "最近没有状态变化。")

        rel = dict(persona.get("relation") or {})
        items = list(rel.get("items") or [])
        self.relation_table.setRowCount(len(items))
        for row_index, item in enumerate(items):
            who = str(item.get("who") or item.get("uid") or "")
            if item.get("master"):
                who += "（主人）"
            cells = [
                who,
                f"{float(item.get('play') or 0):.2f}",
                f"{float(item.get('intimacy') or 0):.2f}",
                "、".join(str(x) for x in (item.get("tease_topics") or [])) or "-",
            ]
            for column, text in enumerate(cells):
                self.relation_table.setItem(row_index, column, QtWidgets.QTableWidgetItem(text))
        fit_table(self.relation_table)

        polarity = {"suppress": "压制", "elicit": "保持", "convention": "约定"}
        self.eval_table.setRowCount(len(traits))
        for row, trait in enumerate(traits):
            score = trait.get("score")
            slug = str(trait.get("slug") or "")
            status = "待修" if slug in targets else ("未评" if score is None else "")
            cells = [
                str(trait.get("trait") or trait.get("slug") or ""),
                polarity.get(str(trait.get("polarity")), str(trait.get("polarity") or "")),
                "-" if score is None else str(score),
                status,
            ]
            for column, text in enumerate(cells):
                self.eval_table.setItem(row, column, QtWidgets.QTableWidgetItem(text))
        fit_table(self.eval_table)

    # ------------------------------------------------------------ 样本库
    def _load_samples(self) -> None:
        self.run_task(
            lambda: self.client().persona_likeness_candidates(),
            on_done=self._fill_samples,
            busy_text="正在读取样本…",
        )

    def _fill_samples(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.sample_hint.setText(
                badge("读取样本失败", level="warn")
                + " 服务端可能还没有 likeness 接口（旧版本或桩服务）。"
            )
            self.sample_hint.setTextFormat(QtCore.Qt.TextFormat.RichText)
            return
        payload = dict(result.value or {})
        items = list(payload.get("items") or [])
        selected = {str(x) for x in (payload.get("selected") or [])}
        self.sample_hint.setText(
            f"候选 {len(items)} 条，已选 {len(selected)} 条"
            "（来源列：聊天 / 模型；勾选后保存）"
        )
        self.sample_table.setRowCount(len(items))
        for row_index, item in enumerate(items):
            sid = str(item.get("id") or "")
            check = QtWidgets.QTableWidgetItem("")
            check.setFlags(QtCore.Qt.ItemFlag.ItemIsUserCheckable
                           | QtCore.Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(QtCore.Qt.CheckState.Checked if sid in selected
                                else QtCore.Qt.CheckState.Unchecked)
            check.setData(QtCore.Qt.ItemDataRole.UserRole, sid)
            self.sample_table.setItem(row_index, 0, check)
            source = "模型" if item.get("source") == "generated" else "聊天"
            ctx = one_line(" / ".join(str(x) for x in (item.get("ctx") or [])), 90)
            for column, text in enumerate((source, ctx, str(item.get("human") or ""))):
                self.sample_table.setItem(
                    row_index, column + 1, QtWidgets.QTableWidgetItem(text))
        fit_table(self.sample_table)

    def _checked_sample_ids(self) -> list[str]:
        ids: list[str] = []
        for row_index in range(self.sample_table.rowCount()):
            item = self.sample_table.item(row_index, 0)
            if item is None or item.checkState() != QtCore.Qt.CheckState.Checked:
                continue
            sid = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "")
            if sid:
                ids.append(sid)
        return ids

    def _save_samples(self) -> None:
        ids = self._checked_sample_ids()
        if not ids:
            self.toast("先勾选几条样本", level="warn")
            return

        def done(result) -> None:  # noqa: ANN001
            self._report(result, "保存样本", detail_key="saved")

        self.run_task(
            lambda: self.client().persona_likeness_select(ids),
            on_done=done,
            busy_text=f"正在保存 {len(ids)} 条样本…",
        )

    def _generate_samples(self) -> None:
        if not self._confirm_cost("生成像人样本", "让模型补 10 条「普通群友」样本到候选。"):
            return

        def done(result) -> None:  # noqa: ANN001
            self._report(result, "生成样本", detail_key="generated")

        self.run_task(
            lambda: self.client().persona_likeness_generate(10),
            on_done=done,
            busy_text="正在生成样本…",
        )

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
        fit_table(self.log_table)

    # ------------------------------------------------------------ 动作
    def _selected_pack(self) -> str:
        """下拉里选中的包 id（从显示文案里取回 id，而不是按下标猜）。"""
        text = self.pack_selector.currentText()
        for item in self._packs:
            pid = str(item.get("id") or "")
            if text.endswith(f"— {pid}（当前）") or text.endswith(f"— {pid}"):
                return pid
        return ""

    def _switch(self) -> None:
        """切换人格包。**唯一的确认文案在这里** —— 说清"立即生效"这件事。"""
        pack_id = self._selected_pack()
        if not pack_id:
            self.toast("先在下拉里选一个包", level="warn")
            return
        current = str((self._persona.get("stats") or {}).get("pack_id") or "")
        if pack_id == current:
            self.toast(f"现在用的就是「{pack_id}」，没有切换", level="warn")
            return
        if QtWidgets.QMessageBox.question(
            self, "切换人格包",
            f"从「{current or '（无）'}」切到「{pack_id}」？\n\n"
            "· **立即生效**：下一句回复就是新人格，不用重启机器人。\n"
            "· 这个包的自我学习成果会留在它自己的目录里，切回来还在。\n"
            "· 角色名 / 唤醒词会跟着包里声明的元数据一起换（有声明的话）。\n\n"
            "群里所有人都能看出它换人了 —— 确认要切吗？",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) != QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            lambda: self.client().persona_switch(pack_id),
            on_done=lambda r: self._report(r, f"切换到 {pack_id}", detail_key="seed"),
            busy_text=f"正在切到 {pack_id}…",
        )

    def _undo(self) -> None:
        if not QtWidgets.QMessageBox.question(
            self, "回滚评估修订",
            "回滚最近一次**评估驱动**的人设修订？\n\n"
            "会用修订前的备份恢复底层 / 禁止事项 / 表层三层。",
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok:
            return
        self.run_task(
            self.client().persona_undo,
            on_done=lambda r: self._report(r, "回滚"),
            busy_text="正在回滚…",
        )

    def _reflect(self) -> None:
        if not self._confirm_cost("评估并修订", "现在跑一轮评估，并按结果改写三层人设（会先备份）。"):
            return
        self.run_task(
            self.client().persona_reflect,
            on_done=lambda r: self._report(r, "评估并修订", detail_key="why"),
            busy_text="正在评估并修订（可能要几分钟）…",
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


class PersonaEvalPage(PersonaPage):
    """「人格评估台」：人格包切换 + 三层只读 + 评估表 + 自动改动日志（**只读**）。

    与完整版的三点差别，都是刻意的：

    1. **不重复放花 token 的动作**（回滚 / 评估并修订 / 测评素材 / 跑分）——
       它们与「功能」目录下「人格评估与自动修订」子菜单里那几个开关是同一件事，
       两个入口各存一份行为迟早对不上。要跑就在那边跑（子菜单里有对应参数）。
    2. **标题不同名**（见模块文档）。
    3. 保留「切到这个包」：那是"它是谁"的唯一开关，人气最高，不该埋在参数里。
    """

    title = "人格评估台"
    subtitle = "人格包切换、三层正文（只读）、特质评分与评估修订记录"

    def __init__(self, manager, parent=None) -> None:  # noqa: ANN001
        super().__init__(manager, parent)
        for name in ("回滚最近一次修订", "立即评估并修订（花 token）",
                     "生成测评素材（花 token）", "跑一轮基线分（花 token）"):
            button = self._action_buttons.get(name)
            if button is not None:
                button.setVisible(False)
        # 这一条**不能写进 `self.hint`**：那是加载状态行，`_apply` 每次都会覆盖它。
        note = label(
            "本页只读：回滚 / 评估并修订 / 生成素材 / 跑基线分请用左侧「功能」目录下的"
            "「人格评估与自动修订」子菜单（那里的开关与这些动作是同一件事）。",
            level="info",
            wrap=True,
        )
        self.layout().insertWidget(2, note)   # 工具栏、hint 之后

