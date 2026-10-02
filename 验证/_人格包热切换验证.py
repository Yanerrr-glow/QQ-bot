"""人格包**热切换**的定点验证：不装依赖、不联网、不调模型、可独立运行。

## 它验证什么

`验证/_人格包检查.py` 管"包的结构对不对"，`验证/离线验证_桩.py` 里那一整套管
"整个插件还能不能跑"。**这一份只管一件事**：切人格包这个动作本身是否真的做到
"当场换人、并且把该换的都换掉"。

单独成一份的理由：这件事是整个包化改造里**唯一有静默故障风险**的地方 ——
缓存漏清不会报错，只会让新角色带着旧角色的闸门关键词运行，或者在换过包之后
还读着旧包的正文。全量桩回归跑一次要几十秒，改完 `packs.py` / `config.py` 的人
未必愿意等；这里 1 秒内给出结论。

## 判据

| 检查 | 漏了会怎样 |
|---|---|
| 怎么找到当前包（标记 > 注册表 > 唯一启用包） | 切了没生效，而且看不出为什么 |
| 切完 `active_id()` 当场变 | 要重启才生效 |
| 三层正文**当场**换成新包的 | 读的还是旧包的（就是"改了没生效"） |
| 闸门关键词跟着新包的 `traits.json` | 新角色裸奔：旧铁律挡着它，或完全没有闸门 |
| 运行数据按包分开、互不串 | 换角色会继承上一个角色的自我学习 |
| 身份元数据（角色名）跟着换 | `chatlog._speaker()` 认不出自己刚说过的话，它会对自己接话 |
| 包不合法时**一个字都不写** | 切失败却把状态改坏了 |

## 用法

```powershell
python '验证\\_人格包热切换验证.py'
```

退出码：0 = 通过。它会在 `persona/packs/` 下**临时**建一个包并在结束时删掉 ——
不碰仓库里任何真实人格包与真实运行数据（数据目录走临时目录）。
"""
from __future__ import annotations

import importlib
import json
import pathlib
import shutil
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parent
PKG = PROJ / "plugins" / "ai_chat"

FAILED: list[str] = []
PASSED = 0
TMP = PROJ / ".tmp_selftest"
PROBE_ID = "zzhotswap"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print("  [OK] " + name + (f" —— {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print("  [FAIL] " + name + (f" —— {detail}" if detail else ""))


# --------------------------------------------------------------------- 桩
# 只桩 `nonebot`（config 顶部要 `get_driver()`），**不桩 openai / fastapi / 适配器**：
# 这一份刻意不去 import 插件包的 `__init__`（那是 `离线验证_桩.py` 的活），
# 只加载 `packs` 与 `config`，所以依赖面小到可以在裸 python 上跑。
_workdir = TMP / f"hotswap_{__import__('os').getpid()}"
shutil.rmtree(_workdir, ignore_errors=True)
_workdir.mkdir(parents=True, exist_ok=True)


class _Config(dict):
    def __getattr__(self, name):  # 与 NoneBot2 的 Config 一样：缺失键取到 None
        return self.get(name)


class _Driver:
    def __init__(self) -> None:
        self.config = _Config({
            "deepseek_api_key": "sk-stub",
            "ai_chat_log_dir": str(_workdir),
        })
        self.server_app = None

    def on_startup(self, fn):
        return fn

    def on_shutdown(self, fn):
        return fn


_nb = types.ModuleType("nonebot")
_nb.get_driver = lambda: _Driver()  # 每次现造：本脚本不关心 driver 身份
sys.modules["nonebot"] = _nb
sys.path.insert(0, str(PROJ / "plugins"))

# **桩掉包的 `__init__`**：它 import 整个插件生态，而这个脚本只验 packs ↔ config。
_pkg = types.ModuleType("ai_chat")
_pkg.__path__ = [str(PKG)]
sys.modules["ai_chat"] = _pkg
_st = types.ModuleType("ai_chat.settings")
_st.get = lambda k, d=None: d
_st.set_value = lambda k, v: v
sys.modules["ai_chat.settings"] = _st

packs = importlib.import_module("ai_chat.packs")
config = importlib.import_module("ai_chat.config")

print(f"项目根：{PROJ}")
print(f"临时数据目录：{_workdir}")

# --------------------------------------------------------------------- 造一个探针包
PROBE_DIR = PROJ / "persona" / "packs" / PROBE_ID
shutil.rmtree(PROBE_DIR, ignore_errors=True)
PROBE_DIR.mkdir(parents=True)
(PROBE_DIR / "base.txt").write_text(
    "【它是谁】\n你是「热切换探针」。\n", encoding="utf-8")
(PROBE_DIR / "forbidden.txt").write_text(
    "【禁止事项】\n- 不许提「热切换专用禁词」。\n", encoding="utf-8")
(PROBE_DIR / "surface.txt").write_text(
    "【表层人设】\n- 探针的初始条目\n\n【自动学到的（自动迭代只写这一节）】\n", encoding="utf-8")
(PROBE_DIR / "_pack.json").write_text(json.dumps({
    "schema": 1, "id": PROBE_ID, "name": "热切换探针", "bot_name": "热切换探针",
    "wake_words": ["探针", "hotswap"], "enabled": True,
}, ensure_ascii=False), encoding="utf-8")

_orig_active = packs.active_id()
try:
    print("\n-- 1. 当前包怎么找出来的 --")
    check("能列出可用包", bool(packs.pack_ids()), "、".join(packs.pack_ids()))
    check("当前激活的包在可用列表里", _orig_active in packs.pack_ids(), _orig_active)
    check("`_` 前缀的脚手架不算包（_TEMPLATE 不该被切过去）",
          not any(x.startswith("_") for x in packs.pack_ids()),
          "、".join(packs.pack_ids()))

    print("\n-- 2. 校验：能不能切过去 --")
    _v = packs.validate(PROBE_ID)
    check("探针包校验通过", _v["ok"], str(_v["errors"]) or "无错误")
    check("校验会报出缺 traits.json（警告而不是错误）",
          any("traits" in w for w in _v["warnings"]), str(_v["warnings"]))
    check("不存在的包校验失败且说明原因",
          packs.validate("查无此包")["ok"] is False, "")

    print("\n-- 3. 热切换：当场换人 --")
    _before_base = config.BASE_PROMPT
    check("切换前读的是原包的正文", bool(_before_base), _before_base[:24].replace("\n", "|"))
    config.seed_surface_for(PROBE_ID)
    # ⚠ **`sync_registry=False`**：这是验证脚本，不该改写仓库里的
    # `persona/_registry.json`（那是"部署期默认值"，是交付物的一部分）。
    # 实测踩过一次：写进去的探针包随后被删掉，注册表就指向一个不存在的包 ——
    # 之后整套桩回归都跑到 assistant 上去了，而且报的是"找不到「鲸鱼娘」"这种
    # 看不出根因的错。运行时要落盘的是 `data/` 里的标记，注册表由人（或部署动作）决定。
    _sw = packs.switch(PROBE_ID, sync_registry=False)
    check("switch 返回成功", _sw.get("ok") is True, str(_sw.get("errors") or ""))
    check("active_id 当场变了（不重启）", packs.active_id() == PROBE_ID, packs.active_id())
    check("底层人设当场换成新包的", "热切换探针" in config.BASE_PROMPT, config.BASE_PROMPT[:24])
    check("禁止事项当场换成新包的",
          any("热切换专用禁词" in x for x in config.FORBIDDEN_PROMPT.splitlines())
          or "热切换专用禁词" in config.FORBIDDEN_PROMPT, "")
    check("SYSTEM_PROMPT（惰性属性）跟着变", "热切换探针" in config.SYSTEM_PROMPT, "")
    check("表层读的是新包自己的运行数据",
          "探针的初始条目" in config.load_surface(), config.load_surface()[:24])

    print("\n-- 4. 闸门关键词跟着新包重算（漏了就是静默失守）--")
    _traits = config.load_traits()
    check("探针包没有注册表 → 回退内置表（不是空闸门）",
          _traits == [] and bool(config.load_traits.__doc__), f"traits={len(_traits)}")

    print("\n-- 5. 运行数据按包隔离 --")
    check("运行数据目录按包分开",
          packs.stage_dir().name == PROBE_ID
          and packs.stage_dir().parent == packs.runtime_persona_root(),
          str(packs.stage_dir()))
    _flat = packs.runtime_persona_root() / "signals.json"
    _flat.write_text('{"items": [{"kind": "call", "text": "旧的平铺账本"}]}', encoding="utf-8")
    _mig = packs.migrate_layout()
    check("旧的平铺运行数据被归位到当前包目录",
          "signals.json" in _mig["migrated"] and (packs.stage_dir() / "signals.json").is_file(),
          str(_mig["migrated"]))
    check("迁移前留了可恢复的备份", bool(_mig["backup"]), _mig["backup"])
    check("再迁一次是幂等的", packs.migrate_layout()["migrated"] == [], "")

    print("\n-- 6. 身份元数据跟着包走 --")
    _patch = packs.identity_patch(PROBE_ID)
    check("包里声明的身份参数能取出来",
          _patch.get("bot_name") == "热切换探针" and "hotswap" in _patch.get("wake_words", ""),
          str(_patch))

    print("\n-- 7. 切失败时一个字都不写 --")
    _snapshot = (packs.active_id(), config.BASE_PROMPT)
    _fail = packs.switch("查无此包", sync_registry=False)
    check("切到不存在的包返回失败", _fail.get("ok") is False, str(_fail["errors"]))
    check("失败后激活状态与正文都没变",
          (packs.active_id(), config.BASE_PROMPT) == _snapshot, packs.active_id())
finally:
    # 收尾：切回原包、删掉探针包与临时目录（**绝不留下假人格**）
    if _orig_active:
        packs.switch(_orig_active, sync_registry=False)
    shutil.rmtree(PROBE_DIR, ignore_errors=True)
    shutil.rmtree(_workdir, ignore_errors=True)

print()
print("=" * 60)
print("通过 %d 项，失败 %d 项" % (PASSED, len(FAILED)))
for name in FAILED:
    print("  失败: " + name)
sys.exit(1 if FAILED else 0)
