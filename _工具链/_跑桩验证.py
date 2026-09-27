r"""跑离线验证_桩.py，把**完整输出**写进 _selftest_full.log。

为什么需要它：套件输出上万行，而这个环境里 powershell 管道 / `subprocess` 管道
都会被沙箱拒绝（`PermissionError: [WinError 5]`），控制台又只保留尾部 ——
于是"到底过没过、挂在哪几条"看不见。
这里改用**文件句柄重定向**（`CreatePipe` 才被拦，普通文件可以），
于是完整输出落盘、可以用 read 工具看任意一段。

用法（在项目根下）：
    python _工具链/_跑桩验证.py
退出码与套件一致（0 = 全过）。之后：
    看摘要： read _selftest_full.log 的末尾
    看失败： grep "\[失败\]" _selftest_full.log
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SUITE = ROOT / "_工具链" / "离线验证_桩.py"
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
