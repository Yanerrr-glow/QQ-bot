"""对话租约的离线验证（纯逻辑，不连网、不调模型）。

用「按文件直接加载 + 桩 settings/config」的方式跑，理由与 fetch自测.py 一致：
`ai_chat/__init__.py` 会拉 nonebot driver，整包导入在无 nonebot 的机器上必失败。
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time
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


# ---- 桩：attention 只依赖 config / settings / openai ----------------------
# 本机可能没有 openai（`.venv` 不一定在），所以按 `离线验证_桩.py` 的做法
# 往 sys.modules 里塞一个最小假模块 —— 租约逻辑本就一行模型调用都不碰。
_openai = types.ModuleType("openai")


class _AsyncOpenAI:
    def __init__(self, *a, **k):
        pass


_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = _openai

cfg = types.ModuleType("ai_chat.config")
cfg.API_KEY = ""
cfg.BASE_URL = "https://example.invalid"
cfg.LOG_DIR = __import__("pathlib").Path(os.path.join(os.environ.get("TEMP", "/tmp"), "dsh_lease_test"))
cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
for f in cfg.LOG_DIR.glob("attention_state.json*"):
    f.unlink()

_vals = {
    "attention_enabled": True,
    "attention_window": 600,
    "attention_initial": 0.7,
    "attention_threshold": 0.8,
    "attention_interval": 20,
    "attention_min_chars": 6,
    "attention_reply_cooldown": 120,
    "attention_max_evals": 10,
    "attention_lease_enabled": True,
    "attention_lease_seconds": 180,
    "attention_lease_max_turns": 12,
    "attention_lease_anyone": False,
    "attention_lease_persist": True,
    "model": "stub",
}
st = types.ModuleType("ai_chat.settings")
st.get = lambda k, d=None: _vals.get(k, d)
st.set_value = lambda k, v: _vals.__setitem__(k, v)

sys.modules["ai_chat"] = types.ModuleType("ai_chat")
sys.modules["ai_chat.config"] = cfg
sys.modules["ai_chat.settings"] = st

spec = importlib.util.spec_from_file_location("ai_chat.attention", os.path.join(PKG, "attention.py"))
att = importlib.util.module_from_spec(spec)
sys.modules["ai_chat.attention"] = att
spec.loader.exec_module(att)

CONV = "g12345"
UID_A = "111"
UID_B = "222"
MASTER = "999"

print("-- 1. 没有租约时不该放行 --")
check("未授予时 check 为假", att.lease_check(CONV, UID_A, is_master=False, text="在吗") is False)
check("未授予时 peek 为假", att.lease_peek(CONV, UID_A, is_master=False, text="在吗") is False)

print("-- 2. 授予后本人追问放行（这就是「连续问答」） --")
att.lease_grant(CONV, UID_A)
check("peek 放行本人", att.lease_peek(CONV, UID_A, is_master=False, text="第二个问题") is True)
check("check 放行本人", att.lease_check(CONV, UID_A, is_master=False, text="第二个问题") is True)

print("-- 3. 默认不认别人（防话痨） --")
check("peek 不放行他人", att.lease_peek(CONV, UID_B, is_master=False, text="我也说一句") is False)
check("check 不放行他人", att.lease_check(CONV, UID_B, is_master=False, text="我也说一句") is False)

print("-- 4. 纯图片/空文本不续期 --")
check("空文本不放行", att.lease_check(CONV, UID_A, is_master=False, text="   ") is False)
check("空文本 peek 不放行", att.lease_peek(CONV, UID_A, is_master=False, text="") is False)

print("-- 5. 主人可以接任何租约 --")
check("主人放行", att.lease_check(CONV, MASTER, is_master=True, text="我问一句") is True)

print("-- 6. lease_anyone 打开后谁都能接 --")
st.set_value("attention_lease_anyone", True)
check("打开后他人放行", att.lease_check(CONV, UID_B, is_master=False, text="我也说一句") is True)
st.set_value("attention_lease_anyone", False)

print("-- 7. 轮数上限 --")
att.clear_lease(CONV)
att.lease_grant(CONV, UID_A)
st.set_value("attention_lease_max_turns", 3)
for i in range(3):
    att.lease_commit(CONV)
check("用满 3 轮后 check 变假", att.lease_check(CONV, UID_A, is_master=False, text="第四问") is False)
check("用满后租约被清掉", att.lease_state(CONV) == {})
st.set_value("attention_lease_max_turns", 12)

print("-- 8. 超时 --")
att.clear_lease(CONV)
st.set_value("attention_lease_seconds", 30)
att.lease_grant(CONV, UID_A)
st_ = att.lease_state(CONV)
check("刚授予时剩余时间 > 0", st_ and st_["left_seconds"] > 0)
# 手工把到期时间拨到过去，模拟超时（不改系统时钟）
att._lease[CONV].expires_at = time.time() - 1
check("超时后 check 变假", att.lease_check(CONV, UID_A, is_master=False, text="晚了") is False)
check("超时后 peek 变假", att.lease_peek(CONV, UID_A, is_master=False, text="晚了") is False)
st.set_value("attention_lease_seconds", 180)

print("-- 9. commit 会续期（每一轮重新计时） --")
att.clear_lease(CONV)
att.lease_grant(CONV, UID_A)
before = att._lease[CONV].expires_at
time.sleep(0.05)
att.lease_commit(CONV)
check("续期后到期时间推后", att._lease[CONV].expires_at > before)
check("轮数记为 1", att.lease_state(CONV)["turns"] == 1)

print("-- 10. 落盘与恢复（重启不丢） --")
att.clear_lease(CONV)
att.lease_grant(CONV, UID_A)
att.lease_commit(CONV)
path = cfg.LOG_DIR / "attention_state.json"
check("状态文件已写出", path.exists())
# 模拟重启：清内存 + 重新加载模块
att._lease.clear()
att._lease_loaded = False
att._load_lease()
check("重启后租约仍在", CONV in att._lease)
check("重启后轮数保留", att.lease_state(CONV).get("turns") == 1)
check("重启后仍放行本人", att.lease_check(CONV, UID_A, is_master=False, text="继续") is True)

print("-- 11. 过期条目在加载时被丢弃（不静默） --")
att._lease[CONV].expires_at = time.time() - 5
att._save_lease()
att._lease.clear()
att._lease_loaded = False
att._load_lease()
check("过期条目未恢复", CONV not in att._lease)

print("-- 12. 开关关掉时全部失效 --")
att.lease_grant(CONV, UID_A)
st.set_value("attention_lease_enabled", False)
check("关掉后 peek 为假", att.lease_peek(CONV, UID_A, is_master=False, text="在吗") is False)
check("关掉后 check 为假", att.lease_check(CONV, UID_A, is_master=False, text="在吗") is False)
st.set_value("attention_lease_enabled", True)

print("-- 13. persist 关掉时不写盘 --")
att.clear_lease(CONV)
path.unlink(missing_ok=True)
st.set_value("attention_lease_persist", False)
att.lease_grant(CONV, UID_A)
check("persist=False 时不产生状态文件", not path.exists())
check("但内存里仍有租约", att.lease_peek(CONV, UID_A, is_master=False, text="在吗") is True)
st.set_value("attention_lease_persist", True)
att.clear_lease(CONV)

print("-- 14. 租约与注意力互不干扰 --")
att.lease_grant(CONV, UID_A)
att.focus(CONV, "某个话题")
check("授予租约不会凭空创建 focus", CONV in att._focus)
att.relax(CONV)   # 回复末尾会调它，把注意力减半
check("relax 之后租约依然有效（关键：问答不被 relax 打断）",
      att.lease_check(CONV, UID_A, is_master=False, text="接着问") is True)
check("relax 确实把注意力减半了", abs(att._focus[CONV].value - 0.35) < 1e-9,
      att._focus[CONV].value)

print("-- 15. 回复后的注意力静默期（与租约分工）--")
st.set_value("attention_reply_cooldown", 120)
_ok, _v = asyncio.run(att.should_speak(CONV, "接着这个话题说点什么"))
check("静默期内 should_speak 为假（该由租约接管）", _ok is False, _v)
check("state 里能看到静默剩余时间",
      att.state(CONV).get("muted_seconds", 0) > 100,
      att.state(CONV))
st.set_value("attention_reply_cooldown", 0)

print("-- 16. 单次聚焦的评估次数上限 --")
att.clear(CONV)
st.set_value("attention_interval", 0)
st.set_value("attention_min_chars", 1)
st.set_value("attention_max_evals", 3)

async def _fake_score(topic, text):
    return 1.0

att.score_relevance = _fake_score
att.focus(CONV, "一个足够长的话题用于触发评估")
for i in range(3):
    asyncio.run(att.should_speak(CONV, "第 %d 条相关发言内容" % i))
check("达到上限后 evaluated 记为 3", att.state(CONV).get("evaluated", 0) == 3, att.state(CONV))
_ok2, _ = asyncio.run(att.should_speak(CONV, "第 4 条相关发言内容"))
check("超上限后结束聚焦（不再评估）", _ok2 is False)
check("超上限后 focus 已被清掉", CONV not in att._focus)
st.set_value("attention_max_evals", 10)
st.set_value("attention_interval", 20)
st.set_value("attention_min_chars", 6)

print("-- 17. 静默期不消耗评估额度 --")
att.clear(CONV)
st.set_value("attention_reply_cooldown", 300)
att.focus(CONV, "另一个足够长的话题用于验证静默不扣额度")
before_ev = att.state(CONV)["evaluated"]
asyncio.run(att.should_speak(CONV, "静默期里的一条发言"))
check("静默期内 evaluated 不变", att.state(CONV)["evaluated"] == before_ev,
      att.state(CONV))
st.set_value("attention_reply_cooldown", 120)
att.clear(CONV)

print()
print("=== 结果：通过 %d 项，失败 %d 项 ===" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  失败: " + f)
sys.exit(1 if FAILED else 0)
