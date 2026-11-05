"""语境解释器（`context_analysis.py`）的离线验证：假模型，不连网。

用法：

    python 验证\\_语境解释验证.py

退出码 0 = 通过。

## 这里验的不是「模型判得准不准」

判得准不准要用 `验证/_语境回放.py --live` 对着多轮样本看。这里验的是**外壳**：

* 模型给了乱七八糟的字段，程序会不会把它洗干净；
* 模型编造「第 9 条证据」，而这轮只有 3 条，程序会不会照单全收；
* 超时、异常、空输入，会不会把 `unknown` 变成别的东西。

这三类错都不会报错，只会让判断悄悄跑偏——所以用假模型把它们钉死。
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import types

try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "ai_chat_context_analysis", ROOT / "plugins" / "ai_chat" / "context_analysis.py")
ca = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(ca)  # type: ignore[union-attr]

passed = 0
failed: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  [OK] {label}" + (f" —— {detail}" if detail else ""))
    else:
        failed.append(label)
        print(f"  [失败] {label}" + (f" —— {detail}" if detail else ""))


def fake(payload: str, *, delay: float = 0.0, boom: bool = False):
    async def _chat(messages, **kwargs):
        if delay:
            await asyncio.sleep(delay)
        if boom:
            raise RuntimeError("模型挂了")
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=payload))])
    return _chat


TURNS = [
    {"role": "user", "speaker": "群友A", "text": "今天忘带伞了，淋成落汤鸡"},
    {"role": "bot", "text": "那你赶紧回去换衣服啊"},
]


def run() -> int:
    print("=== 1. 合法输出 ===")
    good = ('{"act":"玩笑","target":"bot","origin":"self","confidence":"clear","tone":"亲昵",'
            '"intent":"拉近距离","reception":"可能接受",'
            '"evidence":[{"ref":"本轮消息","detail":"称呼同时伴随帮助"}],'
            '"alternatives":["轻微不满"]}')
    got = asyncio.run(ca.analyze("小笨蛋，伞又忘拿了吧", turns=TURNS, chat=fake(good)))
    check("act 保留", got["act"] == "玩笑", got["act"])
    check("target 保留", got["target"] == "bot", got["target"])
    check("tone/intent/reception 保留",
          got["tone"] == "亲昵" and got["intent"] == "拉近距离", got)
    check("origin 保留（言论来源，2026-11-03 按 #0008 新增）", got["origin"] == "self", got["origin"])
    check("证据保留", len(got["evidence"]) == 1 and got["evidence"][0]["ref"] == "本轮消息", got["evidence"])
    check("备选解释保留", got["alternatives"] == ["轻微不满"], got["alternatives"])

    print("\n=== 2. 洗字段 ===")
    bad = ('{"act":"嘲讽","target":"他","confidence":"也许","tone":"阴阳",'
           '"intent":"搞事","reception":"看情况","evidence":[],"alternatives":[]}')
    got2 = asyncio.run(ca.analyze("小笨蛋", turns=TURNS, chat=fake(bad)))
    check("非法 act → unknown", got2["act"] == "unknown", got2["act"])
    check("非法 target → none", got2["target"] == "none", got2["target"])
    check("非法 confidence → unknown", got2["confidence"] == "unknown", got2["confidence"])
    check("非法 tone → 不明确", got2["tone"] == "不明确", got2["tone"])
    check("非法 origin → unknown（不是 self，也不是 other）", got2["origin"] == "unknown",
          got2["origin"])

    empty_act = asyncio.run(ca.analyze("小笨蛋", turns=TURNS,
                                       chat=fake('{"act":"不知道","confidence":"clear"}')))
    check("模型说「不知道」→ 整条按 unknown", empty_act["act"] == "unknown"
          and empty_act["confidence"] == "unknown", empty_act)

    # 复合值：模型常把两个候选并排写。原来精确匹配会把整条丢掉——
    # `act` 变 unknown（这次深判等于白做），`tone` 变不明确（下游拿不到口气）。
    compound = asyncio.run(ca.analyze("小笨蛋", turns=TURNS, chat=fake(
        '{"act":"玩笑/亲密","target":"bot","confidence":"clear","tone":"亲昵、调侃、宠溺",'
        '"intent":"拉近距离","reception":"可能接受",'
        '"evidence":[{"ref":"本轮消息","detail":"称呼同时伴随帮助"}],"alternatives":[]}')))
    check("复合 act「玩笑/亲密」→ 取首个合法值", compound["act"] == "玩笑", compound["act"])
    check("复合 tone「亲昵、调侃、宠溺」→ 取首个合法值", compound["tone"] == "亲昵",
          compound["tone"])

    chatty = asyncio.run(ca.analyze("小笨蛋", turns=TURNS,
                                    chat=fake('{"act":"求助（求证/解读）","tone":"阴阳"}')))
    check("带括号说明的 act「求助（求证/解读）」→ 求助", chatty["act"] == "求助", chatty["act"])
    check("白名单外的 tone 仍回落到不明确", chatty["tone"] == "不明确", chatty["tone"])

    print("\n=== 3. 证据必须对得上号 ===")
    forged = ('{"act":"玩笑","target":"bot","confidence":"clear",'
              '"evidence":[{"ref":"第9条","detail":"编的"},'
              '{"ref":"第2条","detail":"真的存在"},'
              '{"ref":"本轮消息","detail":"本轮的"},'
              '{"ref":"随便写","detail":"格式不对"}],'
              '"alternatives":[]}')
    got3 = asyncio.run(ca.analyze("小笨蛋", turns=TURNS, chat=fake(forged)))
    refs = [row["ref"] for row in got3["evidence"]]
    check("超出范围的「第9条」被丢弃", "第9条" not in refs, refs)
    check("存在的「第2条」保留", "第2条" in refs, refs)
    check("「本轮消息」保留", "本轮消息" in refs, refs)
    check("格式不对的引用被丢弃", "随便写" not in refs, refs)

    print("\n=== 4. 失败一律 unknown，不退化成别的 ===")
    timeout = asyncio.run(ca.analyze("小笨蛋", turns=TURNS,
                                     chat=fake(good, delay=0.3), timeout=0.05))
    check("超时 → unknown", timeout["act"] == "unknown" and timeout["confidence"] == "unknown",
          timeout["act"])
    boom = asyncio.run(ca.analyze("小笨蛋", turns=TURNS, chat=fake("{}", boom=True)))
    check("模型异常 → unknown", boom["act"] == "unknown", boom["act"])
    badjson = asyncio.run(ca.analyze("小笨蛋", turns=TURNS, chat=fake("这不是 JSON")))
    check("返回不是 JSON → unknown", badjson["act"] == "unknown", badjson["act"])
    blank = asyncio.run(ca.analyze("   ", turns=TURNS, chat=fake(good)))
    check("空输入 → unknown 且不调用模型", blank["act"] == "unknown", blank["act"])

    print("\n=== 5. 轮次与触发 ===")
    captured: dict = {}

    async def _spy(messages, **kwargs):
        captured["prompt"] = messages[-1]["content"]
        captured["kwargs"] = kwargs
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=good))])

    asyncio.run(ca.analyze("小笨蛋", turns=TURNS, chat=_spy,
                           boundary="别再叫我小笨蛋了"))
    prompt = captured.get("prompt", "")
    check("本轮消息被标出来", "← 判断这一句" in prompt, prompt[-60:])
    check("已知边界被带进 prompt", "别再叫我小笨蛋了" in prompt)
    check("轮次带编号（供证据引用）", "1. " in prompt and "3. " in prompt, prompt[:40])
    check("prompt 里摆出各字段候选值（否则模型自由发挥、白名单必然洗掉）",
          "敌意" in prompt and "可能接受" in prompt and "已明确拒绝" in prompt)
    check("有边界时提示边界优先", "边界优先" in prompt)
    check("prompt 里写清 target 是态度对象、origin 是言论来源",
          "态度冲着谁" in prompt and "言论来源" in prompt)
    check("关闭思考走 extra_body（openai SDK 不认裸 thinking 参数）",
          (captured.get("kwargs") or {}).get("extra_body") == {"thinking": {"type": "disabled"}},
          str(captured.get("kwargs"))[:120])

    await_off = {}
    async def _spy_default(messages, **kwargs):
        await_off.update(kwargs)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=good))])

    asyncio.run(ca.analyze("小笨蛋", turns=TURNS, chat=_spy_default, thinking=""))
    check("thinking=\"\" 时不传该参数（用接口默认，留作对照）", "extra_body" not in await_off,
          str(await_off)[:120])

    trunc = {}
    async def _spy_trunc(messages, **kwargs):
        trunc.update(kwargs)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(content=good), finish_reason="length")])

    asyncio.run(ca.analyze("小笨蛋", turns=TURNS, chat=_spy_trunc, max_tokens=1234))
    check("预算按参数下传（截断才能与「看不清」分开记）",
          trunc.get("max_tokens") == 1234, str(trunc.get("max_tokens")))

    check("规则层标了 needs_context 才深判", ca.needs_deep_judge({"needs_context": True}) is True)
    check("没有线索时不深判", ca.needs_deep_judge({"needs_context": False}) is False)

    print("\n=== 6. 增量复用（#0002）：只有「同一条消息重复处理」才复用 ===")
    import contextlib  # noqa: E402

    _turns = [{"role": "user", "speaker": "群友A", "text": "小笨蛋"}]
    _verdict_ok = {"act": "亲密", "target": "bot", "origin": "self", "confidence": "clear",
                   "tone": "亲昵", "intent": "拉近距离", "reception": "可能接受",
                   "evidence": [{"ref": "本轮消息", "detail": "称呼同时伴随关心"}],
                   "alternatives": []}
    _key = ca.analysis_key("小笨蛋", _turns, mode="互相调侃", uid="111",
                           boundaries_rev=0, boundaries_hash="sha1:h0")
    _state = {"last_analysis": {**_key, "at": 1000.0, "verdict": dict(_verdict_ok)}}

    calls = {"n": 0}

    async def _counting_chat(messages, **kwargs):
        calls["n"] += 1
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=good))])

    def _decide(**kw):
        base = dict(turns=_turns, mode="互相调侃", uid="111", boundaries_rev=0,
                    boundaries_hash="sha1:h0", state=_state, chat=_counting_chat,
                    now=1001.0, ttl=90.0)
        base.update(kw)
        return asyncio.run(ca.decide("小笨蛋", **base))

    r1 = _decide()
    check("命中复用：source=reuse 且模型调用 0 次",
          r1["source"] == "reuse" and calls["n"] == 0, f"{r1['source']} / {calls['n']}")
    check("复用结果打 reused 标记（evidence 不许冒充新证据）", r1["verdict"].get("reused") is True)
    check("**复用不写状态**（write_back 为空，于是不会刷新深判时间）",
          r1["write_back"] == {}, r1["write_back"])

    # 相反语境：ctx-001 的关心话 → ctx-002 的争执贬低，文本不同，必须重判
    r2 = _decide(now=1001.0)
    r2b = asyncio.run(ca.decide("你就是个小笨蛋，什么都做不好", turns=_turns,
                                mode="互相调侃", uid="111", boundaries_rev=0,
                                boundaries_hash="sha1:h0", state=_state,
                                chat=_counting_chat, now=1001.0, ttl=90.0))
    check("**相反语境必须重判**（阻断项）", r2b["source"] == "analysis" and calls["n"] == 1,
          f"{r2b['source']} / {calls['n']}")

    calls["n"] = 0
    r3 = _decide(turns=[{"role": "user", "text": "今天忘带伞了"}])
    check("上下文指纹变了 → 不复用", r3["source"] == "analysis" and calls["n"] == 1,
          f"{r3['source']} / {calls['n']}")

    calls["n"] = 0
    r4 = _decide(ttl=0.0)
    check("ttl=0 → 强制关闭复用", r4["source"] == "analysis" and calls["n"] == 1,
          f"{r4['source']} / {calls['n']}")

    calls["n"] = 0
    _fuzzy_state = {"last_analysis": {**_key, "at": 1000.0,
                                      "verdict": {**_verdict_ok, "confidence": "fuzzy"}}}
    r5 = _decide(state=_fuzzy_state)
    check("上次不是 clear → 不复用", r5["source"] == "analysis", r5["source"])

    calls["n"] = 0
    r6 = _decide(now=5000.0)
    check("超过 TTL → 不复用", r6["source"] == "analysis", r6["source"])

    # ---- 2026-11-03 22:56 审阅的三条修正 ----
    print("\n=== 7. 复用键的三个边角（审阅 22:56）===")
    check("`a b` 与 `ab` 是不同输入（空白不删，只压缩）",
          ca.analysis_key("a b")["text_norm"] != ca.analysis_key("ab")["text_norm"],
          (ca.analysis_key("a b")["text_norm"], ca.analysis_key("ab")["text_norm"]))
    check("全角空格归一再算同一个",
          ca.analysis_key("a\u3000b")["text_norm"] == ca.analysis_key("a b")["text_norm"],
          ca.analysis_key("a\u3000b")["text_norm"])

    _k_ctx_a = ca.analysis_key("小笨蛋", _turns, mode="互相调侃", uid="111",
                               boundaries_rev=0, boundaries_hash="sha1:h0", context="在聊伞")
    _k_ctx_b = ca.analysis_key("小笨蛋", _turns, mode="互相调侃", uid="111",
                               boundaries_rev=0, boundaries_hash="sha1:h0", context="在聊工作")
    check("语境摘要变了 → 缓存键不同（键里有 context_hash）",
          _k_ctx_a != _k_ctx_b and "context_hash" in _k_ctx_a, sorted(_k_ctx_a))

    _state_ctx = {"last_analysis": {**_k_ctx_a, "at": 1000.0, "verdict": dict(_verdict_ok)}}
    calls["n"] = 0
    _r_ctx = asyncio.run(ca.decide("小笨蛋", turns=_turns, mode="互相调侃", uid="111",
                                   boundaries_rev=0, boundaries_hash="sha1:h0",
                                   context="在聊工作", state=_state_ctx,
                                   chat=_counting_chat, now=1001.0, ttl=90.0))
    check("语境摘要变了 → 不复用（阻断）",
          _r_ctx["source"] == "analysis" and calls["n"] == 1,
          f"{_r_ctx['source']} / {calls['n']}")

    # **本版唯一的现实收益路径**：同一条消息被重复投递/重放。
    # 它要求 `chatlog` 里已经多出一条同文本时仍算同一段上下文 —— 靠尾部去重实现。
    _turns_dup = list(_turns) + [{"role": "user", "speaker": "群友A", "text": "小笨蛋"}]
    _k_dup = ca.analysis_key("小笨蛋", _turns_dup, mode="互相调侃", uid="111",
                             boundaries_rev=0, boundaries_hash="sha1:h0", context="")
    _k_first = ca.analysis_key("小笨蛋", _turns, mode="互相调侃", uid="111",
                               boundaries_rev=0, boundaries_hash="sha1:h0", context="")
    check("重复投递（上下文尾部多一条同文本）→ 键相同", _k_dup == _k_first,
          (_k_dup["ctx_hash"], _k_first["ctx_hash"]))
    calls["n"] = 0
    _r_dup = asyncio.run(ca.decide("小笨蛋", turns=_turns_dup, mode="互相调侃", uid="111",
                                   boundaries_rev=0, boundaries_hash="sha1:h0", context="",
                                   state={"last_analysis": {**_k_first, "at": 1000.0,
                                                            "verdict": dict(_verdict_ok)}},
                                   chat=_counting_chat, now=1001.0, ttl=90.0))
    check("重复投递 → 命中复用（0 次模型调用）",
          _r_dup["source"] == "reuse" and calls["n"] == 0, f"{_r_dup['source']} / {calls['n']}")

    check("判过之后会写 last_analysis（完整 8 个字段）",
          set((r6.get("write_back") or {}).get("last_analysis", {}).get("verdict", {}))
          >= {"act", "target", "origin", "confidence", "tone", "intent", "reception",
              "evidence", "alternatives"},
          sorted(((r6.get("write_back") or {}).get("last_analysis") or {}).get("verdict", {})))

    print(f"\n{'=' * 50}")
    print(f"通过 {passed} 项" + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    print("=" * 50)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
