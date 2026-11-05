"""内置页面的唯一注册入口；顺序决定导航顺序。"""

from .features.page_feature import build_feature_pages
from .features.page_hub import FeatureHubPage
from .page_connections import ConnectionsPage
from .page_imagepolicy import ImagePolicyPage
from .page_memory import MemoryPage
from .page_models import ModelsPage
from .page_overview import OverviewPage
from .page_params import ParamsPage
from .page_persona import PersonaEvalPage
from .page_proposals import ProposalsPage
from .page_plugins import PluginsPage
from .page_stickers import StickersPage
from .page_terminal import TerminalPage

#: 「功能」目录的标题。参数页的 `nav_after` 与顶部「刷新当前页」都按它对齐 ——
#: 这里改一个字，那两个地方不用跟着改（都走常量）。
FEATURES_SECTION = "功能"

#: 内置页面。**顺序即导航顺序**，逐页写而不是排序 —— 用户看的是这个先后。
#: `nav_section` 非空的页面挂进同名目录（与总览、终端同级），目录**可收起**。
BUILTIN_PAGES = (
    OverviewPage,
    # 终端紧跟在总览之后（用户要求）：它和总览一样是"一眼要看的常驻视图"，
    # 而不是连接设置页的附属面板。
    TerminalPage,
    # 「功能」目录：中心页 + 每个功能一个子菜单（见 `gui/features/`）。
    # 功能设置全在这下面，顶层不再一排平铺 —— 用户要求"总览同级别建「功能」目录，
    # 子菜单放功能细节"。
    FeatureHubPage,
    *build_feature_pages(),
    ParamsPage,
    ModelsPage,
    PersonaEvalPage,
    # 修订提案紧挨「人格评估台」：它产出的就是那边的评估/修订结果，
    # 两个入口看的是同一件事（谁改了人格、改了什么）。**放顶层**而不是塞进
    # 「功能」目录：那要同步改 `features/catalog.py` 的 FEATURES，
    # 而 `gui_smoke` 断言「子菜单 == FEATURES 全序」，为一个列表页不值当（#0006）。
    ProposalsPage,
    MemoryPage,
    ImagePolicyPage,
    StickersPage,
    ConnectionsPage,
    PluginsPage,
)

#: 为什么导航里**没有**标题为「人格」的独立页：那个标题归「功能」目录下的「人格」子菜单
#: （页面标题是跳转用的身份 —— `goto_page` 按标题查，同名会二义）。
#: 人格的**表格式明细**（三层正文、评估台、改动日志）在 `PersonaEvalPage`（标题「人格评估台」），
#: 它复用 `PersonaPage` 的界面但把四个花 token 的动作藏起来 —— 那些动作与
#: 「人格评估与自动修订」子菜单里的开关是同一件事，两个入口各存一份行为迟早对不上。

#: 挂进「功能」目录的页面（页面类自己声明 `in_features`，这里只是取出来用）。
FEATURE_PAGE_CLASSES = tuple(
    cls for cls in BUILTIN_PAGES if bool(getattr(cls, "in_features", False))
)

