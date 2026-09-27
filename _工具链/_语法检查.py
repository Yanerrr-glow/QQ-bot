"""语法检查：把 .py 全编译一遍（不进任何 import，只查语法）。

为什么单独一个脚本而不是 powershell 循环：
在 pwsh 里写 `$out = python -c ...; if ($LASTEXITCODE ...)` 时，**赋值会覆盖
`$LASTEXITCODE`**，于是每次都误判成失败（实测被这个坑咬过两次）。
放在一个 python 进程里做，既不产生大量子进程，也没有退出码传递问题。

顺带还核两件仓库一致性的事：Dockerfile 的 COPY 要不要在 `.dockerignore` 里放行，
以及随仓库分发的 `上手自检.exe` 有没有比它的源码旧（烤进二进制的逻辑无法自动同步）。
"""

from __future__ import annotations

import pathlib
import py_compile
import re
import sys

# 与 `离线验证_桩.py` 同一条：输出被管道捕获时 Python 用系统区域编码，英文区域的
# Windows（GitHub runner）是 cp1252，打中文会 `UnicodeEncodeError` 崩掉、退出码 1。
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

# **路径从脚本自身推**：这个脚本随副本分发，写死绝对路径的话，在别人机器上
# 一个文件都查不到，却照样打印"失败 0 个"退出 0 —— 绿得毫无意义。
HERE = pathlib.Path(__file__).resolve().parent.parent
ROOTS = [HERE]
# 作者本机活项目与去敏副本并存时顺带查另一棵。候选路径**由活项目名推导**
# （工作区约定 §1.1：去敏版本放 `项目\Git-open\<名>_open\`），不写死具体项目名。
for _cand in (HERE.parent / "Git-open" / (HERE.name + "_open"),
              HERE.parent / (HERE.name + "_open")):
    try:
        if _cand.is_dir() and _cand.resolve() != HERE:
            ROOTS.append(_cand)
            break
    except OSError:
        continue

bad: list[tuple[pathlib.Path, str]] = []
total = 0
for root in ROOTS:
    if not root.exists():
        print(f"[跳过] 不存在：{root}")
        continue
    count = 0
    for p in sorted(root.rglob("*.py")):
        # `.git` 也要跳过：副本同时是 git 工作区，别去编译版本库里的东西。
        if "__pycache__" in p.parts or ".tmp_selftest" in p.parts or ".git" in p.parts:
            continue
        count += 1
        total += 1
        side = pathlib.Path(str(p) + ".syntaxcheck")
        try:
            py_compile.compile(str(p), cfile=str(side), doraise=True)
        except Exception as exc:  # noqa: BLE001
            bad.append((p, f"{type(exc).__name__}: {exc}"))
        finally:
            if side.exists():
                side.unlink()
    print(f"{root.name}: {count} 个 .py")

# ---------------------------------------------------------------- 容器构建一致性
# **这个坑踩了三次**（2026-09-25 两次、2026-09-27 一次）：新增一个"要进镜像"的脚本，
# 光在 Dockerfile 加 COPY 不够 —— 还得在 `.dockerignore` 里用 `!` 放行，
# 否则 build 报「failed to calculate checksum … not found」，**而文件明明就在**，
# 排查方向一开始就错。原来这条只写在 `.dockerignore` 的注释里（"记得改两处"），靠人记；
# 现在它是个**可执行的检查** —— 改完立刻跑，比等 build 失败快得多。
missing_allow: list[str] = []
_live = ROOTS[0]
_df, _di = _live / "Dockerfile", _live / ".dockerignore"
if _df.exists() and _di.exists():
    need = re.findall(r"^COPY _工具链/(\S+)", _df.read_text(encoding="utf-8"), re.M)
    text = _di.read_text(encoding="utf-8")
    missing_allow = [n for n in need if f"!_工具链/{n}" not in text]
    print(f"\nDockerfile 要求进镜像的 _工具链 脚本：{len(need)} 个")
    if missing_allow:
        print(f"  [X] 有 {len(missing_allow)} 个没在 .dockerignore 里放行：")
        for n in missing_allow:
            print(f"      !_工具链/{n}")
    else:
        print("  [OK] .dockerignore 的放行清单与 Dockerfile 一致")

# ---------------------------------------------------------------- 自检 exe 是否过期
# `上手自检.exe` 随副本分发（没装 Python 的人双击就能体检），代价是它把
# `_工具链/上手自检.py` **烤进了二进制** —— 改了源码忘了重新打包，exe 里跑的还是旧逻辑，
# 而外表完全看不出来。这里用时间戳做一个便宜的提醒。
# **只提醒、不算失败**：从 git clone 出来的仓库里所有文件时间戳都接近，
# 那种情况报"过期"就是误报，不该让验收变红。
stale_exe: list[str] = []
for _root in ROOTS:
    _src = _root / "_工具链" / "上手自检.py"
    # 产物现在落 `dist\`（走 GitHub Release，不进仓库）；旧位置也认，
    # 免得历史上留在项目根的那份漏检。
    _exes = [p for p in (_root / "dist" / "上手自检.exe",
                         _root / "上手自检.exe") if p.exists()]
    if not _src.exists() or not _exes:
        continue
    _newest = max(_exes, key=lambda p: p.stat().st_mtime)
    delta = _src.stat().st_mtime - _newest.stat().st_mtime
    if delta > 60:      # 源码比 exe 新超过一分钟
        stale_exe.append(f"{_root.name}：上手自检.py 新 {int(delta // 60)} 分钟")
if stale_exe:
    print("\n[提醒] 上手自检.exe 可能已过期，重新打包：& '.\\_工具链\\_打包上手exe.ps1'")
    for s in stale_exe:
        print("      " + s)

print(f"\n合计 {total} 个 .py，语法失败 {len(bad)} 个")
for p, why in bad:
    print(f"  [X] {p}\n      {why}")
sys.exit(1 if (bad or missing_allow) else 0)
