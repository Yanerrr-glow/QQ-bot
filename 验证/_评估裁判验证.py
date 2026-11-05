"""#0015 M3 三层评估：硬约束否决、target_kind、盲偏好、校准、状态。

跑法：`python 验证\\_评估裁判验证.py`（退出码 0 = 全部通过）。

全部用桩 client（固定字母返回），不联网；判据本身是纯函数，逐条钉死。
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "temp" / "_eval_grading"


class FakeJudge:
    """按顺序返回固定字母；每次调用记一次。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self, *, messages=None, **kw):
        answer = self.answers[self.calls % len(self.answers)]
        self.calls += 1
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])


def run(check) -> int:  # noqa: ANN001
    from ai_chat import eval_grading as g, settings

    settings.store.load()
    saved = {k: settings.get(k) for k in
             ("eval_grading_enabled", "eval_min_event_groups", "eval_calibration_min_pairs",
              "eval_agreement_threshold", "eval_grading_samples")}
    try:
        for key, value in (("eval_grading_enabled", True), ("eval_min_event_groups", 3),
                           ("eval_calibration_min_pairs", 2), ("eval_agreement_threshold", 0.8),
                           ("eval_grading_samples", 3)):
            settings.set_value(key, value)

        cfg = g.thresholds()
        check("阈值从 settings 读", cfg["min_event_groups"] == 3, cfg)
        check("阈值带 samples", cfg["samples"] == 3)

        # ------------------------------------------------ 第一层：硬约束
        h = g.hard_check("")
        check("空回复是硬错误", not h["passed"] and h["violations"][0]["type"] == "empty")
        h = g.hard_check("好的", case={"banned_words": ["好的"]})
        check("禁用词是硬错误", any(v["type"] == "banned_word" for v in h["violations"]))
        h = g.hard_check("a" * 1300)
        check("超长是硬错误", any(v["type"] == "overlong" for v in h["violations"]))
        check("正常回复通过", g.hard_check("在的。")["passed"])

        # ------------------------------------------------ 第二层：情景行为
        case = {"expected_behavior": {"kind": "clarify", "must_contain": ["你是说"],
                                      "must_not_contain": ["不知道"]}}
        b = g.behavior_check(case, "你是说哪件事？")
        check("行为达成", b["passed"], b)
        check("缺关键表达判失败", not g.behavior_check(case, "嗯。")["passed"])
        check("出现应避免表达判失败", not g.behavior_check(case, "不知道啊。")["passed"])

        # ------------------------------------------------ 第三层：盲偏好
        vote = asyncio.run(g.blind_vote("甲", "乙", client=FakeJudge(["A", "B"])))
        check("交换位置仍选左边", vote["winner"] == "left", vote)
        vote = asyncio.run(g.blind_vote("甲", "乙", client=FakeJudge(["A", "A"])))
        check("交换后矛盾记 uncertain", vote["winner"] == "uncertain", vote)
        vote = asyncio.run(g.blind_vote("甲", "乙", client=FakeJudge(["T", "T"])))
        check("全平记 tie", vote["winner"] == "tie", vote)
        vote = asyncio.run(g.blind_vote("甲", "乙", client=FakeJudge(["N", "N"])))
        check("都不行记 both_bad", vote["winner"] == "both_bad", vote)
        vote = asyncio.run(g.blind_vote("甲", "乙", client=FakeJudge(["？", "？"])))
        check("无效回答记 invalid", vote["winner"] == "invalid", vote)

        # ------------------------------------------------ 校准
        pairs = [{"left": "甲", "right": "乙", "human": "left"},
                 {"left": "丙", "right": "丁", "human": "right"}]
        got = asyncio.run(g.calibrate(pairs[:1], client=FakeJudge(["A", "B"])))
        check("校准样本不足 → inconclusive", got["status"] == "inconclusive", got)
        pairs3 = pairs + [{"left": "戊", "right": "己", "human": "tie"}]
        got = asyncio.run(g.calibrate(pairs3, client=FakeJudge(["A", "B"])))
        check("校准有 client 才跑", got["status"] in ("pass", "fail"), got)
        check("校准报一致率", isinstance(got.get("agreement"), float), got.get("agreement"))
        check("校准逐条留痕", len(got.get("details") or []) == 3)
        got = asyncio.run(g.calibrate(pairs3, client=None))
        check("无 client 记 not_run", got["status"] == "not_run" and got.get("execution_reason"),
              got)

        # ------------------------------------------------ 汇总判定
        ok_hard = {"passed": True, "violations": []}
        before = {"passed": 2, "total": 5}
        after_more = {"passed": 4, "total": 5}
        after_same = {"passed": 2, "total": 5}
        pref_win = {"wins": 3, "losses": 1, "ties": 0, "invalid": 0}
        pref_tie = {"wins": 0, "losses": 0, "ties": 5, "invalid": 0}

        got = g.judge_candidate(target_kind="behavior", hard={"passed": False, "violations": [{"type": "empty"}]},
                                behavior_before=before, behavior_after=after_more,
                                pref=pref_win, groups=5)
        check("硬错误单例否决", got["status"] == "fail", got)
        got = g.judge_candidate(target_kind="behavior", hard=ok_hard, behavior_before=before,
                                behavior_after=after_more, pref=pref_win, groups=5)
        check("behavior 成功数增加 → pass", got["status"] == "pass", got)
        got = g.judge_candidate(target_kind="behavior", hard=ok_hard, behavior_before=before,
                                behavior_after=after_same, pref=pref_win, groups=5)
        check("behavior 持平 → fail（持平不算改善）", got["status"] == "fail", got)
        got = g.judge_candidate(target_kind="behavior", hard=ok_hard, behavior_before=before,
                                behavior_after=after_more, pref={"wins": 1, "losses": 4},
                                groups=5)
        check("behavior 增加但表达退化 → fail", got["status"] == "fail", got)

        got = g.judge_candidate(target_kind="expression", hard=ok_hard, behavior_before=before,
                                behavior_after=after_same, pref=pref_win, groups=5)
        check("expression 胜出 → pass", got["status"] == "pass", got)
        got = g.judge_candidate(target_kind="expression", hard=ok_hard, behavior_before=before,
                                behavior_after=after_same, pref=pref_tie, groups=5)
        check("expression 全平 → inconclusive", got["status"] == "inconclusive", got)
        got = g.judge_candidate(target_kind="expression", hard=ok_hard,
                                behavior_before={"passed": 4, "total": 5},
                                behavior_after={"passed": 3, "total": 5},
                                pref=pref_win, groups=5)
        check("expression 行为退化 → fail", got["status"] == "fail", got)

        got = g.judge_candidate(target_kind="expression", hard=ok_hard, behavior_before=before,
                                behavior_after=after_same, pref=pref_win, groups=1)
        check("事件组不足 → inconclusive", got["status"] == "inconclusive", got)
        got = g.judge_candidate(target_kind="expression", hard=ok_hard, behavior_before=before,
                                behavior_after=after_same, pref=pref_win, groups=5,
                                coverage="partial")
        check("partial 覆盖 → inconclusive", got["status"] == "inconclusive", got)
        got = g.judge_candidate(target_kind="乱写", hard=ok_hard, behavior_before=before,
                                behavior_after=after_more, pref=pref_win, groups=5)
        check("非法 target_kind → fail", got["status"] == "fail", got)
        got = g.judge_candidate(target_kind="expression", hard=ok_hard, behavior_before=before,
                                behavior_after={"passed": 0, "total": 0}, pref=pref_win, groups=5)
        check("无行为样本 → not_run", got["status"] == "not_run" and got.get("execution_reason"),
              got)

        # ------------------------------------------------ 明确反馈不被高分覆盖
        got = g.judge_candidate(target_kind="behavior", hard={"passed": False,
                                                              "violations": [{"type": "fact",
                                                                              "detail": "编造"}]},
                                behavior_before=before, behavior_after=after_more,
                                pref={"wins": 99, "losses": 0}, groups=99)
        check("硬事实错误不被高偏好抵消", got["status"] == "fail", got)

        # ------------------------------------------------ 时间衰减（D3）
        check("年龄 0 权重为 1", g.decay_weight(0, half_life_hours=72) == 1.0)
        check("一个半衰期后剩一半", g.decay_weight(72, half_life_hours=72) == 0.5)
        check("两个半衰期后剩四分之一", g.decay_weight(144, half_life_hours=72) == 0.25)
        check("半衰期 0 = 不衰减", g.decay_weight(1000, half_life_hours=0) == 1.0)
        check("未来时间不产生 >1 的权重", g.decay_weight(-5, half_life_hours=72) == 1.0)
        now = 1_800_000_000.0
        counts = g.weighted_counts([
            {"kind": "win", "ts": now - 3600},       # 1 小时前
            {"kind": "win", "ts": now - 72 * 3600},  # 一个半衰期前 → 0.5
            {"kind": "loss", "ts": now - 144 * 3600},  # 两个半衰期前 → 0.25
        ], now=now, half_life_hours=72)
        check("加权计数按年龄打折", abs(counts["win"] - 1.5) < 0.01, counts)
        check("加权计数保留小数", abs(counts["loss"] - 0.25) < 0.01, counts)
        # 加权后「新样本 1.5 胜 / 老样本 0.25 负」应判胜出，而不是被截断成 1:0 的整数比较
        got = g.judge_candidate(target_kind="expression", hard=ok_hard,
                                behavior_before=before, behavior_after=after_same,
                                pref={"wins": counts["win"], "losses": counts["loss"]},
                                groups=5)
        check("加权计数参与判据（不截断）", got["status"] == "pass", got)
        tiny = g.judge_candidate(target_kind="expression", hard=ok_hard,
                                 behavior_before=before, behavior_after=after_same,
                                 pref={"wins": 0.2, "losses": 0.1}, groups=5)
        check("极小权重仍按大小关系判", tiny["status"] == "pass", tiny)

        # ------------------------------------------------ 状态与旧链标记
        info = g.status()
        check("status 报阈值", "min_event_groups" in info["thresholds"])
        check("status 标旧链不可用", info["legacy"]["available"] is False, info["legacy"])
        check("四种状态齐全", set(info["statuses"]) == {"pass", "fail", "inconclusive", "not_run"},
              info["statuses"])

        from ai_chat import persona_eval as ev
        st = ev.status()
        check("persona_eval.status 带 legacy 标记", st.get("legacy", {}).get("available") is False,
              st.get("legacy"))
        check("persona_eval 记空 content 重试次数",
              "label_retries" in (st.get("legacy") or {}))
    finally:
        for key, value in saved.items():
            if value is not None:
                settings.set_value(key, value)
        shutil.rmtree(WORK, ignore_errors=True)
    return 0


def _bootstrap():
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    end = source.find('print("\\n=== 2.')
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "eval_grading_bootstrap"}
    exec(compile(source[:end], str(harness), "exec"), namespace)  # noqa: S102
    return namespace


if __name__ == "__main__":
    ns = _bootstrap()
    try:
        run(ns["check"])
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    failed = ns["failed"]
    print(f"通过 {ns['passed']} 项"
          + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    sys.exit(1 if failed else 0)
