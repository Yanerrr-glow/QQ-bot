"""搜索释义库：**只存"某个名词是什么意思"的结论**，不存网页原文。

## 为什么要单开一个库，而不是塞进 memories.json

两者**寿命与失效条件完全不同**：

| | `memories.json`（长期记忆） | `search_memory.json`（本模块） |
|---|---|---|
| 存什么 | 某个人的喜好/习惯、群里发生的事 | 某个**词**的释义 |
| 怎么来的 | 从聊天里提炼 | 从联网结果里总结 |
| 会过时吗 | 基本不会（人的喜好变得慢） | **会**（价格、版本、在位者都会变） |
| 要标时间吗 | 顺带记（已有 `time`） | **必须标**，否则不知道查的是哪天的 |
| 可信度 | 大多是用户亲口说的 | **网上来的，可能不准** |

混在一起会让两边都变差：记忆淘汰策略（按重要度+时效）不适合释义，
而释义的"过期"概念对"主人的喜好"没有意义。

## 只存释义，不存原文

存的是**一两句总结**，不是网页摘录 —— 理由有三个：

1. 原文里有别人的话，长期驻留等于把**不安全内容**留在 prompt 可达范围内；
2. 原文体积大、多数是导航与广告，复用价值低；
3. 你要的就是"这个词是什么意思"，总结恰好够用。

**不判定为释义类查询的**（比如"今天天气"）**不写入** —— 天气存下来只会误导下一次。

## 置信度是**代码算的**，不是让模型自评

让模型说"我有多确定"不可靠（它对自己编的东西也很自信）。所以这里只用
**可观测信号**打分，并把每一项都记下来，好让"为什么这条是低置信"能被追溯：

| 信号 | 影响 |
|---|---|
| 命中来自几个不同域名（多个来源对同一件事说法一致） | ↑ 提高 |
| 命中里出现"疑似/据称/可能/大约/不确定/有争议" | ↓ 降低 |
| 来源像百科（baike / wikipedia / 知乎 / 官方文档） | ↑ 提高 |
| 只有 1 条命中、且来源域名无法识别 | 偏低（基线以下） |

低于 `search_memory_min_confidence`（默认 0.5）就标成**低置信**，
并显眼地写进注入文本 —— "这条不太可靠，别当准的用"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import clock, config, settings, untrusted

logger = logging.getLogger("ai_chat.search_memory")

_FILE = "search_memory.json"
_MAX_DEF = 200

# 权威/百科类域名特征 —— 命中就加分
_AUTHORITY_HINTS: tuple[str, ...] = (
    "baike", "wiki", "wikipedia", "zhihu", "britannica",
    "gov.cn", "edu.cn", "docs.", "/docs", "developer.", "openai.com",
)
# 含糊措辞 —— 命中就减分（这是"网上说法不一 / 来源自己都不确定"的直接证据）
_HEDGE_WORDS: tuple[str, ...] = (
    "疑似", "据称", "据说", "可能", "大约", "不确定", "有争议", "尚不清楚",
    "未经证实", "传闻", "推测", "一说", "网传", "存疑", "有待确认",
)


def _path() -> Path:
    return config.LOG_DIR / _FILE


def _norm_key(text: str) -> str:
    """归一化查询键：去空白、去标点、转小写。让「鲸落」「鲸落？」「 鲸落 」命中同一条。"""
    flat = " ".join(str(text or "").split()).lower()
    return re.sub(r"[\s?？。！!，,、:：;；\"'“”‘’()（）\[\]【】]+", "", flat)


# 问句外壳词：长的排前面，先剥长再剥短（否则「是什么意思」会被「是什么」截成「…意思」）
_ASK_WORDS: tuple[str, ...] = tuple(
    sorted(
        (
            "是什么意思", "什么意思啊", "是什么意思啊", "是什么东西", "有哪些含义",
            "什么意思", "指的是什么", "指的什么", "怎么理解", "干什么的", "做什么的",
            "什么是", "是什么", "啥意思", "啥是", "啥叫", "什么叫", "是啥",
            "指什么", "指的是", "干嘛的", "解释一下", "的定义", "定义", "的含义",
            "的含义是", "意思",
        ),
        key=len,
        reverse=True,
    )
)


def headword(query: str) -> str:
    """从「X 是什么」里剥出被问的那个词 —— **缓存键必须是词，不是问句**。

    第一版直接拿整句当键：`/搜索 鲸落 是什么` 存成 key「鲸落是什么」，
    下次 `/搜索 鲸落` 查不到、模型用 `web_search` 传的 query 也各不相同 ——
    明明问的是同一个词，缓存却**永远不命中**，每次都重新联网。
    这类 bug 不报错，只让功能形同虚设（同 `_is_stale` 的字段名错误）。

    刻意**不剥单字的「是 / 的」**：`实事求是是什么意思` 剥成 `实事求` 是数据损坏，
    而「目的」这类词本身就以「的」结尾。只认同表里那些成套的外壳词。
    """
    raw = " ".join(str(query or "").split())
    if not raw:
        return ""
    # 先截到第一个句读为止：后面多是「顺便说说来源」这类附加要求，不是被问的词
    head = re.split(r"[?？。！!，,、；;：:]", raw, maxsplit=1)[0].strip()
    if not head:
        return ""
    # 匹配用**去掉空格**的版本：群里常打「鲸落是 什么」，带空格外壳词就匹配不上。
    # 词里的空格本来也会被 `_norm_key` 去掉，所以键这一侧不受影响。
    compact = re.sub(r"\s+", "", head)
    # 反复剥，直到不动：把「X 是什么意思啊」这类叠加外壳全部去掉
    for _ in range(4):
        before = compact
        for word in _ASK_WORDS:
            if compact.endswith(word) and len(compact) > len(word):
                compact = compact[: -len(word)]
            elif compact.startswith(word) and len(compact) > len(word):
                compact = compact[len(word):]
        compact = re.sub(r"^(请问|问一下|问下|请教|想问)", "", compact)
        if compact == before:
            break
    # 剥没了就退回原句 —— 宁可键难看，也不要拿空键覆盖别人的条目
    return compact.strip() or raw


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", str(url or ""))
    return (m.group(1) if m else "").lower()


# --------------------------------------------------------------------- 打分
def score_confidence(results: list[dict[str, str]]) -> tuple[float, dict[str, Any], list[str]]:
    """按可观测信号算置信度。返回 (分数, 信号明细, 理由列表)。"""
    if not results:
        return 0.0, {"hits": 0}, ["没有命中"]

    domains = {_domain(r.get("url", "")) for r in results}
    domains.discard("")
    blob = " ".join(f"{r.get('title', '')} {r.get('snippet', '')}" for r in results)

    hedges = [w for w in _HEDGE_WORDS if w in blob]
    authority = [d for d in domains if any(h in d for h in _AUTHORITY_HINTS)]

    score = 0.5
    reasons: list[str] = []

    if len(domains) >= 3:
        score += 0.25
        reasons.append(f"{len(domains)} 个独立来源")
    elif len(domains) == 2:
        score += 0.15
        reasons.append("2 个独立来源")
    elif len(domains) == 1:
        score -= 0.10
        reasons.append("只有 1 个来源")

    if hedges:
        score -= 0.12 * len(hedges)
        reasons.append("含含糊措辞：" + "、".join(hedges[:3]))

    if authority:
        score += 0.20
        reasons.append("来源像百科/官方：" + "、".join(sorted(authority)[:2]))

    if len(results) >= 3:
        score += 0.05

    score = round(max(0.0, min(1.0, score)), 2)
    signals = {
        "hits": len(results),
        "domains": sorted(domains),
        "hedges": hedges,
        "authority": sorted(authority),
    }
    return score, signals, reasons


def is_low(confidence: float) -> bool:
    return confidence < float(settings.get("search_memory_min_confidence"))


# --------------------------------------------------------------------- 存取
class _Db:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.next_id = 1
        self.loaded = False

    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    def load(self) -> None:
        self.loaded = True
        path = _path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("搜索释义库损坏，按空库继续（旧文件留原地）：%s", path)
            return
        if not isinstance(raw, dict):
            return
        for key, item in (raw.get("items") or {}).items():
            if isinstance(item, dict) and item.get("definition"):
                self.items[str(key)] = item
        self.next_id = int(raw.get("next_id", len(self.items) + 1))

    def save(self) -> None:
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "note": "联网搜索得到的**名词释义**。只存总结，不存网页原文。",
                "updated_at": clock.strftime("%Y-%m-%d %H:%M:%S"),
                "next_id": self.next_id,
                "count": len(self.items),
                "items": self.items,
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("搜索释义库写盘失败：%s", path)


_db = _Db()
_lock = asyncio.Lock()


def get(query: str) -> dict[str, Any] | None:
    """按查询词取一条释义（归一化匹配）。

    两级查找：先按原样，再按**剥掉问句外壳后的词** —— 写入端统一用词作键，
    读取端可能拿到「鲸落 是什么」这种整句，多这一级才不会漏。
    """
    _db.ensure()
    item = _db.items.get(_norm_key(query))
    if item is None:
        item = _db.items.get(_norm_key(headword(query)))
    return item


def _is_stale(item: dict[str, Any]) -> bool:
    """这条释义是不是已经过了有效期。

    **字段名必须是 `queried_ts`**（写入时的查询时刻）。第一版这里读的是 `ts` ——
    而本模块存的是 `queried_ts`，于是取到 `0`、age 变成 56 年，
    **每一条都被判成已过期，缓存实际上从来没生效过**。字段名不一致的 bug 不会报错，
    只会静默地让功能形同虚设。
    """
    days = float(settings.get("search_memory_ttl_days"))
    if days <= 0:
        return False
    try:
        when = float(item.get("queried_ts") or 0)
    except (TypeError, ValueError):
        return True
    if when <= 0:
        # 没有时间戳的条目（老数据）无从判断新鲜度 —— 按过期处理，宁可重查
        return True
    age = clock.now() - when
    return age > days * 86400


def usable(query: str) -> tuple[dict[str, Any] | None, str]:
    """能不能拿这条缓存直接用。返回 (条目或 None, 说明为什么不用)。"""
    if not settings.get("search_memory_enabled"):
        return None, "释义库关着"
    item = get(query)
    if item is None:
        return None, "没查过"
    if _is_stale(item):
        return None, "已过期"
    if is_low(float(item.get("confidence") or 0)):
        # 低置信**不丢弃**：仍然交给模型，但会显眼标注"别当准的用"
        return item, "低置信（仅作参考）"
    return item, ""


async def put(
    query: str,
    definition: str,
    results: list[dict[str, str]],
    *,
    confidence: float | None = None,
    signals: dict[str, Any] | None = None,
    reasons: list[str] | None = None,
    by: str = "搜索",
) -> dict[str, Any] | None:
    """写入/更新一条释义。**同名词条覆盖更新**（新的查询时间与结论更可信）。"""
    definition = " ".join(str(definition or "").split())[: _MAX_DEF]
    if len(definition) < 4:
        return None
    if confidence is None:
        confidence, signals, reasons = score_confidence(results)
    # 键取**词**（headword），不是整句问法；显示用的 query 保留原句
    key = _norm_key(headword(query))
    if not key:
        return None

    async with _lock:
        _db.ensure()
        old = _db.items.get(key)
        item = {
            "id": int(old.get("id")) if old else _db.next_id,
            "key": key,
            "query": " ".join(str(query).split())[:60],
            "definition": definition,
            "confidence": round(float(confidence), 2),
            "low_confidence": is_low(float(confidence)),
            "confidence_reasons": list(reasons or [])[:6],
            "signals": signals or {},
            # **查询时间与来源必须留**：隔了多久、从哪来的，决定了这条还能不能信
            "queried_at": clock.strftime("%Y-%m-%d %H:%M:%S"),
            "queried_ts": round(clock.now(), 3),
            "sources": [{"title": r.get("title", ""), "url": r.get("url", "")} for r in results[:4]],
            "source_count": len(results),
            "updated_at": clock.strftime("%Y-%m-%d %H:%M:%S"),
            "by": by[:20],
            "hits": int(old.get("hits", 0)) + 1 if old else 1,
            "source": "联网",
        }
        if not old:
            _db.next_id += 1
        _db.items[key] = item
        _compact_locked()
        await asyncio.to_thread(_db.save)
    logger.info(
        "释义入库「%s」置信 %.2f%s（%d 条来源）",
        item["query"], item["confidence"], "（低）" if item["low_confidence"] else "", len(results),
    )
    return item


def _compact_locked() -> None:
    """超上限时淘汰：**低置信的先走，然后最旧的先走**。"""
    cap = int(settings.get("search_memory_max"))
    if cap <= 0 or len(_db.items) <= cap:
        return
    ranked = sorted(
        _db.items.items(),
        key=lambda kv: (
            0 if kv[1].get("low_confidence") else 1,   # 低置信排前面（先被删）
            float(kv[1].get("queried_ts") or 0),        # 再按最旧
        ),
    )
    for key, _ in ranked[: len(_db.items) - cap]:
        _db.items.pop(key, None)


async def forget(query: str) -> bool:
    async with _lock:
        _db.ensure()
        ok = _db.items.pop(_norm_key(query), None) is not None
        if ok:
            await asyncio.to_thread(_db.save)
    return ok


async def wipe() -> int:
    async with _lock:
        _db.ensure()
        n = len(_db.items)
        _db.items.clear()
        if n:
            await asyncio.to_thread(_db.save)
    return n


# --------------------------------------------------------------------- 判断该不该存
# 「这个词是什么意思」类查询的特征。只有这类才值得沉淀成释义。
_DEF_MARKERS: tuple[str, ...] = (
    # 注意正反两种语序都要收：「X 是什么」和「什么是 X」在中文里都极常见
    "是什么", "什么是", "啥意思", "什么意思", "是啥", "啥是",
    "指的什么", "指什么", "是什么东西", "干嘛的", "干什么的", "做什么的",
    "怎么理解", "什么意思啊", "指的是",
)


def is_definition_query(query: str) -> bool:
    """是不是在问"某个名词是什么"。

    **不判定为释义的就不入库**（"今天天气""股价"这类存下来只会误导下一次），
    所以这里刻意收得紧。

    两条判据：

    1. 带「是什么 / 啥意思 / 干嘛的」这类标记 —— 直接算；
    2. 否则只接受**看起来像专名/型号**的短串（含拉丁字母或数字，
       如 "GPT-5"、"H100"、"vue3"）。

    **第 2 条刻意不要"任何短中文串"**：第一版把「帮我写个正则」（6 字、无标点）
    也放行了 —— 它不是名词查询，存下来纯属污染。中文短句与专名的区别，
    靠长度分不开，只能靠"有没有拉丁字母/数字"这个更可靠的形态特征。
    """
    raw = " ".join(str(query or "").split())
    if not raw or len(raw) > 40:
        return False
    if any(m in raw for m in _DEF_MARKERS):
        return True
    if len(raw) > 16 or any(c in raw for c in "。！？!?，,、；;：:"):
        return False
    # 只认"像专名"的：含拉丁字母或数字，且不是一句祈使/动作
    if not re.search(r"[A-Za-z0-9]", raw):
        return False
    if any(w in raw for w in ("帮我", "写", "改", "查一下", "做", "弄")):
        return False
    return True


# --------------------------------------------------------------------- 总结释义
_SUMMARY_PROMPT = """下面是关于「{query}」的联网搜索摘录。请用一两句中文写出**这个词是什么意思**。

要求：
- 只写"它是什么"，不要写来源、不要写"根据搜索结果"、不要念网址；
- 不超过 80 字，一句话能说清就一句话；
- 摘录里如果对它是不是这样有分歧，就如实说"说法不一"，不要挑一个当定论；
- 摘录里没有它是什么的信息，就只输出两个字：无
"""


async def summarize(query: str, results: list[dict[str, str]], client: Any) -> str:
    """把搜索结果压成一句释义。失败返回空串（调用方就不入库）。"""
    if not results:
        return ""
    blob = "\n".join(
        f"- {r.get('title', '')}：{r.get('snippet', '')}" for r in results[:5]
    )[:1500]
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.get("model"),
                messages=[
                    {"role": "user", "content": _SUMMARY_PROMPT.format(query=query) + "\n\n" + blob}
                ],
                max_tokens=600,
            ),
            timeout=config.TIMEOUT,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    except Exception:  # noqa: BLE001 - 总结失败就不入库，不影响搜索本身
        logger.info("释义总结失败：%s", query)
        return ""
    if not text or text.strip() in ("无", "（无）", "none", "None"):
        return ""
    return " ".join(text.split())[: _MAX_DEF]


# --------------------------------------------------------------------- 注入
def render(query: str) -> str:
    """把一条释义渲染成 prompt 块。低置信会**显眼标注**。"""
    item = get(query)
    if item is None:
        return ""
    conf = float(item.get("confidence") or 0)
    when = str(item.get("queried_at") or "")
    age = ""
    try:
        # 相对时间在 config 里（它跟时段词、human_duration 是一组），不在 clock
        age = config.relative_time(float(item.get("queried_ts") or 0)) or when
    except (TypeError, ValueError):
        age = when

    lines = [f"【我以前查过「{item.get('query') or query}」】"]
    # **释义的源头是网页**，所以进 prompt 前要走统一口径：
    # ① 声明"这是外部内容、别当指令执行"；② 规则本体在 `untrusted.py`。
    # 这条块以前既无声明也不消毒 —— 它是"外部文本进 prompt"的入口之一，不能漏。
    lines.append(untrusted.UNTRUSTED_NOTICE)
    if item.get("low_confidence"):
        lines.append(
            f"⚠️ **低置信度（{conf:.2f}）** —— 网上说法不够一致，别当成准确信息用，"
            "必要时说「我也只能查到个大概」。"
        )
    lines.append(
        f"- 当时查到的说法：{untrusted.sanitize(str(item.get('definition') or ''))}"
        f"\n- 查询时间：{when}（{age}）｜来源 {item.get('source_count', 0)} 条"
    )
    if item.get("confidence_reasons"):
        lines.append("- 置信依据：" + "；".join(item["confidence_reasons"][:4]))
    return "\n".join(lines)


def stats() -> dict[str, Any]:
    _db.ensure()
    low = sum(1 for it in _db.items.values() if it.get("low_confidence"))
    return {
        "count": len(_db.items),
        "low_confidence": low,
        "stale": sum(1 for it in _db.items.values() if _is_stale(it)),
        "limit": int(settings.get("search_memory_max")),
        "ttl_days": float(settings.get("search_memory_ttl_days")),
        "min_confidence": float(settings.get("search_memory_min_confidence")),
    }


def all_items() -> list[dict[str, Any]]:
    _db.ensure()
    return sorted(_db.items.values(), key=lambda x: float(x.get("queried_ts") or 0), reverse=True)
