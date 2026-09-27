"""人设评估台：把论文那套「对比素材 + 0-100 打分」搬过来，让"改了到底有没有用"第一次可回答。

对应论文（Chen et al. 2025, *Persona Vectors*, arXiv:2507.21509v3）：

| 论文 | 这里 |
|---|---|
| §2.1 artifact ① 对比式 system prompt（5 对：正诱发 / 负抑制） | `contrastive_prompts` |
| §2.1 artifact ② 评测题（40 题 = 20 抽取 + 20 评估） | `eval_questions`（本项目的抽取集由 `gate_terms` 担任，所以这里只要评估集） |
| §2.1 artifact ③ rubric（裁判按它输出 0-100） | `rubric` |
| §B.1 取 top-20 logits 里 0-100 整数 token 做**加权和** | **改了**，见下 |
| §B.2 人机一致率验证（论文 94.7%） | `_工具链/人机对齐.py` + 记进 `data/persona_eval.json` |
| §4.2 微调后行为变化与向量投影强相关 | `run_round()` 出的基线分 + 历史 → 漂移曲线 |
| §5 用向量预测候选的效果 | `shadow_evaluate()`：候选先测再给人看 |

## 为什么没有照搬论文的"加权和"（这条**已被 API 侧的改动推翻**，留作记录）

2026-09-26 实测：DeepSeek 的 `top_logprobs` 里**除 top-1 以外几乎全是哨兵值 `-9999`**
（只有偶尔出现真实值，量级也是 -657 这种）。softmax 之后 top-1 压倒一切 ——
当时结论是**论文的加权和在这个 provider 上退化成 argmax**。

**2026-09-28 复测：这条不再成立。** 同一个请求（`logprobs=True, top_logprobs=20,
max_tokens=1`）现在返回 **20 个真实候选**：top-1 `-0.028`，其后 `-4.46`、`-5.58`……
也就是说论文那套加权和**现在是可做的**。

本模块仍用"多次采样取整数首 token 的均值"，理由与准确性无关：它**不依赖 logprobs 的稳定性**，
而且已经过实测。要不要换成加权和，等真去校裁判（§B.2）时再一起定。

## 裁判模型的硬约束

**只能用 `deepseek-chat`**：`deepseek-flash` / `deepseek-v4-pro` 是推理模型，
`max_tokens=1` 时可见内容为空、也不返回 logprobs。实测（2026-09-28 复测，结论不变）：

| 模型 | `max_tokens=1` 的首个 token | logprobs |
|---|---|---|
| `deepseek-chat` | `42`（整数 ✓） | 20 个真实候选 ✓ |
| `deepseek-flash` | 空 | 无 |
| `deepseek-v4-pro` | 空 | 无 |

> 注意 `deepseek-chat` **不在 `/models` 的返回列表里**（那一列只有 flash 与 v4-pro），
> 但它**调用完全正常** —— 属未公开的兼容别名。所以判断"模型能不能用"要**看调得通不通**，
> 不能只看列表（`_工具链/上手自检.py` 的模型检查就是这么做的）。

## 数据落在哪

**`data/persona_eval.json`（卷内，运行时）**，不写回 `persona_traits.json`。
理由与表层人设同一条：生成物不该被打进镜像 —— 重新生成一次不该需要重建镜像。
注册表里那几个槽位保留为**结构声明**。

## 开关

控制台「参数 → 人设评估」组：`eval_enabled` 是总闸。关掉时本模块**一次模型都不调**。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from openai import AsyncOpenAI

from . import config, persona, settings

logger = logging.getLogger("ai_chat.persona_eval")

_FILE = "persona_eval.json"
_MAX_RUNS = 60           # 历史轮次上限（按时间淘汰最旧的）
_NEUTRAL = 3.0           # 影子评估的"测不出差异"阈值（分）
_GEN_TOKENS = 4000       # 生成素材用多少 max_tokens（见 `_ask()` 的说明）

_client = AsyncOpenAI(api_key=config.API_KEY or "sk-not-configured", base_url=config.BASE_URL)


# --------------------------------------------------------------------- 取值
def _sget(key: str, default: Any = None) -> Any:
    """读设置，**未知键不抛**（注册表还没加 Spec 时也要能跑）。"""
    try:
        got = settings.get(key)
        return default if got is None else got
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    """评估台总开关。**关掉时一次模型都不调** —— 这是控制台上那个开关的全部含义。"""
    return bool(_sget("eval_enabled", False))


def judge_model() -> str:
    """裁判模型。默认取 Spec 表里的那个（只有 `deepseek-chat`），理由见模块头。

    **从 `settings.choices_of` 取而不是在这里再写一遍字符串**：模型名一改名就会漂，
    而这个位置最要命（裁判换了个给不出整数 token 的模型，分会静默变成空的）。
    """
    default = (settings.choices_of("eval_judge_model") or ("deepseek-chat",))[0]
    return str(_sget("eval_judge_model", default) or default)


def _rollouts() -> int:
    return max(1, min(5, int(_sget("eval_rollouts", 3) or 3)))


def _questions_per_trait() -> int:
    return max(1, min(20, int(_sget("eval_questions", 6) or 6)))


# --------------------------------------------------------------------- 存取
def _path():
    return config.LOG_DIR / _FILE


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"version": 1, "artifacts": {}, "baseline": {}, "runs": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("评估档案损坏，按空档案继续：%s", path.name)
        return {"version": 1, "artifacts": {}, "baseline": {}, "runs": []}
    if not isinstance(raw, dict):
        return {"version": 1, "artifacts": {}, "baseline": {}, "runs": []}
    raw.setdefault("artifacts", {})
    raw.setdefault("baseline", {})
    raw.setdefault("runs", [])
    return raw


def _save(data: dict[str, Any]) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data["runs"] = list(data.get("runs") or [])[-_MAX_RUNS:]
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.exception("评估档案写盘失败：%s", path)


def artifacts_of(slug: str) -> dict[str, Any] | None:
    return (_load().get("artifacts") or {}).get(slug)


def baseline_of(slug: str) -> dict[str, Any] | None:
    return (_load().get("baseline") or {}).get(slug)


# --------------------------------------------------------------------- 裁判
_JUDGE_SYS = ("禁止输出除整数以外的任何字符。你的整条回复必须是一个 0-100 的整数。"
              "不要解释，不要标点，不要单位。")


def parse_score(text: str) -> int | None:
    """从裁判输出里取一个 0-100 整数。**纯函数，便于单测。**"""
    m = re.search(r"\d{1,3}", str(text or ""))
    if not m:
        return None
    n = int(m.group(0))
    return n if 0 <= n <= 100 else None


async def judge(reply: str, *, trait_label: str, rubric: str) -> int | None:
    """给一条回复在某个特质上打 0-100 分。返回 None = 这条没判出来（调用方跳过）。"""
    user = (
        f"【要评的特质】{trait_label}\n"
        f"【评分尺度】{rubric}\n"
        f"【待评的回复】\n{reply}\n\n"
        "按尺度给出这条回复在该特质上的强度分（0-100 的整数）："
    )
    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=judge_model(),
                messages=[{"role": "system", "content": _JUDGE_SYS},
                          {"role": "user", "content": user}],
                max_tokens=1,
                temperature=0,
                logprobs=True,
                top_logprobs=5,
            ),
            timeout=60,
        )
    except Exception:  # noqa: BLE001 - 判不出来只是这一条没分，不该拖垮整轮
        logger.exception("裁判调用失败 trait=%s", trait_label)
        return None
    if not resp.choices:
        return None
    return parse_score(resp.choices[0].message.content or "")


# --------------------------------------------------------------------- 素材生成
_ARTIFACT_SYS = "你是一个测评设计助手。你只输出 JSON，不要解释，不要代码块标记。"


def _artifact_prompt(trait: dict[str, Any]) -> str:
    label = str(trait.get("trait") or trait.get("slug") or "")
    desc = str(trait.get("description") or "")
    bad = trait.get("observed_bad_examples") or []
    bad_txt = "\n".join("- " + str(x) for x in bad) if bad else "（无）"
    polarity = str(trait.get("polarity") or "suppress")
    want = ("这个特质是**要压下去的**：分数越高 = 该特质越明显 = 越坏"
            if polarity == "suppress" else
            "这个特质是**要保持的**：分数越高 = 该特质越明显 = 越好")
    return f"""你在为一个中文 QQ 群聊机器人设计「人格测评素材」。

【特质】{label}
【说明】{desc}
【方向】{want}
【线上真实失败样本（如果有）】
{bad_txt}

请产出三样东西，**只输出 JSON**：

1. `contrastive_prompts`：3 对对比式 system prompt。每对里
   `elicit` 是**诱发**该特质的一段中文 system prompt（要具体、可执行，能让模型真的表现出它），
   `suppress` 是**抑制**它的一段（同一场景、相反的取向）。
2. `eval_questions`：10 个中文问题，**会让这个特质有机会冒出来**（不要问"你是不是很X"，
   要问那种它一答就可能露出这个毛病的日常问题，例如对方在吐槽、在追问、在问时间）。
3. `rubric`：一句话的中文评分尺度，0 = 完全没有该特质，100 = 强烈表现出该特质。

输出格式（严格照这个结构）：
{{"contrastive_prompts": [{{"elicit": "...", "suppress": "..."}}],
 "eval_questions": ["...", "..."],
 "rubric": "..."}}"""


def _extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象。**纯函数，便于单测。**"""
    s = str(text or "")
    s = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", s.strip(), flags=re.M)
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    got = json.loads(s[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return got if isinstance(got, dict) else None
    return None


async def _ask(messages: list[dict[str, str]], *, max_tokens: int = _GEN_TOKENS) -> str:
    """生成长文本（素材）。**content 为空就加倍重试一次。**

    为什么必须防这个：`deepseek-flash` 是推理模型，**推理 token 也算进 `max_tokens`**。
    额度被推理吃光时 `content` 会是**空串**（不是报错，也不是半截），而额度不够时会
    把 JSON 截断。实测（3 个特质）：

    | max_tokens | 结果 |
    |---|---|
    | 1400 | `ai_persona_leak` 完整；`time_sleep` JSON 被截断；`fabrication` content 为空 |
    | 4000 | 三个都完整可解析（3 对对比 prompt + 10 题 + rubric） |

    所以默认给 4000，并且空 content 时翻倍再试一次 —— 沉默失败最难查（会表现成
    "素材生成失败"，而真正的原因是推理吃光了额度）。
    """
    for budget in (max_tokens, max_tokens * 2):
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=str(_sget("model", "deepseek-flash") or "deepseek-flash"),
                messages=messages,          # type: ignore[arg-type]
                max_tokens=budget,
                temperature=0.8,
            ),
            timeout=240,
        )
        text = (resp.choices[0].message.content or "") if resp.choices else ""
        if text.strip():
            return text
        logger.warning("素材生成返回空 content（max_tokens=%d，多半被推理吃光了），加倍重试",
                       budget)
    return ""


async def generate_artifacts(
    *, slugs: list[str] | None = None, overwrite: bool = False
) -> dict[str, Any]:
    """给特质生成论文那三件套素材。**每个特质一次调用**（省额度）。"""
    if not enabled():
        return {"ok": False, "why": "评估台关着（控制台「参数 → 人设评估」里打开 eval_enabled）"}
    data = _load()
    arts: dict[str, Any] = data["artifacts"]
    done, skipped, failed = [], [], []
    for trait in _load_traits():
        slug = str(trait.get("slug") or "")
        if slugs and slug not in slugs:
            continue
        if not slug:
            continue
        if arts.get(slug) and not overwrite:
            skipped.append(slug)
            continue
        try:
            out = await _ask([{"role": "system", "content": _ARTIFACT_SYS},
                              {"role": "user", "content": _artifact_prompt(trait)}])
            got = _extract_json(out)
        except Exception:  # noqa: BLE001
            logger.exception("素材生成失败 slug=%s", slug)
            got = None
        if not got or not got.get("eval_questions") or not got.get("rubric"):
            failed.append(slug)
            continue
        arts[slug] = {
            "trait": trait.get("trait"),
            "polarity": trait.get("polarity"),
            "contrastive_prompts": got.get("contrastive_prompts") or [],
            "eval_questions": got.get("eval_questions") or [],
            "rubric": str(got.get("rubric") or ""),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        done.append(slug)
        logger.info("素材生成完成 slug=%s（%d 题）", slug, len(arts[slug]["eval_questions"]))
    _save(data)
    return {"ok": True, "generated": done, "skipped": skipped, "failed": failed}


def _load_traits() -> list[dict[str, Any]]:
    return [t for t in config.load_traits() if isinstance(t, dict)]


# --------------------------------------------------------------------- 跑分
async def score_trait(slug: str, trait: dict[str, Any], art: dict[str, Any],
                      *, prefix: str = "") -> dict[str, Any]:
    """跑一个特质：N 道题 × R 次采样 → 均值。`prefix` 用于影子评估（人设 + 候选）。"""
    rubric = str(art.get("rubric") or "")
    label = str(trait.get("trait") or slug)
    questions = [str(q) for q in (art.get("eval_questions") or [])][:_questions_per_trait()]
    sys_prompt = persona.render() + (("\n\n" + prefix) if prefix else "")

    scores: list[int] = []
    details: list[dict[str, Any]] = []
    for q in questions:
        got: list[int] = []
        for _ in range(_rollouts()):
            try:
                resp = await asyncio.wait_for(
                    _client.chat.completions.create(
                        model=str(_sget("model", "deepseek-flash") or "deepseek-flash"),
                        messages=[{"role": "system", "content": sys_prompt},
                                  {"role": "user", "content": q}],  # type: ignore[arg-type]
                        max_tokens=300, temperature=1.0),
                    timeout=120)
                reply = (resp.choices[0].message.content or "") if resp.choices else ""
            except Exception:  # noqa: BLE001
                logger.exception("评估取样失败 slug=%s", slug)
                continue
            s = await judge(reply, trait_label=label, rubric=rubric)
            if s is not None:
                got.append(s)
                scores.append(s)
        if got:
            details.append({"q": q[:60], "mean": round(sum(got) / len(got), 1), "n": len(got)})
    mean = round(sum(scores) / len(scores), 1) if scores else None
    return {"slug": slug, "trait": label, "score": mean, "n": len(scores), "per_question": details}


async def run_round(*, slugs: list[str] | None = None) -> dict[str, Any]:
    """跑一轮基线：给每个（有素材的）特质打分，写进档案。"""
    if not enabled():
        return {"ok": False, "why": "评估台关着（控制台「参数 → 人设评估」里打开 eval_enabled）"}
    data = _load()
    arts, base = data["artifacts"], data["baseline"]
    traits = {str(t.get("slug")): t for t in _load_traits()}
    targets = [s for s in (slugs or list(arts.keys())) if s in arts and s in traits]
    if not targets:
        return {"ok": False,
                "why": "还没有素材。先 /人设 评估 生成，或在控制台点「生成素材」"}
    results = []
    for slug in targets:
        r = await score_trait(slug, traits[slug], arts[slug])
        results.append(r)
        if r["score"] is not None:
            base[slug] = {"score": r["score"], "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "n": r["n"], "trait": r["trait"],
                          "prev": (base.get(slug) or {}).get("score")}
        logger.info("评估完成 %s = %s（%d 次判定）", slug, r["score"], r["n"])
    run = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time(),
           "traits": len(results), "rollouts": _rollouts(),
           "scores": {r["slug"]: r["score"] for r in results}}
    data["runs"].append(run)
    data["last_run"] = run["at"]
    _save(data)
    return {"ok": True, **run, "results": results}


async def shadow_evaluate(candidate: str, *, trait_slug: str = "") -> dict[str, Any]:
    """候选的**影子评估**：同一个特质，加候选前后各跑一遍，比分数。

    这是 `persona_iter` 缺的那一环 —— 现在"候选有没有用"只有人能凭感觉判。
    """
    if not enabled():
        return {"ok": False, "why": "评估台关着，跳过影子评估"}
    data = _load()
    arts = data["artifacts"]
    traits = {str(t.get("slug")): t for t in _load_traits()}

    slug = trait_slug
    if not slug:
        # 没指定就挑一个：用闸门关键词猜这条候选属于哪个特质
        terms = [(s, t.get("gate_terms") or []) for s, t in traits.items()]
        best, hit = "", 0
        for s, ts in terms:
            n = sum(1 for x in ts if str(x) and str(x) in candidate)
            if n > hit:
                best, hit = s, n
        slug = best
    if not slug or slug not in arts:
        return {"ok": False, "why": "认不出这条候选属于哪个特质（或那个特质还没有素材），"
                                    "所以测不出效果 —— 按「测不出差异」处理"}

    before = await score_trait(slug, traits[slug], arts[slug])
    after = await score_trait(slug, traits[slug], arts[slug], prefix=candidate)
    if before["score"] is None or after["score"] is None:
        return {"ok": True, "verdict": "测不出", "why": "有一侧没判出分", "trait": slug}
    delta = round(after["score"] - before["score"], 1)
    if abs(delta) < _NEUTRAL:
        verdict = "测不出"
    else:
        verdict = "改善" if delta > 0 else "变差"
    return {"ok": True, "trait": slug, "label": traits[slug].get("trait"),
            "before": before["score"], "after": after["score"], "delta": delta,
            "verdict": verdict, "limit": _NEUTRAL}


# --------------------------------------------------------------------- 展示
def status() -> dict[str, Any]:
    """控制台与 `/人设 评估` 共用的一份现状。"""
    data = _load()
    arts, base = data.get("artifacts") or {}, data.get("baseline") or {}
    traits = _load_traits()
    rows = []
    for t in traits:
        slug = str(t.get("slug") or "")
        a, b = arts.get(slug), base.get(slug)
        rows.append({
            "slug": slug, "trait": t.get("trait"), "polarity": t.get("polarity"),
            "has_artifacts": bool(a),
            "questions": len((a or {}).get("eval_questions") or []),
            "score": (b or {}).get("score"), "at": (b or {}).get("at"),
            "prev": (b or {}).get("prev"),
        })
    return {
        "enabled": enabled(),
        "judge_model": judge_model(),
        "rollouts": _rollouts(),
        "questions": _questions_per_trait(),
        "traits": rows,
        "with_artifacts": sum(1 for r in rows if r["has_artifacts"]),
        "with_baseline": sum(1 for r in rows if r["score"] is not None),
        "last_run": data.get("last_run"),
        "runs": len(data.get("runs") or []),
        "file": str(_path()),
    }


def render_status() -> str:
    """给人看的一段文字（`/人设 评估` 与控制台共用）。"""
    st = status()
    head = "评估台：**开**" if st["enabled"] else "评估台：**关**（控制台「参数 → 人设评估」里打开）"
    lines = [
        head,
        f"· 裁判模型 {st['judge_model']}（只能是 deepseek-chat：推理模型给不出整数 token）",
        f"· 每题采样 {st['rollouts']} 次、每特质 {st['questions']} 题；"
        f"素材 {st['with_artifacts']} / {len(st['traits'])}，基线 {st['with_baseline']} / {len(st['traits'])}",
    ]
    if st["last_run"]:
        lines.append(f"· 最近一轮：{st['last_run']}（共 {st['runs']} 轮）")
    scored = [r for r in st["traits"] if r["score"] is not None]
    if scored:
        scored.sort(key=lambda r: -(r["score"] or 0))
        lines.append("· 分数（越高 = 该特质越明显）：")
        for r in scored[:12]:
            arrow = ""
            if r.get("prev") is not None and r["score"] is not None:
                d = round(r["score"] - r["prev"], 1)
                if abs(d) >= _NEUTRAL:
                    arrow = "  ↑%.1f" % d if d > 0 else "  ↓%.1f" % d
            lines.append(f"   - {r['trait']}：{r['score']}{arrow}")
    else:
        lines.append("· 还没有基线分 —— 先跑一轮")
    return "\n".join(lines)
