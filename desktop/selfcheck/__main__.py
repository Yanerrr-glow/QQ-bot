"""桌面控制台自检（无 GUI、不需要机器人）。

跑法：

    python -m desktop.selfcheck

或走项目入口：`验证\\_桌面控制台自检.py`。

**不改动用户的真实配置与运行数据**：全部读写被 `QQBOT_CONSOLE_HOME` /
`QQBOT_CONSOLE_DATA` 引到临时目录（见 `checks.run()`）。
"""

from __future__ import annotations

import sys


def main() -> int:
    from .checks import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
