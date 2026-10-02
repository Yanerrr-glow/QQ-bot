"""主窗口：左侧导航 + 顶部连接/机器人状态 + 右侧内容区。

生命周期按方案 5.2.1 定死的四步走：

    1. discovery   只读 manifest
    2. register    import 插件、登记页面/操作/状态卡（不碰 Qt 界面）
    3. activate    主窗口与导航就绪后才 on_activate（插件这时才允许建页面）
    4. deactivate  退出时先停插件再关隧道，顺序反了就留幽灵任务

导航里"内置页面"永远在前，插件页面按 manifest 的 `navigation.order` 排在后面 ——
插件不能把总览挤出视野之外。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6 import QtCore, QtGui, QtWidgets

from .. import APP_NAME, APP_VERSION
from ..core import paths
from ..core.connection import ConnectionManager
from ..core.targets import MODE_LOCAL
from ..core.tunnel import STATE_RUNNING
from ..sdk.api import UiBridge
from ..sdk.loader import PluginHost
from .base import Page
from .page_connections import ConnectionsPage
from .page_imagepolicy import ImagePolicyPage
from .page_memory import MemoryPage
from .page_models import ModelsPage
from .page_overview import OverviewPage
from .page_params import ParamsPage
from .page_persona import PersonaPage
from .page_plugins import PluginsPage
from .page_stickers import StickersPage
from .page_terminal import TerminalPage
from .widgets import Async, LEVEL_COLOR, PALETTE, STYLESHEET, TaskResult, esc

logger = logging.getLogger("qqbot.desktop.gui.main")

BUILTIN_PAGES = (
    OverviewPage,
    # 终端紧跟在总览之后（用户要求）：它和总览一样是"一眼要看的常驻视图"，
    # 而不是连接设置页的附属面板。
    TerminalPage,
    ParamsPage,
    ModelsPage,
    PersonaPage,
    MemoryPage,
    ImagePolicyPage,
    StickersPage,
    ConnectionsPage,
    PluginsPage,
)

#: 导航树节点的载荷：`("page", Page 实例)` 或 `("plugin", page_id, title)`。
#: 页面栈索引不再和导航行号绑定 —— 树可以收起，行号本身已经不是个稳定标识了。
_NAV_ROLE = QtCore.Qt.ItemDataRole.UserRole


class QtUiBridge(UiBridge):
    """把宿主的 UI 能力交给插件（插件拿不到主窗口对象，只能调这几个方法）。"""

    def __init__(self, window: "MainWindow") -> None:
        self._window = window

    def toast(self, message: str, *, level: str = "info") -> None:
        self._window.notify(str(message), level=level)

    def confirm(self, title: str, message: str) -> bool:
        return QtWidgets.QMessageBox.question(
            self._window, str(title), str(message),
            QtWidgets.QMessageBox.StandardButton.Ok | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        ) == QtWidgets.QMessageBox.StandardButton.Ok

    def open_dialog(self, title: str, content: object) -> None:
        window = QtWidgets.QDialog(self._window)
        window.setWindowTitle(str(title))
        window.resize(720, 520)
        layout = QtWidgets.QVBoxLayout(window)
        text = QtWidgets.QPlainTextEdit()
        text.setReadOnly(True)
        if hasattr(content, "toPlainText"):
            text.setPlainText(content.toPlainText())  # type: ignore[attr-defined]
        else:
            text.setPlainText(str(content))
        layout.addWidget(text)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(window.reject)
        buttons.accepted.connect(window.accept)
        layout.addWidget(buttons)
        window.exec()

    def run_async(
        self,
        work: Callable[[], Any],
        *,
        on_done: Callable[[Any], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """后台跑一件阻塞事（插件页面取数、批量请求），完成后回 GUI 线程。

        插件手里没有 QThreadPool，这是它唯一"不卡界面"的正路。
        `work` 里**禁止碰控件** —— Qt 要求碰 widget 的代码只在 GUI 线程跑。
        """
        def _done(result: TaskResult) -> None:
            if result.ok:
                on_done(result.value)
            elif on_error is not None:
                on_error(result.message)
            else:
                self.toast(f"后台任务失败：{result.message}", level="error")

        self._window._async.run(work, on_done=_done)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, manager: ConnectionManager | None = None) -> None:
        super().__init__()
        paths.ensure_dirs()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1220, 800)
        self.setMinimumSize(940, 620)
        self.setStyleSheet(STYLESHEET)

        self.manager = manager or ConnectionManager(
            on_target_changed=self._on_target_changed,
            on_state_changed=self._on_state_changed,
        )
        self.pages: list[Page] = []
        self.plugin_pages: list[tuple[str, str, object]] = []  # (page_id, title, plugin_id)
        self.plugin_host: PluginHost | None = None
        #: 导航里「插件」那个树节点：插件页面挂在它下面（目录可收起）。
        self.plugins_item: QtWidgets.QTreeWidgetItem | None = None
        #: 隧道自动启动之后要往这个页里写一句结果（见 `auto_start_tunnel`）。
        self.terminal_page: TerminalPage | None = None
        self._async = Async(self)

        self._build_ui()
        self._load_plugins()

    # ------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        # 状态条要**先建**：导航 `setCurrentRow(0)` 会触发首次加载，而页面加载
        # 有进度提示，用的是状态条（踩过：建在后头就会 AttributeError）。
        self.status = self.statusBar()
        self.status.showMessage("就绪")

        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)
        outer.addWidget(self._build_top_bar())

        body = QtWidgets.QHBoxLayout()
        # 导航是一棵树（`QTreeWidget`）：插件页面挂在「插件」节点下，**目录可收起**。
        self.nav = QtWidgets.QTreeWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(190)
        self.nav.setHeaderHidden(True)
        self.nav.setRootIsDecorated(True)          # 「插件」行前面的展开/收起箭头
        self.nav.setIndentation(14)
        self.nav.setUniformRowHeights(True)
        self.nav.setExpandsOnDoubleClick(False)    # 展开只走箭头，双击别误触
        self.nav.currentItemChanged.connect(self._on_nav_changed)
        body.addWidget(self.nav)

        self.stack = QtWidgets.QStackedWidget()
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        # 内建页面全部装进页面栈，各占一个**顶层**导航节点。
        # **导航高亮与"哪一页被加载"必须一次定清楚**，踩过的两种写法都不行：
        #   * 只设 current 项：当前项本来就对时 Qt 不发 `currentItemChanged`，
        #     页面永远不 `_load()`（连接设置页目标列表空白、点启动隧道没反应）；
        #   * 只手动调一次 `_on_nav_changed()`：页面装好了但**导航没有高亮**。
        # 所以：先占位高亮，再**显式**把每页 `load()` 一遍（`Page.load()` 自身有 `_loaded` 去重）。
        for cls in BUILTIN_PAGES:
            page = cls(self.manager)
            self.pages.append(page)
            self.stack.addWidget(page)
            if isinstance(page, TerminalPage):
                self.terminal_page = page
            label = page.title + ("（只读）" if page.title == "人格" else "")
            item = QtWidgets.QTreeWidgetItem([label])
            item.setData(0, _NAV_ROLE, ("page", page))
            self.nav.addTopLevelItem(item)
            if isinstance(page, PluginsPage):
                self.plugins_item = item      # 插件页面稍后挂到这下面（可收起）
        self.stack.setCurrentIndex(0)
        self.nav.setCurrentItem(self.nav.topLevelItem(0))
        for page in self.pages:      # 先让每页都 load 一次：数据驱动型的页面首屏就是真数据
            page.load()

    def _build_top_bar(self) -> QtWidgets.QWidget:
        bar = QtWidgets.QFrame()
        bar.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(8, 6, 8, 6)

        layout.addWidget(QtWidgets.QLabel("目标："))
        self.target_combo = QtWidgets.QComboBox()
        self.target_combo.setMinimumWidth(220)
        self.target_combo.currentIndexChanged.connect(self._on_target_combo)
        layout.addWidget(self.target_combo)

        self.btn_probe = QtWidgets.QPushButton("测试连接")
        self.btn_probe.clicked.connect(self._probe)
        layout.addWidget(self.btn_probe)

        self.btn_refresh = QtWidgets.QPushButton("刷新当前页")
        self.btn_refresh.clicked.connect(self._refresh_current)
        layout.addWidget(self.btn_refresh)

        self.conn_label = QtWidgets.QLabel("-")
        self.conn_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        layout.addWidget(self.conn_label, 1)
        self._reload_targets()
        return bar

    # ------------------------------------------------------------ 目标
    def _reload_targets(self) -> None:
        combo = self.target_combo
        combo.blockSignals(True)
        combo.clear()
        for target in self.manager.targets():
            label = target.name + ("（隧道）" if target.is_tunnel else "")
            combo.addItem(label, target.id)
        index = combo.findData(self.manager.store.active_id)
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._on_state_changed(self.manager.state)

    def _on_target_combo(self, _index: int) -> None:
        target_id = str(self.target_combo.currentData() or "")
        if not target_id or target_id == self.manager.store.active_id:
            return
        self.manager.switch_target(target_id)
        self._refresh_current()

    def _on_target_changed(self, target_id: str) -> None:
        """切目标：所有页面数据作废，插件收到 on_target_changed 语义的清理。"""
        for page in self.pages:
            page.invalidate()
        for page_id, _title, plugin_id in self.plugin_pages:
            host = self.plugin_host
            if host is None:
                continue
            loaded = host.loaded.get(str(plugin_id))
            if loaded is not None:
                loaded.api.token.cancel("切换了目标")
                loaded.api.token = type(loaded.api.token)()
        index = self.target_combo.findData(target_id)
        if index >= 0 and index != self.target_combo.currentIndex():
            self.target_combo.blockSignals(True)
            self.target_combo.setCurrentIndex(index)
            self.target_combo.blockSignals(False)
        self.notify("已切换目标，页面数据将重新加载", level="info")

    def _on_state_changed(self, state) -> None:  # noqa: ANN001
        online = state.api_ok
        color = LEVEL_COLOR["ok" if online else "err"]
        self.conn_label.setText(
            f"<span style='color:{color};font-weight:600'>{'● 在线' if online else '● 离线'}</span> "
            f"&nbsp;{esc(state.address_summary())} "
            f"&nbsp;<span style='color:{PALETTE['dim']}'>{esc(state.api_detail or '未探测')}</span>"
        )
        self.btn_probe.setEnabled(True)
        # 让"关心连接状态"的页面跟着刷新（连接设置页的地址框与隧道状态就在这儿同步）。
        for page in self.pages:
            hook = getattr(page, "_on_connection_state", None)
            if callable(hook):
                try:
                    hook()
                except Exception:  # noqa: BLE001 - 单页刷新出错不该影响状态条
                    logger.exception("页面 %s 刷新连接状态失败", type(page).__name__)

    def on_connection_changed(self) -> None:
        """连接页探测成功后：把当前页重新拉一遍（认证失败不该被当成空数据）。"""
        self._refresh_current()

    # ------------------------------------------------------------ 导航
    def _on_nav_changed(self, current: QtWidgets.QTreeWidgetItem | None,
                        _previous: QtWidgets.QTreeWidgetItem | None = None) -> None:
        """导航选中项变了：内置页直接切过去，插件页面按需构建。"""
        if current is None:
            return
        payload = current.data(0, _NAV_ROLE)
        if not payload:
            return
        if payload[0] == "page":
            page = payload[1]
            self.stack.setCurrentWidget(page)
            page.load()
        elif payload[0] == "plugin":
            self._ensure_plugin_page(str(payload[1]), str(payload[2]))

    def _current_page(self) -> Page | None:
        widget = self.stack.currentWidget()
        return widget if isinstance(widget, Page) else None

    def _refresh_current(self) -> None:
        page = self._current_page()
        if page is not None:
            page.load(force=True)
            return
        item = self.nav.currentItem()
        payload = item.data(0, _NAV_ROLE) if item is not None else None
        if payload and payload[0] == "plugin":
            self._ensure_plugin_page(str(payload[1]), str(payload[2]))

    def notify(self, message: str, *, level: str = "info") -> None:
        """状态条提示（不弹模态框，避免打断操作）。"""
        text = " ".join(str(message).split())
        # 建界面期间（导航 setCurrentRow 会触发首次加载）状态条可能还不存在；
        # 这时只记日志，不要因为"提示不了"就把启动流程打断。
        status = getattr(self, "status", None)
        if status is not None:
            status.showMessage(text, 12000 if level == "error" else 6000)
        logger.log(
            {"error": logging.ERROR, "warn": logging.WARNING}.get(level, logging.INFO),
            "%s", text,
        )

    # ------------------------------------------------------------ 隧道自动启动
    def auto_start_tunnel(self) -> None:
        """打开控制台时，把**当前激活目标**的隧道默认连起来。

        为什么默认启动：桌面端打开后第一件事几乎总是"连上服务器"，而服务器目标的
        必经之路就是这条隧道（本应用不会回退公网直连，见 README 第 2 节）。
        每次手点一遍纯属重复劳动；非隧道目标（本机直连）这里什么都不做。

        失败只提示、不打断：用户仍可在「连接设置」里改完参数后手动重试。
        ssh 的诊断输出由「终端」页轮询隧道日志显示，这里不重复抄一份。
        """
        target = self.manager.active_target()
        if not target.is_tunnel:
            return
        tunnel = self.manager.tunnel_for(target)
        if tunnel.status.running:
            return
        self.notify(f"正在自动建立隧道（{target.name}）…")
        self._async.run(
            lambda: self.manager.start_tunnel(target),
            on_done=self._after_auto_tunnel,
        )

    def _after_auto_tunnel(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.notify(f"自动建立隧道失败：{result.message}", level="error")
            return
        status = result.value
        if status.state == STATE_RUNNING:
            self.notify(f"隧道已就绪：{status.base_url}", level="ok")
            if self.terminal_page is not None:
                self.terminal_page.append(f"\n[隧道已就绪] {status.base_url} → {status.detail}\n")
        else:
            self.notify(f"隧道未建立（{status.state}）：{status.error or '未知原因'}", level="error")

    # ------------------------------------------------------------ 连接
    def _probe(self) -> None:
        self.notify("正在测试连接…")
        self._async.run(
            self.manager.probe,
            on_done=lambda result: self._after_probe(result),
        )

    def _after_probe(self, result) -> None:  # noqa: ANN001
        if not result.ok:
            self.notify(f"探测失败：{result.message}", level="error")
            return
        probe = result.value
        self._on_state_changed(self.manager.state)
        if probe.ok:
            self.notify(f"连接正常（{probe.elapsed_ms} ms）", level="info")
        else:
            self.notify(f"{probe.detail}｜{probe.hint}", level="error")

    # ------------------------------------------------------------ 插件
    def _load_plugins(self) -> None:
        root = paths.project_root() / "desktop" / "plugins"
        self.plugin_host = PluginHost(
            root,
            client_provider=lambda: self.manager.client(),
            prefix_provider=lambda: self.manager.state.prefix or "/ai",
            ui=QtUiBridge(self),
            capability_probe=self._capabilities,
        )
        records = self.plugin_host.discover()          # 1) 只读 manifest
        self.plugin_host.load_all()                    # 2) import + 登记（不碰界面）
        failures = [r for r in records if r.state.value == "failed"]
        self.plugin_host.activate_all()                # 3) 现在才允许建页面
        self._rebuild_plugin_nav()
        if failures:
            names = "、".join(r.manifest.name for r in failures)
            self.notify(f"有插件加载失败（已隔离，不影响主程序）：{names}", level="error")

    def _capabilities(self):  # noqa: ANN202
        """服务端能力探测：拿不到就返回 None（当前服务端还没有这个端点）。"""
        try:
            return self.manager.client().capabilities()
        except Exception as exc:  # noqa: BLE001 - 能力探测失败不该影响任何东西
            logger.debug("能力探测失败：%s", exc)
            return None

    def _rebuild_plugin_nav(self) -> None:
        host = self.plugin_host
        node = self.plugins_item
        self.plugin_pages.clear()
        if node is not None:
            node.takeChildren()      # 先摘掉旧的插件子节点
        if host is None or node is None:
            return
        for spec in sorted(host.registry.pages, key=lambda p: (p.order, p.id)):
            record = host.records.get(spec.plugin_id)
            if record is None or record.state.value != "active":
                continue
            # 插件页面挂在「插件」节点下面：目录**可收起**（用户要求），
            # 也免得看导航的人把它们当成内置页。
            child = QtWidgets.QTreeWidgetItem([spec.title])
            child.setData(0, _NAV_ROLE, ("plugin", spec.id, spec.title))
            child.setToolTip(0, f"来自插件「{spec.plugin_id}」；权限与启停管理在「插件」页")
            node.addChild(child)
            self.plugin_pages.append((spec.id, spec.title, spec.plugin_id))
        node.setExpanded(bool(self.plugin_pages))   # 有子项就默认展开，随时可点箭头收起
        if self.plugin_pages:
            logger.info("导航里加入了 %s 个插件页面", len(self.plugin_pages))

    def _ensure_plugin_page(self, page_id: str, title: str) -> None:
        # 已经建过就直接切过去。**离线占位页除外**：连接恢复后要把它换成真页面。
        for i in range(len(self.pages), self.stack.count()):
            widget = self.stack.widget(i)
            if widget is not None and getattr(widget, "objectName", lambda: "")() == page_id:
                if widget.property("pluginOfflineHolder") and self.manager.state.api_ok:
                    self.stack.removeWidget(widget)
                    widget.deleteLater()
                    break
                self.stack.setCurrentIndex(i)
                return
        host = self.plugin_host
        if host is None:
            return
        # **插件页面工厂里是同步 HTTP**（两个示范插件原本都这样），而 Qt 控件只能在 GUI
        # 线程里建 —— 离线时调工厂就等于在 GUI 线程干等一次超时（十几秒假死，用户实测报回
        # "点插件页异常卡顿"）。所以先在离线这一侧短路：不发请求，给一张提示页。
        # 在线时照常建页面；"服务端本身慢"由插件自己的异步化兜住。
        if not self.manager.state.api_ok:
            self._show_plugin_offline(str(page_id), title)
            return
        try:
            widget = host.call_page_factory(str(page_id))
        except Exception as exc:  # noqa: BLE001 - 插件页面建不出来只影响它自己
            logger.exception("插件页面创建失败：%s", page_id)
            self.notify(f"插件页面「{title}」创建失败：{exc}", level="error")
            return
        if widget is None:
            self.notify(f"插件页面「{title}」没有返回内容", level="error")
            return
        if not isinstance(widget, QtWidgets.QWidget):
            # 无 Qt 环境下插件会返回纯数据；包一层只读文本显示。
            holder = QtWidgets.QPlainTextEdit(str(widget))
            holder.setReadOnly(True)
            widget = holder
        widget.setObjectName(str(page_id))
        self.stack.addWidget(widget)
        self.stack.setCurrentWidget(widget)

    def _show_plugin_offline(self, page_id: str, title: str) -> None:
        """离线时的插件页面占位：不打请求，也不假装有数据。

        占位页带 `pluginOfflineHolder` 标记 —— `_ensure_plugin_page` 认得它，
        连接恢复后再点这一项就会把它换成真正的插件页面。
        """
        holder = QtWidgets.QWidget()
        holder.setObjectName(page_id)
        holder.setProperty("pluginOfflineHolder", True)
        layout = QtWidgets.QVBoxLayout(holder)
        hint = QtWidgets.QLabel(
            f"插件页面「{title}」要连上服务端才能取数，而当前目标处于离线状态。\n\n"
            "为避免界面卡住，这里没有替它发起请求。恢复连接后（服务器目标请先确认隧道已启动）"
            "再点一下本项，或点顶部「刷新当前页」，就会重建这个页面。\n"
            "隧道诊断输出可在「终端」页实时查看。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        self.stack.addWidget(holder)
        self.stack.setCurrentWidget(holder)

    # ------------------------------------------------------------ 退出
    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self.plugin_host is not None:
            self.plugin_host.deactivate_all()   # 4) 先停插件（断信号、取消任务）
        self.manager.shutdown()                 # 再关自己启动的隧道
        logger.info("桌面控制台已退出")
        super().closeEvent(event)
