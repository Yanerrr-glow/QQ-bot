"""主窗口：左侧导航 + 顶部连接/机器人状态 + 右侧内容区。

生命周期按方案 5.2.1 定死的四步走：

    1. discovery   只读 manifest
    2. register    import 插件、登记页面/操作/状态卡（不碰 Qt 界面）
    3. activate    主窗口与导航就绪后才 on_activate（插件这时才允许建页面）
    4. deactivate  退出时先停插件再关隧道，顺序反了就留幽灵任务

导航里"内置页面"永远在前，插件页面按 manifest 的 `navigation.order` 排在后面 ——
插件不能把总览挤出视野之外。

导航是**两层**：顶层页面 + 两个可收起的目录（「功能」与「插件」）。`nav_section` 非空的页面
挂在同名目录下面；「功能」目录的顶层那一行就是它的中心页（与总览、终端同级）。
详见 `desktop/README.md` 第 4.1 节。
"""

from __future__ import annotations

import logging

from PySide6 import QtCore, QtGui, QtWidgets

from .. import APP_NAME, APP_VERSION
from ..core import paths
from ..core.connection import ConnectionManager
from ..core.targets import MODE_LOCAL
from ..core.tunnel import STATE_RUNNING
from ..core.util import one_line
from ..sdk.loader import PluginHost
from . import theme
from .base import Page
from .page_registry import BUILTIN_PAGES, FEATURES_SECTION, FEATURE_PAGE_CLASSES
from .page_terminal import TerminalPage
from .page_plugins import PluginsPage
from .plugin_bridge import QtUiBridge
from .widgets import Async, STYLESHEET, TaskResult, busy_button, hline

logger = logging.getLogger("qqbot.desktop.gui.main")

#: 导航树节点的载荷：`("page", Page 实例)` / `("section", 标题)` /
#: `("plugin", page_id, title)`。页面栈索引不再和导航行号绑定 ——
#: 树可以收起、还有目录节点，行号本身已经不是个稳定标识了。
_NAV_ROLE = QtCore.Qt.ItemDataRole.UserRole


class _NavItemDelegate(QtWidgets.QStyledItemDelegate):
    """固定导航行高 34px。

    QSS 的 `min-height` 对 `QTreeWidget::item` 不生效（实测行高仍是字体行高），
    而行高又是这次视觉规范里少数必须精确的量 —— 交给 delegate 的 `sizeHint` 最稳。
    """

    def sizeHint(self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex):  # noqa: N802
        size = super().sizeHint(option, index)
        return QtCore.QSize(size.width(), theme.NAV_ITEM_HEIGHT)


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
        #: 导航里「功能」那个树节点：功能子菜单挂在它下面。
        self.features_item: QtWidgets.QTreeWidgetItem | None = None
        #: 页面标题 → 导航项。跳转（功能页之间、总览的快捷入口）按标题查，不靠行号。
        self._nav_by_title: dict[str, QtWidgets.QTreeWidgetItem] = {}
        #: 数据已过期、下次显示时要强制重拉的页面（同一份目录生成的功能页之间联动）。
        self._stale_pages: set[Page] = set()
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
        self.status.setSizeGripEnabled(False)
        self.status.setFixedHeight(theme.STATUS_HEIGHT)
        self._build_status_widgets()
        self.status.showMessage("就绪")

        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["md"], theme.SPACE["lg"], theme.SPACE["md"]
        )
        outer.setSpacing(theme.SPACE["md"])
        outer.addWidget(self._build_top_bar())

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(theme.SPACE["lg"])
        body.addWidget(self._build_side_nav())

        self.stack = QtWidgets.QStackedWidget()
        body.addWidget(self.stack, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        # 注册导航与页面栈；数据在首次访问时加载。
        sections: dict[str, QtWidgets.QTreeWidgetItem] = {}
        for cls in BUILTIN_PAGES:
            page = cls(self.manager)
            self._normalize_page_layout(page)
            self.pages.append(page)
            self.stack.addWidget(page)
            page.busy_changed.connect(self._sync_refresh_button)
            if isinstance(page, TerminalPage):
                self.terminal_page = page
            item = QtWidgets.QTreeWidgetItem([page.title])
            item.setData(0, _NAV_ROLE, ("page", page))
            item.setToolTip(0, page.subtitle or page.title)

            section = str(getattr(cls, "nav_section", "") or "")
            if section:
                node = sections.get(section)
                if node is None:
                    node = QtWidgets.QTreeWidgetItem([section])
                    node.setData(0, _NAV_ROLE, ("section", section))
                    node.setToolTip(0, "各项功能的设置与细节（点箭头收起/展开）")
                    self.nav.addTopLevelItem(node)
                    sections[section] = node
                if page.title == section:
                    # 目录的顶层入口页（中心页）自己就是那一行：**载荷换成页面**，
                    # 否则点这一行只会选中一个节点、`_on_nav_changed` 拿不到页面，
                    # 表现就是"点了「功能」但右侧还停在上一个页面"（实测踩过）。
                    node.setData(0, _NAV_ROLE, ("page", page))
                    node.setToolTip(0, page.subtitle or page.title)
                    self._nav_by_title[page.title] = node
                    if section == FEATURES_SECTION:
                        self.features_item = node
                    if isinstance(page, PluginsPage):
                        self.plugins_item = node
                    continue
                node.addChild(item)
            else:
                self.nav.addTopLevelItem(item)
            self._nav_by_title[page.title] = item

            if isinstance(page, PluginsPage):
                self.plugins_item = item      # 插件页面稍后挂到这下面（可收起）
        if self.features_item is not None:
            self.features_item.setExpanded(True)   # 功能是常用区，默认展开；随时可收起

        self.stack.setCurrentIndex(0)
        first = self.nav.topLevelItem(0)
        self.nav.setCurrentItem(first)
        if self.pages:
            self._set_page_header(self.pages[0].title, self.pages[0].subtitle)
        # 只加载当前页；其它页面在首次导航时加载，避免启动时并发拉取完整状态。
        if self.pages:
            self.pages[0].load()
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+K"), self, activated=self.nav_search.setFocus)
        QtGui.QShortcut(QtGui.QKeySequence("F5"), self, activated=self._refresh_current)

    @staticmethod
    def _normalize_page_layout(page: Page) -> None:
        """页面外框统一为壳里的栅格：外边距 0（壳已给 16px），页内块间距 12。"""
        layout = page.layout()
        if isinstance(layout, QtWidgets.QVBoxLayout):
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(theme.SPACE["md"])

    def _build_side_nav(self) -> QtWidgets.QWidget:
        """左侧栏：品牌区 + 导航树，整体是一张白色卡片（宽 208，200±20 内）。"""
        card = QtWidgets.QFrame()
        card.setObjectName("navCard")
        card.setFixedWidth(theme.NAV_WIDTH)

        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(theme.SPACE["sm"], 14, theme.SPACE["sm"], theme.SPACE["sm"])
        layout.setSpacing(theme.SPACE["sm"])

        brand = QtWidgets.QWidget()
        brand_layout = QtWidgets.QVBoxLayout(brand)
        brand_layout.setContentsMargins(10, 0, 10, 2)
        brand_layout.setSpacing(2)
        brand_title = QtWidgets.QLabel("QQ-bot 控制台")
        brand_title.setObjectName("brandTitle")
        brand_sub = QtWidgets.QLabel("本地控制台")
        brand_sub.setObjectName("brandSub")
        brand_layout.addWidget(brand_title)
        brand_layout.addWidget(brand_sub)
        layout.addWidget(brand)
        layout.addWidget(hline())

        self.nav_search = QtWidgets.QLineEdit()
        self.nav_search.setPlaceholderText("搜索页面  Ctrl+K")
        self.nav_search.setAccessibleName("搜索导航页面")
        self.nav_search.setClearButtonEnabled(True)
        self.nav_search.textChanged.connect(self._filter_nav)
        self.nav_search.returnPressed.connect(self._open_search_result)
        layout.addWidget(self.nav_search)
        self.nav_empty = QtWidgets.QLabel("没有匹配的页面")
        self.nav_empty.setWordWrap(True)
        self.nav_empty.hide()
        layout.addWidget(self.nav_empty)

        # 导航是一棵树（`QTreeWidget`）：插件页面挂在「插件」节点下，**目录可收起**。
        self.nav = QtWidgets.QTreeWidget()
        self.nav.setObjectName("nav")
        self.nav.setHeaderHidden(True)
        self.nav.setRootIsDecorated(True)          # 「插件」行前面的展开/收起箭头
        self.nav.setIndentation(14)
        self.nav.setUniformRowHeights(True)
        self.nav.setExpandsOnDoubleClick(False)    # 展开只走箭头，双击别误触
        self.nav.setItemDelegate(_NavItemDelegate(self.nav))
        self.nav.currentItemChanged.connect(self._on_nav_changed)
        layout.addWidget(self.nav, 1)
        return card

    def _build_top_bar(self) -> QtWidgets.QWidget:
        """顶栏：左侧页面标题/副标题，右侧目标与全局操作。连接状态在状态条右侧。"""
        bar = QtWidgets.QFrame()
        bar.setObjectName("topBar")
        layout = QtWidgets.QHBoxLayout(bar)
        layout.setContentsMargins(
            theme.SPACE["lg"], theme.SPACE["sm"], theme.SPACE["lg"], theme.SPACE["sm"]
        )
        layout.setSpacing(theme.SPACE["sm"])

        titles = QtWidgets.QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(2)
        self.page_title = QtWidgets.QLabel("总览")
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QtWidgets.QLabel("")
        self.page_subtitle.setObjectName("pageSubtitle")
        self.page_subtitle.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred
        )
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_subtitle)
        # 标题块拿**剩余的全部宽度**（不要先 addStretch）：否则副标题只会分到标题文字
        # 那么宽，出现「搜索后端、读」这种截在半句话上的情况（用户实测报回）。
        layout.addLayout(titles, 1)

        layout.addWidget(QtWidgets.QLabel("目标："))
        self.target_combo = QtWidgets.QComboBox()
        self.target_combo.setMinimumWidth(220)
        self.target_combo.currentIndexChanged.connect(self._on_target_combo)
        layout.addWidget(self.target_combo)

        self.btn_probe = busy_button("测试连接", self._probe)
        layout.addWidget(self.btn_probe)

        self.btn_refresh = busy_button("刷新当前页", self._refresh_current)
        layout.addWidget(self.btn_refresh)

        self._reload_targets()
        return bar

    def _build_status_widgets(self) -> None:
        """状态条右侧：连接详情 + 在线/离线 pill（左侧消息由 `showMessage` 管）。"""
        holder = QtWidgets.QWidget()
        holder.setObjectName("statusRight")
        layout = QtWidgets.QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, theme.SPACE["sm"], 0)
        layout.setSpacing(theme.SPACE["sm"])

        self.conn_detail = QtWidgets.QLabel("")
        self.conn_detail.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.conn_detail.setObjectName("connDetail")
        self.conn_detail.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Preferred
        )
        #: 兼容旧引用（`_on_state_changed` 曾经直接写这个 label）。
        self.conn_label = self.conn_detail

        self.conn_pill = QtWidgets.QLabel("● 离线")
        self.conn_pill.setObjectName("connPill")
        self.conn_pill.setProperty("state", "offline")
        self.conn_pill.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.conn_pill.setMinimumWidth(64)

        layout.addWidget(self.conn_detail)
        layout.addWidget(self.conn_pill)
        self.status.addPermanentWidget(holder)

    def _set_page_header(self, title: str, subtitle: str = "") -> None:
        """同步顶栏的页面标题/副标题（切页时调用；不改变任何加载逻辑）。"""
        self.page_title.setText(str(title or ""))
        # 顶栏已经给足宽度，这里不再硬截 52 字；只在异常超长时兜底，完整文本进 tooltip。
        text = one_line(subtitle, 80) if subtitle else ""
        self.page_subtitle.setText(text)
        self.page_subtitle.setToolTip(str(subtitle or ""))
        self.page_subtitle.setVisible(bool(text))

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
        # 状态条右侧 pill：颜色由 QSS 的 `[state=…]` 选择器决定（单一强调色之外的语义色）。
        self.conn_pill.setText("● 在线" if online else "● 离线")
        self.conn_pill.setProperty("state", "online" if online else "offline")
        style = self.conn_pill.style()
        style.unpolish(self.conn_pill)
        style.polish(self.conn_pill)
        detail = one_line(
            f"{state.address_summary()} · {state.api_detail or '未探测'}", 90
        )
        # 连接详情是纯文本（不是富文本）：地址与服务端给的说明都不解释成 HTML。
        self.conn_detail.setText(detail)
        self.conn_detail.setToolTip(detail)
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
    def _sync_refresh_button(self, _busy: bool = False) -> None:
        page = self._current_page()
        busy = page is not None and page.busy
        self.btn_refresh.setEnabled(not busy)
        self.btn_refresh.setText("加载中…" if busy else "刷新当前页")
        self.btn_refresh.setToolTip("正在处理当前页面的请求" if busy else "刷新当前页（F5）")

    def _filter_nav(self, text: str) -> None:
        """标题和说明均可搜索；保留匹配项的父目录。"""
        query = text.strip().casefold()
        if query and not getattr(self, "_nav_search_active", False):
            self._nav_expanded = {
                self.nav.topLevelItem(i).text(0): self.nav.topLevelItem(i).isExpanded()
                for i in range(self.nav.topLevelItemCount())
            }
        self._nav_search_active = bool(query)
        visible = 0
        for i in range(self.nav.topLevelItemCount()):
            node = self.nav.topLevelItem(i)
            match = query in (node.text(0) + " " + node.toolTip(0)).casefold()
            children = False
            for j in range(node.childCount()):
                child = node.child(j)
                show = match or query in (child.text(0) + " " + child.toolTip(0)).casefold()
                child.setHidden(not show)
                children |= show
            node.setHidden(not (match or children))
            visible += int(match or children)
            if query and children:
                node.setExpanded(True)
            elif not query and hasattr(self, "_nav_expanded"):
                node.setExpanded(self._nav_expanded.get(node.text(0), node.isExpanded()))
        self.nav_empty.setVisible(not visible)

    def _open_search_result(self) -> None:
        for i in range(self.nav.topLevelItemCount()):
            node = self.nav.topLevelItem(i)
            if node.isHidden():
                continue
            # 目录仅因子项匹配时，回车直接打开子项。
            query = self.nav_search.text().strip().casefold()
            item = node
            if query not in (node.text(0) + " " + node.toolTip(0)).casefold():
                item = next((node.child(j) for j in range(node.childCount())
                             if not node.child(j).isHidden()), node)
            already_current = self.nav.currentItem() is item
            self.nav.setCurrentItem(item)
            if already_current:
                self._on_nav_changed(item)
            self.nav.setFocus()
            return

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
            self._set_page_header(page.title, page.subtitle)
            # 被标脏的页面（在别处改过参数）在这里才真正重拉：看不到的页面不发请求。
            stale = page in self._stale_pages
            self._stale_pages.discard(page)
            page.load(force=stale)
        elif payload[0] == "plugin":
            self._set_page_header(str(payload[2]), "插件页面")
            self._ensure_plugin_page(str(payload[1]), str(payload[2]))
        self._sync_refresh_button()

    def _current_page(self) -> Page | None:
        widget = self.stack.currentWidget()
        return widget if isinstance(widget, Page) else None

    def _refresh_current(self) -> None:
        page = self._current_page()
        if page is not None:
            page.load(force=True)
            # 「功能」目录下的页面是同一份目录生成的联动视图：在某个子菜单里改了
            # `memory_enabled` 这类开关，中心页与其它子菜单的细节 chip 就已经过期。
            # 所以刷新当前功能页时，把其它功能页标成"下次显示再拉"（`_stale_pages`）——
            # **不在这里替它们发请求**：19 个功能页同时拉 `/api/state` 才是灾难。
            if getattr(page, "in_features", False):
                for other in self.pages:
                    if other is not page and getattr(other, "in_features", False):
                        self._stale_pages.add(other)
            return
        item = self.nav.currentItem()
        payload = item.data(0, _NAV_ROLE) if item is not None else None
        if payload and payload[0] == "plugin":
            self._ensure_plugin_page(str(payload[1]), str(payload[2]))

    def goto_page(self, title: str) -> bool:
        """按页面标题跳转（导航高亮 + 页面栈 + 首次加载）。找不到返回 False。

        为什么按**标题**而不是索引：导航是两层树、还能收起，行号不是稳定标识；
        而页面标题是 `Page.title`，是页面对外唯一的身份（自检也按它找页面）。
        """
        item = self._nav_by_title.get(str(title))
        if item is None:
            logger.warning("要跳转的页面不存在：%s", title)
            return False
        self.nav_search.clear()
        parent = item.parent()
        if parent is not None:
            parent.setExpanded(True)     # 目录收起时得先展开，否则"切过去了但看不见"
        already_current = self.nav.currentItem() is item
        self.nav.setCurrentItem(item)    # 触发 `_on_nav_changed`：切栈 + load
        if already_current:
            # 已经选中该项时 Qt 不发信号，手动补一次（与首屏同样的坑）。
            self._on_nav_changed(item, None)
        self.nav.scrollToItem(item)
        return True

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
        self._filter_nav(self.nav_search.text())
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
