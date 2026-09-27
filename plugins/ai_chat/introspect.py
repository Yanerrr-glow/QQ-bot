"""自我说明：把自己的机制讲清楚，且**说的是真的**。

## 为什么需要它

「详细告诉我你的图片使用机制」这类问题，改造前必然是**凭空编**：模型的 system prompt 里
只有人设和聊天记录，没有任何关于"这个机器人怎么处理图片"的事实。
所以它只能顺着人设答一句「我会把好看的图收起来呀」——
听起来像回答了，实际没有一句是真的。这是**幻觉**，不是记性差。

本模块的做法是：把当前真实配置读出来，渲染成中文事实，塞进 prompt。
模型于是有据可依，答出来的每一句都能对上代码。

## 主人全量、其他人简版

机制里含有**越权信息**：主人的 QQ 号、阈值、存储路径、限流参数、唤醒词。
群友问同样的问题，只给"我会怎么看图、什么图会留"这种自然语言版本。
判断依据是 `is_master`（按 QQ 号认，跟 reply 主流程一致）。

## 敏感项遮蔽是**代码**保证的，不是提示词保证的

API Key 只渲染成「已配置 / 未配置」两态，从不出现内容或前缀；
网桥地址、控制台端口一律不进这段文本。所以即便模型想泄露也没有素材 ——
把「不要让模型说密钥」寄托在提示词上是不负责任的。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import clock, config, llm, memory, persona, settings, state

logger = logging.getLogger("ai_chat.introspect")

# 每个主题对应的说明生成器
_TOPICS = ("图", "记忆", "风格", "触发", "安全", "全部")


def _onoff(value: Any) -> str:
    return "开着" if value else "关着"


# --------------------------------------------------------------------- 图片
def _image_facts(conv: str, is_master: bool) -> str:
    from . import stickers

    lines: list[str] = [
        "【图片是怎么处理的】",
        f"· 这个会话当前的图片策略：{state.MODE_LABEL.get(state.image_mode(conv), '正常')}",
    ]

    if is_master:
        lines += [
            f"· 每张图进来要过四道关：会话策略 → 每群每分钟限 {settings.get('sticker_rate_limit')} 张 → "
            f"文件不超过 {settings.get('sticker_max_kb')} KB → 我觉得值得才留下",
            f"· 读图是{'真的看画面' if settings.get('sticker_vision') else '只看图片类型和上下文，不看画面'}；"
            f"交给我的图不超过 {settings.get('sticker_vision_max_kb')} KB",
            f"· 存下来的图放在 {config.STICKER_DIR}，按内容哈希命名，同一张图转发多少次都只留一份",
            f"· 群里发图时我会顺手记住最近一张（180 秒内），所以「先发图、隔一句再问这是什么」我也接得上",
            f"· 对话时读图的开关：{_onoff(settings.get('chat_vision'))}；"
            f"只发图不配字也回应：{_onoff(settings.get('reply_to_images'))}",
            f"· 我发图给你的概率是 {float(settings.get('sticker_chance')):.0%}，"
            f"用得多的时候会优先挑没用过的",
        ]
    else:
        lines += [
            "· 我会看一眼图，觉得像表情包、斗图素材，或者跟群里正在聊的梗有关，就收进自己的图库；"
            "普通照片、聊天截图、广告图不要",
            "· 太小的图不看，太大的图不要",
        ]

    return "\n".join(lines)


# --------------------------------------------------------------------- 记忆
def _memory_facts(conv: str, is_master: bool) -> str:
    from . import msgindex  # 局部导入：introspect 被很多模块引用，不必顶层拉起 sqlite

    st = memory.stats()
    idx = msgindex.stats()
    lines = ["【我记得什么】"]
    if is_master:
        lines += [
            f"· 记忆库现在有 {st['facts']} 条事实、{st['events']} 条群事件、{st['people']} 个人物画像"
            f"（上限 {st['limit']} 条，超了会淘汰最不重要、最久远的；被 /记忆 保护 的除外）",
            f"· 长期记忆是「{_onoff(settings.get('memory_enabled'))}」、自动抽取是"
            f"「{_onoff(settings.get('memory_extract'))}」",
            f"· 每次回话我会挑最多 {settings.get('memory_recall_count')} 条最相关的带进上下文，"
            "挑选过程不花 token；重要的、近期的、被反复用到的更容易被想起"
            f"（现在库里 {st.get('used_once', 0)} 条被想起过、"
            f"{st.get('confirmed', 0)} 条被反复提到过）",
            f"· 自动抽取每天最多处理 {settings.get('memory_extract_per_day')} 条消息，每次最多记 "
            f"{settings.get('memory_extract_max')} 条 —— 你说「记住这个」是手动存，不受这个限制",
            f"· 记忆存在 {_memory_location()}，重启不丢"
            f"（后端：{settings.get('memory_store')}）",
            f"· 聊天记录我另外建了索引：{idx['messages']} 条消息可翻"
            f"（{_onoff(settings.get('msgindex_enabled'))}）—— 被问「上次原话怎么说的」时用得上",
            f"· 记忆的可见范围是隔离的（{_onoff(settings.get('memory_scope_isolation'))}）："
            "私聊里说的事不会在群里被提起来，这个群的事也不会串到别的群",
        ]
    else:
        lines += ["· 群里说过的重要事情我会记下来，下次聊到能想起来；不用你教我"]

    lines.append(
        "· 跟「聊天记录」的区别：聊天记录是原话、按预算只放最近一段；"
        "记忆是提炼出来的几条事实，能跨很多天。被问「原话怎么说的」我去翻记录，"
        "被问「你还记得吗」我看记忆"
    )
    return "\n".join(lines)


def _memory_location() -> str:
    """记忆落在哪个文件 —— 取决于后端（排查时这是第一个要知道的事）。"""
    backend = str(settings.get("memory_store") or "sqlite")
    name = "memory.db" if backend == "sqlite" else "memories.json"
    return str(config.LOG_DIR / name)


# --------------------------------------------------------------------- 风格
def _style_facts(conv: str, is_master: bool) -> str:
    st = persona.stats()
    lines = ["【我说话的风格是怎么定的】"]
    # 三层结构：底层与禁止事项只能改文件，表层由自动迭代学。
    lines.append(
        f"· 我的人格分三层：底色（{st['base_chars']} 字）、禁止事项（{st['forbidden_chars']} 字）、"
        f"表层说话方式（{st['surface_chars']} 字）"
    )
    if is_master:
        lines += [
            "· **底色和禁止事项只能由你直接编辑文件改**（系统和我都不能动它们）：",
            f"    {st['base_file']}",
            f"    {st['forbidden_file']}",
            "· **表层是唯一会自己慢慢学的部分**，但每次改动都要过一道闸门："
            "碰到上面两条里任何一条就**直接丢弃、不写入**：",
            f"    {st['surface_file']}",
            f"· 到现在为止自动改过 {st['changes']} 次（写入 + 被丢弃都记）——"
            "/人设 日志 看明细，/人设 撤回 撤销最近一次",
            "· 改人格**不用重启**：每轮回复前都重新读这三个文件",
        ]
    else:
        lines.append("· 这些只有主人能看，我就按现在的样子说话")
    return "\n".join(lines)


# --------------------------------------------------------------------- 触发
def _trigger_facts(conv: str, is_master: bool) -> str:
    rules: list[str] = [
        "【我什么时候会说话】",
        "· 被 @、或者消息里提到唤醒词 —— 这是最主要的一条",
    ]
    if is_master:
        wake = str(settings.get("wake_words") or "")
        if wake:
            rules.append(f"  唤醒词是：{wake}")
        rules += [
            "· 引用我自己发过的消息 → 一定回，不看概率",
            f"· **叫过我一次之后，接着聊不必每句都 @**（对话租约："
            f"{_onoff(settings.get('attention_lease_enabled'))}，"
            f"{settings.get('attention_lease_seconds')} 秒内、最多 "
            f"{settings.get('attention_lease_max_turns')} 轮；每回一轮重新计时。"
            f"默认只认刚跟我说话的那个人"
            f"{'，但谁都能接着续' if settings.get('attention_lease_anyone') else ''}）",
            f"· 别人聊到跟我刚说过的话题相关时，我可能自己接话"
            f"（注意力机制：{_onoff(settings.get('attention_enabled'))}）",
            f"· 随机插话：{_onoff(settings.get('random_reply_enabled'))}"
            f"，每条消息 {float(settings.get('random_reply_chance')):.0%} 的概率",
            f"· 主动发言：{_onoff(settings.get('proactive_enabled'))}；"
            f"定时问候：{_onoff(settings.get('greet_enabled'))}",
            f"· 被 @ 时我能看到：更早的聊天记录（压缩过，预算 {settings.get('read_budget')} 字）"
            "+ 刚才的新发言 + 引用的消息 + 图片 + 消息里的文件 + 相关的长期记忆",
            f"· 记录里我自己的发言会标成「{config.BOT_SELF_LABEL}」，我不会把自己的话当成别人说的、"
            "也不会对着自己接话",
        ]
    else:
        rules.append("· 平时不 @ 我，我一般不会插话")
    return "\n".join(rules)


# --------------------------------------------------------------------- 时间
def _time_facts(conv: str, is_master: bool) -> str:
    now = clock.now()
    stamp, weekday, period = config.time_parts(now)
    st = clock.status()
    lines = [
        "【我怎么知道现在几点】",
        f"· 每次回话前，系统会把当前时间塞给我：现在是 {stamp} {weekday}（{period}），"
        f"时区 {time.strftime('%Z%z', time.localtime(now))} —— 这是真实时间，不是我在猜",
        "· 所以「现在几点」「今天几号」我能直接答；算「多久之前/之后」也能算",
    ]
    if is_master:
        source = "NTP 校准过" if st["calibrated"] else "系统时钟"
        detail = (
            f"（跟系统时钟差 {st['offset_seconds']:+.3f} 秒，来源 {st['server']}）"
            if st["offset_seconds"]
            else ""
        )
        lines += [
            f"· 时间来源：{source}{detail}",
            f"· NTP 校准是{'开' if st['enabled'] else '关'}的，"
            f"每 {settings.get('ntp_sync_interval')} 秒对一次；服务器："
            f"{'、'.join(st['servers']) or '（没配）'}",
            "· 聊天记录里每行的时间会随新旧变化：当天只写 03:38，隔天写「昨天 03:38」"
            "或「3 天前 03:38」，更早直接写日期；每天第一行前还有一条日期锚点",
            f"· 时间注入的开关：{_onoff(settings.get('time_context'))}"
            "（关掉我就完全不知道现在几点了）",
            "· 想要精确到秒的答案用 /时间、算间隔用 /时间 差、临时对时用 /时间 校准 —— "
            "那几条我不猜，直接算",
        ]
    else:
        lines.append("· 记录里也会标出「昨天」「3 天前」，所以别提错时间")
    return "\n".join(lines)


# --------------------------------------------------------------------- 模式
def _mode_facts(conv: str, is_master: bool) -> str:
    from . import mode

    cur = mode.current(conv)
    manual = mode.manual_of(conv)
    lines = [
        "【我怎么调整说话的场合】",
        f"· 现在对着这个会话是「{cur}」"
        + ("（有人明确指定的）" if manual else "（我按场合自己判的）"),
        "· 三种：日常（默认，闲聊打岔）、专注（在问正事，先给结论不闲聊）、"
        "安慰（对方情绪不好，先接情绪不给方案）",
        "· 切换只改语气、长度、发图与主动开口的多少，**不换人设** —— 还是同一个人，只是场合不同",
    ]
    if is_master:
        lines.append(
            f"· 自动判定只认「几乎不会出现在闲聊里」的词；拿不准就**不判定**，"
            "直接按日常来（改错语气比不改更糟）"
        )
        lines.append(
            f"· 判出来的场合会沿用 {settings.get('mode_sticky_seconds')} 秒，"
            "免得上一句在问报错、下一句说谢谢就突然换个人"
        )
        lines.append("· 想固定就用 /模式 专注（或 日常 / 安慰），/模式 自动 交回给我判断")
    else:
        lines.append("· 想固定也可以说 /模式 专注 这样指定")
    return "\n".join(lines)


# --------------------------------------------------------------------- 搜索
def _search_facts(conv: str, is_master: bool) -> str:
    from . import search

    st = search.stats()
    lines = ["【我会不会上网查】"]
    if not st["available"]:
        lines.append(f"· 现在**查不了**：{st['reason']}")
        lines.append("· 所以遇到不认识的词或最新的事，我只能说不确定 —— 不会假装知道")
        return "\n".join(lines)

    lines.append("· 会。遇到**我不认识的词、梗、型号、缩写**，或者**我知识截止之后的事**，"
                 "我会先去查再回答")
    lines.append("· 查到的网页内容只当资料看；搜不到我就直说没查到，不编")
    if is_master:
        from . import search_memory

        mem = search_memory.stats()
        lines.append(f"· 后端：{st['backend']}；最多带回 {st['max_results']} 条；"
                     f"每会话每分钟 {st['rate_limit']} 次，单条消息最多 {st['max_per_message']} 次")
        lines.append(f"· 本地兜底预取：{'开' if settings.get('search_prefetch') else '关'}"
                     "（模型没主动查、但看着确实需要时先查一次）")
        lines.append(
            f"· **查到的名词释义会存下来**（单独一份，不混进长期记忆）：现在 {mem['count']} 条，"
            f"其中低置信 {mem['low_confidence']} 条、过期 {mem['stale']} 条；"
            f"有效期 {mem['ttl_days']:.0f} 天"
        )
        lines.append(
            "· 每条都带**查询时间**；置信度是按「几个独立来源 / 有无含糊措辞 / 来源像不像百科」"
            "算出来的，低置信的我会明说是低置信、不装准"
        )
        lines.append("· 想让我现在查就发 /搜索 <关键词>；/搜索 清 清空释义库")
    return "\n".join(lines)


# --------------------------------------------------------------------- 安全
def _safety_facts(conv: str, is_master: bool) -> str:
    lines = ["【关于我自己的信息】"]
    if not is_master:
        # 群友问"你是什么模型/怎么实现的"，给一段像人话的自我说明 ——
        # 既不是把问题怼回去，也不泄露密钥、路径、阈值这些越权信息。
        return "\n".join(
            [
                "【关于我自己的信息】",
                "· 我在群里就是个普通成员，来聊天的，不是客服；",
                "· 我会记住聊过的重要事情，也会看图片 —— 不喜欢存的图可以让我别存；",
                "· 具体的参数、密钥、跑在哪儿这些只跟主人讲。",
            ]
        )

    lines += [
        # **必须读 llm 而不是 config.MODEL**：对话实际调用走的是 `llm.chat`，
        # 用哪个模型由「当前档案 + `settings.model` 覆盖」一起决定，而 `config.MODEL`
        # 只是 .env 的初始值。控制台换过档案/覆盖过模型名之后，读 config 自述就会出现
        # 「它说的模型和实际用的不是一个」—— 这正是 5.6.8 想根治的那类"问到机制只能编"。
        f"· 对话模型：{llm.model_name()}（走「{llm.active_id()}」这个接口档案）",
        f"· 接口密钥：{'已配置' if llm.api_key() else '**没配置**'}（内容不会出现在任何回复里）",
        f"· 聊天记录落在 {config.LOG_DIR} 下的 chatlog_*.json，按会话分文件",
        f"· 记忆落在 {_memory_location()}，会话图片策略落在 "
        f"{config.LOG_DIR / 'conv_state.json'}"
        "（人设已改为三层文件，不再有运行时槽位文件）",
        f"· 控制台挂在 {config.WEBUI_PREFIX}/（只在服务器本机访问）",
        f"· 会话切分间隔 {settings.get('session_gap')} 秒；单条消息截断 {settings.get('msg_clip')} 字",
    ]
    return "\n".join(lines)


_BUILDERS = {
    "图": _image_facts,
    "图片": _image_facts,
    "记忆": _memory_facts,
    "风格": _style_facts,
    "人设": _style_facts,
    "触发": _trigger_facts,
    "唤醒": _trigger_facts,
    "时间": _time_facts,
    "几号": _time_facts,
    "模式": _mode_facts,
    "场合": _mode_facts,
    "搜索": _search_facts,
    "联网": _search_facts,
    "安全": _safety_facts,
    "信息": _safety_facts,
}


def explain(topic: str, *, conv: str, is_master: bool) -> str:
    """生成一段"机制事实"，可直接回给人，也可塞进 prompt 让模型转述。

    返回的是**事实文本**而不是"给模型看的小抄" —— 所以 /机制 可以不过模型直接发，
    这样保证关键数字不会被文风改写。
    """
    topic = (topic or "全部").strip()
    builders: list[Any] = []

    if topic in ("全部", "所有", "全"):
        builders = [_image_facts, _memory_facts, _style_facts, _trigger_facts, _time_facts,
                    _mode_facts, _search_facts]
        if is_master:
            builders.append(_safety_facts)
    else:
        for key, builder in _BUILDERS.items():
            if key in topic:
                if builder not in builders:
                    builders.append(builder)
    if not builders:
        builders = [_image_facts]

    blocks: list[str] = []
    for builder in builders:
        try:
            blocks.append(builder(conv, is_master))
        except Exception:  # noqa: BLE001 - 说明生成失败不能影响对话
            logger.exception("机制说明生成失败：%s", getattr(builder, "__name__", builder))
    if not blocks:
        return "这块我一时说不清楚，等下再说。"

    head = "" if is_master else "（我只说个大概，细节只跟主人讲）\n"
    return head + "\n\n".join(blocks)


def topics_text() -> str:
    return " / ".join(_TOPICS)


# --------------------------------------------------------------------- prompt 注入
def prompt_block(query: str, *, conv: str, is_master: bool) -> str:
    """当提问疑似在问「你自己怎么工作的」时，把真实机制喂给模型。

    这是幻觉的正面解法：模型之所以编，是因为它无据可依；
    给了它据，它就能答得既准确又保持人设语气。
    """
    from . import instructions

    topic = query
    # 用指令层同一套主题词推断，保证"自然语言问"和"/机制"给的是同一份事实
    if not any(key in query for key in _BUILDERS):
        return ""
    text = explain(topic, conv=conv, is_master=is_master)
    if not text:
        return ""
    audience = "主人" if is_master else "群里的朋友"
    return (
        "【关于你自己的真实机制（下面这些是系统提供的准确信息，"
        f"提问的是{audience}）】\n{text}\n"
        "（回答时用你自己的语气说，保留关键数字和事实，**不要编造上面没有的内容**；"
        "对方只是想了解，不需要逐条念出来。）"
    )
