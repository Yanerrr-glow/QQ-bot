r"""跑离线验证_桩.py，把**完整输出**写进 _selftest_full.log。

为什么需要它：套件输出上万行，而这个环境里 powershell 管道 / `subprocess` 管道
都会被沙箱拒绝（`PermissionError: [WinError 5]`），控制台又只保留尾部 ——
于是"到底过没过、挂在哪几条"看不见。
这里改用**文件句柄重定向**（`CreatePipe` 才被拦，普通文件可以），
于是完整输出落盘、可以用 read 工具看任意一段。

用法（在项目根下）：
    python 验证/_跑桩验证.py
退出码与套件一致（0 = 全过）。之后：
    看摘要： read _selftest_full.log 的末尾
    看失败： grep "\[失败\]" _selftest_full.log
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

# 控制台代码页可能是 GBK（Windows），而套件输出里有 emoji/特殊符号 ——
# 不重配就会在 print 摘要时抛 UnicodeEncodeError，把真实退出码掩盖掉。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - 老 Python 或非标准流就算了
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
# 套件与**本脚本同目录**（`验证\`）。原来写的是 `_工具链\离线验证_桩.py` ——
# 文件搬过一次之后这里就成了死路径，报错还长得像"套件自己挂了"。
SUITE = pathlib.Path(__file__).resolve().parent / "离线验证_桩.py"
LOG = ROOT / "_selftest_full.log"

with open(LOG, "w", encoding="utf-8") as fh:
    proc = subprocess.run(
        [sys.executable, str(SUITE)],
        cwd=str(ROOT),
        stdout=fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

tail = LOG.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:]
print(f"退出码: {proc.returncode}  完整输出: {LOG.name}")
print("\n".join(tail))
sys.exit(proc.returncode)
