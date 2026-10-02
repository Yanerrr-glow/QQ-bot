"""自示监控：把"它自己说过的话"当成训练数据来筛 —— 论文 §6（ΔP）的文本版同构。

## 为什么需要它

`chatlog.render_background()` 取的是"所有已读消息"（`chatlog.py` 里没有过滤机器人自己），
只有 `render_unread(skip_bot=True)` 才把自己排除在"刚才的新发言"之外。
所以**它每一轮的 prompt 里都带着自己过去说过的话**。

也就是说：**它的历史发言就是它自己的训练数据**，每轮 in-context 地喂回去。
论文说"不用看权重，只看数据沿人格方向的投影差就能预测漂移"—— 这里是同一个东西的
文本版：不看激活，只看**它自己的发言里带了多少个特质的痕迹**。

## 判据

用 `persona/packs/<包>/traits.json` 的 **`output_markers`**（输出特征）—— 那是专门为
"在它自己的回复里找痕迹"写的一套词，与给候选用的 `gate_terms` 是**两套**词：
`gate_terms` 判的是"候选人会怎么写"，`output_markers` 判的是"它已经说出来的话"。

* 每条 `is_bot=True` 的消息 → 对每个有输出特征的特质统计命中：
  `命中条数`（至少命中一个词的消息数）与 `痕迹数`（词命中总次数）
* **固定系统文案默认排除**：2026-09-26 起它们不进聊天记录，但历史记录里可能还留着；
  排除掉才是在量"她自己说的话"。判据是注册表 `fixed_notice_channels` 里的 `match`
  （**子串命中**即视为固定文案）。

## 覆盖声明（重要）

**elicit 特质的衰减没有表面标记**（`genki` / `softness` / `tsundere`），
`verbal_tic`（要跨条比短语）/ `address_form`（要知道收件人）/ `language_zh`（要看句子结构）
同样没有输出特征。这 6 个特质**本工具监控不到**，报告里会明确列出来 ——
elicit 那三个必须靠打分（论文那套评估台），这正是它不是可有可无的理由。

## 用法

```powershell
    python '验证\\_自示监控.py'                 # 读项目 data/runtime/ 下的 chatlog
python '验证\\_自示监控.py' --days 7        # 只看最近 7 天
python '验证\\_自示监控.py' --top 8         # 多列几个特质
    python '验证\\_自示监控.py' --log-dir /app/data/runtime   # 容器里
python '验证\\_自示监控.py' --json          # 机器可读
python '验证\\_自示监控.py' --selftest      # 用合成数据自验（没数据也能跑）
```

纯本地、不联网、不调模型、不写盘。词面打分，零依赖。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PACKS_DIR = os.path.join(PROJ, "persona", "packs")


def active_pack_id() -> str:
    """当前激活的包：运行时标记 > 注册表 > 唯一一个启用的包。

    **与 `plugins/ai_chat/packs.py` 同序**，这里复算一遍是因为本脚本要能脱依赖直跑
    （同 `_人设结构检查.py`）：它只读 traits.json，不需要起插件。
    """
    marker = os.path.join(PROJ, "data", "runtime", "persona", "_active")
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            got = fh.read().strip()
        if got and os.path.isdir(os.path.join(PACKS_DIR, got)):
            return got
    except OSError:
        pass
    try:
        with open(os.path.join(PROJ, "persona", "_registry.json"), "r", encoding="utf-8") as fh:
            got = str((json.load(fh) or {}).get("active") or "").strip()
        if got and os.path.isdir(os.path.join(PACKS_DIR, got)):
            return got
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    try:
        names = sorted(n for n in os.listdir(PACKS_DIR)
                       if not n.startswith(("_", ".")) and os.path.isdir(os.path.join(PACKS_DIR, n)))
    except OSError:
        return ""
    return names[0] if len(names) == 1 else ""


# 特质注册表在**当前人格包**里（包化后不再有固定路径）；可用 `--pack` 覆盖。
TRAITS_FILE = os.path.join(PACKS_DIR, active_pack_id(), "traits.json")


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_registry() -> tuple[list[dict], list[str]]:
    """返回 `(特质列表, 固定文案的 match 列表)`。"""
    reg = read_json(TRAITS_FILE)
    traits = [t for t in (reg.get("traits") or []) if isinstance(t, dict)]
    fixed = [
        str(it.get("match") or "")
        for it in ((reg.get("fixed_notice_channels") or {}).get("items") or [])
        if isinstance(it, dict)
    ]
    return traits, [f for f in fixed if f]


def compile_markers(traits: list[dict]) -> list[tuple[str, str, list[str], list[re.Pattern]]]:
    """`[(slug, 特质名, 原始词, 已编译)]`，只保留**有**输出特征的特质。"""
    out = []
    for t in traits:
        marks = [str(m) for m in (t.get("output_markers") or []) if str(m).strip()]
        if not marks:
            continue
        pats = []
        for m in marks:
            try:
                pats.append(re.compile(m))
            except re.error:
                pats.append(re.compile(re.escape(m)))
        out.append((str(t.get("slug") or "?"), str(t.get("trait") or "?"), marks, pats))
    return out


def is_fixed_notice(text: str, fixed_matches: list[str]) -> bool:
    return any(m and m in text for m in fixed_matches)


def scan(messages: list[dict], skills, fixed_matches: list[str]) -> dict:
    """扫一批消息。返回统计字典（纯函数，便于自验）。"""
    bot_total = 0
    skipped_fixed = 0
    per: dict[str, dict] = {
        slug: {"trait": label, "messages": 0, "hits": 0, "markers": {}}
        for slug, label, _marks, _pats in skills
    }
    for msg in messages:
        if not isinstance(msg, dict) or not msg.get("is_bot"):
            continue
        text = str(msg.get("text") or "")
        if not text.strip():
            continue
        if is_fixed_notice(text, fixed_matches):
            skipped_fixed += 1
            continue
        bot_total += 1
        for slug, _label, marks, pats in skills:
            hit_marks = [m for m, p in zip(marks, pats) if p.search(text)]
            if hit_marks:
                per[slug]["messages"] += 1
                per[slug]["hits"] += len(hit_marks)
                for m in hit_marks:
                    per[slug]["markers"][m] = per[slug]["markers"].get(m, 0) + 1
    return {
        "bot_messages": bot_total,
        "skipped_fixed": skipped_fixed,
        "traits": per,
    }


def merge(dst: dict, src: dict) -> dict:
    dst["bot_messages"] += src["bot_messages"]
    dst["skipped_fixed"] += src["skipped_fixed"]
    for slug, row in src["traits"].items():
        d = dst["traits"].setdefault(
            slug, {"trait": row["trait"], "messages": 0, "hits": 0, "markers": {}}
        )
        d["messages"] += row["messages"]
        d["hits"] += row["hits"]
        for m, n in row["markers"].items():
            d["markers"][m] = d["markers"].get(m, 0) + n
    return dst


def empty_result() -> dict:
    return {"bot_messages": 0, "skipped_fixed": 0, "traits": {}}


def iter_chatlogs(log_dir: str, days: float) -> list[tuple[str, list[dict]]]:
    """读 `chatlog_*.json`，按时间窗过滤消息。"""
    if not os.path.isdir(log_dir):
        return []
    since = (time.time() - days * 86400) if days > 0 else 0.0
    out = []
    for name in sorted(os.listdir(log_dir)):
        if not name.startswith("chatlog_") or not name.endswith(".json"):
            continue
        path = os.path.join(log_dir, name)
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        msgs = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(msgs, list):
            continue
        if since:
            msgs = [m for m in msgs if float(m.get("ts") or 0) >= since]
        out.append((name, msgs))
    return out


def report(result: dict, skills, traits: list[dict], top: int, log_dir: str) -> int:
    total = result["bot_messages"]
    print("自示监控（它自己的发言 = 它自己的训练数据）")
    print("  记录目录  : %s" % log_dir)
    print("  它的发言  : %d 条（另有 %d 条是固定系统文案，已排除）"
          % (total, result["skipped_fixed"]))
    print("  可监控特质: %d / %d（其余没有输出特征，见下方覆盖声明）"
          % (len(skills), len(traits)))
    if not total:
        print("\n没有可分析的数据。")
        print("（本机若没跑过机器人，`data/runtime/` 是空的 —— 线上用 --log-dir /app/data/runtime。）")
        return 0

    rows = []
    for slug, _label, _marks, _pats in skills:
        row = result["traits"].get(slug) or {"messages": 0, "hits": 0, "markers": {}}
        rows.append((row["messages"], row["hits"], slug, row))
    rows.sort(reverse=True)

    print("\n== 自我示范排行（按命中条数降序；自示率 = 命中条数 / 它的发言数）==")
    print("  %-24s %6s %6s %8s  最多的三个痕迹" % ("特质", "条数", "痕迹", "自示率"))
    for n_msg, n_hit, slug, row in rows[:top]:
        if n_msg == 0:
            continue
        top_marks = sorted(row["markers"].items(), key=lambda kv: -kv[1])[:3]
        print("  %-24s %6d %6d %7.1f%%  %s"
              % (slug, n_msg, n_hit, 100.0 * n_msg / total,
                 "、".join("%s×%d" % (m, c) for m, c in top_marks)))

    print("\n== 被它自己的发言违反最多的铁律 ==")
    # 一个 suppress 特质 ≈ 一条铁律（见 persona/packs/<包>/traits.json 的 channels）
    ranked = [r for r in rows if r[0] > 0 and r[1] > 0]
    if not ranked:
        print("  没查出任何自我示范 —— 这阵子它说话很干净。")
    else:
        for n_msg, n_hit, slug, row in ranked[:3]:
            print("  %s（%s）：%d 条里带痕迹 %d 次" % (slug, row["trait"], n_msg, n_hit))

    print("\n== 覆盖声明（这些特质监控不到，别把「没报」当成「没有」）==")
    for t in traits:
        if t.get("output_markers"):
            continue
        print("  · %-18s %s" % (t.get("slug"), t.get("output_markers_note") or "无输出特征"))
    print("\n（elicit 三个特质的衰减**没有表面标记**，必须靠打分：见论文那套评估台。）")
    return 0


def selftest() -> int:
    """用合成数据自验：打分、固定文案排除、覆盖率声明都对。"""
    traits, fixed = load_registry()
    skills = compile_markers(traits)
    fails: list[str] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        print(("  [OK] " if ok else "  [FAIL] ") + name + ((" —— " + detail) if detail else ""))
        if not ok:
            fails.append(name)

    msgs = [
        {"is_bot": False, "text": "都两点了你还不睡"},          # 别人的，不算
        {"is_bot": True, "text": "都两点了，早点睡吧"},          # 提时间 → time_sleep
        {"is_bot": True, "text": "你先睡啦，晚安"},              # 无痕迹
        {"is_bot": True, "text": "作为一个AI助手，很高兴为您解答"},  # 客服腔 → ai_persona_leak
        {"is_bot": True, "text": "你问这个干嘛"},                # 追问 → question_bounce
        {"is_bot": True, "text": "米饭真好吃，深海那边更冷"},      # 角色元素 → prop_abuse
        {"is_bot": True, "text": "晚安啦主人，今天也辛苦了，早点睡~"},  # 固定文案 → 必须排除
    ]
    res = scan(msgs, skills, fixed)
    ck("只算机器人自己的发言", res["bot_messages"] == 5, str(res["bot_messages"]))
    ck("固定文案被排除", res["skipped_fixed"] == 1, str(res["skipped_fixed"]))
    ck("提时间被认出", res["traits"]["time_sleep"]["messages"] == 1,
       str(res["traits"]["time_sleep"]["messages"]))
    ck("客服腔被认出", res["traits"]["ai_persona_leak"]["messages"] == 1)
    ck("追问被认出", res["traits"]["question_bounce"]["messages"] == 1)
    ck("角色元素被认出", res["traits"]["prop_abuse"]["messages"] == 1)
    ck("干净的那条不误报", res["traits"]["commentary"]["messages"] == 0)
    ck("有输出特征的特质 >= 8", len(skills) >= 8, str(len(skills)))
    ck("elicit 三个没有输出特征", all(
        not (t.get("output_markers") or [])
        for t in traits if t.get("slug") in ("genki", "softness", "tsundere")))

    print()
    print("=== 自验：%s ===" % ("全部通过" if not fails else "失败 %d 项" % len(fails)))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="自示监控（论文 ΔP 的文本版同构）")
    ap.add_argument("--log-dir", default=os.path.join(PROJ, "data", "runtime"), help="chatlog 所在目录")
    ap.add_argument("--days", type=float, default=0, help="只看最近 N 天（0 = 全部）")
    ap.add_argument("--top", type=int, default=5, help="排行显示几个特质")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true", help="用合成数据自验")
    ap.add_argument("--pack", default="", help="指定人格包 id（默认用当前激活的那个）")
    args = ap.parse_args()

    if args.pack:
        global TRAITS_FILE
        TRAITS_FILE = os.path.join(PACKS_DIR, args.pack, "traits.json")

    if args.selftest:
        return selftest()

    if not os.path.isfile(TRAITS_FILE):
        print("找不到特质注册表：%s" % TRAITS_FILE)
        print("（它在当前人格包里。用 --pack <id> 指定，或先 /人设 包 看有哪些包）")
        return 1
    print("特质注册表：%s" % TRAITS_FILE)

    traits, fixed = load_registry()
    skills = compile_markers(traits)
    result = empty_result()
    for _name, msgs in iter_chatlogs(args.log_dir, args.days):
        merge(result, scan(msgs, skills, fixed))

    if args.json:
        print(json.dumps({"log_dir": args.log_dir, "days": args.days, **result},
                         ensure_ascii=False, indent=2))
        return 0
    return report(result, skills, traits, args.top, args.log_dir)


if __name__ == "__main__":
    sys.exit(main())
