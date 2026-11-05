"""示例库：按标签挑"可以怎么说 / 不能这么说"，只在预算内注入。

## 为什么不是把示例全写进 surface

行为维度一多，示例就多；全常驻会撑爆 prompt。这里只放**具体情绪/场景**的示例，
按当前矢量轴、模式、话题和对象过滤，取 1~2 条 good + 0~1 条 bad。

## 过滤规则（这些是硬条件，不是排序偏好）

* `audience:master` 只在主人面前出现；`audience:other` 只在非主人面前出现；
* 互损类（`mode:闹脾气` 且带外貌/能力梗）只在 `play` 够时出现；
* `topic:sex` 的 good 示例若带性内容，必须是 `audience:master`；
  非主人只能看到"收住 / 转移"的示例；
* 没有命中就整块不注入，不拿随机示例凑数。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from . import config, persona_relation

logger = logging.getLogger("ai_chat.persona_examples")

_USAGE = "examples_usage.json"
_MAX_USED = 200

# ---- 2-gram 兜底的门槛 ----
# 为什么需要它：示例**只靠标签命中**时，标签写歪了的那条就永远出不来。
# 实测（2026-11-03，22 条）：`scene:分享` 三条、`scene:玩笑` 一条、
# `scene:纠正` 一条都属于这种"存了但取不出"的死条目，而文档 §6.3 里
# 本来写了「与最近对话的 2-gram 重叠×2」—— 代码里从来没有这一项，这是**补 bug**。
#
# 门槛刻意定得高：2-gram 是兜底，不能盖过标签。0.5 意味着对方这句话里
# 一半以上的双字都出现在示例的上文里 —— 那基本就是同一个场合在重演。
_OVERLAP_MIN = 0.5
_OVERLAP_SHARED = 2      # 至少两个 gram 相交：一个字（「累」）就命中太滥


def _path() -> Path:
    return config.examples_file_path()


def render_examples(rows: list[dict[str, Any]]) -> str:
    """把示例渲染成**人能直接读改**的多行文本（每条一个对象，记录间空一行）。

    字段一行一个，但**数组与嵌套对象内联** —— 展开成竖排反而更难扫读，
    而且 diff 噪声大。写盘与一次性转换共用这个函数，保证只有一种排版。
    """
    blocks: list[str] = []
    for item in rows:
        lines = ["{"]
        keys = list(item.keys())
        for pos, key in enumerate(keys):
            value = item[key]
            body = json.dumps(value, ensure_ascii=False)
            # 纯标量数组里补一个空格：`["a", "b"]` 比 `["a","b"]` 好读
            if isinstance(value, list) and all(
                    not isinstance(x, (dict, list)) for x in value):
                body = body.replace('","', '", "')
            comma = "," if pos < len(keys) - 1 else ""
            lines.append(f"  {json.dumps(key, ensure_ascii=False)}: {body}{comma}")
        lines.append("}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def parse_examples(text: str) -> list[dict[str, Any]]:
    """把示例库文本解析成条目。

    **两种排布都认**（2026-11-04 起写盘用第二种）：

    * 一行一条（历史 JSONL 格式）；
    * 每条一个多行 JSON 对象（人可读、可手工改）。

    用 `json.JSONDecoder().raw_decode` 连续解码：它不在乎对象之间是换行还是空白，
    所以旧文件不用迁移、新文件也能被旧代码以外的任何读者按同样逻辑读。
    """
    body = "\n".join(
        line for line in str(text or "").splitlines() if not line.lstrip().startswith("#"))
    decoder = json.JSONDecoder()
    items: list[dict[str, Any]] = []
    idx = 0
    while idx < len(body):
        while idx < len(body) and body[idx] in " \t\r\n":
            idx += 1
        if idx >= len(body):
            break
        try:
            item, idx = decoder.raw_decode(body, idx)
        except json.JSONDecodeError:
            logger.warning("示例库有读不动的片段，跳到下一行：%s", body[idx:idx + 40])
            nxt = body.find("\n", idx)
            if nxt < 0:
                break
            idx = nxt + 1
            continue
        if isinstance(item, dict) and item.get("id"):
            items.append(item)
    return items


def _load() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    try:
        return parse_examples(path.read_text(encoding="utf-8"))
    except OSError:
        logger.warning("示例库读不动：%s", path)
        return []


def _usage_path() -> Path:
    return config.persona_data_dir() / _USAGE


def _usage() -> list[str]:
    path = _usage_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    items = raw.get("used") if isinstance(raw, dict) else raw
    return [str(x) for x in (items or []) if str(x)]


def mark_used(ids: list[str]) -> None:
    from .persona_workspace import root
    if root() is not None:
        return
    if not ids:
        return
    used = _usage() + [str(x) for x in ids]
    used = used[-_MAX_USED:]
    path = _usage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"used": used}, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("示例使用记录写盘失败", exc_info=True)


def _tags_of(item: dict[str, Any]) -> set[str]:
    return {str(x) for x in (item.get("tags") or []) if str(x)}


def _audience_ok(item: dict[str, Any], *, is_master: bool) -> bool:
    tags = _tags_of(item)
    if "audience:master" in tags and not is_master:
        return False
    if "audience:other" in tags and is_master:
        return False
    return True


def _topic_ok(item: dict[str, Any], *, is_master: bool, play: float, topics: list[str],
              private: bool = False, adult: bool = False) -> bool:
    tags = _tags_of(item)
    needs = item.get("requires") or {}
    if not isinstance(needs, dict):
        return False
    if needs.get("private") and not private:
        return False
    if needs.get("adult") and not adult:
        return False
    if needs.get("master") and not is_master:
        return False
    if play < float(needs.get("min_play", 0)):
        return False
    topic_tags = [t.split(":", 1)[1] for t in tags if t.startswith("topic:")]
    if any(t not in topics for t in topic_tags):
        return False
    if topic_tags and play < 0.4:
        return False
    if "sex" in topic_tags and not adult:
        return False
    # 旧示例也要经过兜底，不能靠漏写标签绕过对象与场合约束。
    speech = " ".join(str(x) for x in item.get("say", []))
    intimate = needs.get("contact") == "intimate" or bool(re.search(r"抱|亲吻|爱抚|亲我|亲你", speech))
    if item.get("polarity") == "good" and intimate and not (is_master and private):
        return False
    if "mode:闹脾气" in tags and play < 0.4:
        return False
    return True


def _bigrams(text: str) -> set[str]:
    """字符 2-gram（与 memory / msgindex 同一套取舍：中文不引分词器）。"""
    body = re.sub(r"[\s\W_]+", "", str(text or ""))
    return {body[i:i + 2] for i in range(len(body) - 1)} if len(body) >= 2 else set()


def _overlap(query: str, item: dict[str, Any]) -> float:
    """对方这句话有多少 2-gram 出现在示例的上文里（0~1）。"""
    q = _bigrams(query)
    if not q:
        return 0.0
    ctx = _bigrams("".join(str(x) for x in (item.get("ctx") or [])))
    shared = q & ctx
    if len(shared) < _OVERLAP_SHARED:
        return 0.0
    return len(shared) / len(q)


def _fit_bonus(item: dict[str, Any], axes: dict[str, float] | None) -> float:
    """`fit.axes` 窗口内"离得多近"的加分；没有 `fit` 就是 0。

    为什么要它：`_fit_ok()` 只筛"适不适用"，同一条触发的多个变体（同一句话、
    不同情绪下的不同回复）会**同时通过**，排序时它们分数一样 —— 取谁全看文件顺序。
    这条奖励让"情绪最贴近"的那支胜出。

    上限 1.5：低于 tags 交集(3)与 scene(2) —— 先看这句在讲什么，再在同场景里挑语气。
    """
    if not isinstance(axes, dict) or not axes:
        return 0.0
    fit = item.get("fit")
    if not isinstance(fit, dict):
        return 0.0
    spans = fit.get("axes")
    if not isinstance(spans, dict) or not spans:
        return 0.0
    ratios: list[float] = []
    for name, span in spans.items():
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            continue
        try:
            lo, hi = float(span[0]), float(span[1])
        except (TypeError, ValueError):
            continue
        value = axes.get(str(name))
        if value is None:
            continue
        center, half = (lo + hi) / 2.0, max(1e-6, (hi - lo) / 2.0)
        ratios.append(min(1.0, abs(float(value) - center) / half))
    if not ratios:
        return 0.0
    return 1.5 * (1.0 - sum(ratios) / len(ratios))


def _score(item: dict[str, Any], tags: set[str], scenes: set[str], used: list[str],
           query: str = "", axes: dict[str, float] | None = None) -> float:
    own = _tags_of(item)
    score = 3.0 * len(own & tags) + 2.0 * len(own & scenes)
    # 兜底项：只在标签都没命中时才可能把条目带进来，权重也低于一个标签
    score += 2.0 * _overlap(query, item)
    if item.get("id") in used[-20:]:
        score -= 2.0
    if str(item.get("polarity")) == "good":
        score += 0.5
    # 同一条触发的几个变体（同一句话、不同情绪不同回法）靠这一项分先后
    score += _fit_bonus(item, axes)
    return score


# ---- `fit`：这条示例的适用区间（2026-11-03 按 #0004 加）----
# 两个维度**必须分开**：
#   `fit.axes`  数值轴区间，键必须是 `effective()` 返回的那 5 个轴之一，值是 `[lo, hi]`；
#   `fit.tags`  要求当前标签集合里必须包含的标签（复用现有 `mode:` / `scene:` 词汇）。
# 为什么不能把模式塞进 axes：`effective()` 只返回数值轴，模式来自 `tags()` 或
# `scene_state.mode` —— 上一版方案写过 `fit.axes.mode=["撒娇"]`，那个永远匹配不上。
#
# **轴值缺失（`axes is None`）时，带 `fit` 的条目跳过**：不知道当前状态就别用有适用区间的样本。
# 没有 `fit` 的条目完全不受影响 —— 这是向后兼容的锚点。
def _fit_ok(item: dict[str, Any], axes: dict[str, float] | None,
            cur_tags: set[str] | None) -> bool:
    fit = item.get("fit")
    if not isinstance(fit, dict) or not fit:
        return True
    want_axes = fit.get("axes") or {}
    if isinstance(want_axes, dict) and want_axes:
        if not isinstance(axes, dict):
            return False
        for name, span in want_axes.items():
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                continue
            try:
                lo, hi = float(span[0]), float(span[1])
            except (TypeError, ValueError):
                continue
            value = axes.get(str(name))
            if value is None or not (lo <= float(value) <= hi):
                return False
    want_tags = fit.get("tags") or []
    if isinstance(want_tags, list) and want_tags:
        if cur_tags is None:
            return False
        if not {str(x) for x in want_tags} <= set(cur_tags):
            return False
    return True


def _not_banned(item: dict[str, Any], banned: tuple[str, ...]) -> bool:
    """这条示例是否没踩到被显式拒绝的词。

    只看 `good`：`bad` 示例本身就是"别这么说"的反例，挡掉它们反而丢信息。
    """
    if not banned or str(item.get("polarity")) != "good":
        return True
    text = "".join(str(x) for x in (item.get("ctx") or []) + (item.get("say") or []))
    return not any(word in text for word in banned)


def pick(
    *,
    tags: list[str] | None = None,
    scenes: list[str] | None = None,
    is_master: bool = False,
    uid: str = "",
    play: float | None = None,
    topics: list[str] | None = None,
    private: bool = False,
    adult: bool = False,
    limit_good: int = 2,
    limit_bad: int = 1,
    query: str = "",
    banned: tuple[str, ...] | list[str] = (),
    axes: dict[str, float] | None = None,
    cur_tags: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """挑示例。返回 `{"good": [...], "bad": [...]}`。

    `banned` 是**被显式拒绝的词**（来自 `scene_state.boundaries` 的 `word`）：
    示例的 `ctx`/`say` 里只要出现它，这条 `good` 就不选。
    **不看场景标签** —— 归到 `scene:亲密` 的称呼示例一样要挡（#0005 审阅指出的漏洞）。

    `axes` / `cur_tags` 用来判 `fit`（见 `_fit_ok`）：**轴值缺失时带 `fit` 的条目跳过**。
    

    **没有相关性就不选**：`tags` / `scenes` 都没命中、且 2-gram 也够不上时，
    这条示例不注入 —— 平淡的对话里不带"别把话头抛回去"。
    2-gram 是兜底：标签写歪的条目靠它救回来（见 `_OVERLAP_MIN` 的注释）。
    """
    want_tags = {str(x) for x in (tags or [])}
    want_scenes = {f"scene:{s}" for s in (scenes or [])}
    want = want_tags | want_scenes
    if not want:
        return {"good": [], "bad": []}
    play_value = persona_relation.play(uid, is_master=is_master) if uid else 0.0
    if play is not None:
        play_value = float(play)
    topic_list = topics if topics is not None else (
        persona_relation.topics(uid, is_master=is_master) if uid else []
    )
    used = _usage()
    banned_words = tuple(str(w) for w in (banned or ()) if str(w).strip())
    rows = [
        item for item in _load()
        if _not_banned(item, banned_words)
        and _fit_ok(item, axes, cur_tags)
        and _audience_ok(item, is_master=is_master)
        and _topic_ok(item, is_master=is_master, play=play_value, topics=topic_list, private=private, adult=adult)
        # 标签有交集，或者上文跟当前这句高度相似（2-gram 兜底）
        and (bool(_tags_of(item) & want) or _overlap(query, item) >= _OVERLAP_MIN)
    ]
    rows.sort(key=lambda item: -_score(item, want_tags, want_scenes, used, query, axes))
    good = [x for x in rows if str(x.get("polarity")) == "good"][:limit_good]
    bad = [x for x in rows if str(x.get("polarity")) == "bad"][:limit_bad]
    return {"good": good, "bad": bad}


def _render_item(item: dict[str, Any], *, good: bool) -> str:
    lines: list[str] = []
    for row in (item.get("ctx") or [])[:3]:
        lines.append(str(row))
    for row in (item.get("say") or [])[:3]:
        lines.append(str(row) if good else "✗ " + str(row))
    why = str(item.get("why") or "").strip()
    if why:
        lines.append("  → " + why)
    return "\n".join(lines)


def block(
    *,
    tags: list[str] | None = None,
    scenes: list[str] | None = None,
    is_master: bool = False,
    uid: str = "",
    budget: int = 420,
    private: bool = False,
    adult: bool = False,
    limit_good: int = 2,
    limit_bad: int = 1,
    query: str = "",
    banned: tuple[str, ...] | list[str] = (),
    axes: dict[str, float] | None = None,
    cur_tags: set[str] | None = None,
    record: dict[str, Any] | None = None,
) -> str:
    """按预算渲染示例块；没有命中返回空串。`banned` 原样透传给 `pick()`。

    `record` 是**出参**（#0015 M2）：传一个 dict 进来，会把「实际注入的 id」与
    「因预算被裁掉的 id」写进去，供回放 trace 核对。不传时行为与改造前完全一致 ——
    这个参数存在的原因只有一个：`block()` 原本只返回字符串，**裁剪过程对外不可见**，
    于是「候选到底有没有被注入」事后无法回答，而不被注入的候选不能算它的功劳。
    """
    try:
        picked = pick(tags=tags, scenes=scenes, is_master=is_master, uid=uid,
                      limit_good=limit_good, limit_bad=limit_bad, private=private,
                      banned=banned, axes=axes, cur_tags=cur_tags,
                      adult=adult, query=query)
    except Exception:  # noqa: BLE001
        logger.debug("示例挑选失败", exc_info=True)
        return ""
    if not picked["good"] and not picked["bad"]:
        if record is not None:
            record.setdefault("used", [])
            record.setdefault("dropped", [])
            record.setdefault("picked", 0)
        return ""
    parts: list[str] = []
    used: list[str] = []
    dropped: list[dict[str, str]] = []
    for kind, heading in (("good", "【这个状态下的说法】"), ("bad", "【不要这么说】")):
        for item in picked[kind]:
            part = heading + "\n" + _render_item(item, good=kind == "good")
            if len("\n\n".join(parts + [part])) <= budget:
                parts.append(part)
                used.append(str(item["id"]))
            else:
                dropped.append({"id": str(item.get("id")), "kind": kind,
                                "why": f"超出预算 {budget} 字"})
    mark_used(used)
    if record is not None:
        record["used"] = used
        record["dropped"] = dropped
        record["picked"] = len(used) + len(dropped)
        record["budget"] = budget
    return "\n\n".join(parts)


def _write_rows(rows: list[dict[str, Any]]) -> str:
    """把示例写回运行副本（**不写包内源文件**）。"""
    path = config.persona_data_dir() / "examples.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        # **每条一个多行对象**（数组内联）：人能直接看、直接改。
        # 解析走 `parse_examples()` 的 `raw_decode`，不再依赖"一行一条"。
        body = render_examples(rows)
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(path)
        return str(path)
    except OSError as exc:
        logger.warning("示例写盘失败：%s", exc)
        return ""


def delete(example_id: str) -> dict[str, Any]:
    """按 id 删除一条示例（评估台删除权的落点）。"""
    eid = str(example_id or "").strip()
    rows = _load()
    if any(str(x.get("id")) == eid and x.get("locked") for x in rows):
        return {"ok": False, "why": "锁定示例不能删除"}
    keep = [x for x in rows if str(x.get("id")) != eid]
    if len(keep) == len(rows):
        return {"ok": False, "why": f"没有 id={eid} 的示例"}
    path = _write_rows(keep)
    return {"ok": bool(path), "id": eid, "remaining": len(keep), "file": path}


def check_fit(fit: Any) -> str:
    """校验 `fit` 结构，返回空串表示通过，否则返回中文原因。

    `fit` 是"这条示例的适用区间"，两面都要对得上：
      * `axes`：键必须是 `vector.json` 里登记过的轴，值是 `[lo, hi]`、`-1..1`、`lo <= hi`；
      * `tags`：要求当前标签集合里必须包含的标签（复用现有 `mode:` / `scene:` 词汇）。

    **为什么非校验不可**：写错的 `fit` 不会报错，只会让 `_fit_ok()` 永远判假 ——
    这条示例从此再也进不了 prompt，而且没人知道。
    """
    if fit is None:
        return ""
    if not isinstance(fit, dict):
        return "fit 必须是对象"
    unknown = set(fit) - {"axes", "tags"}
    if unknown:
        return f"fit 里只能有 axes / tags，多了：{sorted(unknown)}"

    spans = fit.get("axes")
    if spans is not None:
        if not isinstance(spans, dict) or not spans:
            return "fit.axes 必须是非空对象"
        try:
            from . import persona_vector          # 延迟导入：本模块要能离线单独加载
            known = set(persona_vector.effective("").keys())
        except Exception:  # noqa: BLE001 - 拿不到轴表就跳过键名校验，其余照验
            known = set()
        for name, span in spans.items():
            if known and str(name) not in known:
                return f"fit.axes 的轴名不在 vector.json 里：{name}（已知 {sorted(known)}）"
            if not isinstance(span, (list, tuple)) or len(span) != 2:
                return f"fit.axes[{name}] 必须是 [lo, hi]"
            try:
                lo, hi = float(span[0]), float(span[1])
            except (TypeError, ValueError):
                return f"fit.axes[{name}] 的区间不是数字"
            if lo > hi:
                return f"fit.axes[{name}] 的 lo > hi（{lo} > {hi}）"
            if not (-1.0 <= lo <= 1.0 and -1.0 <= hi <= 1.0):
                return f"fit.axes[{name}] 超出 -1..1"

    tags = fit.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or not tags:
            return "fit.tags 必须是非空数组"
        if any(not isinstance(x, str) or not x.strip() for x in tags):
            return "fit.tags 里必须是非空字符串"
    return ""


def append(item: dict[str, Any]) -> dict[str, Any]:
    """追加一条示例。**id 必须唯一**；缺字段直接拒绝。"""
    if not isinstance(item, dict) or not item.get("id"):
        return {"ok": False, "why": "示例缺 id"}
    for key in ("tags", "ctx", "say"):
        value = item.get(key)
        if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
            return {"ok": False, "why": f"{key} 必须是非空字符串列表"}
    if len(json.dumps(item, ensure_ascii=False)) > 3000:
        return {"ok": False, "why": "单条示例超过预算"}
    needs = item.get("requires", {})
    if not isinstance(needs, dict) or set(needs) - {"private", "adult", "master", "contact", "min_play"}:
        return {"ok": False, "why": "示例条件不合法"}
    for key in ("private", "adult", "master"):
        if key in needs and not isinstance(needs[key], bool):
            return {"ok": False, "why": f"{key} 必须是布尔值"}
    if "contact" in needs and needs["contact"] not in ("light", "intimate"):
        return {"ok": False, "why": "contact 不合法"}
    if "min_play" in needs and (type(needs["min_play"]) not in (int, float) or not 0 <= needs["min_play"] <= 1):
        return {"ok": False, "why": "min_play 必须在 0..1"}
    # `fit`：结构必须先过，否则写进去也永远选不中（见 `check_fit` 的说明）
    _fit_why = check_fit(item.get("fit"))
    if _fit_why:
        return {"ok": False, "why": _fit_why}
    # `source`：四种来源，**模型产出不许标成 chat**（#0004 审阅原话）
    src = item.get("source")
    if src is not None:
        if not isinstance(src, dict):
            return {"ok": False, "why": "source 必须是对象"}
        stype = str(src.get("source_type") or "")
        if stype not in ("chat", "manual", "migrated", "model"):
            return {"ok": False, "why": "source_type 只能是 chat/manual/migrated/model"}
        if stype == "chat" and src.get("message_id") in (None, ""):
            return {"ok": False, "why": "源类型是 chat 就必须带 message_id（不许凭印象标真实对话）"}
    eid = str(item["id"])
    rows = _load()
    if any(str(x.get("id")) == eid for x in rows):
        return {"ok": False, "why": f"id 已存在：{eid}"}
    if item.get("polarity") not in ("good", "bad"):
        return {"ok": False, "why": "polarity 必须是 good/bad"}
    if not item.get("say"):
        return {"ok": False, "why": "示例缺 say"}
    if any(x.get("ctx") == item.get("ctx") and x.get("say") == item.get("say") for x in rows):
        return {"ok": False, "why": "重复示例"}
    if len(rows) >= 200:
        return {"ok": False, "why": "示例库达到 200 条预算，请先整理"}
    rows.append(item)
    path = _write_rows(rows)
    return {"ok": bool(path), "id": eid, "total": len(rows), "file": path}


def stats() -> dict[str, Any]:
    rows = _load()
    return {
        "total": len(rows),
        "good": sum(1 for x in rows if str(x.get("polarity")) == "good"),
        "bad": sum(1 for x in rows if str(x.get("polarity")) == "bad"),
        "used": len(_usage()),
        "file": str(_path()),
    }
