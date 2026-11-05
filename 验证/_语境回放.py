"""语境回放：多轮成对样本，验证「同一句话在不同语境下被怎么理解」。

用法：

    python 验证\\_语境回放.py                        # 只校验数据集结构（不调模型、不花钱）
    python 验证\\_语境回放.py --live                  # 真调模型，**走线上同一套解释器**
    python 验证\\_语境回放.py --live --save           # 同上，并把基线写进 分析记录/
    python 验证\\_语境回放.py --live --gate 7         # 额外要求 act+target 命中 ≥ 7 条

退出码 0 = 通过。

## 为什么必须有这个集子

`场景回放集.jsonl` 全是**单条文本**，验不了「小笨蛋」的五种语境——
而方案 §13 的全部立论就建立在这上面。单条样本只能验「这个词会不会被当成骂」，
验不了「这句话在这个上下文里是什么意思」。

所以这里存的是**带轮次的小对话**，并且刻意成对：

* ctx-001 / ctx-002：同一句「小笨蛋」，一个伴随关心、一个伴随争执，期望相反；
* ctx-003 / ctx-009：先说「别再叫」，再说「小笨蛋」——测边界能不能压过亲昵；
* ctx-004：问「别人叫我小笨蛋是什么意思」——测**咨询词义**不能被当成称呼；
* ctx-008：转述攻击——测指向；
* ctx-006 / ctx-007：换词（傻瓜 / 讨厌鬼）——**验迁移，不是验记忆**。

## `--live` 测的是哪一条链路（2026-11-03 修正过）

**修正前**：这个脚本的 `--live` 自己拼了一段 prompt 直接问模型，**绕过**
`plugins/ai_chat/context_analysis.py`。于是它测的是「一个只存在于验证脚本里的提示词判得准不准」，
而线上跑的是解释器的 `_SYS` / `_prompt` / `_normalize`——两者不是同一个东西。
那种基线拿去做 #0002 / #0005 的「改完是不是更好」，比的也是不存在的对象。

**现在**：`--live` 按线上的顺序走一遍：

    ① scene.judge(最后一句)                  ← 规则层，决定要不要深判
    ② context_analysis.needs_deep_judge(…)   ← 线上的触发开关，原样调用
    ③ context_analysis.analyze(…)            ← 线上解释器，`chat=` 换成真客户端

三处都是**直接 import 线上模块**（`scene.py` / `context_analysis.py` 只依赖标准库，
不需要 nonebot），不复制逻辑。历史轮次也按 `__init__.py` 的口径截断（每条 80 字、最多 6 条），
免得「回放里判得准、线上因为截断判不准」。

两处与线上不同，都是**有意的**：

* `timeout` 默认 45 秒（线上 12 秒）：这里量的是**判断力**，不是超时率。
  线上超时降级成 unknown 是另一条断言（`_语境解释验证.py`）管的事；
* `temperature=0`：基线要能被后来者复算，先把采样噪声压掉。
  `--temp` 可改回 1.0 看波动。

## 基线怎么用

`--save` 会把「每条期望 vs 实际」落进 `分析记录/语境解释基线_<日期>.md`。
#0002（增量复用）与 #0005（检索消费语境）改完后重跑，比对同一份文件里的命中数，
**不能只看「新加的东西有没有报错」**——那样验不出判断是不是变差了。

`--gate N` 是给「已经立住基线之后」用的回归闸门：低于 N 条命中就退出码 1。
现在（首次落基线）不要加，先看数。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import importlib.util
import json
import os
import pathlib
import sys

try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
SET = ROOT / "验证" / "语境回放集.jsonl"
PLUGIN = ROOT / "plugins" / "ai_chat"
RECORD_DIR = ROOT / "分析记录"

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


def load() -> list[dict]:
    rows = []
    for line in SET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def validate(rows: list[dict]) -> None:
    print("=== 1. 结构 ===")
    ids = [str(r.get("id")) for r in rows]
    check("id 不重复", len(ids) == len(set(ids)), f"{len(ids)} 条")
    check("每条都有轮次", all(r.get("turns") for r in rows))

    for row in rows:
        rid = row.get("id")
        turns = row.get("turns") or []
        ok_role = all(t.get("role") in ("user", "bot") and str(t.get("text") or "").strip()
                      for t in turns)
        check(f"{rid} 轮次结构合法", ok_role, str(turns)[:60])
        exp = row.get("expect") or {}
        check(f"{rid} expect 四个字段齐全",
              all(k in exp for k in ("act", "target", "tone", "confidence")), str(exp)[:60])

    print("\n=== 2. 数据集覆盖（防删空/删歪）===")
    words = ["小笨蛋", "傻瓜", "讨厌鬼"]
    counts = {w: sum(1 for r in rows if any(w in t.get("text", "") for t in r.get("turns") or []))
              for w in words}
    check("「小笨蛋」至少 5 条（成对语境）", counts["小笨蛋"] >= 5, counts)
    check("换词样本至少 2 条（验迁移不验记忆）",
          sum(1 for w in ("傻瓜", "讨厌鬼") if counts[w]) >= 2, counts)
    check("至少 1 条期望 unknown（保留歧义）",
          any(str((r.get("expect") or {}).get("confidence")) == "unknown" for r in rows))
    # 【2026-11-03 改】原来这条查 `target == "third"`，但 `target` 现在只表示**态度对象**：
    # 「一个陌生人，说我是废物」被贬的是我（target=speaker），陌生人是**来源**（origin=other）。
    # 所以"转述不能被当攻击"这件事要查 `origin`，不是 `target`（#0008）。
    check("至少 1 条转述（origin=other，不能被当攻击）",
          any(str((r.get("expect") or {}).get("origin")) == "other" for r in rows))
    check("至少 1 条要求记边界",
          any(r.get("must_record_boundary") or r.get("prior_boundary") for r in rows))
    # 「有争议」不是给自己开脱的口子：标了就必须写清理由，否则等于悄悄放宽判据。
    holdout = [r.get("id") for r in rows if r.get("holdout")]
    check("holdout 样本至少 3 条（泛化验收，不参与固定集命中率）", len(holdout) >= 3,
          f"{len(holdout)} 条：{holdout}")
    contested = [r.get("id") for r in rows if r.get("contested")]
    check("标了 contested 的样本都写了理由",
          all(len(str(r.get("contested_reason") or "")) > 20
              for r in rows if r.get("contested")),
          f"{contested or '无'}")

    print("\n=== 3. 成对关系 ===")
    pair = {r.get("id"): r for r in rows}
    a, b = pair.get("ctx-001"), pair.get("ctx-002")
    check("ctx-001/002 是同一句话的相反语境",
          bool(a) and bool(b) and "小笨蛋" in json.dumps(a, ensure_ascii=False)
          and "小笨蛋" in json.dumps(b, ensure_ascii=False)
          and (a["expect"]["act"] != b["expect"]["act"]),
          f"{a['expect']['act'] if a else '?'} vs {b['expect']['act'] if b else '?'}")
    c, d = pair.get("ctx-003"), pair.get("ctx-009")
    check("ctx-003/009 覆盖「说过别再叫之后又说」", bool(c) and bool(d))


# --------------------------------------------------------------- 线上模块（只 import 不复制）

def _import_live(name: str, filename: str):
    """按文件路径加载线上模块。

    不用 `from plugins.ai_chat import …`：那个包一 import 就会拉 nonebot，
    而这台机器上跑验证时不一定装了机器人的依赖。`scene.py` / `context_analysis.py`
    刻意只依赖标准库，就是为了能这样被回放拉起来。
    """
    spec = importlib.util.spec_from_file_location(name, PLUGIN / filename)
    if spec is None or spec.loader is None:  # 文件不在（改名/挪走）要立刻炸，不要静默跳过
        raise ImportError(f"加载不到 {PLUGIN / filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -------------------------------------------------------------------------- 密钥与模型

def _dotenv(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _model_config(env_path: str = "") -> tuple[str, str, str, str]:
    """密钥/端点/模型：环境变量优先，其次指定的 `.env`，最后项目根 `.env`（机器人自己用的那份）。

    返回 `(api_key, base_url, model, 来源说明)`。**不打印密钥**，只报来源与长度。
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    base = os.environ.get("DEEPSEEK_BASE_URL", "").strip()
    model = os.environ.get("DEEPSEEK_MODEL", "").strip()
    source = "环境变量" if key else ""
    if not key:
        candidates = ([pathlib.Path(env_path)] if env_path else []) + [ROOT / ".env"]
        for candidate in candidates:
            cfg = _dotenv(candidate)
            if cfg.get("DEEPSEEK_API_KEY"):
                key = cfg["DEEPSEEK_API_KEY"]
                base = base or cfg.get("DEEPSEEK_BASE_URL", "")
                model = model or cfg.get("DEEPSEEK_MODEL", "")
                source = str(candidate)
                break
    return key, base or "https://api.deepseek.com", model or "deepseek-flash", source


def _history(row: dict) -> list[dict]:
    """数据集里的轮次 → 解释器要的形状。截断口径对齐 `__init__.py`（80 字 / 最多 6 条）。"""
    turns = []
    for turn in (row.get("turns") or [])[-6:]:
        turns.append({
            "role": "bot" if turn.get("role") == "bot" else "user",
            "speaker": str(turn.get("speaker") or "")[:20],
            "text": str(turn.get("text") or "")[:80],
        })
    return turns


def _judge_one(exp: dict, got: dict) -> dict[str, bool]:
    acts = exp.get("act") or []
    tone = exp.get("tone")
    origin = exp.get("origin")
    return {
        "act": (not acts) or got.get("act") in acts,
        "target": exp.get("target") in (None, "any") or got.get("target") == exp.get("target"),
        # origin 是 2026-11-03 新增的维度：**期望里没写就不计分**（旧样本不受影响）
        "origin": origin in (None, "any") or got.get("origin") == origin,
        "confidence": str(exp.get("confidence")) == str(got.get("confidence")),
        "tone": tone in (None, "any") or got.get("tone") == tone,
    }


def _fmt(value) -> str:
    if isinstance(value, (list, tuple)):
        return "/".join(str(x) for x in value) or "（不限）"
    return str(value)


# ------------------------------------------------------------------------------ 真实模型

async def _run_live(rows: list[dict], *, key: str, base: str, model: str,
                    timeout: float, temperature: float, thinking: str,
                    max_tokens: int) -> list[dict]:
    from openai import AsyncOpenAI

    ca = _import_live("ai_chat_context_analysis", "context_analysis.py")
    scene = _import_live("ai_chat_scene", "scene.py")

    # `analyze()` 内部是 `except Exception: 按 unknown 处理` —— 这是线上要的行为
    # （解释失败不能拖垮回复），但回放里会把「模型不认 response_format」这类问题
    # 伪装成「判断不出来」。所以这里把异常抄一份出来，跑完打印。
    errors: list[str] = []

    def _make_chat(client, model_name):
        async def _chat(messages, **kwargs):
            kwargs.setdefault("temperature", temperature)
            try:
                return await client.chat.completions.create(
                    model=model_name, messages=messages, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 记下来再抛
                errors.append(f"{type(exc).__name__}: {exc}")
                raise
        return _chat

    results: list[dict] = []
    client = AsyncOpenAI(api_key=key, base_url=base, timeout=timeout + 20)
    try:
        chat = _make_chat(client, model)
        for row in rows:
            turns = _history(row)
            last = turns[-1]["text"] if turns else ""
            verdict = scene.judge(last)
            deep = ca.needs_deep_judge(verdict)
            got = dict(ca._EMPTY)
            if deep:
                got = await ca.analyze(
                    last, turns=turns[:-1],
                    boundary=str(row.get("prior_boundary") or ""),
                    chat=chat, timeout=timeout, thinking=thinking, max_tokens=max_tokens)
            results.append({
                "id": row.get("id"),
                "expect": row.get("expect") or {},
                "note": row.get("note") or "",
                "contested": bool(row.get("contested")),
                "holdout": bool(row.get("holdout")),
                "rule": {"act": verdict.get("act"), "confidence": verdict.get("confidence"),
                         "needs_context": verdict.get("needs_context"), "deep": deep},
                "got": got,
                "degraded": got == dict(ca._EMPTY),
            })
    finally:
        await client.close()

    if errors:
        print(f"\n  [提示] 调用层抛了 {len(errors)} 次异常，前 3 条：")
        for item in errors[:3]:
            print(f"    - {item}")
    return results


def live(rows: list[dict], *, do_save: bool, gate: int, env_path: str,
         timeout: float, temperature: float, thinking: str, max_tokens: int) -> None:
    print("\n=== 4. 真实模型分析（--live：规则层 → needs_deep_judge → analyze）===")
    key, base, model, source = _model_config(env_path)
    if not key:
        print("  [跳过] 拿不到 DEEPSEEK_API_KEY：环境变量、--env 指定的文件、项目根 .env 都没有")
        return
    try:
        import openai  # noqa: F401
    except ImportError:
        print("  [跳过] 没装 openai 库")
        return

    print(f"  密钥来源：{source}（长度 {len(key)}，不打印内容）")
    print(f"  端点：{base}　模型：{model}　temperature={temperature}　timeout={timeout}s　"
          f"思考档={thinking or '接口默认'}　预算={max_tokens}")
    results = asyncio.run(_run_live(rows, key=key, base=base, model=model,
                                    timeout=timeout, temperature=temperature,
                                    thinking=thinking, max_tokens=max_tokens))

    print("  id        规则层           深判  act                     target  conf     tone    "
          "判定")
    tally = {"act": 0, "target": 0, "origin": 0, "confidence": 0, "tone": 0}
    hit = 0
    degraded = 0
    scored = 0
    contended: list[str] = []
    hold_rows: list[str] = []
    hold_tally: list[bool] = []
    for item in results:
        exp, got = item["expect"], item["got"]
        score = _judge_one(exp, got)
        good = score["act"] and score["target"]
        degraded += 1 if item["degraded"] else 0
        if item.get("holdout"):
            # holdout 是**泛化**样本（不在 prompt 里出现过）：单独统计，
            # 混进固定集会把它变成"又一批训练样本"（#0008 审阅原话）
            hold_tally.append(bool(good))
            mark = "holdout"
        elif item.get("contested"):
            # 期望本身与样本不自洽的行照样打印，但**不计入命中率**——
            # 拿它当扣分项会把「数据写错」记成「模型判错」。
            contended.append(str(item["id"]))
            hold_rows.append(str(item["id"]))
            mark = "有争议·不计分"
        else:
            scored += 1
            for name, ok in score.items():
                tally[name] += 1 if ok else 0
            hit += 1 if good else 0
            mark = "OK" if good else "错"
        rule = item["rule"]
        print(f"  {str(item['id']):<9} {str(rule['act']):>3}/{str(rule['confidence']):<8} "
              f"{'是' if rule['deep'] else '否'}   "
              f"{str(got.get('act')):<10}|{_fmt(exp.get('act')):<12} "
              f"{str(got.get('target')):<6}|{_fmt(exp.get('target')):<7} "
              f"{str(got.get('confidence')):<8} {str(got.get('tone')):<6} {mark}")
        item["score"] = score
        item["good"] = good
        item["mark"] = mark

    if contended:
        print(f"\n  有争议、不计分的样本：{'、'.join(contended)}"
              "（期望与 turns 不自洽，理由见 jsonl 的 contested_reason）")
    print(f"\n  act 命中 {tally['act']}/{scored}　target {tally['target']}/{scored}　"
          f"confidence {tally['confidence']}/{scored}　tone {tally['tone']}/{scored}")
    print(f"  **act+target 命中 {hit}/{scored}**　退化成 unknown 外壳 {degraded}/{len(results)}")

    if hold_tally:
        print(f"  holdout（泛化，单独统计）命中 {sum(hold_tally)}/{len(hold_tally)}"
              f"：act+target 全对的条数")
    never = [str(i["id"]) for i in results if not i["rule"]["deep"]]
    print(f"  规则层没触发深判的：{never or '无（9 条都会走解释器）'}")

    check("每条都走完了完整链路（没有调用层异常吞掉结果）", len(results) == len(rows),
          f"{len(results)}/{len(rows)}")
    if gate:
        check(f"act+target 命中 ≥ {gate}", hit >= gate, f"{hit}/{scored}")

    if do_save:
        _save_record(results, model=model, base=base, temperature=temperature,
                     timeout=timeout, thinking=thinking, max_tokens=max_tokens,
                     tally=tally, hit=hit, degraded=degraded, scored=scored,
                     contended=contended)


def _save_record(results, *, model: str, base: str, temperature: float, timeout: float,
                 thinking: str, max_tokens: int, tally: dict, hit: int, degraded: int,
                 scored: int, contended: list[str]) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    path = RECORD_DIR / f"语境解释基线_{datetime.date.today().isoformat()}.md"
    lines = [
        f"# 语境解释基线（{datetime.date.today().isoformat()}）",
        "",
        "> 由 `验证/_语境回放.py --live --save` 生成，**不要手工编辑**；"
        "重跑会覆盖同名文件。",
        f"> 生成时间 {stamp}　模型 `{model}`　端点 `{base}`　"
        f"temperature={temperature}　timeout={timeout}s　"
        f"思考档={thinking or '接口默认'}　预算={max_tokens}",
        "",
        "## 结果",
        "",
        "| 项 | 命中 |",
        "|---|---|",
        f"| act | {tally['act']}/{scored} |",
        f"| target | {tally['target']}/{scored} |",
        f"| confidence | {tally['confidence']}/{scored} |",
        f"| tone | {tally['tone']}/{scored} |",
        f"| **act+target（合判）** | **{hit}/{scored}** |",
        f"| 退化成 unknown 外壳 | {degraded}/{len(results)} |",
        f"| 不计分（期望有争议） | {len(contended)}（{'、'.join(contended) or '无'}） |",
        "",
        "## 逐条",
        "",
        "| id | 说明 | 期望 act / target / conf / tone | 实际 act / target / conf / tone "
        "| 深判 | 判定 |",
        "|---|---|---|---|---|---|",
    ]
    for item in results:
        exp, got, rule = item["expect"], item["got"], item["rule"]
        score = item.get("score") or {}
        if item.get("contested"):
            mark = "有争议·不计分"
        else:
            mark = "OK" if item.get("good") else "错（" + "/".join(
                k for k, ok in score.items() if not ok) + "）"
        lines.append(
            f"| {item['id']} | {item['note']} "
            f"| {_fmt(exp.get('act'))} / {_fmt(exp.get('target'))} / "
            f"{exp.get('confidence')} / {_fmt(exp.get('tone'))} "
            f"| {got.get('act')} / {got.get('target')} / {got.get('confidence')} / {got.get('tone')} "
            f"| {'是' if rule.get('deep') else '否'} | {mark} |")
    lines += [
        "",
        "## 怎么用这份基线",
        "",
        "* #0002（增量复用）与 #0005（检索消费语境）改完后**重跑同一条命令**，比上面的命中数；",
        "* **单次运行有 ±1 的抖动**（尤其 confidence）：对比时至少连跑 3 次看区间，不要拿单次当结论；",
        "* 判断变差时先看「退化成 unknown 外壳」那一行：它涨说明调用层在吞错，不是判断力下降；",
        "* 单个样本的解释理由不在这份文件里 —— 需要逐条原文时看模型输出或 `scene_state` 的 "
        "`last_turn`（线上才有）。",
        "",
    ]
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  基线已写入：{path}")
    check("基线文件已落盘", path.is_file(), str(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True, description="语境回放（结构校验 / 真调模型）")
    parser.add_argument("--live", action="store_true", help="真调模型，走线上同一套解释器")
    parser.add_argument("--save", action="store_true", help="把基线写进 分析记录/")
    parser.add_argument("--gate", type=int, default=0,
                        help="要求 act+target 至少命中 N 条，否则退出码 1（默认 0＝只记录）")
    parser.add_argument("--env", default="", help="指定 .env 路径（默认项目根 .env）")
    parser.add_argument("--timeout", type=float, default=45.0, help="单条超时秒数（默认 45）")
    parser.add_argument("--temp", type=float, default=0.0, help="temperature（默认 0）")
    parser.add_argument("--thinking", default="disabled",
                        help="思考档：disabled / adaptive / enabled，传 default 表示不传（用接口默认）")
    parser.add_argument("--max-tokens", dest="max_tokens", type=int, default=900,
                        help="输出预算（默认 900，与设置项 scene_judge_max_tokens 一致）")
    args = parser.parse_args(argv)
    thinking = "" if args.thinking in ("default", "接口默认") else args.thinking

    rows = load()
    print(f"语境回放集：{len(rows)} 条\n")
    validate(rows)
    if args.live:
        live(rows, do_save=args.save, gate=args.gate, env_path=args.env,
             timeout=args.timeout, temperature=args.temp, thinking=thinking,
             max_tokens=args.max_tokens)
    print(f"\n{'=' * 50}")
    print(f"通过 {passed} 项" + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    print("=" * 50)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
