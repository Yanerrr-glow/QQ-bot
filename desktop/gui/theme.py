"""桌面端设计令牌与全局 QSS（**唯一来源**）。

为什么单独一个模块：颜色、圆角、间距、字号以前散在 `widgets.PALETTE` 与一段
f-string QSS 里，页面又各自写 inline stylesheet。改一次强调色要翻十几个文件，
而且很容易漏掉某处 —— 那种"大部分变了、一个角还是旧蓝"的界面比不改更难看。

约定（改设计先改这里，页面不再出现裸色值）：

* **画布**浅灰、**卡片**纯白，1px 浅边框，不用重阴影；
* **单一强调色**（靛蓝 `#4d6bfe`）：所有蓝色只从 `accent*` 这一族取，不引入第二种蓝；
* 中性文字三级：主 `text` / 次 `text_secondary` / 弱 `text_muted`；
* 圆角：按钮与输入 8px、卡片 10px、导航项 6px；间距只用 4/8/12/16/24；
* 按钮实际盒高 **32px**、最小盒宽 **72px**。

关于按钮尺寸的写法（Qt 的盒模型坑）：QSS 的 `min-height` / `min-width` 作用于
**内容盒**，padding 与 1px 边框要另加。所以"实际 32 × 72"在这里写成
`min-height: 30px; padding: 0 14px; min-width: 42px` ——
高度 30 + 上下无 padding + 上下边框 1×2 = 32；宽度 42 + 左右 padding 14×2 + 边框 2 = 72。
把 `min-height` 直接写 30 再加 5px 上下 padding 会得到 42px 的胖按钮（实测过）。

`widgets.PALETTE` / `widgets.STYLESHEET` 是兼容别名：页面里已有大量
`PALETTE['dim']` 之类的引用，改名会直接 ImportError。
"""

from __future__ import annotations

from string import Template

# ---------------------------------------------------------------- 颜色
#: 设计令牌：颜色。页面与 QSS 都从这里取，不再写裸色值。
COLORS: dict[str, str] = {
    # 画布 / 卡片
    "canvas": "#f5f6f8",
    "surface": "#ffffff",
    "surface_subtle": "#f0f2f6",
    "surface_sunken": "#f7f8fa",
    # 边框
    "border": "#e4e8ef",
    "border_strong": "#d6dce7",
    "gridline": "#eef0f5",
    # 文字三级
    "text": "#1f2430",
    "text_secondary": "#5b6472",
    "text_muted": "#8a93a5",
    # 唯一强调色（靛蓝）及其浅底
    "accent": "#4d6bfe",
    "accent_hover": "#3f5cf0",
    "accent_pressed": "#3550d8",
    "accent_soft": "#eef1ff",
    "accent_soft_hover": "#e3e8ff",
    # 状态色（配浅底 pill）
    "ok": "#16845b",
    "ok_soft": "#e8f6ef",
    "warn": "#a96100",
    "warn_soft": "#fdf4e5",
    "err": "#c53945",
    "err_soft": "#fdecee",
    # 终端（深色屏幕，仍只把强调色用于选区）
    "terminal_bg": "#111827",
    "terminal_fg": "#dbe7f5",
    "terminal_border": "#344054",
}

#: 兼容别名：页面里只认这九个键（多出来的键可以放心加）。
PALETTE: dict[str, str] = {
    "bg": COLORS["canvas"],
    "panel": COLORS["surface"],
    "panel2": COLORS["surface_subtle"],
    "fg": COLORS["text"],
    "dim": COLORS["text_secondary"],
    "muted": COLORS["text_muted"],
    "accent": COLORS["accent"],
    "ok": COLORS["ok"],
    "warn": COLORS["warn"],
    "err": COLORS["err"],
    "border": COLORS["border"],
    "border_strong": COLORS["border_strong"],
    "accent_soft": COLORS["accent_soft"],
}

# ---------------------------------------------------------------- 尺寸
#: 8px 栅格：间距只用这几个值。
SPACE: dict[str, int] = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}

#: 圆角。按钮/输入 8，卡片 10，导航项 6。
RADIUS: dict[str, int] = {"button": 8, "input": 8, "card": 10, "nav": 6}

#: 字号。
FONT: dict[str, int] = {"body": 13, "small": 12, "title": 18, "brand": 16}

#: 实际盒高 / 最小盒宽（见模块说明里对 Qt 盒模型的解释）。
BUTTON_HEIGHT = 32
BUTTON_MIN_WIDTH = 72
INPUT_HEIGHT = 32
TABLE_ROW_HEIGHT = 32
TABLE_HEADER_HEIGHT = 34
NAV_WIDTH = 208
NAV_ITEM_HEIGHT = 34
STATUS_HEIGHT = 26
FORM_LABEL_WIDTH = 110

#: 导航里选中项左侧强调竖条的宽度。
NAV_ACCENT_BAR = 3


# ---------------------------------------------------------------- QSS
# 用 `string.Template` 而不是 f-string：CSS 的大括号不用成对转义，
# 改一条规则不会因为漏转义一个 `{` 让整张表静默失效。
_QSS = Template(
    r"""
/* ---------- 基础 ---------- */
/* **容器一律透明**：背景色只由"画布"（主窗口/对话框/页面栈）与"卡片"（QFrame#card）决定。
   原来这里写的是 `background: $bg`，于是卡片内部任何没有自己背景的 QWidget 容器
   都会画出灰条 —— 参数页布尔项那个"细长灰色方块"就是这么来的（2026-10-18 实拍）。 */
QWidget {
    background: transparent;
    color: $fg;
    font-size: ${font_body}px;
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
}
QMainWindow, QDialog, QStackedWidget, QSplitter { background: $bg; }
QLabel { background: transparent; }
QToolTip {
    background: $fg; color: #ffffff; border: 1px solid $fg;
    padding: 5px 8px; border-radius: 6px; font-size: ${font_small}px;
}

/* ---------- 壳：顶栏 / 导航卡片 / 页面标题 ---------- */
QFrame#topBar, QFrame#navCard {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius_card}px;
}
QFrame#card {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius_card}px;
}
QLabel#pageTitle { font-size: ${font_title}px; font-weight: 600; color: $fg; }
QLabel#pageSubtitle { font-size: ${font_small}px; color: $muted; }
QLabel#cardTitle { font-size: ${font_body}px; font-weight: 600; color: $fg; }
QLabel#descriptionText { color: $secondary; }
QLabel#brandTitle { font-size: ${font_brand}px; font-weight: 600; color: $fg; }
QLabel#brandSub { font-size: ${font_small}px; color: $muted; }
QLabel#toolbarNote { font-size: ${font_small}px; color: $muted; }
QLabel#connDetail { font-size: ${font_small}px; color: $muted; }
QLabel#connPill {
    font-size: ${font_small}px; font-weight: 600;
    padding: 1px 10px; border-radius: 10px;
}
QLabel#connPill[state="online"] { color: $ok; background: $ok_soft; }
QLabel#connPill[state="offline"] { color: $err; background: $err_soft; }

/* ---------- 左侧导航 ---------- */
QTreeView#nav { show-decoration-selected: 0; }
QTreeWidget#nav {
    background: transparent;
    border: none;
    outline: none;
    padding: 2px 0;
}
QTreeWidget#nav::item {
    color: $secondary;
    min-height: ${nav_item_height}px;
    padding: 0 8px 0 10px;
    margin: 1px 4px;
    border-radius: ${radius_nav}px;
    border-left: ${nav_bar}px solid transparent;
}
QTreeWidget#nav::item:hover { background: $panel2; color: $fg; }
QTreeWidget#nav::item:selected {
    background: $accent_soft;
    color: $accent;
    border-left: ${nav_bar}px solid $accent;
}

/* ---------- 列表 / 标签页 ---------- */
QListWidget {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius_card}px;
    outline: none;
    padding: 4px;
}
QListWidget::item {
    padding: 7px 10px;
    border-radius: 6px;
    margin: 1px 0;
    color: $secondary;
}
QListWidget::item:hover { background: $panel2; color: $fg; }
QListWidget::item:selected { background: $accent_soft; color: $accent; }
/* 目标列表已经在一张白卡里，去掉它自己的边框，避免「卡片套卡片」。 */
QListWidget#flatList { background: transparent; border: none; padding: 0; }

QTabWidget::pane {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius_card}px;
    top: -1px;
}
QTabBar::tab {
    padding: 8px 14px;
    background: transparent;
    color: $secondary;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 4px;
}
QTabBar::tab:hover { color: $fg; }
QTabBar::tab:selected {
    color: $accent;
    border-bottom: 2px solid $accent;
    font-weight: 600;
}

/* ---------- 按钮：三种角色 + 默认（secondary） ---------- */
QPushButton {
    background: $panel;
    color: $fg;
    border: 1px solid $border_strong;
    border-radius: ${radius_button}px;
    padding: 0 14px;
    min-height: 30px;
    min-width: 42px;
    font-size: ${font_body}px;
}
QPushButton:hover { background: $accent_soft; border-color: $accent; color: $accent; }
QPushButton:pressed { background: $accent_soft_hover; border-color: $accent_pressed; }
QPushButton:disabled {
    color: $muted;
    background: $surface_sunken;
    border-color: $border;
}
QPushButton:focus { outline: none; }

QPushButton[variant="primary"] {
    background: $accent;
    border: 1px solid $accent;
    color: #ffffff;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover {
    background: $accent_hover;
    border-color: $accent_hover;
    color: #ffffff;
}
QPushButton[variant="primary"]:pressed {
    background: $accent_pressed;
    border-color: $accent_pressed;
}
QPushButton[variant="primary"]:disabled {
    background: #b9c3f8;
    border-color: #b9c3f8;
    color: #ffffff;
}

QPushButton[variant="danger"] {
    background: $panel;
    border: 1px solid #f0c9cd;
    color: $err;
}
QPushButton[variant="danger"]:hover {
    background: $err_soft;
    border-color: $err;
    color: $err;
}
QPushButton[variant="danger"]:pressed { background: #fbdfe2; border-color: $err; }
QPushButton[variant="danger"]:disabled {
    background: $surface_sunken;
    border-color: $border;
    color: $muted;
}

QPushButton[variant="ghost"] {
    background: transparent;
    border: 1px solid transparent;
    color: $secondary;
}
QPushButton[variant="ghost"]:hover {
    background: $panel2;
    border-color: transparent;
    color: $fg;
}
QPushButton[variant="ghost"]:pressed { background: $border; }
QPushButton[variant="ghost"]:disabled { background: transparent; color: $muted; }

/* ---------- 输入 ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: $panel;
    color: $fg;
    border: 1px solid $border_strong;
    border-radius: ${radius_input}px;
    padding: 5px 10px;
    min-height: 20px;
    selection-background-color: $accent;
    selection-color: #ffffff;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover,
QPlainTextEdit:hover, QTextEdit:hover { border-color: $accent; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus,
QTextEdit:focus, QComboBox:focus { border: 1px solid $accent; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled,
QPlainTextEdit:disabled, QTextEdit:disabled {
    background: $surface_sunken; color: $muted; border-color: $border;
}
QComboBox QAbstractItemView {
    background: $panel;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px;
    selection-background-color: $accent_soft;
    selection-color: $accent;
    outline: none;
}
QComboBox::drop-down { border: none; width: 24px; }

/* ---------- 表格 ---------- */
QTableWidget, QTreeWidget, QListWidget#plain {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius_card}px;
    gridline-color: $gridline;
    alternate-background-color: #fafbfd;
    outline: none;
}
QTableWidget::item, QTreeWidget::item { padding: 0 8px; }
QTableWidget::item:selected, QTreeWidget::item:selected {
    background: $accent_soft;
    color: $fg;
}
QHeaderView::section {
    background: $surface_sunken;
    color: $muted;
    padding: 6px 10px;
    border: none;
    border-bottom: 1px solid $border;
    font-size: ${font_small}px;
    font-weight: 600;
}
QTableCornerButton::section { background: $surface_sunken; border: none; }
QTableWidget QTableCornerButton::section { border: none; }

/* ---------- 分组卡片（QGroupBox） ---------- */
QGroupBox {
    background: $panel;
    border: 1px solid $border;
    border-radius: ${radius_card}px;
    margin-top: 14px;
    padding: 14px 12px 12px;
    font-size: ${font_body}px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 1px;
    padding: 0 6px;
    background: transparent;
    color: $fg;
    font-size: ${font_body}px;
    font-weight: 600;
}
QGroupBox::indicator { width: 14px; height: 14px; }

/* ---------- 状态条 ---------- */
QStatusBar {
    background: $panel;
    color: $secondary;
    border-top: 1px solid $border;
    font-size: ${font_small}px;
    padding: 0 8px;
}
QStatusBar::item { border: none; }
QStatusBar QLabel { background: transparent; }
QSizeGrip { width: 0; height: 0; }

/* ---------- 滚动 / 分隔 / 其他 ---------- */
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: #ccd3df; border-radius: 4px; min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: #aeb8c8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal {
    background: #ccd3df; border-radius: 4px; min-width: 28px;
}
QScrollBar::handle:horizontal:hover { background: #aeb8c8; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

QCheckBox { spacing: 8px; background: transparent; }
/* 复选框：默认尺寸由样式给的，但**不设 indicator 尺寸时 Qt 会按控件高度拉伸**，
   在网格里就变成一根细长条（2026-10-18 参数页实拍）。这里钉死 16×16。 */
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid $border_strong;
    border-radius: 4px;
    background: $panel;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked { background: $accent; border-color: $accent; }
QCheckBox::indicator:disabled { background: $surface_sunken; border-color: $border; }
/* 数字框的上下按钮：不指定宽度时会被压成一根竖线，箭头也挤在一起。 */
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; background: transparent;
    border-left: 1px solid $border;
    border-top-right-radius: ${radius_input}px;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 18px; background: transparent;
    border-left: 1px solid $border;
    border-bottom-right-radius: ${radius_input}px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background: $accent_soft;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { width: 8px; height: 8px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { width: 8px; height: 8px; }
QSplitter::handle { background: transparent; }
QSplitter::handle:hover { background: $accent_soft; }
QProgressBar {
    background: $panel2;
    border: none;
    border-radius: 4px;
    text-align: center;
    min-height: 8px;
    max-height: 8px;
}
QProgressBar::chunk { background: $accent; border-radius: 4px; }
"""
)

#: QSS 模板变量。新增令牌时**这里要一起加**，否则 Template.substitute 会报 KeyError
#: （这是有意的：漏一个变量就该在启动时炸，而不是静默留下空颜色）。
_QSS_VARS = {
    # 颜色
    "bg": COLORS["canvas"],
    "panel": COLORS["surface"],
    "panel2": COLORS["surface_subtle"],
    "surface_sunken": COLORS["surface_sunken"],
    "border": COLORS["border"],
    "border_strong": COLORS["border_strong"],
    "gridline": COLORS["gridline"],
    "fg": COLORS["text"],
    "secondary": COLORS["text_secondary"],
    "muted": COLORS["text_muted"],
    "accent": COLORS["accent"],
    "accent_hover": COLORS["accent_hover"],
    "accent_pressed": COLORS["accent_pressed"],
    "accent_soft": COLORS["accent_soft"],
    "accent_soft_hover": COLORS["accent_soft_hover"],
    "ok": COLORS["ok"],
    "ok_soft": COLORS["ok_soft"],
    "err": COLORS["err"],
    "err_soft": COLORS["err_soft"],
    # 尺寸
    "radius_card": RADIUS["card"],
    "radius_button": RADIUS["button"],
    "radius_input": RADIUS["input"],
    "radius_nav": RADIUS["nav"],
    "font_body": FONT["body"],
    "font_small": FONT["small"],
    "font_title": FONT["title"],
    "font_brand": FONT["brand"],
    "nav_item_height": NAV_ITEM_HEIGHT,
    "nav_bar": NAV_ACCENT_BAR,
}

#: 全局样式表。`main_window.MainWindow` 在构造时 `setStyleSheet(STYLESHEET)`。
STYLESHEET = _QSS.substitute(**_QSS_VARS)
