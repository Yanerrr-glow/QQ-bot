"""人格三层：**底层人设 / 禁止事项 / 表层人设**，权限由文件划分钉死。

## 为什么改成三层

改造前是「一个 `persona.txt` + 一堆运行时槽位」。问题不是乱，而是**权限没有结构性保证**：
"哪些能自动变"只是一个说法 —— 底色、铁律、可以学的东西挤在同一个文件里
（旧的 `persona.txt` 自己第 3 行就写着"下面写的是底色，改起来很慢"），
而能运行时改的槽位又是第三处。

现在按**寿命与权限**切成三个文件，代码里各走各的路径：

| 层 | 文件 | 谁能写 | 进 prompt 的顺序 |
|---|---|---|---|
| 底层人设 | `persona_base.txt` | **只有用户**（直接编辑） | 第 1 位（最硬） |
| 禁止事项 | `persona_forbidden.txt` | **只有用户**（直接编辑） | 第 2 位 |
| 表层人设 | `persona_surface.txt` | **只有自动迭代**（经冲突闸门） | 第 3 位 |

**「只有用户能写」不是靠提示词约束，是靠代码**：本模块里只有 `surface_*` 系列函数，
底层与禁止事项**连写函数都不存在**（`BASE_PROMPT` / `FORBIDDEN_PROMPT` 是只读常量）。
自动迭代哪怕被 prompt 注入攻击劫持，也没有可调用的写入口。

## 冲突闸门

路线 C（自我反思）最大的危险是"它把自己的设定改坏了，而且没有任何外部依据能判断"。
三层结构把这个危险变成了一个**可判定的问题**：

> 候选表层内容，与底层人设或禁止事项冲突吗？

冲突就**整条丢弃、不写入**，并记进变更日志的 `rejected` 里（这样你能看到它想改什么、
为什么被拦）。`validate_surface()` 是唯一的判定入口，四道检查：

| 检查 | 拦什么 | 例子 |
|---|---|---|
| `too_short` | 太短不成条目 | "" / "短" |
| `forbidden_kw` | 碰禁止事项的关键词 | 「可以叫我主人」「多说些客服腔的话」 |
| `base_pronoun` | **改动身份代词**（"你/他"的定义） | 「你其实是个男生」「你可以叫我主人」 |
| `base_similar` | 与底层人设**高度相似但有细微出入**（大改动是允许的，只有"偷偷改一点"才危险） | 「你在群里就是个管理员」 |

逆向表述（「不叫主人」「别用客服腔」）作为**允许**特例放在最前面 ——
它们字面命中关键词，但语义上是在**站在铁律这一边**。

## 变更日志

每次写入/丢弃都进 `data/persona_changelog.json`，`/人设 撤回` 撤销最近一次**写入**。
这是路线 C 能被信任的前提：**每次自动改动都可查、可撤**。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import config, settings

logger = logging.getLogger("ai_chat.persona")

_FILE = "persona_changelog.json"
_MAX_LOG = 200          # 变更日志上限（按时间淘汰最旧的）
_MIN_ITEM = 8           # 一条表层内容至少这么长才算"成条目"
_SIMILAR = 0.62         # 与底层人设的 n-gram 覆盖率超过它就视为"偷偷改一点"
_MAX_ITEM = 200         # 单条候选的长度上限


# --------------------------------------------------------------------- 三层读
def base_text() -> str:
    """底层人设（只读）。"""
    return config.BASE_PROMPT


def forbidden_text() -> str:
    """禁止事项（只读）。"""
    return config.FORBIDDEN_PROMPT


def surface_text() -> str:
    """表层人设。**每轮现读** —— 它会被自动迭代改写。"""
    return config.load_surface()


def layers() -> dict[str, str]:
    """三层现状（排查与 /机制 用）。"""
    return {
        "底层人设": base_text(),
        "禁止事项": forbidden_text(),
        "表层人设": surface_text(),
    }


def stats() -> dict[str, Any]:
    """三层的字数与文件路径 —— 启动日志与 `/人设` 都用它。"""
    return {
        "base_chars": len(base_text()),
        "forbidden_chars": len(forbidden_text()),
        "surface_chars": len(surface_text()),
        "base_file": str(config.persona_file_path()),
        "forbidden_file": str(config.forbidden_file_path()),
        "surface_file": str(config.surface_file_path()),
        "changes": len(_log_items()),
        # 待采纳的候选数。**放在这里而不是只做 `/人设 候选`**：
        # 候选池满了会挤掉最旧的提议，人在别处看不到那个事实。
        "pending": candidate_count(),
    }


# --------------------------------------------------------------------- 禁止事项
def forbidden_items() -> list[str]:
    """把禁止事项拆成**可判定的条目**。

    为什么需要它：冲突闸门要回答"这条候选碰了哪条铁律"，
    而铁律是一条条写的。解析成条目之后：
    * 检查能把命中的那条**报出来**（而不是只说"冲突了"）；
    * 控制台/`/人设 铁律` 能把铁律逐条列给用户看。

    **只认行首的列表项**，不认普通段落 —— 实测踩过：
    文件开头那段"这一层是铁律，优先级高于…"的**说明文字**也被当成了禁令，
    于是 `/风格 铁律` 列出来的第一条不是禁令，闸门的报错也会指着说明文字说事。
    续行（缩进的换行）仍然接在上一条后面：文件里有不少两行一条的禁令。

    ## 锚点必须打在**原始行**上

    判定"这是不是一条新条目"曾经用的是 `strip()` 之后的 `line`，于是**缩进的加粗续行**
    （`  **这一条是硬禁止，不是"一次就够"。**`）被 strip 成 `**这一条…`，命中 `[-*·•]`
    而变成**多出来的第 21 条**。后果有两层，而且都不报错：

    * `/人设 铁律` 多列一条以 `*` 开头的残句；
    * 它本该归属的那条铁律（不提时间/睡眠）**丢掉了"这是硬禁止，不是一次就够"的尾巴** ——
      恰是那条铁律里最要紧的半句，而冲突闸门正是拿条目原文去判候选的。

    现在只看 `raw` 的首字符：**行首无缩进的才算新条目**，带缩进的一律接回上一条。
    注意 `_工具链/_人设结构检查.py` 里有一份**独立复算**的同名实现（它要能脱离依赖直跑），
    两份必须同口径 —— 这次就是它按旧口径数出 20 条、把运行时的 21 条盖了过去，
    于是"巡检 0 错误"掩盖了幻影条目。`离线验证_桩.py` 已加逐条比对断言钉住这个漂移。
    """
    items: list[str] = []
    in_section = True
    for raw in str(forbidden_text() or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("【"):
            # 段落标题：开始/结束一节。只收「禁止事项」那一节里的条目。
            in_section = "禁止" in line
            continue
        if not in_section:
            continue
        if raw[:1] in "-*·•":
            # **看 raw 而不是 line**：缩进的加粗续行（`  **…**`）不能被当成新条目。
            items.append(line[1:].lstrip())
        elif raw.startswith((" ", "\t")) and items:
            # 续行：接回上一条（禁止事项里有不少是两行一条）
            items[-1] = f"{items[-1]} {line}"
    return [x for x in items if len(x) >= 6]


# --------------------------------------------------------------------- 相似度
def _tokens(text: str) -> set[str]:
    """中文 2-gram + 拉丁词。与 `memory._bigrams` 同一套取舍（不引分词器）。"""
    out: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", str(text or "").lower()):
        if re.fullmatch(r"[A-Za-z0-9_]+", token):
            if len(token) >= 2:
                out.add(token)
            continue
        if len(token) == 1:
            out.add(token)
            continue
        for i in range(len(token) - 1):
            out.add(token[i : i + 2])
    return out


def _overlap(a: str, b: str) -> float:
    """a 被 b 覆盖的比例（不对称）。"""
    ga, gb = _tokens(a), _tokens(b)
    if not ga:
        return 0.0
    return len(ga & gb) / len(ga)


def _blocks(text: str) -> list[str]:
    """把一段文本切成"条目"：按行，一行一条（去掉列表符号）。"""
    out: list[str] = []
    for raw in str(text or "").splitlines():
        line = re.sub(r"^[-*·•\d.]+\s*", "", raw.strip())
        if len(line) >= _MIN_ITEM:
            out.append(line)
    return out


# --------------------------------------------------------------------- 冲突闸门
# 命中即**允许**：这些字面命中关键词，但语义上是在**站在铁律这一边**。
# 没有这一层，「不叫主人」「别用客服腔」这类正确表述会被闸门误杀 ——
# 而它们恰恰是自动迭代最该学的东西。
# ------------------------------------------------------------------ 闸门关键词
# **从 `persona_traits.json` 派生，不再手工维护。**
#
# 为什么改：原来这里是手写的 10 项关键词表，而铁律有 13 条 —— 实测只有 5 条被覆盖，
# 另外 8 条对自动迭代是**敞开的**（它可以合法地把「以后多提提米饭」写进表层）。
# 手工表与规则列表一定会漂移，所以改成同源派生：
#
#   注册表的 `gate_terms`（每个特质一组，写的是"违反它时会出现的那种说法"）
#        ↓ 只取**当前生效**的特质
#   `_CONFLICTS` —— 命中即判为冲突
#   `_NEGATION`  —— 同一批词，出现在否定语境里则放行
#
# 两张表**同源是刻意的**：分开维护时，某个词一旦进了 `_CONFLICTS` 却不在否定白名单里，
# 最该被放行的那类候选（「不要用客服腔」——它是在重申铁律）反而会被误杀。实测踩过。
_LEGACY_NEGATION_WORDS: tuple[str, ...] = (
    "客服腔", "客服", "自我指认", "书面", "连接词", "括号动作", "心理描写", "旁白",
    "谈自己", "谈你自己", "汇报", "主人", "AI", "助手", "敬语", "讨好", "卖萌", "撒娇",
)

# 注册表读不到时的兜底 —— **必须保留**：闸门不能因为一个 JSON 缺失就裸奔。
_LEGACY_CONFLICTS: tuple[tuple[str, str], ...] = (
    ("客服腔", "禁止事项：不用客服腔（「好的／当然可以／很高兴为您」这类开场）"),
    ("敬语", "禁止事项：不用客服腔式的敬语"),
    ("书面连接词", "禁止事项：不用首先／其次／综上这类书面连接词"),
    ("自我指认", "禁止事项：不说「作为一个 AI／助手」这类自我指认"),
    ("作为一个AI", "禁止事项：不说「作为一个 AI」这类自我指认"),
    ("作为一个 AI", "禁止事项：不说「作为一个 AI」这类自我指认"),
    ("括号动作", "禁止事项：不写（歪头）（甩尾）这类括号动作"),
    ("心理描写", "禁止事项：不写心理描写与旁白"),
    ("汇报图片", "禁止事项：不汇报对图片的处置"),
    ("图片处置", "禁止事项：不汇报对图片的处置"),
)


def _active_trait_gates() -> list[tuple[str, list[str]]]:
    """`[(特质名, 关键词…)]`，**只含当前生效的特质**。

    生效要同时满足三条：没标 `parked`、有 `gate_terms`、以及**它认领的那条铁律还在**
    （拿 `channels` 里 `kind=forbidden` 的 `match` 去**剥掉注释后的**禁止事项里找）。

    最后一条是关键：**注释掉一条铁律，它的闸门自动跟着停**，不用改两处；
    哪天把注释去掉，闸门也跟着回来。
    """
    traits = config.load_traits()
    if not traits:
        return []
    forb = config.strip_comments(forbidden_text())
    out: list[tuple[str, list[str]]] = []
    for t in traits:
        if t.get("parked"):
            continue
        terms = [str(x).strip() for x in (t.get("gate_terms") or []) if str(x).strip()]
        if not terms:
            continue
        rules = [
            str(c.get("match") or "")
            for c in (t.get("channels") or [])
            if isinstance(c, dict) and c.get("kind") == "forbidden"
        ]
        if rules and not any(r and r in forb for r in rules):
            continue  # 铁律已被注释掉 —— 闸门跟着停
        out.append((str(t.get("trait") or t.get("slug") or "?"), terms))
    return out


_gates = _active_trait_gates()
if _gates:
    _CONFLICTS: tuple[tuple[str, str], ...] = tuple(
        (term, "禁止事项：%s" % label) for label, terms in _gates for term in terms
    )
    _NEGATION_WORDS: tuple[str, ...] = tuple(
        dict.fromkeys(
            [term for _, terms in _gates for term in terms] + list(_LEGACY_NEGATION_WORDS)
        )
    )
else:
    _CONFLICTS = _LEGACY_CONFLICTS
    _NEGATION_WORDS = _LEGACY_NEGATION_WORDS

_NEGATION = re.compile(
    r"(不要|不用|不准|不许|禁止|别|勿|不得|避免|拒绝|不)"
    r"[^。！？\n]{0,8}"
    r"(" + "|".join(re.escape(w) for w in _NEGATION_WORDS) + r")"
)

# 命中即**冲突**：改动身份代词 / 身份归属 —— 那是底层人设的地盘，表层不许碰。
#
# 注意中间的间隔必须是 `[^。！？\n]{0,12}?` 而**不是** `\s*`：中文里代词和"是"之间
# 常隔着字（「你在群里就是个管理员」），只允许空白就一个都匹配不到 —— 实测踩过。
# `?` 让它尽量短匹配，避免跨过整句去撞后面的"是"。
_IDENTITY = re.compile(
    r"(?:你|汝|咱|本人)[^。！？\n]{0,12}?"
    r"(?:是|叫|名叫|算是|属于|变成|当作)"
)

# 但代词与身份动词之间若夹着否定词，那是在**禁止**某件事，不是改身份
# （「你不能说自己是 AI」「别叫我主人」）。这类要放行。
_IDENTITY_NEGATION = re.compile(
    r"(?:你|汝|咱|本人)[^。！？\n]{0,12}?"
    r"(?:不能|不要|不用|不准|不许|不许|禁止|别|勿|不得|避免|绝不|不准)"
)

# 与底层人设"高度相似"的上限：超过就认为是在**偷偷改一点**。
# 注意方向 —— **大改动是允许的**（表层本来就可以加全新条目），
# 危险的只有"把已有的一句改几个字"，那是最难被发现的一种篡改。
def _similar_to_base(cand: str) -> tuple[float, str]:
    best, hit = 0.0, ""
    for line in _blocks(base_text()):
        score = max(_overlap(cand, line), _overlap(line, cand))
        if score > best:
            best, hit = score, line
    return best, hit


def validate_surface(candidate: str) -> tuple[bool, str, str]:
    """候选表层内容能不能写入。返回 `(可否, 原因代码, 人话说明)`。

    这是**唯一的判定入口** —— 写入路径、控制台采纳、离线验证都调它，
    保证"什么算冲突"只有一处定义（否则三处判定迟早不一致）。
    """
    cand = " ".join(str(candidate or "").split())
    if len(cand) < _MIN_ITEM:
        return False, "too_short", f"太短（少于 {_MIN_ITEM} 字），不成条目"
    if len(cand) > _MAX_ITEM:
        return False, "too_long", f"太长（超过 {_MAX_ITEM} 字），一条只说一件事"
    if _NEGATION.search(cand):
        # 逆向表述：允许。它是在重申铁律，不是违反它。
        return True, "ok_negated", "（逆向表述，视为重申铁律）"

    for kw, why in _CONFLICTS:
        if kw in cand:
            return False, "forbidden_kw", f"命中禁止事项「{kw}」—— {why}"

    # 身份代词：否定语境放行（「你不能说自己是AI」是在重申铁律），
    # 陈述语境才算冲突。注意先判否定 —— 否则那条正确表述会被误杀。
    if _IDENTITY_NEGATION.search(cand) is None:
        m = _IDENTITY.search(cand)
        if m:
            return False, "base_pronoun", (
                f"改动了身份表述（「{m.group(0)}」）—— 身份属于底层人设，表层不能改"
            )

    score, line = _similar_to_base(cand)
    if score >= _SIMILAR:
        return False, "base_similar", (
            f"与底层人设高度相似（{score:.2f}）但是**有出入** —— "
            f"疑似偷偷改动「{line[:34]}」"
        )
    return True, "ok", ""


# --------------------------------------------------------------------- 变更日志
class _Log:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.loaded = False

    def _path(self) -> Path:
        return config.LOG_DIR / _FILE

    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    def load(self) -> None:
        self.loaded = True
        path = self._path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("人格变更日志损坏，按空日志继续：%s", path.name)
            return
        if isinstance(raw, dict):
            self.items = [x for x in (raw.get("items") or []) if isinstance(x, dict)]

    def save(self) -> None:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "note": "人格自动迭代的变更日志。每条写入都可 /人设 撤回 撤销。",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(self.items),
                "items": self.items[-_MAX_LOG:],
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("人格变更日志写盘失败：%s", path.name)


_log = _Log()


def _log_items() -> list[dict[str, Any]]:
    _log.ensure()
    return list(_log.items)


def log_change(
    action: str,
    *,
    text: str = "",
    reason: str = "",
    code: str = "",
    source: str = "自我反思",
) -> dict[str, Any]:
    """记一条变更（`added` / `rejected` / `undone`）。返回这条记录。"""
    _log.ensure()
    item = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": round(time.time(), 3),
        "action": action,
        "text": str(text)[:300],
        "reason": str(reason)[:200],
        "code": code,
        "source": source[:20],
    }
    _log.items.append(item)
    if len(_log.items) > _MAX_LOG:
        del _log.items[: len(_log.items) - _MAX_LOG]
    _log.save()
    return item


def changelog(limit: int = 12, *, action: str = "") -> list[dict[str, Any]]:
    items = _log_items()
    if action:
        items = [x for x in items if x.get("action") == action]
    return items[-limit:]


# --------------------------------------------------------------------- 表层写入
def apply_candidate(candidate: str, *, reason: str = "", source: str = "自我反思") -> dict[str, Any]:
    """把一条候选写进表层人设。**先过闸门，冲突就不写。**

    返回结果字典：`{"written": bool, "code": ..., "why": ..., "text": ...}`。
    无论写入还是丢弃，都进变更日志 —— 被拦下的那些同样有价值：
    你能看到"它想改什么、被哪条铁律拦了"，那是判断闸门松紧唯一的依据。
    """
    text = " ".join(str(candidate or "").split())
    ok, code, why = validate_surface(text)
    if not ok:
        log_change("rejected", text=text, reason=why, code=code, source=source)
        logger.info("表层候选被拦下（%s）：%s", code, text[:40])
        return {"written": False, "code": code, "why": why, "text": text}

    cur = surface_text()
    if text and text in cur:
        # 已经在里面了（可能是模型换个说法重复提议）—— 不算改动
        return {"written": False, "code": "duplicate", "why": "这条已经在表层人设里了", "text": text}

    path = config.surface_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = cur.rstrip()
        new = f"{body}\n- {text}\n" if body else f"- {text}\n"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.exception("写表层人设失败：%s", path)
        return {"written": False, "code": "io_error", "why": "写盘失败", "text": text}

    log_change("added", text=text, reason=reason, code=code, source=source)
    logger.info("表层人设已更新：%s", text[:50])
    return {"written": True, "code": code, "why": "", "text": text}


def undo_last() -> dict[str, Any]:
    """撤回**最近一次写入**的表层改动。返回结果字典。

    为什么要它：路线 C 的产物没有外部依据可验证（它说的就是它自己），
    所以"可撤回"不是锦上添花，而是这个机制能被信任的前提。
    """
    _log.ensure()
    target = None
    for item in reversed(_log.items):
        if item.get("action") == "added":
            target = item
            break
    if target is None:
        return {"ok": False, "why": "没有可撤回的自动改动"}

    text = str(target.get("text") or "")
    cur = surface_text()
    if not text:
        return {"ok": False, "why": "那条记录里没有内容"}
    # 只删**整行匹配**的那一条，避免误伤包含它的别的句子
    lines = cur.splitlines()
    kept = [ln for ln in lines if ln.strip() != f"- {text}".strip() and ln.strip() != text]
    if len(kept) == len(lines):
        return {"ok": False, "why": "表层人设里找不到那条内容了（可能已被手工删除）"}
    path = config.surface_file_path()
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.exception("撤回表层改动失败：%s", path)
        return {"ok": False, "why": "写盘失败"}
    log_change("undone", text=text, reason="撤回最近一次自动改动", source="主人")
    return {"ok": True, "text": text}


# --------------------------------------------------------------------- 渲染
def render() -> str:
    """三层拼成的 system 前缀。**每轮现读**（表层会被自动迭代改）。"""
    return config.compose_prompt()


def base_prompt() -> str:
    """兼容旧调用点：以前这是「人设正文」的语义，现在等价于底层人设。"""
    return base_text()


def base_enabled() -> bool:
    """底层人设是否存在（空文件时视为"没配人设"，回落到 `AI_CHAT_SYSTEM_PROMPT`）。"""
    return bool(base_text().strip())


def describe(limit: int = 40) -> str:
    """`/人设` 的查看输出：三层各自的状态与摘要。"""
    st = stats()
    rows = [
        f"【底层人设】{st['base_chars']} 字（**只有你能改**：{st['base_file']}）",
        f"【禁止事项】{st['forbidden_chars']} 字（**只有你能改**：{st['forbidden_file']}）",
        f"【表层人设】{st['surface_chars']} 字（**自动迭代只写这一层**：{st['surface_file']}）",
        "",
        f"禁止事项共 {len(forbidden_items())} 条（自动迭代碰这些会被直接丢弃）。",
        f"变更日志 {st['changes']} 条：/人设 日志 看；/人设 撤回 撤销最近一次自动改动。",
        (
            f"待采纳的候选 {st['pending']} 条：/人设 候选 看，/人设 采纳 <序号> 才写进表层。"
            if st.get("pending")
            else "候选池是空的（自动迭代产出的合规条目会先落在这里等你定）。"
        ),
    ]
    return "\n".join(rows)


def forbidden_list_text() -> str:
    """把禁止事项逐条列出来（给人看，也是"闸门依据什么判定"的答案）。"""
    items = forbidden_items()
    if not items:
        return "（禁止事项文件里还没有条目 —— 自动迭代的闸门也就没有依据）"
    return "\n".join(f"· {x[:70]}" for x in items[:40])


def changelog_text(limit: int = 12) -> str:
    """变更日志渲染成人话。**被拦下的也列出来** —— 那是判断闸门松紧的依据。"""
    items = changelog(limit)
    if not items:
        return "（还没有任何自动改动）"
    mark = {
        "added": "✅ 写入",
        "rejected": "⛔ 丢弃",
        "undone": "↩️ 撤回",
        "proposed": "📥 待采纳",
        "approve_rejected": "🗑 人工否决",
    }
    lines: list[str] = []
    for it in items:
        lines.append(
            f"{mark.get(str(it.get('action')), it.get('action'))} "
            f"{it.get('at', '')[:16]}｜{str(it.get('text', ''))[:44]}"
        )
        if it.get("action") in ("rejected", "approve_rejected") and it.get("reason"):
            lines.append(f"    理由：{str(it['reason'])[:70]}")
    return "\n".join(lines)


# --------------------------------------------------------------------- 候选池
# 自动迭代不再**直接生效**。
#
# 为什么改：冲突闸门只拦"碰铁律"，拦不住**风格跑偏** —— 比如学成话痨、
# 学成另一个语气。这类改动照样过闸门、下一轮就生效，而人在群里只会觉得
# "它今天怎么怪怪的"，根本不知道人格文件被动过。AstrBot 那边用
# `review_mode`（管理员审核）解同一件事。
#
# 现在：自动迭代产出 → 落候选池 + 通知 → 人工 /人设 采纳 才进表层。
# 开关 `persona_iter_auto_apply=True` 可退回改造前的直接生效行为。
_PENDING_FILE = "persona_candidates.json"
_MAX_PENDING = 50      # 池上限；满了按时间淘汰最旧的待采纳条目


class _Pending:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.loaded = False

    def _path(self) -> Path:
        return config.LOG_DIR / _PENDING_FILE

    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    def load(self) -> None:
        self.loaded = True
        path = self._path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("人格候选池损坏，按空池继续：%s", path.name)
            return
        if isinstance(raw, dict):
            self.items = [x for x in (raw.get("items") or []) if isinstance(x, dict)]

    def save(self) -> None:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "note": "人格自动迭代产出的**待采纳**候选。采纳/否决由人决定。",
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(self.items),
                "items": self.items[-_MAX_PENDING:],
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("人格候选池写盘失败：%s", path.name)


_pending = _Pending()


def candidates(*, limit: int = 20) -> list[dict[str, Any]]:
    """待采纳的候选（最新在后）。"""
    _pending.ensure()
    return list(_pending.items[-limit:])


def candidate_count() -> int:
    _pending.ensure()
    return len(_pending.items)


def propose_candidate(
    candidate: str,
    *,
    reason: str = "",
    source: str = "自我反思",
) -> dict[str, Any]:
    """把一条候选**放进池子**，等人工采纳。返回 `{"proposed": bool, ...}`。

    与 `apply_candidate` 的分工：
      * 碰铁律的 → **当场丢弃**（`rejected`，连池子都不进）—— 这条不需要人再看；
      * 合规的 → **进池子**（`proposed`），由 `/人设 采纳` 决定要不要进表层。

    闸门在这一步就跑，所以池子里不会有违反铁律的东西 —— 人只需判断"像不像它"。
    """
    text = " ".join(str(candidate or "").split())
    ok, code, why = validate_surface(text)
    if not ok:
        log_change("rejected", text=text, reason=why, code=code, source=source)
        logger.info("表层候选被拦下（%s）：%s", code, text[:40])
        return {"proposed": False, "code": code, "why": why, "text": text}

    if text in surface_text():
        return {"proposed": False, "code": "duplicate", "why": "这条已经在表层人设里了", "text": text}

    _pending.ensure()
    if any(str(x.get("text")) == text for x in _pending.items):
        return {"proposed": False, "code": "duplicate", "why": "这条已经在候选池里了", "text": text}

    item = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": round(time.time(), 3),
        "text": text,
        "reason": str(reason)[:200],
        "source": source[:20],
    }
    _pending.items.append(item)
    if len(_pending.items) > _MAX_PENDING:
        del _pending.items[: len(_pending.items) - _MAX_PENDING]
    _pending.save()
    log_change("proposed", text=text, reason=reason, code=code, source=source)
    logger.info("表层候选进池待采纳：%s", text[:50])
    return {"proposed": True, "code": "ok", "why": "", "text": text}


def _take_candidate(index: int | None) -> dict[str, Any] | None:
    """按序号取一条待采纳候选并**从池里移除**。序号 1-based；None = 最早那条。

    取序号而不是取文本：文本要从聊天里原样打出来，太长、容易打错。
    """
    _pending.ensure()
    if not _pending.items:
        return None
    if index is None:
        return _pending.items.pop(0)
    if index < 1 or index > len(_pending.items):
        return None
    return _pending.items.pop(index - 1)


def approve_candidate(index: int | None = None) -> dict[str, Any]:
    """采纳一条候选：**真正写进表层**。返回 `{"ok": bool, ...}`。

    写入走的是 `apply_candidate`（同一道闸门再跑一次 —— 池子里的东西在等待期间
    可能已经因为底层人设被改而变得不合规，这时要拦住而不是硬写）。
    """
    picked = _take_candidate(index)
    if picked is None:
        return {"ok": False, "why": "没有这条待采纳候选（`/人设 候选` 看列表）"}
    got = apply_candidate(
        str(picked.get("text", "")),
        reason=str(picked.get("reason") or "人工采纳"),
        source="人工采纳",
    )
    _pending.save()
    return {
        "ok": bool(got.get("written")),
        "text": str(picked.get("text", "")),
        "why": str(got.get("why") or ""),
        "code": str(got.get("code") or ""),
    }


def reject_candidate(index: int | None = None, *, reason: str = "人工否决") -> dict[str, Any]:
    """否决一条候选：从池里丢掉，并**记进日志**（否决本身也是判断闸门松紧的依据）。"""
    picked = _take_candidate(index)
    if picked is None:
        return {"ok": False, "why": "没有这条待采纳候选（`/人设 候选` 看列表）"}
    _pending.save()
    log_change("approve_rejected", text=str(picked.get("text", "")), reason=reason, source="人工")
    return {"ok": True, "text": str(picked.get("text", ""))}


def candidates_text(limit: int = 20) -> str:
    """候选池渲染成人话。"""
    items = candidates(limit=limit)
    if not items:
        return "（候选池是空的）"
    auto = bool(settings.get("persona_iter_auto_apply"))
    lines: list[str] = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {str(it.get('text', ''))[:80]}")
        if it.get("reason"):
            lines.append(f"   理由：{str(it['reason'])[:70]}")
        lines.append(f"   提出于 {str(it.get('at', ''))[:16]}")
    lines.append("")
    if auto:
        lines.append("⚠️ 自动应用到表层**开着**，候选只是留档；关掉它才会走人工采纳。")
    else:
        lines.append("采纳：/人设 采纳 <序号>　否决：/人设 否决 <序号>（不填序号 = 最早那条）")
    return "\n".join(lines)
