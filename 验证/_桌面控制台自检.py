r"""桌面控制台自检入口（项目 `_工具链\` 下的脚本会调它）。

真正的检查逻辑在 `desktop.selfcheck.checks` —— 一份实现，两个入口：
这个文件，或 `python -m desktop selfcheck`。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许直接 `python 验证\_桌面控制台自检.py` 跑：把项目根放进 sys.path。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> int:
    from desktop.selfcheck.checks import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
