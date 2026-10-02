"""`python -m desktop` 的入口。

三个子命令：

    python -m desktop            启动桌面控制台（需要 PySide6）
    python -m desktop selfcheck  跑无头自检（不需要 PySide6、不需要机器人）
    python -m desktop paths      打印配置/数据落点体检（**排查"存不了目标"先看这个**）

自检放在这里而不是单独脚本里，是为了让"验证"和"使用"走同一个入口 ——
否则自检脚本会慢慢腐化，最后谁也不敢信它。
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("selfcheck", "check"):
        # 自检路径**不 import Qt**：这台机器的 PySide6 可能根本装不上。
        from .selfcheck.checks import run

        return run()
    if args and args[0] in ("paths", "where", "diag"):
        from .core import paths

        for line in paths.diagnose():
            print(line)
        return 0
    from .gui.app import main as gui_main

    return gui_main(args)


if __name__ == "__main__":
    sys.exit(main())
