"""表层人设落在 data/ 的验证（纯逻辑、不连网、不调模型）。

为什么需要它：表层是**唯一会被自动迭代写入**的一层，而它原来躺在镜像里
（`Dockerfile` 的 `COPY persona_surface.txt ./`）—— 每次重建都会用"本机那份"
把"线上学到的"顶掉，而且**不报错**。2026-09-26 找现场时才发现
（服务器 surface 49 行、本地 48 行，多出来的正是一条迭代成果）。

修法 A：读写都挪到 `data/`（卷内），镜像里那份只作**首次播种**的模板。
本脚本守的就是这条不变量：**播种幂等、且绝不覆盖已有内容**。
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parent
PKG = PROJ / "plugins" / "ai_chat"
# 自定位回退：脚本被拷到别处（例如容器里的 /tmp）时，按自身位置推出来的 PROJ 是错的
# （会推成 `/`，于是找不到 persona_*.txt）。允许用参数指定，或退到容器标准路径 `/app`。
if len(sys.argv) > 1:
    PROJ = pathlib.Path(sys.argv[1]).resolve()
    PKG = PROJ / "plugins" / "ai_chat"
elif not (PROJ / "persona_surface.txt").is_file() and (pathlib.Path("/app") / "persona_surface.txt").is_file():
    PROJ = pathlib.Path("/app")
    PKG = PROJ / "plugins" / "ai_chat"

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  [OK] " + name)
    else:
        FAILED.append(name)
        print("  [FAIL] " + name + ((" —— " + str(detail)) if detail else ""))


# ---- 造一个干净的项目根：模板放根目录，LOG_DIR 指向临时 data/ ----
root = pathlib.Path(tempfile.mkdtemp(prefix="dsh_surface_"))
(root / "data").mkdir()
for f in ("persona_surface.txt", "persona_base.txt", "persona_forbidden.txt"):
    shutil.copy(PROJ / f, root / f)

sys.modules["ai_chat"] = types.ModuleType("ai_chat")
st = types.ModuleType("ai_chat.settings")
st.get = lambda k, d=None: d
sys.modules["ai_chat.settings"] = st

src = (PKG / "config.py").read_text(encoding="utf-8")
src = src.replace("_ROOT = Path(__file__).resolve().parent.parent.parent",
                  '_ROOT = pathlib.Path(r"%s")' % root)
src = src.replace("from nonebot import get_driver",
                  "def get_driver():\n    raise RuntimeError('stub')")
src = src.replace("_cfg = get_driver().config", "_cfg = types.SimpleNamespace()")
if "import types" not in src:
    src = src.replace("import logging", "import logging\nimport types", 1)

mod = types.ModuleType("ai_chat.config")
mod.__dict__.update({"__file__": str(PKG / "config.py"), "pathlib": pathlib, "types": types})
sys.modules["ai_chat.config"] = mod
try:
    exec(compile(src, "config.py", "exec"), mod.__dict__)
except Exception as exc:  # noqa: BLE001
    print("加载 config 失败:", type(exc).__name__, exc)
    sys.exit(1)
c = sys.modules["ai_chat.config"]

print("-- 1. 路径归属 --")
check("读写路径在 data/ 内（卷内，跨重建保留）",
      c.surface_file_path().parent == root / "data", c.surface_file_path())
check("模板路径在项目根（只作播种用）",
      c.surface_seed_path().parent == root, c.surface_seed_path())
check("模块导入期不炸（`SYSTEM_PROMPT = compose_prompt()` 会提前调它）",
      isinstance(c.SYSTEM_PROMPT, str))

print("-- 2. 首次启动：播种 --")
msg = c.seed_surface()
check("播种到 data/", c.surface_file_path().exists(), msg)
check("播种是**字节级**搬运（不引入 CRLF↔LF 差异）",
      c.surface_file_path().read_bytes() == c.surface_seed_path().read_bytes())
check("读得到内容", len(c.load_surface()) > 0)

print("-- 3. 核心不变量：自我学习不被重建覆盖 --")
learned = c.load_surface() + "\n- 【学到的】不要在每条回复里都追问。\n"
c.surface_file_path().write_text(learned, encoding="utf-8")
check("写入后读到的是学到的那份", "【学到的】" in c.load_surface())
msg2 = c.seed_surface()          # 模拟"重建容器"
check("重建后再播种：学习成果仍在（这就是这条修复的全部意义）",
      "【学到的】" in c.load_surface(), msg2)
check("日志明说未覆盖", ("未覆盖" in msg2) or ("已存在" in msg2), msg2)

print("-- 4. 边界：卷被清空 / 关掉这一层 --")
c.surface_file_path().unlink()
c.seed_surface()
check("卷被清空后能重新播种（退回模板）", c.surface_file_path().exists())
c._SURFACE_CONFIGURED = ""
check("配置为空串时不读（返回空）", c.load_surface() == "")
check("配置为空串时播种不抛且给出说明", "关闭" in c.seed_surface())

shutil.rmtree(root, ignore_errors=True)
print()
print("=== 结果：通过 %d 项，失败 %d 项 ===" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  失败: " + f)
sys.exit(1 if FAILED else 0)
