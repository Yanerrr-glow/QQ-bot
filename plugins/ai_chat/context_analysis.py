"""语境解释器：给一段对话，判断**最后一句**在这个语境里是什么意思。

## 它解决什么

`scene.judge()` 只能做快速否定（硬辱骂 / 纯应答），遇到「小笨蛋」这类词一律
`unknown`——因为它看不出上下文。这个模块补的就是那一步：

    规则层说「这里有个需要解释的词」
      → 解释器带上下文问一次模型
      → 得到 act / target / tone / intent / reception / evidence
      → 写进 session 语境（`scene_state`），供检索与状态更新共用

## 三条约束

1. **不是每轮都调**（方案 §14.4 第四条）：只有规则层标了 `needs_context`、
   或者调用方明确要求深判时才走这里。
2. **证据必须对得上**：模型只能引用**实际提供给它的**轮次（「本轮消息」/「第 N 条」），
   引用不存在的一律丢弃——防「编造关系证据」。
3. **看不清就说 unknown**：允许返回空解释，不允许硬给一个情绪方向。

## 边界

这里只解释**这一句**，不修改五轴、不记边界、不决定要不要继续某个称呼。
那些是调用方的事（`persona_vector.apply` / `scene_state.note_boundary`）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("ai_chat.context_analysis")

# 与 `scene.ACTS` **逐项一致**，只多一个「不知道」——那是模型的自我报告，不是场景。
# 少一项就会被 `_split_choice` 打回 unknown（「告别」以前就是这样丢的）。
# `验证/_语法检查.py` 里有断言钉住两者，改一边会红。
ACTS = ("求助", "诉苦", "分享", "玩笑", "纠正", "骂", "亲密", "寒暄", "指令", "告别",
        "夸赞", "不知道")
# `target` 只表示**态度对象**：这句话的褒贬/要求冲着谁（2026-11-03 按 #0008 拆开）。
TARGETS = ("bot", "speaker", "third", "none")
# `origin` 表示**言论来源**：这句话是本人说的，还是在转述别人的话/行为。
# 拆开的理由：`ctx-008`「一个陌生人，说我是废物」——被贬低的是 **speaker（我）**，
# 陌生人是**来源**。一个字段同时装这两件事，怎么填都能自圆其说（审阅原话）。
ORIGINS = ("self", "other", "unknown")
TONES = ("亲昵", "敌意", "认真", "不满", "开玩笑", "不明确")
INTENTS = ("拉近距离", "表达关心", "贬低", "求助", "陈述事实", "试探", "不明确")
RECEPTIONS = ("可能接受", "可能不接受", "已明确拒绝", "不适用", "不明确")

_EMPTY = {"act": "unknown", "target": "none", "origin": "unknown", "confidence": "unknown",
          "tone": "不明确", "intent": "不明确", "reception": "不明确",
          "evidence": [], "evidence_unresolved": [], "evidence_dropped": 0,
          "alternatives": [], "pending": {}}

_SYS = (
    "你在判断 QQ 对话里**最后一句**的含义。同一个词在不同语境下意思可能相反，"
    "不要因为出现某个词就下定论。看不清就填 unknown。"
)


def _prompt(turns: list[dict[str, Any]], boundary: str, context: str = "") -> str:
    lines = []
    for index, turn in enumerate(turns, 1):
        who = "我" if turn.get("role") == "bot" else (turn.get("speaker") or "对方")
        mark = " ← 判断这一句" if index == len(turns) else ""
        lines.append(f"{index}. {who}：{turn.get('text', '')}{mark}")
    body = "\n".join(lines)
    extra = ""
    if context:
        extra += f"\n\n【已经知道的语境】{context}"
    if boundary:
        extra += f"\n\n【已知的边界】他之前明确说过：{boundary}"
    # **枚举必须写进 prompt**（2026-11-03 修正）。
    # 原来的模板写的是 `"tone":"…"`、`"intent":"…"`、`"reception":"…"`——只给了省略号，
    # 模型就填自然语言（`"亲昵、调侃、宠溺"`、`"委屈、无奈"`），而 `_normalize` 按白名单
    # 精确匹配，于是三个字段在 9 条样本上**全部**被打回默认值。`act` 同理会写成
    # `"玩笑/亲密"`、`"求助（求证/解读）"`，整条降成 unknown。
    # 现在把候选值直接摆出来并声明「只能填一个」，`_normalize` 那头再做复合值兜底。
    enums = (
        f"- act：{' / '.join(a for a in ACTS if a != '不知道')}\n"
        f"- target（态度对象：这句话的褒贬或要求冲着谁）：{' / '.join(TARGETS)}\n"
        f"- origin（言论来源：本人说的，还是转述别人的）：{' / '.join(ORIGINS)}\n"
        "- confidence：clear / fuzzy / unknown\n"
        f"- tone：{' / '.join(TONES)}\n"
        f"- intent：{' / '.join(INTENTS)}\n"
        f"- reception：{' / '.join(RECEPTIONS)}\n"
    )
    rules = (
        "- `target` 问的是**态度冲着谁**，不是「话从哪来」：「老板骂我废物」被骂的是 **speaker**"
        "（来源是老板，填 `origin=other`）；「他就是个废物」的对象才是 `third`。\n"
        "- 称呼类词（小笨蛋 / 傻瓜 / 讨厌鬼）**本身不带褒贬**：要看这句里还有没有别的信息——"
        "是在帮忙、在关心，还是在指责「什么都做不好」。\n"
    )
    if boundary:
        rules += ("- 【已知的边界】里记着他明确要求过别再叫某个称呼时，这次再用**不能**按亲昵解释"
                  "（边界优先于口气）。\n")
    rules += (
        "- `pending` 只回答**他有没有留下没说完/没答的事**。"
        "没有任何未完成项就填 `{}`（空对象），不要为了填而编。\n"
        "- 判定完成要严：只有他明确说这件事已有结论、已经不需要、或已经完成，才算完成；"
        "寒暄、换话题、「嗯/好/收到」、重复先说过的要求、只是再次提起，都**不算**完成。"
        "`detail` 要用他自己的话概括**结论**（≤40 字），不要复述待办本身。\n"
    )
    return (
        f"【对话】\n{body}{extra}\n\n"
        "请判断**最后一句**：是在做什么、指向谁、什么口气、他大概想得到什么反应；"
        "如果之前已经明确拒绝过某件事，这次算不算又越界。\n\n"
        f"{rules}\n"
        "**每个字段只能填下面候选值里的一个**，不要写解释、不要用斜杠把两个值并列：\n"
        f"{enums}\n"
        "只输出 JSON，不要多余文字：\n"
        '{"act":"玩笑","target":"bot","origin":"self","confidence":"clear",'
        '"tone":"亲昵","intent":"拉近距离","reception":"可能接受",'
        '"evidence":[{"ref":"本轮消息","detail":"依据之一"},{"ref":"第2条","detail":"依据之二"}],'
        '"alternatives":["另一种可能的解释"],'
        '"pending":{}}\n'
        "`evidence.ref` 只能是「本轮消息」或「第N条」（N 必须是上面出现过的编号）；"
        "没有依据就不要编，宁可少写。**没有依据就不要给 clear。**\n"
        "`pending` 三种写法（没有就写 `{}`）：\n"
        '  · 新出现一件没答的事：{"op":"create","kind":"clarify|open_topic","summary":"≤40字",'
        '"evidence":[{"ref":"本轮消息","detail":"他问的是…"}]}\n'
        '  · 【还没说完的事】里的某条还没答：{"op":"keep","ref":"P1"}\n'
        '  · 他明确给出结论了：{"op":"resolve","ref":"P1","evidence":[{"ref":"本轮消息",'
        '"detail":"他说已经…"}]}'
    )


def _clean_evidence(rows: Any, total: int) -> dict[str, list[dict[str, Any]]]:
    """把模型给的引用分成**能解析的**与**诊断用的**两类。

    为什么要分（#0011 P1-6）：原来对不上号的引用被静默丢掉，于是
    「`confidence=clear` 但一条依据都举不出来」这条守门（`_normalize` 里把 clear 降成
    fuzzy）**永远不会触发**——列表里总还剩别的条目。分成两类之后，守门只数 `valid`，
    诊断（越界、缺 mid）单独留在 `unresolved` 里，既不进原因行、也不再假装是证据。
    """
    out: dict[str, list[dict[str, Any]]] = {"valid": [], "unresolved": []}
    if not isinstance(rows, list):
        return out
    for row in rows[:4]:
        if not isinstance(row, dict):
            continue
        ref = str(row.get("ref") or "").strip()
        detail = str(row.get("detail") or "").strip()[:60]
        if not detail:
            continue
        if ref == "本轮消息":
            out["valid"].append({"ref": ref, "detail": detail})
            continue
        match = re.fullmatch(r"第\s*(\d+)\s*条", ref)
        if match and 1 <= int(match.group(1)) <= total:
            out["valid"].append({"ref": ref, "detail": detail})
        elif match:
            out["unresolved"].append({"ref": ref, "detail": detail, "why": "out-of-range"})
        else:
            out["unresolved"].append({"ref": ref, "detail": detail, "why": "bad-ref"})
    return out


def resolve_evidence(valid: list[dict[str, Any]], unresolved: list[dict[str, Any]],
                     turns: list[dict[str, Any]] | None, current_mid: Any) -> dict[str, list[dict[str, Any]]]:
    """把「本轮消息 / 第 N 条」翻译成**稳定的 message_id**。

    `第 N 条` 的编号与 `_prompt()` 里给模型看的行号是同一套（`enumerate(turns, 1)`），
    而 `_normalize` 收到的 `turns` 就是实际送进 prompt 的那一份，所以映射是确定的。
    拿不到 mid（历史没带、或是空）→ 降级为诊断，**不补猜**。
    """
    good: list[dict[str, Any]] = []
    bad = list(unresolved)
    seq = [dict(t) for t in (turns or [])]
    for row in valid:
        mid = 0
        if str(row.get("ref")) == "本轮消息":
            mid = int(current_mid or 0) or int((seq[-1] or {}).get("mid") or 0 if seq else 0)
        else:
            match = re.fullmatch(r"第\s*(\d+)\s*条", str(row.get("ref") or ""))
            if match and 1 <= int(match.group(1)) <= len(seq):
                mid = int(seq[int(match.group(1)) - 1].get("mid") or 0)
        if mid:
            good.append({"ref": row["ref"], "message_id": mid, "detail": row["detail"]})
        else:
            bad.append({"ref": row.get("ref"), "detail": row.get("detail"), "why": "no-mid"})
    return {"valid": good, "unresolved": bad}


_SEP = re.compile(r"[/|、，,；;（）()【】\[\]\s]+")


def _split_choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    """把模型给的值对齐到白名单。

    **为什么要拆分**：模型经常把两个候选并排写——`玩笑/亲密`、`求助（求证/解读）`、
    `亲昵、调侃、宠溺`。原来这里是精确匹配，对不上就整条丢弃：
    `act` 变 `unknown`（等于这次深判白做了），`tone` 变 `不明确`（下游拿不到口气）。
    实测 9 条样本里 `tone` 因此 9/9 落空。

    现在的口径：**先认整串，不认就按分隔符切开，取第一个能对上白名单的分量**。
    `玩笑/亲密` → `玩笑`。这不改变「看不清就 unknown」——切出来的仍然是模型自己写的值，
    只是不再因为排版把它当成非法值。
    """
    text = str(value or "").strip()
    if text in allowed:
        return text
    for part in _SEP.split(text):
        part = part.strip()
        if part in allowed:
            return part
    return default


def _normalize(data: Any, total: int, *, turns: list[dict[str, Any]] | None = None,
               current_mid: Any = None, reused: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        return dict(_EMPTY)

    confidence = _split_choice(data.get("confidence"), ("clear", "fuzzy", "unknown"), "unknown")
    act = _split_choice(data.get("act"), ACTS, "unknown")
    # 「不知道」是模型的自我报告，不是场景
    if act == "不知道":
        act, confidence = "unknown", "unknown"
    split = _clean_evidence(data.get("evidence"), total)
    if reused:
        # 复用命中的判定属于**上一条**消息：它的引用不能当这一轮的证据（#0011 P1-3）
        resolved = {"valid": [], "unresolved": [],
                    "dropped": len(split["valid"]) + len(split["unresolved"])}
    else:
        resolved = resolve_evidence(split["valid"], split["unresolved"], turns, current_mid)
        resolved["dropped"] = 0
    # **没有能对上的证据，就不该是 clear**：模型说「我很确定」但一条依据都举不出来时，
    # 它给的其实是印象，不是判断。降成 fuzzy，让下游按「不确定」处理。
    # 注意这里数的是 `valid`（解析成功的那批），不是原始条目数——越界引用不算证据。
    if confidence == "clear" and not resolved["valid"]:
        confidence = "fuzzy"
    return {
        "act": act,
        "target": _split_choice(data.get("target"), TARGETS, "none"),
        "origin": _split_choice(data.get("origin"), ORIGINS, "unknown"),
        "confidence": confidence,
        "tone": _split_choice(data.get("tone"), TONES, "不明确"),
        "intent": _split_choice(data.get("intent"), INTENTS, "不明确"),
        "reception": _split_choice(data.get("reception"), RECEPTIONS, "不明确"),
        "evidence": resolved["valid"],
        "evidence_unresolved": resolved["unresolved"],
        "evidence_dropped": resolved["dropped"],
        "alternatives": [str(x)[:40] for x in (data.get("alternatives") or []) if str(x).strip()][:3],
        "pending": _normalize_pending(data.get("pending"), resolved["valid"], reused=reused),
    }


def _normalize_pending(raw: Any, evidence: list[dict[str, Any]], *, reused: bool = False) -> dict[str, Any]:
    """`pending` 的结构校验。**认不出就返回 `{}`，不猜。**

    令牌是否真实存在由 `persona_intents` 在锁内复核（它才知道本回合发了哪些 `[P1]`）；
    这里只保证形状对、`create` 带得动证据。
    """
    if reused or not isinstance(raw, dict):
        return {}
    op = str(raw.get("op") or "").strip()
    if op == "create":
        kind = str(raw.get("kind") or "").strip()
        summary = str(raw.get("summary") or "").strip()[:40]
        if kind not in ("open_topic", "clarify") or not summary:
            return {}
        rows = [row for row in (raw.get("evidence") or [])
                if isinstance(row, dict) and int(row.get("message_id") or 0) > 0]
        if not rows and not evidence:
            return {}
        return {"op": "create", "kind": kind, "summary": summary,
                "evidence": (rows or evidence)[:2]}
    if op in ("keep", "resolve"):
        ref = str(raw.get("ref") or "").strip().upper()
        if not ref.startswith("P"):
            return {}
        out: dict[str, Any] = {"op": op, "ref": ref}
        if op == "resolve":
            rows = [row for row in (raw.get("evidence") or [])
                    if isinstance(row, dict) and int(row.get("message_id") or 0) > 0]
            out["evidence"] = (rows or evidence)[:1]
        return out
    return {}



# --------------------------------------------------------------- 增量复用（#0002）
#
# 缓存键 = **文本一致 + 相关上下文未变 + 边界未变 + 同人 + 同 mode**。
# 不做任何"相似度"判断：`ctx-001`（关心）与 `ctx-002`（争执贬低）在"同人同 mode"
# 这一层完全一样，靠相似度会复用到含义相反的新消息（审阅 P1-1）。
#
# 现实里能满足全部条件的场景是**同一条消息被重复处理**：重试、双投递、手动重放。
# 「连续三轮小笨蛋只判一次」属于相似消息复用，**不在本版**（见议题 §六）。

REUSE_KEYS = ("text_norm", "ctx_hash", "context_hash", "mode", "uid",
              "boundaries_rev", "boundaries_hash")


def _norm_text(text: str) -> str:
    r"""归一化文本：**压缩**空白，不删除。

    2026-11-03 修正（审阅第 4 条）：原来写的是 `re.sub(r"\s+", "", …)`，把空白整个删掉，
    于是 `a b` 与 `ab` 会被当成同一句话。空白在英文与代码里是有意义的，
    这里只做两件事：全角空格/NBSP 归一成半角空格、连续空白压成一个、去首尾。
    """
    body = str(text or "").replace("\u3000", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", body).strip()


def _ctx_hash(turns: list[dict[str, Any]], current: str = "") -> str:
    """送给解释器的那几条轮次的指纹：`role|speaker|text[:80]` 序列。

    截断口径与生产一致（`__init__.py` 里每条 80 字、最多 6 条），
    免得"回放里判得准、线上因为截断判不准"。

    **尾部与当前文本相同的轮次会被去掉**（2026-11-03 修正，审阅第 2 条）：
    当前消息在生成回复前就可能已经写进 `chatlog`，于是第一次投递的切片是 `[…, X]`、
    重复投递变成 `[…, X, X]`。两者指的其实是"同一句 + 同一段前文"，
    算同一个指纹才不会让本版唯一的现实收益路径变成 0 命中。
    """
    seq = [dict(t) for t in (turns or [])][-6:]
    if current:
        mark = _norm_text(current)
        while seq and _norm_text(str(seq[-1].get("text") or "")) == mark:
            seq.pop()
    parts = [
        f"{t.get('role')}|{t.get('speaker') or ''}|{str(t.get('text') or '')[:80]}"
        for t in seq
    ]
    return "sha1:" + hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def analysis_key(question: str, turns: list[dict[str, Any]] | None = None, *,
                 mode: str = "", uid: str = "", boundaries_rev: int = 0,
                 boundaries_hash: str = "", context: str = "") -> dict[str, Any]:
    """算一次深判的缓存键。**纯函数**，不读盘、不写盘。

    `context` 是**模型实际看到的**那段语境摘要（`scene_state.summary()`）。
    2026-11-03 补（审阅第 3 条）：摘要里含话题与语气，`attention` 一改 `topic`，
    模型看到的输入就变了 —— 键里不带它就会错误复用。
    """
    body = str(question or "")
    return {
        "text_norm": _norm_text(body),
        "ctx_hash": _ctx_hash(turns or [], body),
        "context_hash": "sha1:" + hashlib.sha1(
            str(context or "").encode("utf-8")).hexdigest()[:16],
        "mode": str(mode or ""),
        "uid": str(uid or ""),
        "boundaries_rev": int(boundaries_rev or 0),
        "boundaries_hash": str(boundaries_hash or ""),
    }


def reuse_hit(state: dict[str, Any], key: dict[str, Any], *, now: float,
              ttl: float) -> dict[str, Any]:
    """能不能沿用上一次深判？返回 `{hit, verdict, why}`。

    判据**全部**满足才命中；任何一条对不上都返回 `hit=False` 并给出原因（给日志与测试看）。
    """
    last = (state or {}).get("last_analysis") or {}
    if not isinstance(last, dict) or not last:
        return {"hit": False, "why": "没有可复用的上次深判", "verdict": {}}
    if float(ttl or 0) <= 0:
        return {"hit": False, "why": "复用关闭（ttl<=0）", "verdict": {}}
    age = float(now) - float(last.get("at") or 0)
    if age < 0:
        return {"hit": False, "why": "上次深判的时间在未来", "verdict": {}}
    if age > float(ttl):
        return {"hit": False, "why": f"超过 TTL（{age:.0f}s > {ttl:.0f}s）", "verdict": {}}
    for name in REUSE_KEYS:
        if str(last.get(name)) != str(key.get(name)):
            return {"hit": False, "why": f"{name} 不一致", "verdict": {}}
    verdict = dict(last.get("verdict") or {})
    # 复用命中的判定属于**上一条**消息：引用一律不带走（#0011 P1-3）
    dropped = len(verdict.get("evidence") or []) + len(verdict.get("evidence_unresolved") or [])
    verdict["evidence"] = []
    verdict["evidence_unresolved"] = []
    verdict["evidence_dropped"] = int(verdict.get("evidence_dropped") or 0) + dropped
    if str(verdict.get("confidence")) != "clear":
        return {"hit": False, "why": "上次不是 clear 结论", "verdict": {}}
    if str(verdict.get("act") or "unknown") in ("", "unknown"):
        return {"hit": False, "why": "上次没判出 act", "verdict": {}}
    # 打上来源标记：`evidence` 里写「本轮消息」的那条是**上一次**的本轮，
    # 下游要区分就判 `reused`，不许当成新消息的证据。
    verdict["reused"] = True
    verdict["reused_at"] = float(last.get("at") or 0)
    return {"hit": True, "why": "命中", "verdict": verdict}


_VERDICT_KEYS = ("act", "target", "origin", "confidence", "tone", "intent",
                 "reception", "evidence", "evidence_unresolved", "evidence_dropped",
                 "alternatives", "pending")


async def decide(
    text: str,
    *,
    turns: list[dict[str, Any]] | None = None,
    boundary: str = "",
    context: str = "",
    mode: str = "",
    uid: str = "",
    boundaries_rev: int = 0,
    boundaries_hash: str = "",
    state: dict[str, Any] | None = None,
    chat: Callable[..., Awaitable[Any]] | None = None,
    timeout: float = 12.0,
    thinking: str = "disabled",
    max_tokens: int = 900,
    now: float | None = None,
    ttl: float = 0.0,
    current_mid: Any = None,
) -> dict[str, Any]:
    """**生产与回放共用的决策入口**：先查能否复用，不行才调模型。

    返回 `{"verdict": {...}, "source": "reuse"|"analysis", "write_back": {...}}`。

    * `source="reuse"` 时 `write_back` 是**空的** —— 复用不写状态，只由调用方记日志。
      这一条是设计而不是省事：写 `last_analysis` 会刷新 `at`，缓存就永远不过期了。
    * `source="analysis"` 时 `write_back` 里带 `last_analysis`（完整 8 个 verdict 字段）。
      `last_turn` 由调用方自己拼（它要 message_id 之类的事件信息）。
    * `ttl<=0` 表示**关闭复用**（默认值），等于每次都判。
    """
    stamp = time.time() if now is None else float(now)
    body = str(text or "").strip()
    if not body:
        return {"verdict": dict(_EMPTY), "source": "empty", "write_back": {}}

    seq: list[dict[str, Any]] = [dict(t) for t in (turns or [])][-6:]
    key = analysis_key(body, seq, mode=mode, uid=uid,
                       boundaries_rev=boundaries_rev, boundaries_hash=boundaries_hash,
                       context=context)
    hit = reuse_hit(state or {}, key, now=stamp, ttl=ttl)
    if hit["hit"]:
        return {"verdict": hit["verdict"], "source": "reuse", "write_back": {},
                "why": hit["why"]}

    verdict = await analyze(body, turns=seq, boundary=boundary, context=context,
                            chat=chat, timeout=timeout, thinking=thinking,
                            max_tokens=max_tokens, current_mid=current_mid)
    write_back: dict[str, Any] = {}
    if str(verdict.get("act") or "unknown") != "unknown":
        write_back["last_analysis"] = {
            "at": stamp,
            **key,
            "verdict": {name: verdict.get(name) for name in _VERDICT_KEYS},
        }
    return {"verdict": verdict, "source": "analysis", "write_back": write_back,
            "why": hit["why"]}


async def analyze(
    text: str,
    *,
    turns: list[dict[str, Any]] | None = None,
    boundary: str = "",
    context: str = "",
    chat: Callable[..., Awaitable[Any]] | None = None,
    timeout: float = 12.0,
    thinking: str = "disabled",
    max_tokens: int = 900,
    current_mid: Any = None,
) -> dict[str, Any]:
    """解释最后一句。`turns` 不含本轮时自动补在末尾（补的那条带 `mid`）。

    `context` 是已经知道的语境摘要（话题 / 互动模式 / 是否过时），
    传进来可以**少判一次**——省掉重复推断，也让前后两轮的结论不打架。

    返回 `_EMPTY` 结构（`act=unknown`）表示没解释出来——**调用方应当据此不注入语料、
    不更新情绪**，而不是退回词表猜。

    ## `thinking`：默认 **disabled**，这是实测出来的，不是偏好

    2026-11-03 拿 9 条多轮样本对照（`分析记录/探针/_语境思考档AB.py`，temperature=0）：

    | 档位 | act 命中 | 被截断 | 中位耗时 | 超 12s |
    |---|---|---|---|---|
    | 接口默认（有思维链） | 5/9 | 1/9 | 10.8 s | 4/9 |
    | **关闭思考** | **7/9** | 0/9 | 2.0 s | 0/9 |

    思维链在这个**结构化判类**任务上既慢又更容易跑偏：它的 `reasoning_tokens`
    会先吃掉 `max_tokens` 预算（实测 900 预算时 `completion=900 / reasoning=900`、
    正文为空），于是一部分深判直接变成 unknown——而日志只会写「按 unknown 处理」，
    看不出是截断。关掉之后预算回到正文，精度反而更高。
    这是**这一个任务**的结论，不是「别处也该关思考」。

    `thinking` 传 `""` 表示不传这个参数（用接口默认），留作对照。
    """
    body = str(text or "").strip()
    if not body:
        return dict(_EMPTY)

    seq: list[dict[str, Any]] = [dict(t) for t in (turns or [])][-6:]
    # **同一轮靠消息身份判断，不靠文本相等**（#0011 P1-6）：历史末条文字恰好相同、
    # 但不是同一条消息时，模型嘴里的「第 N 条」会绑到旧消息上。`current_mid` 缺失
    # （离线/回放）才退回文本比较。
    same_turn = bool(seq) and (
        (current_mid is not None and str(seq[-1].get("mid")) == str(current_mid))
        or (current_mid is None and str(seq[-1].get("text") or "").strip() == body))
    if not same_turn:
        seq.append({"role": "user", "text": body, "mid": current_mid})

    prompt = _prompt(seq, boundary, context)
    call = chat
    if call is None:
        from . import llm

        call = llm.chat

    extra: dict[str, Any] = {"response_format": {"type": "json_object"},
                             "max_tokens": int(max_tokens)}
    if thinking:
        # **必须走 `extra_body`**：openai SDK 不认裸的 `thinking=` 关键字，
        # 传了会 `TypeError`——然后被下面那个 `except Exception` 吞成 unknown。
        extra["extra_body"] = {"thinking": {"type": thinking}}

    import asyncio

    try:
        resp = await asyncio.wait_for(
            call(messages=[{"role": "system", "content": _SYS},
                           {"role": "user", "content": prompt}], **extra),
            timeout=timeout,
        )
        choice = resp.choices[0] if getattr(resp, "choices", None) else None
        raw = (choice.message.content or "").strip() if choice is not None else ""
        # 截断要与「模型说看不清」分开记：前者是配置问题（预算/思考档），后者才是判断问题。
        if choice is not None and getattr(choice, "finish_reason", "") == "length":
            logger.info("语境解释被截断（max_tokens=%s，思考档=%s）——这是配置问题，不是判断不出",
                        max_tokens, thinking or "默认")
        data = json.loads(raw)
    except asyncio.TimeoutError:
        logger.info("语境解释超时（%.1fs），按 unknown 处理", timeout)
        return dict(_EMPTY)
    except Exception:  # noqa: BLE001 - 解释失败不能拖垮回复
        logger.info("语境解释失败，按 unknown 处理")
        return dict(_EMPTY)

    got = _normalize(data, len(seq), turns=seq, current_mid=current_mid)
    logger.info("语境解释 act=%s target=%s conf=%s tone=%s",
                got["act"], got["target"], got["confidence"], got["tone"])
    return got


def needs_deep_judge(verdict: dict[str, Any]) -> bool:
    """要不要走深判。

    两个条件**同时**满足才调：

    1. 规则层标了 `needs_context`（出现了离开语境就没有确定意思的词）；
    2. 规则层**没能给出 clear 结论**（`act=unknown`，或者硬辱骂那种先判「骂」但只敢给 fuzzy）。

    第 2 条是关键：已经 clear 的（「哈哈」「帮我查一下天气」）不必再问一次模型。
    **不要在这里加别的触发条件** —— 关系语气突变、出现拒绝这些，
    要等有多轮样本、能验收之后再进来。
    """
    row = verdict or {}
    if not row.get("needs_context"):
        return False
    return str(row.get("act") or "") == "unknown" or str(row.get("confidence") or "") != "clear"
