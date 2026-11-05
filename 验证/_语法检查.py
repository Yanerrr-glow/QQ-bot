"""语法检查：把 .py 全编译一遍（不进任何 import，只查语法）。

为什么单独一个脚本而不是 powershell 循环：
在 pwsh 里写 `$out = python -c ...; if ($LASTEXITCODE ...)` 时，**赋值会覆盖
`$LASTEXITCODE`**，于是每次都误判成失败（实测被这个坑咬过两次）。
放在一个 python 进程里做，既不产生大量子进程，也没有退出码传递问题。

顺带还核三件仓库一致性的事：Dockerfile 的 COPY 要不要在 `.dockerignore` 里放行、
导出与核对两份发布脚本的清单是否逐字一致，以及随仓库分发的 `上手自检.exe`
有没有比它的源码旧（烤进二进制的逻辑无法自动同步）。
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
#
# 2026-10-02 起：进镜像的那组是**顶层 `验证\` 整目录 COPY**，`_工具链\` 整组不进镜像。
# 于是"两处都要改"这条基本消失了。这个检查改成两条更硬的：
#   ① Dockerfile 里 `COPY` 的路径**必须在磁盘上真实存在**（防手误写错目录名）；
#   ② `_工具链\` 必须仍在 `.dockerignore` 里被排除（防哪天顺手把它放进镜像）。
missing_allow: list[str] = []
_live = ROOTS[0]
_df, _di = _live / "Dockerfile", _live / ".dockerignore"


def _read_text_any(path: pathlib.Path) -> str:
    """按 UTF-8 读，读不动再按 GBK 读。

    `.dockerignore` 是 GBK 编码的（既有状况：早期用 PS 5.1 写过），直接按 UTF-8
    读会 `UnicodeDecodeError` 把这个检查整个打断 —— 实测踩过。文件编码不该由
    "编辑器的默认值"决定，这里多试几种即可。
    """
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


if _df.exists() and _di.exists():
    dockerfile = _df.read_text(encoding="utf-8")
    ignore_text = _read_text_any(_di)
    copied = re.findall(r"^COPY\s+(\S+)\s+(\S+)", dockerfile, re.M)
    # ① COPY 的源路径是否存在（排除多源 COPY 与通配）
    for src, _dst in copied:
        if any(ch in src for ch in "*?") or src.startswith("--"):
            continue
        if not (_live / src).exists():
            missing_allow.append(f"COPY 的源不存在：{src}")
    # ② `_工具链\` 仍是「默认不进镜像」，只有白名单里的两个脚本被放行 ——
    #    2026-10-29 起：`诊断状态.py` 要在容器里能跑、`_修复画像重复.py` 被离线验证加载。
    #    检查同时盯住 `.dockerignore` 的三条规则与 Dockerfile 的 COPY，少一处就报。
    _allow = ("_工具链/启动/诊断状态.py", "_工具链/维护/_修复画像重复.py")
    if not re.search(r"^_工具链/\*\s*$", ignore_text, re.M):
        missing_allow.append("`.dockerignore` 里缺少 `_工具链/*` 这条排除（宿主脚本默认不进镜像）")
    for _rel in _allow:
        if not re.search(r"^!" + re.escape(_rel) + r"\s*$", ignore_text, re.M):
            missing_allow.append(f"`.dockerignore` 里缺少 `!{_rel}` 放行")
        if not any(src.replace("\\", "/") == _rel for src, _dst in copied):
            missing_allow.append(f"Dockerfile 里缺少 `COPY {_rel}`")
    print(f"\nDockerfile 的 COPY 条目：{len(copied)} 条；`_工具链/` 放行白名单："
          f"{len(_allow)} 个 —— {'一致' if not missing_allow else '有缺失'}")
    if missing_allow:
        print(f"  [X] {len(missing_allow)} 处不一致：")
        for item in missing_allow:
            print(f"      {item}")
    else:
        print("  [OK] COPY 路径都存在，且 `_工具链/` 只放行了白名单里的脚本")

# ------------------------------------------------- 发布脚本的两处清单一致性
# 导出（`_导出开源版.ps1`）与核对（`_核对脱敏对齐.ps1`）各有一份 `$SkipPrefix` /
# `$Exts` / `$NameFiles` / `$JsonlAllow`。**两份不是共享文件**（核对脚本本身不随副本分发，
# 因为它含"真群号 → 占位值"的映射表），所以历史上漂移过三次：
#   条目数 37 vs 38（2026-09-26）、tripoca 只登记一边（2026-10-02）、
#   `.jsonl` 两边都没有（2026-11-03 才补上 —— 结果是回放集既不导出也不核对，
#   核对照样报"完全一致"）。
# 这里只盯**必须逐字一致**的三项；逐文件的 `$Skip` 表**不比对**（那两张表本来就该不同：
# 导出侧要写理由，核对侧是另一份措辞）。
publish_bad: list[str] = []
_export = _live / "_工具链" / "发布" / "_导出开源版.ps1"
_check = _live / "_工具链" / "发布" / "_核对脱敏对齐.ps1"


def _ps_array(text: str, name: str) -> list[str]:
    """抠出 `$name = @('a','b')` 里的字符串项（跨行也认）。"""
    m = re.search(r"^\$" + re.escape(name) + r"\s*=\s*@\((.*?)\)\s*$", text, re.M | re.S)
    return re.findall(r"'([^']*)'", m.group(1)) if m else []


if _export.exists() and _check.exists():
    _e_text = _export.read_text(encoding="utf-8")
    _c_text = _check.read_text(encoding="utf-8")
    for _name in ("SkipPrefix", "JsonlAllow"):
        _a, _b = _ps_array(_e_text, _name), _ps_array(_c_text, _name)
        if not _a:
            publish_bad.append(f"`_导出开源版.ps1` 里找不到 `${_name}`（改名或删了？）")
        elif sorted(_a) != sorted(_b):
            publish_bad.append(f"`${_name}` 两边不一致：导出 {_a} / 核对 {_b}")
    # `$Exts` 在核对脚本里是内联字面量而不是变量，单独抠一次
    _exts_export = _ps_array(_e_text, "Exts")
    _inline = re.search(r"\.Extension -in @\(([^)]*)\)", _c_text)
    _exts_check = re.findall(r"'([^']*)'", _inline.group(1)) if _inline else []
    if sorted(_exts_export) != sorted(_exts_check):
        publish_bad.append(f"扩展名清单两边不一致：导出 {_exts_export} / 核对 {_exts_check}")
    print(f"\n发布脚本清单：$SkipPrefix {len(_ps_array(_e_text, 'SkipPrefix'))} 条、"
          f"$JsonlAllow {len(_ps_array(_e_text, 'JsonlAllow'))} 条、"
          f"$Exts {len(_exts_export)} 项 —— {'一致' if not publish_bad else '有差异'}")
    if publish_bad:
        for item in publish_bad:
            print(f"  [X] {item}")
    else:
        print("  [OK] 导出与核对的目录/扩展名/测试数据清单逐字一致")

# ------------------------------------------------- 两份 ACTS 必须逐项一致
# `scene.ACTS`（规则层）与 `context_analysis.ACTS`（深判）各自维护一份枚举。
# 2026-11-03 发现它们不一致：规则层有「告别」没有，深判有「不知道」规则层没有。
# 后果不报错 —— 深判回「告别」会被 `_split_choice` 打回 unknown，只是"判不出来"。
# 约定：深判那份 = 规则层那份 + 「不知道」（模型的自我报告，不是场景）。
acts_bad: list[str] = []
_scene_py = _live / "plugins" / "ai_chat" / "scene.py"
_ctx_py = _live / "plugins" / "ai_chat" / "context_analysis.py"


def _acts(path: pathlib.Path, name: str) -> list[str]:
    """抠出 `ACTS = (...)` / `ACTS: tuple[str, ...] = (...)` 里的字符串项。"""
    src = path.read_text(encoding="utf-8")
    m = re.search(r"^ACTS\s*(?::\s*tuple\[str, \.\.\.\])?\s*=\s*\((.*?)\)", src, re.M | re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


if _scene_py.exists() and _ctx_py.exists():
    _a, _b = _acts(_scene_py, "ACTS"), _acts(_ctx_py, "ACTS")
    _extra = set(_b) - set(_a)
    _missing = set(_a) - set(_b)
    if _missing:
        acts_bad.append(f"深判的 ACTS 少了规则层的这些 act：{sorted(_missing)}")
    if _extra - {"不知道"}:
        acts_bad.append(f"深判的 ACTS 多了规则层没有的 act：{sorted(_extra - {'不知道'})}")
    if "不知道" not in _b:
        acts_bad.append("深判的 ACTS 里没有「不知道」（模型的自我报告要能表达）")
    print(f"\nACT 枚举：规则层 {len(_a)} 项、深判 {len(_b)} 项 —— "
          f"{'一致（深判多「不知道」）' if not acts_bad else '有差异'}")
    if acts_bad:
        for item in acts_bad:
            print(f"  [X] {item}")
    else:
        print("  [OK] 两份枚举逐项对齐，深判只多一个「不知道」")

# ------------------------------------------------- act → 场景名 的映射必须齐
# 检索侧拿 `scene:` 标签去匹配示例，而 act 要经 `context._LEGACY_SCENE` 映射成场景名。
# 漏一条的后果是**静默的**：那个 act 永远召不到语料，不报错、只是"没有示例"。
# （2026-11-03 补：`_人格结构v2检查.py` 的 `ACT_TO_SCENE` 与 `context.py` 的映射必须一致）
map_bad: list[str] = []
_scene_src = (_live / "plugins" / "ai_chat" / "scene.py").read_text(encoding="utf-8")
_ctx_src = (_live / "plugins" / "ai_chat" / "context.py").read_text(encoding="utf-8")
_chk_src = (_live / "验证" / "_人格结构v2检查.py").read_text(encoding="utf-8")

_m = re.search(r"^ACTS\s*(?::\s*tuple\[str, \.\.\.\])?\s*=\s*\((.*?)\)", _scene_src, re.M | re.S)
_acts = re.findall(r'"([^"]+)"', _m.group(1)) if _m else []
_m2 = re.search(r"^_LEGACY_SCENE\s*=\s*\{(.*?)\}", _ctx_src, re.M | re.S)
_runtime_map = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', _m2.group(1))) if _m2 else {}
_m3 = re.search(r"^ACT_TO_SCENE\s*=\s*\{(.*?)\}", _chk_src, re.M | re.S)
_check_map = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', _m3.group(1))) if _m3 else {}
_m4 = re.search(r"^SCENE_TAGS\s*=\s*\{(.*?)\}", _chk_src, re.M | re.S)
_scene_tags = set(re.findall(r'"([^"]+)"', _m4.group(1))) if _m4 else set()

if not _acts:
    map_bad.append("抠不出 `scene.py` 的 ACTS")
if _runtime_map != _check_map:
    map_bad.append(f"act→场景名 两份映射不一致：context {_runtime_map} / 检查脚本 {_check_map}")
_unknown = {v for v in _runtime_map.values() if _scene_tags and v not in _scene_tags}
if _unknown:
    map_bad.append(f"映射到的场景名不在 SCENE_TAGS 里：{sorted(_unknown)}")
_unmapped = [a for a in _acts if a not in _runtime_map and a not in _scene_tags]
if _unmapped:
    map_bad.append(f"这些 act 召不到任何语料（既不在映射表、也不是同名场景）：{_unmapped}")
print(f"\nact → 场景名：{len(_acts)} 个 act、映射 {len(_runtime_map)} 条 —— "
      f"{'齐了' if not map_bad else '有缺口'}")
if map_bad:
    for item in map_bad:
        print(f"  [X] {item}")
else:
    print("  [OK] 每个 act 都能落到一个已登记的场景名上")

# ---------------------------------------------------------------- 自检 exe 是否过期
# `上手自检.exe` 随副本分发（没装 Python 的人双击就能体检），代价是它把
# `_工具链/启动/上手自检.py` **烤进了二进制** —— 改了源码忘了重新打包，exe 里跑的还是旧逻辑，
# 而外表完全看不出来。这里用时间戳做一个便宜的提醒。
# **只提醒、不算失败**：从 git clone 出来的仓库里所有文件时间戳都接近，
# 那种情况报"过期"就是误报，不该让验收变红。
stale_exe: list[str] = []
for _root in ROOTS:
    # 用按名搜索而不是写死路径：脚本按用途分了子目录，写死会在重组后再踩一次（刚踩过）。
    _sources = list((_root / "_工具链").rglob("上手自检.py")) if (_root / "_工具链").exists() else []
    _exes = [p for p in (_root / "dist" / "上手自检.exe",
                         _root / "上手自检.exe") if p.exists()]
    if not _sources or not _exes:
        continue
    _src = max(_sources, key=lambda p: p.stat().st_mtime)
    _newest = max(_exes, key=lambda p: p.stat().st_mtime)
    delta = _src.stat().st_mtime - _newest.stat().st_mtime
    if delta > 60:      # 源码比 exe 新超过一分钟
        stale_exe.append(f"{_root.name}：上手自检.py 新 {int(delta // 60)} 分钟")
if stale_exe:
    print("\n[提醒] 上手自检.exe 可能已过期，重新打包：& '.\\_工具链\\发布\\_打包上手exe.ps1'")
    for s in stale_exe:
        print("      " + s)

print(f"\n合计 {total} 个 .py，语法失败 {len(bad)} 个")
for p, why in bad:
    print(f"  [X] {p}\n      {why}")
sys.exit(1 if (bad or missing_allow or publish_bad or acts_bad or map_bad) else 0)
