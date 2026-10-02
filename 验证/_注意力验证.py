"""注意力机制 + 对话租约共享的「注意力状态」的离线验证：**纯逻辑，不联网、不调模型**。

两件事：

1. **数值性质**（`AstrBot连续对话与本地注意力机制对比_2026-09-25.md` §5 的复算）：
   `_DECAY=0.5` / `_GAIN=0.55` / `_THRESHOLD_RISE=0.15` 这几个数**互相耦合** ——
   改错了只表现为"它变得话多/话少"，不会报错。这里把它们钉成回归用例：
   * 阈值随话题推进抬高：0% → 0.800，50% → 0.875，90% → 0.935；
   * `relax()` 减半后（0.35）再来一条满分只有 0.725 < 0.80 → **必须连续两条**；
   * 满分连击会被 `min(1.0, ·)` 封顶到 1.0，所以"越拖越挑"挡不住连击 ——
     真正的主约束是 `relax()`（这三条以前只写在注释里，没有守卫）。
2. **状态落盘**（2026-09-28 补）：`_focus` 以前是纯内存态，重启静默清零。
   现在与租约同文件（`data/attention_state.json`）两个键，这里验恢复/过期丢弃/不落盘的东西。

用「按文件直接加载 + 桩 settings/config/llm」的方式跑，理由与 `_对话租约验证.py` 一致：
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


def close(a, b, eps=1e-9):
    return abs(float(a) - float(b)) < eps


# ---- 桩：attention 只依赖 config / settings / llm / openai -----------------
_openai = types.ModuleType("openai")


class _AsyncOpenAI:
    def __init__(self, *a, **k):
        pass


_openai.AsyncOpenAI = _AsyncOpenAI
sys.modules["openai"] = _openai

import pathlib  # noqa: E402

TMP = pathlib.Path(os.environ.get("TEMP", "/tmp")) / "dsh_attention_test"
TMP.mkdir(parents=True, exist_ok=True)
for f in TMP.glob("attention_state.json*"):
    f.unlink()

cfg = types.ModuleType("ai_chat.config")
cfg.API_KEY = ""
cfg.BASE_URL = "https://example.invalid"
cfg.LOG_DIR = TMP

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
    "attention_persist": True,
    "model": "stub",
}
st = types.ModuleType("ai_chat.settings")
st.get = lambda k, d=None: _vals.get(k, d)
st.set_value = lambda k, v: _vals.__setitem__(k, v)

_llm = types.ModuleType("ai_chat.llm")
_llm.api_key = lambda: ""
async def _no_chat(*a, **k):
    raise RuntimeError("注意力验证不该走到模型调用")
_llm.chat = _no_chat

sys.modules["ai_chat"] = types.ModuleType("ai_chat")
sys.modules["ai_chat.config"] = cfg
sys.modules["ai_chat.settings"] = st
sys.modules["ai_chat.llm"] = _llm

spec = importlib.util.spec_from_file_location("ai_chat.attention", os.path.join(PKG, "attention.py"))
att = importlib.util.module_from_spec(spec)
sys.modules["ai_chat.attention"] = att
spec.loader.exec_module(att)

CONV = "g9001"
TOPIC = "一个足够长的话题用于触发评估"


def _score(value):
    async def _fake(topic, text):
        return value
    return _fake


def _ready(item):
    """模拟"已经过了限流与静默"：把 last_eval 与 muted_until 都拨回过去。

    `relax()` 会把 `muted_until` 推到 120 秒后（那几句交给对话租约），
    所以刚 relax 完直接评估是**评估不到**的 —— 这正是产品要的行为，
    测试要验的是"静默期过去之后"的数值。
    """
    item.last_eval = 0.0
    item.muted_until = 0.0


def _progress_to(item, frac):
    """把话题推进到活跃期的 frac（0~1）。"""
    item.started_at = time.time() - int(_vals["attention_window"]) * float(frac)


print("-- 1. 阈值随话题推进抬高（0% / 50% / 90%）--")
att.focus(CONV, TOPIC)
item = att._focus[CONV]
# 容差 2e-3：`_progress()` 是拿"现在"减 started_at 算的，取阈值时又走了几微秒，
# 严格相等会**看运气**（本地过、容器里挂 —— 2026-09-28 实测过一次）。
for frac, want in ((0.0, 0.800), (0.5, 0.875), (0.9, 0.935)):
    _progress_to(item, frac)
    got = att._effective_threshold(item)
    check("阈值 @%.0f%% = %.3f" % (frac * 100, want), abs(got - want) < 2e-3, got)

print("\n-- 2. relax 减半后一条满分不够（必须连续两条）--")
att.clear(CONV)
att.focus(CONV, TOPIC)          # value = 0.7
att.relax(CONV)                 # → 0.35
item = att._focus[CONV]
check("relax 之后是 0.35", close(item.value, 0.35), item.value)
att.score_relevance = _score(1.0)
_ready(item)
_ok, val = asyncio.run(att.should_speak(CONV, "第一条满分相关发言"))
check("一条满分 → 0.725，不触发", close(val, 0.725) and _ok is False, "%s %s" % (val, _ok))
_ready(att._focus[CONV])
_ok2, val2 = asyncio.run(att.should_speak(CONV, "第二条满分相关发言"))
check("第二条满分 → 0.9125，触发", close(val2, 0.9125) and _ok2 is True, "%s %s" % (val2, _ok2))

print("\n-- 3. 满分连击会顶到 1.0：阈值抬到 0.935 也拦不住 --")
att.clear(CONV)
att.focus(CONV, TOPIC)
item = att._focus[CONV]
item.evaluated = 0
_progress_to(item, 0.9)          # 阈值 = 0.935
att.score_relevance = _score(1.0)
for i in range(4):
    _ready(att._focus[CONV])
    _okn, valn = asyncio.run(att.should_speak(CONV, "满分连击第 %d 条" % (i + 1)))
check("连击后 value 封顶到 1.0", close(valn, 1.0), valn)
check("所以「越拖越挑」挡不住连击（主约束是 relax）", _okn is True, valn)

print("\n-- 4. 跑题一条掉多少（score=0）--")
att.clear(CONV)
att.focus(CONV, TOPIC)
item = att._focus[CONV]
att.score_relevance = _score(0.0)
_ready(item)
_ok0, val0 = asyncio.run(att.should_speak(CONV, "一条完全无关的发言"))
check("0.70 → 0.35（掉 0.35）", close(val0, 0.35), val0)

print("\n-- 5. 落盘与恢复（重启不再静默清零）--")
path = TMP / "attention_state.json"
att.clear(CONV)
path.unlink(missing_ok=True)
att.focus(CONV, TOPIC)
item = att._focus[CONV]
_ready(item)
att.score_relevance = _score(1.0)
asyncio.run(att.should_speak(CONV, "让它写一次盘"))
check("状态文件已写出", path.exists())
_value_before = att._focus[CONV].value
# 模拟重启：清内存 + 重新加载
att._focus.clear()
att._lease.clear()
att._lease_loaded = False
att._load_lease()
check("重启后话题还在", CONV in att._focus, str(att._focus))
check("重启后话题正文保留", att._focus.get(CONV) and att._focus[CONV].topic == TOPIC)
check("重启后 value 保留", CONV in att._focus and close(att._focus[CONV].value, _value_before),
      att._focus[CONV].value if CONV in att._focus else None)
check("短静默不落盘（重启后 muted_until 归零）",
      CONV in att._focus and att._focus[CONV].muted_until == 0.0,
      att._focus[CONV].muted_until if CONV in att._focus else None)

print("\n-- 6. 过期话题不恢复（超过活跃期）--")
att._focus[CONV].started_at = time.time() - int(_vals["attention_window"]) - 5
att._save_lease()
att._focus.clear()
att._lease_loaded = False
att._load_lease()
check("过期话题未恢复", CONV not in att._focus, str(att._focus))

print("\n-- 7. persist 关掉时不写盘（回到纯内存态）--")
att.clear(CONV)
path.unlink(missing_ok=True)
st.set_value("attention_persist", False)
att.focus(CONV, TOPIC)
check("attention_persist=False 时不产生状态文件", not path.exists())
check("但内存里仍有话题", CONV in att._focus)
st.set_value("attention_persist", True)

print("\n-- 8. 两个键互不覆盖（同文件两个键）--")
att.clear(CONV)
path.unlink(missing_ok=True)
att.focus(CONV, TOPIC)
att.lease_grant(CONV, "111")
import json as _json  # noqa: E402

_raw = _json.loads(path.read_text(encoding="utf-8"))
check("文件里同时有 lease 与 focus 两个键",
      "lease" in _raw and "focus" in _raw, str(list(_raw)))
att.clear_lease(CONV)
check("清掉租约不影响话题（各自独立）", CONV in att._focus)

print("\n=== 结果：通过 %d 项，失败 %d 项 ===" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  失败：", f)
sys.exit(1 if FAILED else 0)
