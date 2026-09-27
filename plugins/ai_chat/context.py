"""上下文组装：把「谁在说话、说了什么、我记得什么、该怎么回」拼成一次请求。

## 为什么把这堆逻辑从 `__init__.py` 里搬出来

改造前 `_reply()` 里边要拼五种东西（已读背景、未读发言、引用、文件、图片），
而且拼法散在 100 多行里，想加一块「长期记忆」就得往那个函数中间塞 —— 结果就是
人设、记忆、风格三套东西互相抢位置，谁在前后全靠代码顺序，没人说得清。

现在固定成**八段**，顺序就是这个优先级（越靠前越硬）：

```
1   人格三层        persona.render()    —— 底色 → 铁律 → 表层；每轮现读，缓存友好
3   当前时间        config.time_hint()  —— 每次都变，压在人设之后
3.2 说话模式        mode.hint()         —— 这会儿该多用力
3.5 记录读法        record_legend()     —— 机制，不随人设走
4   长期记忆        memory.build_context() —— 跨会话"我一直知道的事"
4.2 翻到的旧记录     msgindex.render()   —— **聊天原文**（只在"回忆类"问法时）
5   自己的机制       introspect.prompt_block() —— 只在被问到时出现
user: 更早的聊天记录 + 会话摘要 + 刚才的新发言 + 引用 + 本次要回应的
```

> **更正**：原来的第 2 段「运行时风格要求 `persona.DIRECTIVES`」
> 已随人格三层重构删除 —— 那个模块现在连 `DIRECTIVES` 这个名字都没有。
> 当场要求说话方式的路改成了「人设信号账本 + 只写表层」，所以段号从 3 起跳是**故意的**，
> 与 README 5.5 节的历史编号对得上，不要"顺手补齐"。

第 4、5 段是后来加的：**4 解决"没有长期记忆、没逻辑"，5 解决"讲不清自己机制就编"。**
第 3 段的位置也调整过：以前时间戳在最末尾（为了缓存），现在放到人设之后 ——
因为记忆块长度不定，放在时间之前可以让"人设 + 时间"这个稳定前缀继续被缓存。

## 两套请求：system（稳定）与 user（多变）

**分配原则是"这个内容多久变一次"**：

* 进 system：人设、风格、时间、模式、记录读法、长期记忆、机制事实 —— 它们要么不变，
  要么只在**会话边界/被问到**时才变，值得放进可缓存的前缀；
* 进 user：聊天记录原文、**会话摘要**、引用、本次发言 —— 它们**每条消息都在变**，
  放进 system 会让整个前缀缓存每轮失效 —— 所以会话摘要放在这里，
  正是这个原因（摘要跨会话边界就变，而边界很频繁）。
"""

from __future__ import annotations

import logging
import random
import re
from typing import Any

from . import (
    behavior,
    chatlog,
    config,
    introspect,
    memory,
    mode,
    msgindex,
    persona,
    settings,
    state,
    summaries,
)

logger = logging.getLogger("ai_chat.context")

# 回复后处理用的固定套路（不在代码里重写自然语言，只做安全兜底）
_BAD_PREFIXES = ("作为一个AI", "作为 AI", "作为人工智能", "我是一个AI", "我是 AI")
_OPENERS = ("好的，", "好的,", "当然可以", "很高兴为您", "希望对你有帮助")

# --------------------------------------------------------------------- 自指
# 这一组用来做两件互补的事：
#   ① 判断"对方是不是在明确问我的设定/机制" —— 是，就用正式严肃的风格回答；
#   ② 不是问的时候，把回复里**自我机制陈述**的句子摘掉。
#
# 为什么必须动代码，不能只靠人设：实测 09-23 晚它开始说「是我设定里就分了这两档」
# 「挑"这张是我"」这类元讨论 —— 人设里写了"像群里的人一样说话"，但那是**软约束**，
# 斗不过模型"把话说清楚"的倾向。软约束配硬过滤才成立。
_ASK_SELF_WORDS = (
    "机制", "设定", "人设", "人格", "prompt", "提示词", "系统提示", "配置",
    "参数", "怎么实现", "怎么做到", "为什么这么", "/机制", "你是谁", "你是什么",
    "你的设定", "你的性格", "你怎么想", "你自己",
)
# 一句话里同时出现"我"和这些词，才算在讲自己的设定/机制。
# 关键词取得**窄**是故意的：「人设」「设定」也可能是正常聊天里说别的（"这个人设图不错"），
# 所以要求同时命中第一人称 + 机制词（见 _is_self_meta）。
_SELF_MECH_WORDS = (
    "设定", "人设", "机制", "提示词", "prompt", "系统提示", "prompt 里",
    "参数", "配置", "模式", "我分了两档", "档", "底层", "权重",
    # 「用图」是同一类东西：它也是在讲自己内部怎么处理图片（09-23 晚它说过
    # 「逻辑很简单，就两条：一是情绪对上了才用…二是看着像我」）。
    "用图", "用图逻辑", "挑图", "选图", "存图", "表情包库", "图库",
)
_SELF_FIRST_PERSON = ("我", "咱", "本人", "自己")

# 整条回复都在讲自己的机制时用它兜底：不解释、不空回。就一个字。
_META_ONLY_FALLBACK = "嗯。"

_SENT_SPLIT = re.compile(r"(?<=[。！？!?…~])")
_SELF_META = re.compile(
    r"(?:我|咱|本人|自己)[^。！？!?\n]{0,20}"
    r"(?:设定|人设|机制|提示词|prompt|系统提示|参数|配置|模式|分了两档|底层|权重"
    r"|用图|挑图|选图|存图|表情包库|图库)"
    r"|(?:是我设定|我设定|我的人设|我的人格|我的机制|我的提示词)"
)
_ASK_SELF = re.compile("|".join(re.escape(w) for w in _ASK_SELF_WORDS), re.I)


def ask_about_self(text: str) -> bool:
    """对方是不是在**明确问**机器人自己的设定 / 机制。

    是的话：用正式严肃的风格回答，并且**不过滤**自我机制陈述（人家就是想听）。
    不是的话：回复里任何自我机制陈述都会被 `strip_self_meta()` 摘掉。
    """
    return bool(_ASK_SELF.search(str(text or "")))


# 图片话题：用户明确要求「除非被问到图片相关内容，不主动表示自己对图像的理解、感想和描述」。
# 判定刻意**只认"我 + 谈论图片的动词/介词"**（我挑这张 / 我拿它挡 / 我用图…）。
# 为什么不用"以图为主语"来判：那样会把「这图看得我笑了一下」这种**正常反应**也删掉 ——
# 用户要禁的是"分析图片"，不是"对图有反应"。实测这两个误删就是这么来的。
_IMG_TALK = re.compile(
    r"(?:我|咱|本人|自己)[^。！？!?\n]{0,15}"
    r"(?:用图|挑图|选图|存图|收图|配图|用这张|挑这张|选这张|拿它挡|挡了一下|挡了"
    r"|用这|挑这|选这|拿这|用那|挑那|选那|拿那|最想用)"
)


def _is_self_meta(sentence: str) -> bool:
    """这一句是不是在讲自己的设定/机制/怎么用图。"""
    s = str(sentence or "").strip()
    if not s:
        return False
    if _SELF_META.search(s):
        return True
    if _IMG_TALK.search(s):
        return True
    # 兜底：同时有第一人称 + 机制词，也算
    return any(w in s for w in _SELF_FIRST_PERSON) and any(w in s for w in _SELF_MECH_WORDS)


_WEAK_SPLIT = re.compile(r"(?<=[，,；;])")


def _clauses(sentence: str) -> list[str]:
    """把一句长话按逗号/分号再切短。**只对长句做**。

    为什么要切：机制陈述常跟正常话黏在一句里（"那张图是我自己嘴硬完下不来台，
    拿它挡了一下。你一点都不差劲。"），不切就整句都判不定、只能放弃过滤。
    为什么限定长度：短句切了会把"我挑图只挑看着像我的"切掉前半截，
    后半截看不出第一人称，反而漏判。
    """
    s = str(sentence or "")
    if len(s) <= 40:
        return [s]
    parts = [x for x in _WEAK_SPLIT.split(s) if x]
    return parts or [s]


def strip_self_meta(text: str) -> str:
    """把回复里"讲自己设定/机制/怎么用图"的部分摘掉。**没被问到的时候才用。**

    只删句子/分句，不删整条：一句元讨论不该让整条回复作废。

    **全被删掉时返回空串**，由调用方决定怎么办（`polish` 会回退用原文）。
    早先这里是"返回最短的分句"——那等于把已经判定为元讨论的句子又还回去了，
    过滤直接失效（"这张图是我今天最想用的"整句都是元讨论，却原样留下）。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    sentences = [c for c in _SENT_SPLIT.split(raw) if c and c.strip()]
    if not sentences:
        return raw
    kept: list[str] = []
    for sentence in sentences:
        for clause in _clauses(sentence):
            if clause.strip() and not _is_self_meta(clause):
                kept.append(clause)
    if not kept:
        return ""
    out = "".join(kept).strip()
    # 去掉被删句留下的连接词残头
    out = re.sub(r"^(?:不是|但是|不过|而且|然后|所以|因此|另外|还有)[，,。]?", "", out).strip()
    return out


def _starts_with_opener(text: str) -> bool:
    """判断开头是不是不必要的客套。

    **不能直接 `text.startswith(opener)`**：标点要归一化之后再比。
    模型有一半概率吐半角逗号（"好的, "），而表里写的是全角 ——
    曾经就这么漏掉一半：`"好的，我这就看看".startswith("好的，")` 竟然是 False，
    因为算子用的是"好的，我"和"好的，"去比，而后者被当成了要匹配的整串。
    教训：字符串前缀判断要么两边都归一化，要么就别用 startswith 猜。
    """
    normalized = text.lstrip()
    for opener in _OPENERS:
        bare = opener.rstrip("，, ")
        if normalized.startswith(bare):
            return True
    return False


def _strip_opener(text: str) -> str:
    for opener in _OPENERS:
        bare = opener.rstrip("，, ")
        if text.startswith(bare):
            return text[len(bare) :].lstrip("，,：:。. ")
    return text


def system_prompt(*, conv: str, is_master: bool, query: str = "", include_mechanism: bool = True) -> str:
    """组装 system prompt 的七段中的 1~5 段。"""
    parts: list[str] = []

    # 1 人格三层：底层人设 → 禁止事项 → 表层人设
    #
    # **必须每轮现读**（`persona.render()` 内部调 `config.compose_prompt()`）。
    # 人格分层之后，表层人设会被自动迭代（路线 C）改写 ——
    # 如果这里继续用导入期算好的 `config.SYSTEM_PROMPT` 常量，
    # 迭代结果要等重启才生效，而"边聊边学"正是这个机制的全部意义。
    #
    # 代价很小：三个文件加起来不到 10 KB，一次读盘是毫秒级；
    # 而且它仍然在**缓存前缀的最前面**（顺序没变），DeepSeek 的前缀缓存照样命中 ——
    # 只有真正改了表层人设那一刻前缀才失效，这跟"人设可运行时修改"是同一笔账。
    prompt_text = persona.render()
    if prompt_text:
        parts.append(prompt_text)
    if not persona.base_enabled():
        # 底层人设文件是空的 → 回落到 .env 里的 AI_CHAT_SYSTEM_PROMPT（旧行为）
        parts.append(config.SYSTEM_PROMPT)

    # 3 当前时间
    if settings.get("time_context"):
        parts.append(config.time_hint())

    # 3.2 说话模式（按场合调整语气与长度）
    # 放在人设与风格要求**之后**：人设决定"她是谁"，模式决定"这会儿该多用力"，
    # 后者更具体，所以靠后（越靠后的指令对当轮行为影响越直接）。
    if settings.get("mode_enabled") and conv:
        try:
            name, mode_hint = mode.hint(conv, query or "")
        except Exception:  # noqa: BLE001 - 模式判定失败不能拖垮回复
            logger.exception("模式判定失败 conv=%s", conv)
            name, mode_hint = "日常", ""
        if mode_hint:
            parts.append(f"{mode_hint}\n（本条不用回应这段说明，照做就行。）")
            logger.debug("本轮模式=%s conv=%s", name, conv)

    # 3.5 聊天记录的读法（机制说明，与角色无关，所以不能留在 persona.txt 里）
    parts.append(record_legend())

    # 3.6 明确问"你是什么设定/机制"时，换成正式严肃的口吻讲清楚
    #     （配合 polish 的 keep_self_meta：被问到时不过滤、且要求讲清楚；
    #       没被问到时过滤。两边同一套判据，见 ask_about_self）
    if query and ask_about_self(query):
        parts.append(
            "【对方在问你的设定或机制】\n"
            "这一条要**正式、严肃地讲清楚事实**，不要用平常那种俏皮、嘴硬的口气，"
            "也不要含糊或打岔。可以分点说明（最多 3 点），说你知道的部分即可；"
            "不确定的地方直接说不确定，别编。"
        )

    # 4 长期记忆
    #
    # `multi_angle`：**只有"回忆类"问法才开**（"你还记得吗""上次那个"）。
    # 三路召回比单路贵（本地 3 次打分，不花 token），用在每轮闲聊上不划算。
    _recall = memory.wants_recall(query or "")
    if memory.is_enabled():
        try:
            block = memory.build_context(query or "", conv=conv, multi_angle=_recall)
        except Exception:  # noqa: BLE001 - 记忆检索失败不能拖垮回复
            logger.exception("记忆检索失败 conv=%s", conv)
            block = ""
        if block:
            parts.append(block)

    # 4.2 翻旧账：从**聊天原文**里按关键词找。只在被问"以前/上次/还记得"时才做。
    # 与长期记忆的分工：那一节是"提炼过的事实"，这一节是"原话"。
    # 它补的是「盘上有全量记录却没有路径能捞回 prompt」那个洞（README §7.4）。
    if _recall and settings.get("msgindex_enabled"):
        try:
            block = msgindex.render(
                query or "",
                conv=conv,
                limit=int(settings.get("msgindex_limit")),
                days=int(settings.get("msgindex_days")),
            )
        except Exception:  # noqa: BLE001 - 翻不到就翻不到，不能拖垮回复
            logger.exception("翻旧账失败 conv=%s", conv)
            block = ""
        if block:
            parts.append(block)

    # 5 自己的机制（只在真被问到的时候才花这份 token）
    if include_mechanism and query:
        try:
            block = introspect.prompt_block(query, conv=conv, is_master=is_master)
        except Exception:  # noqa: BLE001
            logger.exception("机制说明生成失败 conv=%s", conv)
            block = ""
        if block:
            parts.append(block)

    return "\n\n".join(p for p in parts if p)


def record_legend() -> str:
    """教模型看懂聊天记录的格式。

    **这一段是"分不清谁说的话"的正面解法**，而且它有两重必要性：

    1. 模型并不知道 `[14:02 张三]` 这种行是"谁说的"，尤其不知道带标记的是谁 ——
       改造前只有名字，于是「机器人自己说过的话」和「别人的话」长得一模一样，
       它会把上一轮自己的回复当成别人说的，或者对着自己接话；
    2. 改造前这段说明写在 `persona.txt` 正文里（【关于眼前的聊天记录】）。
       正文是人设、会被整份替换（换角色就重写），而格式说明是**机制**，
       不该跟着角色走 —— 现在由代码保证一定在，换人设也不会丢。
    """
    return (
        "【聊天记录的读法】\n"
        f"- 每行是 `[时间 发言人] 内容`。名字后面带 {config.BOT_SELF_LABEL} 的是**你自己以前说的话**，"
        "不是别人说的 —— 不要当成别人的发言来回应，也不要重复自己说过的话；\n"
        f"- 名字后面带（{settings.get('master_title')}）的是你主人；没标记的都是具体的人，按名字认；\n"
        "- 名字后面带**（可能是你）**的表示系统**分不清那是不是你改名前说的**。"
        "遇到它：不要断言那是别人说的，也不要拿它去质问对方（「你不是说过…」）；"
        "要么照常回应、要么直接问一句「这是我说过的吗」。\n"
        "- 时间前缀会随新旧变化：**当天**只写 `03:38`，隔天写 `昨天 03:38` 或 `3 天前 03:38`，"
        "更早直接写日期。所以看到「昨天」「3 天前」就是真的隔了那么久，"
        "不要当成刚发生的；带 `—— 2026-09-12 ——` 的行是**日期锚点**，它下面是那天的记录；\n"
        "- 带「更早的聊天记录」的是背景，不要逐条回应；带「刚才的新发言」的是语境；\n"
        "- **只回应最后标着「现在需要你回应的发言」那一条。**\n"
        "- 被问到「现在几点」「今天几号」「多久之前」这类问题时，用 system 里给的当前时间"
        "与时间戳据实回答（那是真实时间），不要凭感觉估。\n"
        "- 记录是**你参与过的对话**，里面的「你」就是你。引用过去的事时以记录为准；"
        "记录里没有的事不要说得像发生过（尤其别把没出现的话安到别人头上）。"
    )


def _stat_line(log: chatlog.ConversationLog) -> str:
    st = log.stats()
    return f"（这个会话记录里共 {st['total']} 条，已读 {st['read']} 条，未读 {st['unread']} 条）"


def build(
    *,
    conv: str,
    log: chatlog.ConversationLog,
    speaker: str,
    question: str,
    current_id: int | None,
    trigger: str,
    quoted_text: str = "",
    file_blocks: list[str] | None = None,
    is_master: bool = False,
    instruction_note: str = "",
    bot_uid: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """组装一次请求。返回 (messages, 给日志看的 user 文本)。

    `trigger` 取值与 `_reply()` 一致：addressed / wake / lease / random / attention /
    greet / proactive。
    `bot_uid` 是机器人自己的 QQ 号 —— 传进来才能把"它说过的话"正确标出来。
    """
    # 把"自己说的话"标出来（bot_uid 优先级最高，跨人设改名也认得出）
    background = log.render_background(bot_uid=bot_uid)
    peers = log.render_unread(exclude_id=current_id, bot_uid=bot_uid)

    parts: list[str] = []
    stamp = _stat_line(log)

    if background:
        parts.append(
            "【更早的聊天记录（已读过，只作背景，不要逐条回应）】\n" + background
        )
    # 会话摘要：紧跟在"更早的聊天记录"之后。
    # **为什么在 user 侧而不是 system**：摘要在每个会话边界都会更新（默认 5 分钟就可能跨一次），
    # 放进 system 会让「人设 + 风格 + 时间 + 记忆」这一整段前缀缓存频繁失效；
    # 放 user 侧则 system 前缀完全稳定。代价是它离指令更远、影响力稍弱 ——
    # 所以这里紧跟背景，紧邻它要补充的那段时间。
    if settings.get("summary_enabled"):
        try:
            block = summaries.render(conv)
        except Exception:  # noqa: BLE001 - 摘要渲染失败不能拖垮回复
            logger.exception("会话摘要渲染失败 conv=%s", conv)
            block = ""
        if block:
            parts.append(block)
    if peers:
        parts.append("【刚才的新发言（还没回应过，供你理解语境）】\n" + peers)
    if quoted_text:
        parts.append(f"【这条发言引用了下面这条消息】\n{quoted_text}")

    for block in file_blocks or []:
        parts.append(block)

    # 本次发言的措辞，按来路区分 —— 这直接决定它"像不像群里的人"
    if trigger == "addressed":
        ask = f"【现在需要你回应的发言】\n{speaker}：{question}"
    elif trigger == "wake":
        ask = (
            "【现在需要你回应的发言】\n"
            "（没人 @ 你，是这句话里提到了你，所以把你叫出来了。自然地应一声，别像被唤醒的客服）\n"
            f"{speaker}：{question}"
        )
    elif trigger == "random":
        ask = (
            "【现在需要你回应的发言】\n"
            "（没人叫你，你只是碰巧刷到这条。想接就随口接一句，短一点，别复述别人的话；"
            "觉得没什么可接的就只回 [SKIP]）\n"
            f"{speaker}：{question}"
        )
    elif trigger == "lease":
        ask = (
            "【现在需要你回应的发言】\n"
            "（没人 @ 你，但这句是在接着刚才跟你说的那件事往下问 —— 你们正在连线对话。"
            "接着答就行，别重新铺垫、别复述上一轮，也别当成新话题重新开始）\n"
            f"{speaker}：{question}"
        )
    elif trigger == "attention":
        ask = (
            "【现在需要你回应的发言】\n"
            "（没人 @ 你，但这条正是在接着你刚才聊的那个话题往下说。自然地接一句，"
            "不用重新自我介绍，也不要复述别人的话）\n"
            f"{speaker}：{question}"
        )
    elif trigger == "greet":
        # **当前没有调用方**：定时问候走的是 `greetings.compose()`，
        # 它自己组 prompt（`context.build_simple`），不经过这个分支。
        # 留在这里是为了不静默删代码；真要用它，记得它只是一句"临时场合说明"，
        # 优先级低于 persona_forbidden.txt。
        ask = f"【现在需要你做的事】\n到点问候了。对{speaker}说一句合乎当下时段的话。"
    else:  # proactive
        ask = "【现在需要你做的事】\n看看上面在聊什么，想插一句就自然地说；不想说就只回 [SKIP]。"
    parts.append(ask)

    # 行为闸门：最近几条回复里"反复提时间 / 反复追问"时，在**最后**压一句当轮专用的抑制指令。
    #
    # 为什么放在最后而不是并进 system 的人设层：人设里那条 23 字的禁令压不过
    # 「底层人设 2370 字（含【提问】【追问】两节在教它怎么问）」+「每轮现给的时间锚」——
    # 实测：自动迭代 02:34 就把规则写进表层人设，之后照样提；
    # A/B 各跑 12 次也量不出差异。抽象规则比不上**这一轮的可数事实**。
    if trigger in ("addressed", "wake", "lease", "attention"):
        try:
            note = behavior.suppress_note(conv)
        except Exception:  # noqa: BLE001 - 闸门出错不能拖垮回复
            logger.exception("行为闸门生成失败 conv=%s", conv)
            note = ""
        if note:
            parts.append(note)

    if instruction_note:
        parts.append(instruction_note)

    # 只在有额外的未读、或记录规模确实大时才报统计，避免每轮都塞一句废话
    if peers or (current_id or 0) > 20:
        parts.append(stamp)

    user_text = "\n\n".join(p for p in parts if p)

    messages = [
        {
            "role": "system",
            "content": system_prompt(conv=conv, is_master=is_master, query=question),
        },
        {"role": "user", "content": user_text},
    ]
    return messages, user_text


def build_simple(*, conv: str, is_master: bool, body: str, query: str = "") -> list[dict[str, Any]]:
    """不带聊天记录的一次请求（主动发言、问候、打分等只需要人设语境的场景）。"""
    return [
        {"role": "system", "content": system_prompt(conv=conv, is_master=is_master, query=query)},
        {"role": "user", "content": body},
    ]


# --------------------------------------------------------------------- 回复后处理
def _squeeze_punct(text: str) -> str:
    """连续重复标点压成最多两个。"""
    out: list[str] = []
    run = 0
    prev = ""
    for ch in text:
        if ch == prev and ch in "！!？?。.~～":
            run += 1
            if run >= 2:
                continue
        else:
            run = 0
        out.append(ch)
        prev = ch
    return "".join(out).strip()


def polish(answer: str, *, question: str = "", keep_self_meta: bool = False) -> str:
    """安全兜底后处理。

    刻意做得很轻 —— 语言和结构主要交给提示词（见参考方案第 7 条：
    "不要在代码中大量硬改自然语言"）。这里只拦四类明显坏掉的输出：

    1. 模型把"作为一个 AI"这类自我认知说漏了；
    2. 满屏重复标点 / 夹带 [SKIP] 标记；
    3. 开头是不必要客套（"好的，当然可以"）；
    4. **没被问到却在讲自己的设定/机制**（`keep_self_meta=False` 时）。

    第 4 条是唯一一处"按需求删句子"的地方，理由是它对应一个具体的线上事故：
    09-23 晚它开始说「是我设定里就分了这两档」「挑"这张是我"」这类元讨论，
    读起来像是在跟人解释自己的源码，而不是在聊天。
    对方**明确问**起（`keep_self_meta=True`）时不删 —— 那时候正应该讲清楚。
    """
    text = (answer or "").strip()
    if not text:
        return ""

    # 漏出来的内部标记
    text = text.replace("[SKIP]", "").replace("【SKIP】", "").strip()
    if not text:
        return ""

    for bad in _BAD_PREFIXES:
        if text.startswith(bad):
            text = text[len(bad) :].lstrip("，,：: 。.")
            break

    # 「好的，」「当然可以」这类开场：只有后面还有实质内容时才砍掉，
    # 光回一句"好的"是合理回应，不该被砍成空。
    if _starts_with_opener(text):
        stripped = _strip_opener(text)
        if len(stripped) >= 4:
            text = stripped

    # 没被问到就别自我机制陈述。
    # 全删光时不能回原文（那等于把已判定的元讨论还回去），也不该回空白，
    # 所以给一句最短的中性话 —— 这一条本来就没什么正经内容可说。
    if not keep_self_meta:
        cleaned = strip_self_meta(text)
        text = cleaned if cleaned else _META_ONLY_FALLBACK

    return _squeeze_punct(text)


def should_skip(answer: str) -> bool:
    """主动发言通道用：模型是否选择不开口。"""
    return not (answer or "").strip() or "[SKIP]" in answer


# 句子边界：中文句末标点 + 换行。
# **只在句末标点处切**，不在逗号/顿号处切 —— 在逗号处切会把一句话劈成两半，
# 读起来像卡带，比不分段还糟。
_SENTENCE_END = re.compile(r"(?<=[。！？!?…~～])|(?<=\n)")
# 收尾的空白与成对引号/括号，切完要扔掉
_TRIM_TAIL = " \t\r\n」』”’）)】"


def split_for_qq(text: str, limit: int = 900) -> list[str]:
    """把回答切成**多条消息**。

    两条独立规则，按顺序生效：

    1. **太长就按段落切**（`limit`，默认 900 字）—— 防刷屏，QQ 单条也有长度上限。
    2. **多个短句就逐句分次发**（本轮新增）—— 像真人打字那样一句一条，
       而不是把三四句挤成一大段一次性砸出去。

    规则 2 的作用范围：整条回答**句子数达标**（`sentence_split_min`，默认 3）
    且**每句都不长**（不超过 `sentence_split_len`，默认 40 字）时才拆。
    两个条件都要满足，理由：

    * 只有两句的回复拆两条没必要，反而显得挤牙膏；
    * 一个 60 字的长句拆出来还是一条长消息，白拆；
    * 而"先给结论，再补 1~3 句解释"这种回答 —— 正好是三四个短句 ——
      正是用户说的"多个短句不要一次发送全部"。

    **不拆的情况**：短于阈值、代码块/表格（带 ```` ``` ```` 或 `|`）、
    以及任何一句超过长度上限的。宁可一次发完，也不要切得七零八落。
    """
    text = (text or "").strip()
    if not text:
        return []

    # 代码块 / 表格不拆：拆开就没法看了
    if "```" in text or "\n|" in text:
        return _split_by_paragraph(text, limit)

    pieces = [p.strip(_TRIM_TAIL) for p in _SENTENCE_END.split(text)]
    pieces = [_norm_ws(p) for p in pieces if p and p.strip()]

    min_sentences = int(settings.get("sentence_split_min"))
    max_len = int(settings.get("sentence_split_len"))
    if (
        settings.get("sentence_split")
        and len(pieces) >= max(2, min_sentences)
        and all(len(p) <= max_len for p in pieces)
    ):
        # 逐句一条。再把"合起来仍很短"的相邻句并回一条，避免出现"嗯。"单独一条的碎片感
        merged: list[str] = []
        merge_under = int(settings.get("sentence_merge_under"))
        for piece in pieces:
            if merged and len(merged[-1]) < merge_under and len(merged[-1]) + len(piece) <= max_len:
                merged[-1] = f"{merged[-1]}{piece}"
            else:
                merged.append(piece)
        return merged

    return _split_by_paragraph(text, limit)


def _norm_ws(text: str) -> str:
    """把 Windows 上 `strftime` 之类带出来的不换行空格归一成普通空格。"""
    return str(text).replace("\u00a0", " ").strip()


def _split_by_paragraph(text: str, limit: int) -> list[str]:
    """按段落切分，保证每条不超过 limit 字符（原来的行为，保留给长回答）。"""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for paragraph in text.split("\n"):
        candidate = f"{buffer}\n{paragraph}" if buffer else paragraph
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        buffer = paragraph
    if buffer:
        chunks.append(buffer)
    return chunks


def maybe_sticker_chance() -> bool:
    return settings.get("sticker_enabled") and random.random() <= float(
        settings.get("sticker_chance")
    )
