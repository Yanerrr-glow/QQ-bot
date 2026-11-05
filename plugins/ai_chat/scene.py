"""场景判类：行为 / 指向 / 把握。

## 为什么单独一个模块

判类要能被**离线回放重复验证**（`验证/_场景回放.py`），而 `context.py` 依赖 nonebot，
在没有装依赖的机器上跑不起来。这里只依赖标准库，所以回放可以脱离机器人直接跑。

## 为什么不再用「一组关键词」

旧实现（`context._SCENE_WORDS`）有三个实测错误：

1. **单字误判**：「亲密」里有单字「睡」，于是「今天加班累得想睡觉」被判成亲密，
   召回撒娇语料；
2. **顺序掩盖语义**：亲密排在吐槽之前，先命中就返回，后面的场景永远轮不到；
3. **没有出口**：猜不出来也硬给一个「闲聊」，而「闲聊」并不对应任何语料。

现在的做法：

* 强词（单独出现即可判定）记 2 分，弱词记 1 分，**按分数竞争**而不是先到先得；
* 有明确的 `unknown` 出口——**宁可不判，也不要猜错**；
* 并列最高分时降级为 `fuzzy`，并给出多个候选；
* 昵称化用法（「小笨蛋」）通过排除表挡掉，不当骂人。

## 边界

这里只判**行为与指向**，不判情绪强度，也不判断句子里的人称指代是否真的指向机器人
（那需要上下文）。指向是**线索级**判断，够用来排除「老板骂我废物」这类误判即可。
"""

from __future__ import annotations

import re
from typing import Any

# act 的取值集合。新增一个 act 必须同时：加规则、加回放样本、检查示例库里有对应语料。
ACTS: tuple[str, ...] = (
    "求助", "诉苦", "分享", "玩笑", "纠正", "骂", "亲密", "寒暄", "指令", "告别", "夸赞",
)
# 「夸赞」是 2026-11-03 按 #0007 新增的：原来「被夸」表达不出来，情绪只会下行。
# ⚠ 它与 `context_analysis.ACTS` 必须**逐项一致**（那边多一个「不知道」是模型的自我报告，
# 不属于场景）。`验证/_语法检查.py` 里有一条断言钉住两者，别只改一边。

# (act, 强词, 弱词)。**顺序有意义**：并列最高分时取靠前的那个。
# 「骂」这里只放**硬辱骂**：不因关系亲疏豁免的那几个。软辱骂走 `_SOFT_INSULT`。
_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("指令", ("帮我查", "帮我打开", "帮我写", "设置一下", "执行一下"), ("帮我", "去做")),
    ("纠正", ("记错了", "说错了", "搞错了", "不是说", "你不是", "明明"), ("又忘", "忘了", "不是这样")),
    ("骂", ("废物", "垃圾", "滚", "闭嘴", "恶心", "去死"), ("垃圾话", "讨厌你")),
    ("亲密", ("抱你", "抱抱", "抱一下", "亲你", "牵手", "心跳", "喜欢你", "爱你", "想你"),
     ("抱", "亲", "贴", "靠")),
    # 「告别」原先只在 `ACTS` 里挂着、**没有任何规则产出它**（2026-11-04 补）。
    # 放在「寒暄」之前：两句都命中时（「去睡了，晚安」）按顺序取靠前者，
    # 而"要离开"比"打招呼"更该决定这一轮的回应方式。
    ("告别", ("我走了", "我下线了", "我先下", "先忙了", "回头聊", "改天聊",
              "去睡了", "不聊了", "先撤了"), ("下线", "撤了")),
    ("寒暄", ("早上好", "早安", "晚安", "在吗", "你在不在"), ("你好", "再见", "拜拜")),
    ("诉苦", ("累", "难受", "烦死", "崩溃", "睡不着", "熬了", "加班", "疼", "要死", "磨人"),
     ("困", "烦", "难熬", "顶不住")),
    ("求助", ("怎么办", "怎么弄", "怎么解决", "怎么", "帮我", "如何", "为什么", "能不能", "是不是",
              "怎么样"),
     ("求", "问一下", "会不会")),
    # 「夸赞」写在「玩笑」**之前**：并列最高分时按 _RULES 顺序取胜者，
    # 「厉害了大佬」这种既像夸又像玩梗的句子，应当先按夸赞出（再由深判用 tone 修正）。
    ("夸赞", ("真厉害", "好厉害", "太厉害", "你真棒", "你好棒", "真棒", "好棒", "真强",
              "太强了", "有你的", "佩服", "了不起", "干得好", "做得好", "夸你"),
     ("厉害", "棒", "牛啊", "优秀", "厉害啊", "夸我", "夸了")),
    ("玩笑", ("哈哈", "笑死", "离谱", "梗", "草"), ("6", "牛", "绝了")),
    ("分享", ("刚刚", "今天", "昨天", "买了", "吃了", "去了", "玩了", "看了", "跑了", "试了"),
     ("要出去", "出门", "在路上", "干完")),
)

# ---- 需要语境才能解释的词：规则层**只标记，不判定** ----
#
# 「小笨蛋」是昵称还是骂，取决于前文、关系、最近是否被拒绝过——**词本身不携带这个区别**。
# 所以这里不建亲昵词白名单，也不做「play 够高就算玩笑」这类推断：
# 命中这些词时置 `needs_context=True`，交给上层的语境分析（方案 §13/§14）。
#
# 硬辱骂（`废物 / 滚 / 闭嘴`）不在此列：那几个不需要语境就能定性。
_SOFT_CUES: tuple[str, ...] = ("笨蛋", "傻子", "傻", "笨", "蠢", "讨厌鬼", "傻瓜", "臭鱼", "死鱼")

# 语境化排除：这些组合不是「亲密」语境里的「睡/靠」
_ANTI: dict[str, tuple[str, ...]] = {
    "亲密": ("睡不着", "一起睡", "睡不睡"),
}

_NORM = re.compile(r"[\s\u3000]+")

# ---- 判断「第三方是不是主语」用的线索（2026-11-03 从 `_target_of` 里提出来）----
# 允许中间夹一个标点或空白：「一个陌生人，说我是废物」里的「，」不能让它漏判。
_THIRD_SUBJECT = re.compile(
    r"(他|她|他们|别人|有人|网上|陌生人|网友)[，,、\s]*(都|也|还|不|在|说|就|骂|夸|嫌|笑)")
_THIRD_ROLE = re.compile(r"(老板|我爸|我妈|老师|同事|同学|朋友|领导|客户|室友)")
# 这句话是不是在说「我」——决定第三方主语是"来源"还是"对象"
_ABOUT_ME = re.compile(r"(我(是|就|这|真|又|怎么|好|没|被)|骂我|说我|夸我|笑我|嫌我|叫我)")
# 是不是在说「你」（机器人）
_ABOUT_YOU = re.compile(r"你(这个|太|真是|怎么|又|不是|说|要|干|做|就|也)")


def _body(text: str) -> str:
    return _NORM.sub("", str(text or ""))


def _blocked(act: str, body: str) -> bool:
    return any(word in body for word in _ANTI.get(act, ()))


def _target_of(body: str, act: str) -> str:
    """指向：bot / speaker / third / none。

    ## 2026-11-03 修正：`target` 只表示**态度对象**，不再兼职"言论来源"

    原来"第三方做主语"就返回 `third`，于是「老板骂我废物」被判成 `third`（第三方）——
    可按"这句话的褒贬冲着谁"看，被骂的是**我**，`third` 只是**来源**。
    两件事混在一个字段里，`ctx-008`「一个陌生人，说我是废物」就是活证据（#0008）。

    现在：来源交给 `_origin_of()`（`other` / `self`），`target` 只管对象。
    `third` 仍然有合法含义 —— 议论/贬低的**对象**就是第三方（「他就是个废物」）。

    顺序很关键：**第三方主语必须先判**，否则「老板说你真厉害」会先命中"含你即 bot"，
    把转述错判成本人被夸（#0007 审阅点名的那处）。
    """
    # ---- 1. 第三方做主语：来源是别人，对象要看它指向我、你还是它自己 ----
    if _THIRD_SUBJECT.search(body) or _THIRD_ROLE.search(body):
        if _ABOUT_ME.search(body):
            return "speaker"                       # 「老板骂我废物」→ 被骂的是我
        if "你" in body:
            return "bot"                           # 「老板说你真厉害」→ 夸的是你
        return "third"                             # 「他就是个废物」→ 贬的是他

    # ---- 2. 没有第三方主语：沿用线索级判断 ----
    if act == "寒暄" and "你" in body:
        return "bot"                                        # 「你在吗」
    if act == "亲密" and ("你" in body or "抱你" in body or "亲你" in body):
        return "bot"
    if act in ("求助", "玩笑") and "你说" in body:
        return "bot"
    if act in ("夸赞", "骂", "指令"):
        if _ABOUT_YOU.search(body) or body.startswith("你"):
            return "bot"
        if re.search(r"^(这|那)?(问题|东西|话)", body):
            return "none"                                   # 「这问题有点蠢」骂的是事
    if act == "纠正":
        return "bot"                                        # 纠正的对象默认是另一方
    if act in ("诉苦", "分享"):
        return "speaker"                                    # 讲自己的事
    if "我" in body:
        return "speaker"
    return "none"


# 「第三方在做**动作**」才算来源：他说/骂/夸/嫌…；只是被议论（「他就是个废物」）不算，
# 那是本人的判断，`origin` 仍是 `self`。
# 中间允许夹几个字（「老板**今天**夸我厉害」），但**不能跨句**（`。！？` 截断）。
_SOURCE_CUE = re.compile(
    r"(他|她|他们|别人|有人|网上|陌生人|网友|老板|我爸|我妈|老师|同事|同学|朋友|领导|客户|室友)"
    r"[^。！？]{0,6}(说|骂|夸|嫌|笑|讲|提|告诉|评价|吐槽)")


def _origin_of(body: str) -> str:
    """言论来源：`other`（在转述别人的话）/ `self`（本人说的）。

    **规则层只能给线索级判断**：出现「第三方 + 说/骂/夸…」就算 `other`。
    真正的指代与引述由深判确认（`context_analysis` 的 `origin` 字段）。
    """
    return "other" if _SOURCE_CUE.search(body) else "self"


def _score_act(strong: tuple[str, ...], weak: tuple[str, ...], body: str) -> int:
    """算一个 act 的分。**命中的词会被吃掉**，避免「怎么」重复命中「怎么样」。

    没有这一步时，「今天过得怎么样」会因为 `怎么` + `怎么样` 各算一次而得 4 分，
    压过一句明确的昵称（3 分）——实测就是这么错的。
    """
    text = body
    score = 0
    for word in sorted(set(strong), key=len, reverse=True):
        if word in text:
            score += 2
            text = text.replace(word, "□")
    for word in sorted(set(weak), key=len, reverse=True):
        if word in text:
            score += 1
            text = text.replace(word, "□")
    return score


def judge(text: str, *, play: float = 0.0) -> dict[str, Any]:
    """规则层判类：**只做快速否定，不假装能判语境**。

    返回 `{act, target, confidence, candidates, scores, needs_context, cues}`。

    * `act="unknown"` —— 判不出来。调用方据此**不注入语料**；
    * `needs_context=True` —— 出现了「笨蛋 / 傻 / 蠢」这类**离开语境就没有确定意思**的词。
      规则层不猜它是昵称还是骂，只把线索交出去（方案 §13/§14：由语境分析层解释）。

    `play` 参数保留但**不再参与判定**：用关系值推断语气的做法已按审阅意见删除，
    关系只作为语境分析的输入之一。
    """
    body = _body(text)
    if not body:
        return {"act": "unknown", "target": "none", "origin": "unknown", "confidence": "unknown",
                "candidates": [], "scores": {}, "needs_context": False, "cues": []}

    cues = [word for word in _SOFT_CUES if word in body]

    scores: dict[str, int] = {}
    for act, strong, weak in _RULES:
        if _blocked(act, body):
            continue
        score = _score_act(strong, weak, body)
        if score:
            scores[act] = score

    # **有软词线索时一律 unknown。**
    # 哪怕同一句里还有「今天」「怎么样」这些能命中别的场景的词，也不下结论：
    # 「小笨蛋，今天过得怎么样」的 act 取决于那句称呼怎么被理解，
    # 而那是规则层给不出的答案。`scores` 仍然返回，供语境分析层与排查参考。
    if cues:
        return {"act": "unknown", "target": "none", "origin": _origin_of(body), "confidence": "unknown",
                "candidates": [], "scores": dict(sorted(scores.items(), key=lambda kv: -kv[1])),
                "needs_context": True, "cues": cues}

    if not scores:
        return {"act": "unknown", "target": "none", "origin": _origin_of(body),
                "confidence": "unknown",
                "candidates": [], "scores": {}, "needs_context": False, "cues": []}

    # 硬辱骂**也标 needs_context**：它同样可能被语境改写。
    # 「垃圾分类」含「垃圾」、「你说『闭嘴』是什么意思」含「闭嘴」——按词直判会误伤。
    # 所以这里给结论但**不给 clear**：act 先按「骂」出，置信度降为 fuzzy，
    # 由上层决定要不要深判。宁可让它先当「不确定」，也不要凭一个词定性。
    # 夸赞与硬辱骂同款处理：规则层**只标线索、不给 clear**。
    # 理由：`judge()` 不返回 tone，而「你可真厉害啊」这种讽刺、以及「开玩笑式夸」，
    # 规则层一个都判不了。给 fuzzy 会触发深判（`needs_deep_judge` 要求 clear 才跳过），
    # 由深判带回 tone 再决定要不要动情绪（#0007）。
    if "夸赞" in scores:
        return {"act": "夸赞", "target": _target_of(body, "夸赞"), "origin": _origin_of(body),
                "confidence": "fuzzy", "candidates": ["夸赞"],
                "scores": dict(sorted(scores.items(), key=lambda kv: -kv[1])),
                "needs_context": True, "cues": []}

    if "骂" in scores:
        hard = [word for word in ("废物", "垃圾", "滚", "闭嘴", "恶心", "去死") if word in body]
        return {"act": "骂", "target": _target_of(body, "骂"), "origin": _origin_of(body),
                "confidence": "fuzzy",
                "candidates": ["骂"], "scores": dict(sorted(scores.items(), key=lambda kv: -kv[1])),
                "needs_context": True, "cues": hard}

    top = max(scores.values())
    winners = [act for act, value in scores.items() if value == top]
    act = winners[0]                     # 并列时按出现顺序取靠前的
    confidence = "fuzzy" if len(winners) > 1 else "clear"
    return {
        "act": act,
        "target": _target_of(body, act),
        "origin": _origin_of(body),
        "confidence": confidence,
        "candidates": winners[:3],
        "scores": dict(sorted(scores.items(), key=lambda kv: -kv[1])),
        # 有软词线索时标出来：即使 act 判成了别的（比如「诉苦」），
        # 上层也可能需要结合语境确认那句「小笨蛋」是什么意思。
        "needs_context": bool(cues),
        "cues": cues,
    }


def act_of(text: str, *, play: float = 0.0) -> str:
    """只要 act 的简写。`unknown` 原样返回，调用方据此不注入。"""
    return str(judge(text, play=play).get("act") or "unknown")
