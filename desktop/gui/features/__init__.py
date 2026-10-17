"""「功能」目录：中心页 + 每个功能的子菜单（设置 + 细节）。

    catalog.py       功能目录（有哪些功能、归哪些参数组、看哪些运行细节）
    panels.py        两处共用的控件（状态 chip 行 / 参数设置面板 / 功能卡片）
    page_hub.py      「功能」中心页：一眼看清每个功能是开是关、现在什么状态
    page_feature.py  功能子菜单页：同一份目录生成的“设置 + 细节”

约定（与 `desktop/README.md` 一致）：这里只走 `ConnectionManager` 那一个出口，
不读配置文件、不碰运行数据；取数一律异步（`Page.run_task`）。
"""
