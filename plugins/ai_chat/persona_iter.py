"""人格自我迭代（路线 C）：定期反思 → 生成候选 → **过冲突闸门** → 只写表层。

## 它是三层结构里唯一有"写权限"的东西

```
底层人设（persona/active/base.txt）    ← 只有用户能改；本模块**没有写它的代码**
禁止事项（persona/active/forbidden.txt）← 同上
表层人设（persona/active/surface.txt）  ← 本模块唯一的写入目标，且必须先过 persona.validate_surface()
```

「没有写它的代码」不是修辞：本模块只调 `config.surface_file_path()`，
另外两个文件的路径连引用都没有。所以即使提示词被注入攻击劫持，
它也拿不到改底层人设的入口。

## 为什么路线 C 现在可以做，而之前建议拦

之前的判断是「路线 C 的产物没有外部依据可验证」，所以风险最高。三层结构把这个危险
**变成了一个可判定的问题**：候选与底层/铁律冲突吗？冲突就丢。

真正无法验证的只剩"表层内部累积的自洽性"，而那一层：
* 每次改动都进 `data/runtime/persona/changelog.json`（写入与丢弃都记）；
* `/人设 撤回` 一条命令撤销；
* 改动后会**主动通知主人**（自动写入但不偷偷写）。

## 它读什么

| 输入 | 为什么 |
|---|---|
| 最近的聊天记录 | 这是唯一能看出"我最近说话哪里不对"的材料 |
| 人设信号账本（`signals.py`） | **用户显式提过的要求**，权重远高于自己猜的 |
| 当前三层内容 | 避免重复提议已经在里面的话；也是给模型的约束材料 |

**刻意不做**的事：不读 `strip_self_meta` 的过滤日志、不读情绪、不推断关系。
输入越窄，产物越可核对 —— 这是这一版最重要的取舍。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any


from . import chatlog, clock, config, llm, persona, settings, signals

logger = logging.getLogger("ai_chat.persona_iter")


_MAX_CANDIDATE = 200     # 与 persona._MAX_ITEM 对齐（闸门也会挡一次）


_PROMPT = """你在帮一个群聊机器人「{name}」**改进它说话的方式**。

## 你要改的是哪一层（非常重要）

它的人格分三层，**你只能改最下面那层**：

1. **底层人设**（它是谁）—— **你绝对不能改**。下面是它的原文，请当作不可动摇的前提：
<底层人设>
{base}
</底层人设>

2. **禁止事项**（铁律）—— **你绝对不能违反**。任何与这些冲突的建议会被系统直接丢弃：
<禁止事项>
{forbidden}
</禁止事项>

3. **表层人设**（怎么说话）—— **这才是你要改的**。下面是它现在的内容：
<表层人设>
{surface}
</表层人设>

## 你的任务

看下面的聊天记录和"主人提过的要求"，找出**表层人设里缺的、或者该改的**说话方式，
输出 0~{max_items} 条具体的改进条目。

### 输出要求

- 每条是一句**给机器人自己看的指令**，中文，第二人称（"你…"），不超过 {max_len} 字；
- 只谈**怎么说话**：长度、语气、开头方式、什么时候多说什么时候少说、怎么应对某类话；
- **不要**谈它是谁、它的性格、它的身份、叫它什么（那些在上面两层，你改不动）；
- **不要**重复表层人设里已经有的内容（除非是要把它改得更准，那就在 reason 里说明）；
- 宁可少给、给准。**给不出就返回空数组** —— 硬凑的条目比没有更糟。

### 特别注意

聊天记录里主人明确提过的要求（见下面"主人提过的要求"）**优先级最高**：
如果他反复提同一件事，那就是真要改的地方。没有这类要求时，
才从聊天记录里看"它哪句话说得不像群里的人"。

## 输入材料

【主人提过的要求（按次数排序）】
{demands}

【最近的聊天记录】
{lines}

## 输出格式

只输出 JSON，不要任何多余文字：
{{"candidates": [{{"text": "你要加进表层人设的那句话", "reason": "为什么（引用上面哪条依据）"}}]}}

没有值得改的就输出 {{"candidates": []}}。"""


def _recent_lines(limit: int) -> list[str]:
    """挑最近有点内容的对话（跨会话，最近的优先）。"""
    out: list[str] = []
    for conv, log in list(chatlog.all_logs().items()):
        for msg in log.messages[-limit:]:
            text = chatlog.ConversationLog.clip_text(msg.get("text", ""), 120)
            if not text or len(text) < 4:
                continue
            who = "你" if msg.get("is_bot") else str(msg.get("name", ""))
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(msg.get("ts") or 0)))
            out.append((float(msg.get("ts") or 0), f"[{stamp} {who}] {text}", conv))
    out.sort(key=lambda x: x[0])
    return [x[1] for x in out[-limit:]]


def _demands_text() -> tuple[str, int]:
    """把"主人提过的要求"按次数排出来 —— 这是权重最高的输入。

    直接复用路线 A 第 1 步攒下的信号账本：它是**用户亲口说的**，
    比模型自己从聊天里猜准得多。次数就是天然的重要性权重。
    """
    counts = signals.counts()
    if not counts:
        return "（还没有——主人还没提过说话方式上的要求）", 0
    rows = sorted(counts.items(), key=lambda kv: -kv[1])
    lines: list[str] = []
    for kind, n in rows[:8]:
        label = signals._LABELS.get(kind, kind)  # noqa: SLF001 - 同类内取个显示名
        quotes = [
            str(x.get("quote") or "")
            for x in signals.all_items()
            if x.get("kind") == kind
        ][-2:]
        lines.append(f"· {label}（说过 {n} 次）原话：{'、'.join(q for q in quotes if q)}")
    return "\n".join(lines), sum(counts.values())


async def _ask_model(base: str, forbidden: str, surface: str, demands: str, lines: list[str]) -> list[dict[str, Any]]:
    """调一次模型产出候选。失败返回空列表，绝不抛。"""
    prompt = (
        _PROMPT.replace("{name}", str(config.bot_name()))
        .replace("{base}", base[:2500])
        .replace("{forbidden}", forbidden[:1500])
        .replace("{surface}", surface[:2500])
        .replace("{demands}", demands)
        .replace("{max_items}", str(int(settings.get("persona_iter_max"))))
        .replace("{max_len}", "60")
        .replace("{lines}", "\n".join(lines)[-4000:])
    )
    try:
        resp = await asyncio.wait_for(
            llm.chat(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=900,
            ),
            timeout=config.TIMEOUT * 2,
        )
        raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - 反思失败绝不能影响聊天
        logger.info("人格反思调用失败（不影响聊天）")
        return []
    items = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items[: int(settings.get("persona_iter_max"))]:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        out.append({"text": text[:_MAX_CANDIDATE], "reason": str(item.get("reason") or "")[:160]})
    return out


async def reflect_once(*, notify: bool = True) -> dict[str, Any]:
    """跑一次自我反思。返回统计：写入几条、被拦下几条、为什么没跑。

    **每一步都能单独失败而不影响别的**：模型调用失败 → 空结果；写盘失败 → 记日志。
    这个函数永远不会抛异常 —— 它由后台循环调用。
    """
    if not settings.get("persona_iter_enabled"):
        return {"ok": False, "why": "自我迭代开关关着（控制台「人格」组可以打开）"}
    if not llm.api_key():
        return {"ok": False, "why": "没配 API Key"}

    base, forbidden, surface = persona.base_text(), persona.forbidden_text(), persona.surface_text()
    if not base.strip():
        # 没有底层人设就没有"约束"可言，闸门也就失去依据 —— 拒绝跑
        return {"ok": False, "why": "底层人设是空的，没有约束可依，先写 persona/active/base.txt"}

    lines = _recent_lines(int(settings.get("persona_iter_min_lines")))
    if len(lines) < 3:
        return {"ok": False, "why": f"最近的对话太少（{len(lines)} 条），没什么可反思的"}
    demands, n_demands = _demands_text()

    candidates = await _ask_model(base, forbidden, surface, demands, lines)

    # 默认**不直接生效**，先进候选池等人采纳。
    # 为什么：闸门只拦"碰铁律"，拦不住风格跑偏（学成话痨、学成另一个语气），
    # 而那种改动照过闸门、下一轮就生效，人在群里只觉得"它今天怪怪的"。
    # 开关 `persona_iter_auto_apply` 打开则退回改造前的直接生效行为。
    auto_apply = bool(settings.get("persona_iter_auto_apply"))
    written: list[str] = []
    proposed: list[str] = []
    rejected: list[dict[str, str]] = []
    for cand in candidates:
        if auto_apply:
            got = persona.apply_candidate(
                cand["text"], reason=cand.get("reason", ""), source="自我反思"
            )
            if got.get("written"):
                written.append(cand["text"])
            elif got.get("code") not in ("duplicate",):
                rejected.append({"text": cand["text"], "why": str(got.get("why") or ""), "code": str(got.get("code"))})
            continue
        got_p = persona.propose_candidate(
            cand["text"], reason=cand.get("reason", ""), source="自我反思"
        )
        if got_p.get("proposed"):
            proposed.append(cand["text"])
        elif got_p.get("code") == "duplicate":
            pass
        elif got_p.get("code") == "forbidden_kw" or got_p.get("code") == "base_pronoun" \
                or got_p.get("code") == "base_similar" or got_p.get("code") == "too_short":
            # 闸门当场拦下的 —— 与直接生效时的口径一致，列出来给人看
            rejected.append({"text": cand["text"], "why": str(got_p.get("why") or ""), "code": str(got_p.get("code"))})

    result = {
        "ok": True,
        "written": len(written),
        "proposed": len(proposed),
        "rejected": len(rejected),
        "candidates": len(candidates),
        "auto_apply": auto_apply,
        "demands": n_demands,
        "lines": len(lines),
        "texts": written,
        "proposed_texts": proposed,
        "rejected_detail": rejected,
    }
    logger.info(
        "人格反思完成：候选 %d → %s、拦下 %d（依据：%d 条主人要求 / %d 行对话）",
        len(candidates),
        (f"写入 {len(written)}" if auto_apply else f"进候选池 {len(proposed)}"),
        len(rejected), n_demands, len(lines),
    )
    if notify and (written or proposed or rejected):
        await _notify(written, rejected, proposed=proposed)
    return result


async def _notify(
    written: list[str],
    rejected: list[dict[str, str]],
    *,
    proposed: list[str] | None = None,
) -> None:
    """改动后**主动告诉主人**。自动写入但不偷偷写 —— 这是它能被信任的前提。

    发不出去也不算失败：变更日志里都有，`/人设 日志` 随时能看。
    """
    try:
        from nonebot import get_bots

        bots = get_bots()
        if not bots:
            return
        bot = next(iter(bots.values()))
        master = int(settings.get("master_qq") or 0)
        if not master:
            return
        proposed = proposed or []
        if proposed:
            # 候选池模式下：没写任何东西，是"请你看一眼要不要"
            lines = ["【我想改一下自己的说话方式，等你定】"]
            lines.append("我提议（**还没生效**，只是候选）：")
            lines += [f"· {t[:60]}" for t in proposed]
            if rejected:
                lines.append("另有想改但被铁律拦下的（没进候选）：")
                lines += [f"· {r['text'][:40]} —— {r['why'][:50]}" for r in rejected[:3]]
            lines.append("采纳：/人设 采纳 <序号>　否决：/人设 否决 <序号>　全部：/人设 候选")
            await bot.send_private_msg(user_id=master, message="\n".join(lines))
            return
        lines = ["【我改了自己的说话方式】"]
        if written:
            lines.append("写进去了：")
            lines += [f"· {t[:60]}" for t in written]
        if rejected:
            lines.append("想改但被铁律拦下了（没写入）：")
            lines += [f"· {r['text'][:40]} —— {r['why'][:50]}" for r in rejected[:3]]
        lines.append("不满意就 /人设 撤回（撤最近一条），或者 /人设 日志 看明细。")
        await bot.send_private_msg(user_id=master, message="\n".join(lines))
    except Exception:  # noqa: BLE001
        logger.exception("人格改动通知发送失败（不影响别的）")


async def reflect_loop() -> None:
    """后台定期反思。**第一次等一会儿再跑**，避开冷启动。"""
    await asyncio.sleep(300)
    while True:
        try:
            if settings.get("persona_iter_enabled"):
                await reflect_once(notify=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("人格反思循环出错（下一轮继续）")
        try:
            await asyncio.sleep(max(1800, int(settings.get("persona_iter_interval"))))
        except asyncio.CancelledError:
            raise


def stats() -> dict[str, Any]:
    """给控制台与启动日志看的现状。"""
    added = persona.changelog(500, action="added")
    rejected = persona.changelog(500, action="rejected")
    return {
        "enabled": bool(settings.get("persona_iter_enabled")),
        "interval": int(settings.get("persona_iter_interval")),
        "max": int(settings.get("persona_iter_max")),
        "written_total": len(added),
        "rejected_total": len(rejected),
        "last_added": added[-1] if added else None,
        "last_rejected": rejected[-1] if rejected else None,
        "demands": sum(signals.counts().values()),
    }
