"""行为闸门（behavior.py）的离线验证：纯逻辑，不连网、不调模型。

为什么需要它：2026-09-26 现场证明"**光靠人设里写一条禁令**"无效 ——
自动迭代 02:34 就写进了表层人设，之后照样反复提时间；A/B 各跑 12 次量不出差异。
闸门把"反复"变成代码能数清的事实，所以它本身必须有回归守卫。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PKG = os.path.join(PROJ, "plugins", "ai_chat")

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


sys.modules["ai_chat"] = types.ModuleType("ai_chat")
spec = importlib.util.spec_from_file_location("ai_chat.behavior", os.path.join(PKG, "behavior.py"))
beh = importlib.util.module_from_spec(spec)
sys.modules["ai_chat.behavior"] = beh
spec.loader.exec_module(beh)

C = "g_test"

print("-- 1. 没有历史时不产生任何指令（不占 prompt）--")
beh.clear(C)
check("空历史返回空串", beh.suppress_note(C) == "")

print("-- 2. 单次提时间不算「反复」 --")
beh.note_reply(C, "都两点了，你怎么还不睡？")
check("只提 1 次不提闸门", beh.suppress_note(C) == "")
check("stats 计数正确", beh.stats(C)["time"] == 1, beh.stats(C))

print("-- 3. 连续两条提时间 → 触发 --")
beh.note_reply(C, "这么晚了你还不睡啊")
n = beh.suppress_note(C)
check("触发抑制指令", "不要再提时间" in n, n[:80])
check("指令里说明了次数（可核对）", "已经提了" in n, n[:120])

print("-- 4. 追问：只认追问句，不认普通问句结尾 --")
beh.clear(C)
beh.note_reply(C, "今天天气不错啊，你那边呢？")
beh.note_reply(C, "那本书我看完了，挺好的？")
check("普通问句结尾**不**触发（否则命中率天然 35%，闸门无意义）",
      beh.suppress_note(C) == "", beh.stats(C))
beh.clear(C)
beh.note_reply(C, "你问这个想干嘛")
beh.note_reply(C, "你到底想问什么")
n2 = beh.suppress_note(C)
check("连续追问 → 触发", "不要追问" in n2, n2[:100])

print("-- 5. 只算最近 3 条（更早的不该影响判定）--")
beh.clear(C)
beh.note_reply(C, "都两点了")          # 早，应被挤出窗口
beh.note_reply(C, "今天吃什么")
beh.note_reply(C, "好啊")
beh.note_reply(C, "行")
check("窗口外的那条不计入", beh.suppress_note(C) == "", beh.stats(C))

print("-- 6. 两类同时上头 → 两条指令都给 --")
beh.clear(C)
beh.note_reply(C, "都两点了，你问这个想干嘛")
beh.note_reply(C, "这么晚了你到底想问什么")
both = beh.suppress_note(C)
check("时间与追问两条都在", "不要再提时间" in both and "不要追问" in both, both[:160])

print("-- 7. clear 之后回到安静 --")
beh.clear(C)
check("clear 后为空", beh.suppress_note(C) == "")

print("-- 8. 空文本不记账 --")
beh.clear(C)
beh.note_reply(C, "")
beh.note_reply(C, "   ")
check("空回复不占窗口", beh.stats(C)["window"] == 0, beh.stats(C))

print()
print("=== 结果：通过 %d 项，失败 %d 项 ===" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  失败: " + f)
sys.exit(1 if FAILED else 0)
