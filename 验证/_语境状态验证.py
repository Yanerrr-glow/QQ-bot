"""语境状态（`scene_state.py`）的离线验证：纯逻辑，不连网、不依赖 nonebot。

用法：

    python 验证\\_语境状态验证.py

退出码 0 = 通过。

## 为什么要它

这块最容易出的三个错都是**静默**的：

1. 晚到的分析把新判断覆盖掉（版本没管住）——表现是「明明刚拒绝过，她还当玩笑」；
2. 重试同一轮让状态走两遍（去重没做）——表现是情绪/语气突然跳一档；
3. 边界被关系值抵消——这正是 §13 明确要求防的。

三个都不会报错，只能靠断言钉住。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import sys

try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORK = ROOT / "temp" / "_scene_state_test"

_spec = importlib.util.spec_from_file_location(
    "ai_chat_scene_state", ROOT / "plugins" / "ai_chat" / "scene_state.py")
st = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(st)  # type: ignore[union-attr]

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


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    C = "u100000001"

    print("=== 1. 空状态 ===")
    s = st.load(C, root=WORK)
    check("文件不存在时返回空状态", s["version"] == 0 and s["topic"] == "", str(s)[:80])
    check("空状态也带 conv", s["conv"] == C)
    check("空摘要为空串", st.summary(C, root=WORK) == "")

    print("\n=== 2. 合并与版本 ===")
    r1 = st.merge(C, {"topic": "体测跳远", "mode": "互相调侃", "confidence": "clear",
                      "partners": {"100000001": "主人"},
                      "last_turn": {"message_id": 719}}, root=WORK, now=1000.0)
    check("首次合并成功", bool(r1.get("ok")), str(r1)[:80])
    check("版本从 0 → 1", r1["state"]["version"] == 1, r1["state"]["version"])
    check("话题写入", r1["state"]["topic"] == "体测跳远")
    check("对象写入", r1["state"]["partners"].get("100000001") == "主人")

    print("\n=== 3. 晚到的结果必须丢弃 ===")
    late = st.merge(C, {"topic": "旧话题"}, base_version=0, root=WORK)
    check("base_version 过期 → 丢弃", late.get("dropped") is True, str(late.get("why")))
    check("丢弃后话题没被改", st.load(C, root=WORK)["topic"] == "体测跳远")
    check("丢弃后版本没涨", st.load(C, root=WORK)["version"] == 1)

    print("\n=== 4. 同一条消息重试不累计 ===")
    before = st.load(C, root=WORK)["version"]
    again = st.merge(C, {"topic": "体测跳远", "last_turn": {"message_id": 719}}, root=WORK)
    check("同一 message_id 被跳过", again.get("skipped") is not None, str(again)[:80])
    check("版本不变", st.load(C, root=WORK)["version"] == before, before)

    print("\n=== 5. 边界 ===")
    st.note_boundary(C, "别再叫我小笨蛋", message_id=720, root=WORK)
    st.note_boundary(C, "别再叫我小笨蛋", message_id=721, root=WORK)
    state = st.load(C, root=WORK)
    check("边界去重（同一句话只留一条）", len(state["boundaries"]) == 1, len(state["boundaries"]))
    # 重复时保留**最近一次**：他第二次说「别再叫我小笨蛋」，证据要指向更近的那次
    check("边界保留最近一次的 message_id", state["boundaries"][0].get("message_id") == 721,
          state["boundaries"][0])

    # 2026-11-03（#0005）：边界要能说清"禁的是什么"，否则检索侧没法据此过滤
    st.note_boundary(C, "别喊我老公", kind="称呼", word="老公", message_id=722, root=WORK)
    rows = {str(x.get("text")): x for x in st.load(C, root=WORK)["boundaries"]}
    check("边界存下了 kind / word",
          rows.get("别喊我老公", {}).get("kind") == "称呼"
          and rows.get("别喊我老公", {}).get("word") == "老公", rows.get("别喊我老公"))
    check("没给 kind/word 的边界不凭空编",
          "kind" not in rows.get("别再叫我小笨蛋", {})
          and "word" not in rows.get("别再叫我小笨蛋", {}), rows.get("别再叫我小笨蛋"))
    check("boundary_of 抽得出被禁词", st.boundary_of("别再叫我小笨蛋了") == ("称呼", "小笨蛋"),
          st.boundary_of("别再叫我小笨蛋了"))

    text = st.summary(C, root=WORK)
    check("摘要里边界排在话题之前", text.index("别再叫我小笨蛋") < text.index("体测跳远"), text)
    check("摘要点明边界优先于玩笑", "优先于任何玩笑" in text, text)

    print("\n=== 6. 证据上限 ===")
    for i in range(20):
        st.merge(C, {"evidence": [{"ref": 1000 + i, "detail": f"证据{i}"}],
                     "last_turn": {"message_id": 2000 + i}}, root=WORK)
    rows = st.load(C, root=WORK)["evidence"]
    check("证据被截到上限（8）", len(rows) <= st._MAX_EVIDENCE, len(rows))
    check("留下的是最新的", rows[-1]["ref"] == 1019, rows[-1] if rows else None)

    print("\n=== 7. 时效 ===")
    fresh = st.load(C, root=WORK)
    check("刚更新不算过时", st.is_stale(C, root=WORK) is False)
    check("年龄是有限值", st.age_seconds(fresh) != float("inf"))
    st.merge(C, {"topic": "很久以前"}, root=WORK, now=1.0)
    check("三小时前的判断算过时", st.is_stale(C, root=WORK) is True)
    check("过时状态在摘要里有提示", "过时" in st.summary(C, root=WORK), st.summary(C, root=WORK))

    print("\n=== 8. 坏文件不崩 ===")
    bad = WORK / f"{C}.json"
    bad.write_text("{不是 JSON", encoding="utf-8")
    recovered = st.load(C, root=WORK)
    check("损坏文件按空状态处理", recovered["version"] == 0 and recovered["topic"] == "")
    check("损坏后仍能写入", bool(st.merge(C, {"topic": "恢复"}, root=WORK).get("ok")))
    check("恢复后话题正确", st.load(C, root=WORK)["topic"] == "恢复")

    print("\n=== 9. 原子写不留半文件 ===")
    leftovers = [p.name for p in WORK.iterdir() if p.name.endswith(".tmp")]
    check("目录里没有残留 .tmp", not leftovers, leftovers)

    print("\n=== 10. 群聊区分对象 ===")
    G = "g100000003"
    st.merge(G, {"partners": {"111": "群友A"}, "last_turn": {"message_id": 1}}, root=WORK)
    st.merge(G, {"partners": {"222": "群友B"}, "last_turn": {"message_id": 2}}, root=WORK)
    partners = st.load(G, root=WORK)["partners"]
    check("两个说话者都记下（不互相覆盖）",
          partners.get("111") == "群友A" and partners.get("222") == "群友B", partners)

    print("\n=== 11. 回复自带的 <ctx> 标记 ===")
    clean, delta = st.extract_marker(
        "那你就别看啦，手机放一边去。\n<ctx mode=\"互相调侃\" topic=\"催睡\" conf=\"clear\"/>")
    check("标记被剥掉", "<ctx" not in clean and clean.endswith("去。"), repr(clean[-20:]))
    check("三个字段解析出来",
          delta == {"mode": "互相调侃", "topic": "催睡", "confidence": "clear"}, delta)

    clean2, delta2 = st.extract_marker("随便说一句，没有标记。")
    check("没有标记时原文不变", clean2 == "随便说一句，没有标记。" and delta2 == {}, delta2)

    _, d3 = st.extract_marker('<ctx mode="胡说八道" topic="" conf="maybe"/>')
    check("非法 mode/topic/conf 全被丢弃", d3 == {}, d3)

    _, d4 = st.extract_marker("<ctx mode=争执/>")
    check("不带引号的属性也能解析", d4 == {"mode": "争执"}, d4)

    _, d5 = st.extract_marker('<ctx mode="争执"/>\n<ctx mode="冷场"/>')
    check("多个标记以最后一个为准", d5.get("mode") == "冷场", d5)

    mid, d6 = st.extract_marker('前半句<ctx mode="冷场"/>后半句')
    check("标记在中间也能剥干净", mid == "前半句后半句", repr(mid))
    check("中间标记仍能解析", d6.get("mode") == "冷场", d6)

    print("\n=== 12. 写入路径（record_llm）===")
    L = "u_llm_ctx"
    st.save(L, st.empty(L), root=WORK)
    text, applied = st.record_llm("好呀<ctx mode=\"互相调侃\" topic=\"哄睡\"/>",
                                  conv=L, message_id=555, root=WORK)
    check("record_llm 返回干净文本", text == "好呀", repr(text))
    saved = st.load(L, root=WORK)
    check("record_llm 写进状态", saved["mode"] == "互相调侃" and saved["topic"] == "哄睡", saved)
    check("record_llm 记下来源", saved["last_turn"].get("source") == "llm", saved["last_turn"])

    _before = st.load(L, root=WORK)["version"]
    st.record_llm("嗨<ctx mode=\"互相调侃\" topic=\"哄睡\"/>", conv=L, message_id=555, root=WORK)
    check("同一条消息重复 record 不涨版本", st.load(L, root=WORK)["version"] == _before, _before)

    print("\n=== 9. 深判缓存槽与边界修订号（#0002） ===")
    fresh = st.empty("cache")
    check("schema 里有 last_analysis", "last_analysis" in fresh, sorted(fresh))
    check("schema 里有 boundaries_rev", "boundaries_rev" in fresh, sorted(fresh))

    rev0 = st.load(C, root=WORK).get("boundaries_rev")
    st.note_boundary(C, "别喊我名字", kind="称呼", word="名字", message_id=730, root=WORK)
    rev1 = st.load(C, root=WORK).get("boundaries_rev")
    check("新增一条边界 → rev 恰好 +1", rev1 == rev0 + 1, f"{rev0} → {rev1}")

    state_a = st.load(C, root=WORK)
    for i in range(15):
        st.note_boundary(C, f"边界{i}", message_id=800 + i, root=WORK)
    state_b = st.load(C, root=WORK)
    check("边界最多留 12 条", len(state_b["boundaries"]) == st._MAX_BOUNDARIES,
          len(state_b["boundaries"]))
    st.note_boundary(C, "边界X", message_id=900, root=WORK)
    state_c = st.load(C, root=WORK)
    check("**满 12 条后再加一条：条数不变**", len(state_c["boundaries"]) == len(state_b["boundaries"]),
          f"{len(state_b['boundaries'])} → {len(state_c['boundaries'])}")
    check("**但 rev 仍 +1**", state_c["boundaries_rev"] == state_b["boundaries_rev"] + 1,
          f'{state_b["boundaries_rev"]} → {state_c["boundaries_rev"]}')
    check("**且内容指纹变了**（条数法看不见这个变化）",
          st.boundary_fingerprint(state_b) != st.boundary_fingerprint(state_c),
          st.boundary_fingerprint(state_c))

    st.merge(C, {"last_analysis": {"at": 1.0, "verdict": {"act": "亲密"}}}, root=WORK)
    st.merge(C, {"last_turn": {"message_id": 77, "source": "attention"}}, root=WORK)
    check("attention 写 last_turn **不会**冲掉 last_analysis",
          (st.load(C, root=WORK).get("last_analysis") or {}).get("at") == 1.0,
          st.load(C, root=WORK).get("last_analysis"))

    print(f"\n{'=' * 50}")
    print(f"通过 {passed} 项" + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    print("=" * 50)
    shutil.rmtree(WORK, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
