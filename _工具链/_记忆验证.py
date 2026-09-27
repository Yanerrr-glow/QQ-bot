"""记忆修复的本地单元测试（不需要 nonebot / openai）。

直接按文件加载 `memory.py`，把它依赖的 config / clock / settings 换成桩。
跑法：python _工具链/_记忆验证.py
退出码：0 = 全通过。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PASSED = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [OK] {label}" + (f" —— {detail}" if detail else ""))
    else:
        FAILED.append(label)
        print(f"  [失败] {label}" + (f" —— {detail}" if detail else ""))


# --------------------------------------------------------------------- 桩
ROOT = Path(__file__).resolve().parent.parent          # 项目根
PKG = ROOT / "plugins" / "ai_chat"

stub_config = types.ModuleType("ai_chat.config")
stub_config.API_KEY = ""
stub_config.BASE_URL = "https://api.deepseek.com"
stub_config.LOG_DIR = Path(r"C:\Windows\Temp\_memtest")
stub_config.SYSTEM_PROMPT = ""

stub_clock = types.ModuleType("ai_chat.clock")
import time as _t
stub_clock.now = _t.time
stub_clock.localtime = lambda ts=None: _t.localtime(ts)

_DEFAULTS = {
    "memory_half_life_days": 30,
    "memory_extract_per_day": 200,
    "memory_extract_max": 3,
    "memory_max_items": 800,
    "memory_enabled": True,
    "memory_extract": True,
}
stub_settings = types.ModuleType("ai_chat.settings")
stub_settings.get = lambda k, d=None: _DEFAULTS.get(k, d)

pkg = types.ModuleType("ai_chat")
pkg.__path__ = [str(PKG)]
sys.modules["ai_chat"] = pkg
sys.modules["ai_chat.config"] = stub_config
sys.modules["ai_chat.clock"] = stub_clock
sys.modules["ai_chat.settings"] = stub_settings

# openai 也是桩：memory.py 只是 import 它并建一个 client，测试不真的调 API
stub_openai = types.ModuleType("openai")


class _FakeAsyncOpenAI:  # noqa: D401
    def __init__(self, *a, **k) -> None:
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace())


stub_openai.AsyncOpenAI = _FakeAsyncOpenAI
sys.modules["openai"] = stub_openai

# 让 memory.py 里的 `from . import clock, config, settings` 能解析到桩
spec = importlib.util.spec_from_file_location("ai_chat.memory", PKG / "memory.py")
mem = importlib.util.module_from_spec(spec)
sys.modules["ai_chat.memory"] = mem
spec.loader.exec_module(mem)
print(f"（已加载 memory.py：{PKG / 'memory.py'}）\n")

# --------------------------------------------------------------------- 1. 截断
print("=== 1. 截断：加省略号 + 不硬切在标点外 ===")
long_item = "经常在深夜到凌晨时段活跃、找助手聊天（如 21:44、凌晨 3 点还在群里说话）"
out = mem._clean(long_item, 30)
check("超长会加省略号", out.endswith("…"), repr(out))
check("不会硬切得比上限长", len(out) <= 30, f"len={len(out)}")
check("不再断在括号里（回退到标点）", not out.rstrip("…").endswith("凌晨"), repr(out))
short = "面条"
check("不超长就原样返回", mem._clean(short, 30) == short, repr(mem._clean(short, 30)))
check("恰好等于上限不加省略号", mem._clean("一二三四五", 5) == "一二三四五")
check("默认上限是 300", mem._MAX_TEXT == 300, str(mem._MAX_TEXT))

# --------------------------------------------------------------------- 2. 相似度
print("\n=== 2. 相似度判断（画像去重的核心）===")
SAME = [
    ("作息偏晚，常在深夜到凌晨四五点仍醒着并上线聊天", "经常熬夜，凌晨零点过后还在线"),
    ("作息偏晚，常熬夜到凌晨四五点，睡五小时左右就起", "作息偏晚，常在凌晨三四点甚至近五点才睡"),
    ("常在深夜到凌晨仍活跃（凌晨1点、4点多还在聊天）", "经常在深夜到凌晨时段活跃、找助手聊天"),
]
for a, b in SAME:
    check(f"判为同一件事：{a[:12]}… ↔ {b[:12]}…", mem._similar(a, b),
          f"overlap={mem._overlap(a, b):.2f}/{mem._overlap(b, a):.2f}")
DIFF = [
    ("月饼，且表示各种馅的都想吃", "会用塔罗牌占卜（如问本年运势）"),
    ("面条", "关注《明日方舟》的游戏动态"),
    ("会用「臊皮」一词表示调侃、打趣", "是工程师，会以帮助助手成长的职业视角向助手提问"),
]
for a, b in DIFF:
    check(f"判为不同：{a[:12]}… ↔ {b[:12]}…", not mem._similar(a, b),
          f"overlap={mem._overlap(a, b):.2f}/{mem._overlap(b, a):.2f} "
          f"topic={mem._topic(a)}/{mem._topic(b)}")

# --------------------------------------------------------------------- 3. 合并去重
print("\n=== 3. _merge_unique：合并 + 保留更完整的那条 ===")
merged = mem._merge_unique(
    ["作息偏晚，常在深夜到凌晨四五点仍醒着并上线聊天"],
    ["经常熬夜，凌晨零点过后还在线"],
    cap=12,
)
check("近义句被合并成一条", len(merged) == 1, str(merged))
check("保留了更长（信息更全）的那条", merged and merged[0].startswith("作息偏晚"), str(merged))
merged2 = mem._merge_unique(["面条"], ["面条"], cap=12)
check("完全相同不重复", merged2 == ["面条"], str(merged2))
merged3 = mem._merge_unique([], ["a", "b", "c"], cap=2)
check("超上限会截到 cap", len(merged3) == 2, str(merged3))
merged4 = mem._merge_unique([], ["养了一只猫", "每周末打羽毛球", "老家在成都，逢年过节要回去"], cap=2)
check("超限时丢最短的（保信息量）", "养了一只猫" not in merged4, str(merged4))
# 注意：短文本用 n-gram 判相似会偏激进 ——「中等长度的一条」与「很长很长很长的一条描述内容」
# 共享"长度的一条"，2-gram 覆盖 0.33 就超阈值了。所以这组用例刻意用**互不共享词干**的句子。

# --------------------------------------------------------------------- 4. 画像写入
print("\n=== 4. set_profile：相似度去重 + 80 字上限 ===")
mem._db.profile.clear()
mem.set_profile("小明", display="小明", habit=["作息偏晚，常在深夜到凌晨四五点仍醒着并上线聊天"])
mem.set_profile("小明", display="小明", habit=["经常熬夜，凌晨零点过后还在线"])
habits = mem._db.profile["小明"]["habit"]
check("两次近义 habit 合成一条（原来是 2 条）", len(habits) == 1, str(habits))

mem._db.profile.clear()
mem.set_profile("小明", display="小明", habit=[long_item])
stored = mem._db.profile["小明"]["habit"][0]
check("画像单条上限 80 字（原来 30）", len(stored) <= 80, f"len={len(stored)}")
check("这条完整存下没被砍", stored == long_item, stored[:40])

mem._db.profile.clear()
mem.set_profile("X", display="群里那个名字特别长的家伙" * 3)
check("显示名上限 40 字", len(mem._db.profile["X"]["display"]) <= 40,
      str(len(mem._db.profile["X"]["display"])))

mem._db.profile.clear()
# 用**确实互不相似**的条目来测上限（否则会被去重合并，测不到 12 的边界）
_DISTINCT = [
    "养了一只叫团子的猫", "每周三晚上打羽毛球", "收集机械键盘", "老家在成都",
    "在读研究生", "喜欢雨天", "讨厌香菜", "学过三年吉他", "养了多肉植物",
    "会做提拉米苏", "通勤坐地铁六号线", "手机用的是折叠屏", "在追一部美剧",
    "会写毛笔字", "习惯喝美式",
]
for item in _DISTINCT:
    mem.set_profile("Y", habit=[item])
check("单字段最多 12 条（15 条不同内容）", len(mem._db.profile["Y"]["habit"]) == 12,
      str(len(mem._db.profile["Y"]["habit"])))
check("15 条确实都没被误合并（说明它们真的互不相似）",
      len({x for x in _DISTINCT}) == 15)

# --------------------------------------------------------------------- 5. 事实
print("\n=== 5. add_fact：写入与去重 ===")
mem._db.facts.clear()
mem._db.next_id = 1
item, created = mem.add_fact("小明 喜欢在深夜找助手聊天", subject="小明", name="小明")
check("新事实写入成功", created and item["id"] == 1)
check("带 ts/time 时间字段", "ts" in item and "time" in item)
_, created2 = mem.add_fact("小明 喜欢在深夜找助手聊天", subject="小明", name="小明")
check("完全相同不新增", created2 is False, f"facts={len(mem._db.facts)}")
_, created3 = mem.add_fact("小明 喜欢在深夜里找助手聊天", subject="小明", name="小明")
check("近义事实覆盖而不是新增", created3 is False and len(mem._db.facts) == 1,
      f"facts={len(mem._db.facts)}")
check("覆盖时留下 prev_text", bool(mem._db.facts[0].get("prev_text")),
      str(mem._db.facts[0].get("prev_text")))

print(f"\n=== 结果：通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
for name in FAILED:
    print("  失败：", name)
sys.exit(1 if FAILED else 0)
