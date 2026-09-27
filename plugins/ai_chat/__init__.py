"""AI 群聊插件：群聊记录落盘，被 @ 时基于「长期记忆 + 已读背景 + 未读新发言」回复。

与"靠对话上下文记忆"的做法有本质区别：

* 不再在内存里维护多轮 user/assistant 历史；
* 群聊逐条落盘到 `data/chatlog_<会话>.json`，每条带**已读 / 未读**状态；
* 被 @ 时临时组装 prompt —— 已读记录压缩成背景，未读记录原样呈现，
  回复后把这些未读标记为已读。模型每次只看到"该看的东西"，不背历史包袱。

在此之上又加了三层（改造前没有，是"没有记忆 / 人设锁死 / 听不懂当场要求"的解法）：

* `memory`  —— 长期记忆：跨会话、跨重启的事实条目，检索在本地做，不花 token；
* `persona` —— 运行时人设：`/风格`、`/人设` 改的东西立即生效，不用重启；
* `instructions` —— 实时指令：把「不要保存这张图片」这类话翻译成真的状态变更。

四个 handler / 任务分工：
1. `recorder`（priority=1, block=False）：记录所有消息；图片顺手丢给表情包库；
2. `ai_chat`（priority=50, block=True）：只在被 @ / 命中前缀时回复；
3. `proactive.loop()`：后台常驻，按概率主动冒泡（可在 Web 控制台里调）。
4. `greetings.loop()`：到点问候。

一次 `_reply()` 的完整路径（改造后）：

```
指令解析 → 命中就直接发回执（不过模型），同时状态已经改好
        ↓ 没命中
组装 prompt（人设 / 风格 / 时间 / 长期记忆 / 机制事实 / 记录）
        ↓
调模型 → polish 兜底 → 发送
        ↓
落盘 + 标已读 + 异步抽取记忆（不占回复时间）
```
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.rule import Rule

from . import (
    attention,
    behavior,
    chatlog,
    clock,
    config,
    context,
    dsh_bridge,
    fetch,
    files,
    greetings,
    instructions,
    introspect,  # noqa: F401 —— 供插件内其它模块按需引用，同时保证注册顺序
    llm,
    memory,
    mode,
    msgindex,
    persona,
    persona_iter,
    proactive,
    render,
    search,
    settings,
    signals,
    state,
    stickers,
    summaries,
)
from . import webui  # noqa: F401 —— 导入即注册控制台路由（副作用）

logger = logging.getLogger("ai_chat")

# 未配置 key 时也要能 import 成功，否则插件加载阶段就崩，报错不直观。
_semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY)

RESET_WORDS = {"清空对话", "重置对话", "清空记录", "reset", "/reset", "新对话"}

# 持有后台任务的强引用，否则可能被 GC 提前回收
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:  # noqa: ANN001
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# --------------------------------------------------------------------- 工具
def _display_name(event: MessageEvent) -> str:
    sender = getattr(event, "sender", None)
    if sender is None:
        return str(event.user_id)
    return getattr(sender, "card", "") or getattr(sender, "nickname", "") or str(event.user_id)


def _is_master(event: MessageEvent) -> bool:
    """主人按 QQ 号认，不按昵称 —— 昵称随时会改。"""
    try:
        return int(event.user_id) == int(settings.get("master_qq"))
    except (TypeError, ValueError):
        return False


def _speaker_label(event: MessageEvent) -> str:
    name = _display_name(event)
    return f"{name}（{settings.get('master_title')}）" if _is_master(event) else name


def _is_addressed(event: MessageEvent) -> bool:
    """是否在跟机器人说话：被 @，或命中配置的前缀。"""
    try:
        if event.is_tome():
            return True
    except Exception:  # 私聊等场景下 is_tome 可能不可用
        pass
    return bool(config.PREFIX) and event.get_plaintext().strip().startswith(config.PREFIX)


def _wake_words() -> list[str]:
    """唤醒词列表。逗号（中英文都认）、顿号或空白都能分隔。"""
    raw = str(settings.get("wake_words") or "")
    return [w for w in re.split(r"[,，、\s]+", raw) if w]


def _mentions_wake_word(event: MessageEvent) -> bool:
    """消息里有没有提到唤醒词。

    包含式匹配，所以配一个「肥鱼」就能同时命中「大肥鱼」「肥鱼!」「死肥鱼」。
    """
    if not settings.get("wake_enabled"):
        return False
    text = event.get_plaintext()
    if not text:
        return False
    return any(word in text for word in _wake_words())


# 会话键 -> 上次被唤醒的时间戳。只放内存：重启后重新计时无所谓。
_last_wake: dict[str, float] = {}


def _wake_cooldown_ok(conv: str) -> bool:
    cooldown = int(settings.get("wake_cooldown"))
    now = time.time()
    if now - _last_wake.get(conv, 0.0) < cooldown:
        return False
    _last_wake[conv] = now
    return True


# 会话键 -> 上次随机插话的时间戳
_last_random: dict[str, float] = {}


def _random_cooldown_ok(conv: str) -> bool:
    cooldown = int(settings.get("random_reply_cooldown"))
    now = time.time()
    if now - _last_random.get(conv, 0.0) < cooldown:
        return False
    _last_random[conv] = now
    return True


# 会话键 -> (时间戳, 图片字节)。
# 群里最常见的问法不是「把图附在问题里」，而是「先发一张图，隔一条再问这是什么」——
# 图片压根不在当前消息里。有了这份缓存，被 @ 时就能把刚发过的那张一并交给模型。
_recent_images: dict[str, tuple[float, bytes]] = {}
_RECENT_IMAGE_TTL = 180.0  # 秒


def _remember_image(conv: str, data: bytes) -> None:
    _recent_images[conv] = (time.time(), data)


def _recent_image(conv: str) -> bytes | None:
    item = _recent_images.get(conv)
    if not item:
        return None
    ts, data = item
    if time.time() - ts > _RECENT_IMAGE_TTL:
        _recent_images.pop(conv, None)
        return None
    return data


def _allowed(event: MessageEvent) -> bool:
    if not config.GROUP_WHITELIST:
        return True
    group_id = getattr(event, "group_id", None)
    return group_id is None or group_id in config.GROUP_WHITELIST


def _question_text(event: MessageEvent) -> str:
    """取出提问正文：去掉 @ 机器人留下的痕迹与前缀。"""
    text = event.get_plaintext()
    for token in (f"[CQ:at,qq={event.self_id}]", f"@{event.self_id}"):
        text = text.replace(token, " ")
    stripped = text.strip()
    if config.PREFIX and stripped.startswith(config.PREFIX):
        text = stripped[len(config.PREFIX) :]
    return text.strip()


async def _ask_deepseek(messages: list[dict]) -> str:
    response = await asyncio.wait_for(
        llm.chat(messages=messages, stream=False),
        timeout=config.TIMEOUT,
    )
    if not response.choices:
        return ""
    return (response.choices[0].message.content or "").strip()


# --------------------------------------------------------------------- 联网搜索
# 工具循环：让模型自己决定要不要搜。**这是"某个词意义不明时主动搜索"的主通道** ——
# 最清楚"我这儿不确定"的其实是模型自己，本地关键词永远只能猜。
#
# 三个刻意的约束：
#  1. **不传 `tool_choice`**（用默认的 auto）。DeepSeek V4-Pro 明确拒绝
#     `tool_choice="required"` 与指定函数的写法（见 deepseek-ai/DeepSeek-V3#1376），
#     传了就直接报错。auto 也正好是我们想要的：它自己判断要不要搜。
#  2. **有轮数上限**（`search_max_per_message`）。模型偶尔会连着搜好几次，
#     既慢又贵，还会把 prompt 撑满。
#  3. **额度用尽如实告知**，不是悄悄摘掉工具。摘掉的话它会退回"凭记忆硬答"。
_MAX_TOOL_ROUNDS = 4


def _search_tool_enabled(conv: str) -> bool:
    """**只查不记** —— 这一句每条消息都会被调用（`_ask_with_tools` 开头）。

    改造前这里用的是 `rate_ok()`，而它会顺手占掉一个配额。群里聊满
    `search_rate_limit` 条消息之后，这个函数就恒为 False，**模型再也拿不到
    `web_search` 工具** —— 于是它凭记忆答，还会编出"我查了下，没搜到"。
    详见 `search.rate_peek` 的 docstring。
    """
    return search.available() and search.rate_peek(conv)


async def _remember_definition(
    query: str,
    results: list[dict[str, Any]],
    *,
    by: str = "模型",
    raw: str = "",
) -> None:
    """把这次搜索沉淀成一句释义（**只对"X 是什么"类查询**）。

    三步：判断是不是释义类 → 让模型压成一句 → 按可观测信号算置信度后入库。

    **`raw` 是用户原话，`query` 是拧出来的搜索词 —— 判定要用 `raw`。**
    这是个踩过的坑：`query_from("鲸落 是什么")` 会拧成「鲸落」，
    而「鲸落」不含任何释义标记，于是 `is_definition_query` 判 False，
    **真搜到了却什么都不沉淀**。拧词是为了搜得准，不是为了判定。

    刻意放在 `_spawn` 里异步跑：它要多一次模型调用，不该让群友等。
    失败一律静默（`summarize` 返回空就不入库），不影响搜索本身。
    """
    from . import search_memory

    if not settings.get("search_memory_enabled"):
        return
    # 原话与拧过的词**任一**看着像释义查询就沉淀（原话优先）
    if not (search_memory.is_definition_query(raw or query)
            or search_memory.is_definition_query(query)):
        return
    if not results:
        return
    definition = await search_memory.summarize(query, results)
    if not definition:
        return
    # 入库用拧过的词当 key：这样 /搜索 与自动搜索会命中同一条
    await search_memory.put(query, definition, results, by=by)


async def _run_tool_calls(
    messages: list[dict],
    *,
    conv: str,
    calls: list[Any],
    used: int,
    question: str = "",
) -> tuple[list[str], int, list[str]]:
    """执行这一轮的**每个**工具调用。返回 (每个调用各自的结果文本, 新用量, 搜过的词)。

    **为什么按调用逐个返回**：同一轮里模型可能一次调好几个工具（先搜再读）。
    早先所有调用共用一个结果文本，把它们分别塞进各自的 tool 消息时就会**串台** ——
    两个 `tool_call_id` 收到一模一样的内容，模型看到的对应关系是错的。
    DeepSeek 的 OpenAI 兼容接口返回的 `message.tool_calls` 本来就是列表，
    所以这里按 `call.id` 一一对上。

    计数 `used` 是**搜索与读正文共用**的（两者都是联网动作，共用一个总额更简单，
    也更好解释"这条消息你联网查了几次"）。
    """
    notes: list[str] = []
    results: list[str] = []

    for call in calls:
        fn = getattr(call, "function", None)
        name = str(getattr(fn, "name", "") or "")
        try:
            args = json.loads(getattr(fn, "arguments", "") or "{}")
        except (ValueError, TypeError):
            args = {}

        if used >= int(settings.get("search_max_per_message")):
            # 用尽要说清，别让它以为自己搜过了
            results.append("（这条消息的联网次数已经用完了。没查到的就说没查到，不要凭记忆编。）")
            logger.info("联网次数用尽 conv=%s tool=%s", conv, name)
            continue
        if not search.rate_ok(conv):
            results.append("（这个群这会儿联网太频繁了，等一下再查。先按你知道的说。）")
            logger.info("联网被限流 conv=%s tool=%s", conv, name)
            continue

        if name == "web_search":
            query = str(args.get("query") or "").strip()
            used += 1
            payload = await search.search(query)
            results.append(search.render_block(payload))
            notes.append(query)
            logger.info("模型主动搜索 conv=%s query=%s 命中=%d", conv, query, len(payload["results"]))
            # 沉淀释义（异步，不占这次回复的等待时间）。
            # **判定用用户原话**：模型给的是搜索词（比如「鲸落」），不含「是什么」这类标记，
            # 拿它去判定必然为 False —— 这就是"搜到了却不沉淀"的原因。
            _spawn(_remember_definition(query, payload["results"], raw=question or query))
        elif name == "web_fetch":
            url = str(args.get("url") or "").strip()
            used += 1
            page = await fetch.fetch_page(
                url,
                timeout=float(settings.get("search_read_timeout")),
                max_chars=int(settings.get("search_read_chars")),
            )
            results.append(fetch.render_block(page, url=url))
            logger.info("模型主动读正文 conv=%s url=%s 字数=%d err=%s",
                        conv, url[:100], len(page.get("text") or ""), page.get("error") or "")
        else:
            results.append(f"（没有「{name}」这个工具）")

    return results, used, notes


async def _ask_with_tools(messages: list[dict], *, conv: str, question: str = "") -> tuple[str, list[str]]:
    """带工具循环地问一次。返回 (最终回答, 搜过的查询词)。

    工具有两个：`web_search`（搜）与 `web_fetch`（读链接正文）。
    **任何一步失败都退回"不带工具的普通问答"** —— 搜索挂了不该让回复整个失败。
    """
    if not _search_tool_enabled(conv):
        return await _ask_deepseek(messages), []

    queries: list[str] = []
    used = 0
    # 工具说明只在提供了工具时注入，避免不搜的时候白占 prompt
    messages = list(messages)
    messages[0] = {
        **messages[0],
        "content": f"{messages[0]['content']}\n\n{search.system_note()}",
    }

    for _round in range(_MAX_TOOL_ROUNDS):
        try:
            response = await asyncio.wait_for(
                llm.chat(
                    messages=messages,
                    stream=False,
                    tools=[search.tool_schema(), fetch.tool_schema()],
                ),
                timeout=config.TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - 工具调用不被支持时退回普通问答
            logger.exception("带工具的请求失败，退回普通问答 conv=%s", conv)
            return await _ask_deepseek(messages), queries

        if not response.choices:
            return "", queries
        message = response.choices[0].message
        calls = list(getattr(message, "tool_calls", None) or [])

        if not calls:
            return (message.content or "").strip(), queries

        # 把这一轮的回答与工具结果接进历史，再问一次。
        #
        # **`reasoning_content` 必须原样带回**：DeepSeek 的 thinking 模式下，
        # 只要这一轮带过思维链，下一次请求就得把它一起送回来，否则整次调用被 400 拒掉
        # （`The reasoning_content in the thinking mode must be passed back to the API`）。
        # 早先这里只回传 content + tool_calls，一旦模型"先思考再调工具"就会整条回复失败。
        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": getattr(c.function, "name", ""),
                        "arguments": getattr(c.function, "arguments", "") or "{}",
                    },
                }
                for c in calls
            ],
        }
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning
        messages.append(assistant_msg)
        result_texts, used, notes = await _run_tool_calls(
            messages, conv=conv, calls=calls, used=used, question=question
        )
        queries.extend(notes)
        for index, call in enumerate(calls):
            # **按调用各自取结果**：同一轮里 web_search 与 web_fetch 可能一起出现，
            # 共用一份结果会让模型把"读到的正文"当成"搜索的返回"，对应关系就错了。
            content = result_texts[index] if index < len(result_texts) else "（没有结果）"
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": content or "（没有结果）"}
            )

    # 轮数用完还没给出正文：让它基于已有结果直接回答
    logger.info("工具轮数用尽 conv=%s，要求直接作答", conv)
    messages.append(
        {"role": "user", "content": "（搜到这里就够了，直接用你查到的东西回答，别再搜了。）"}
    )
    return await _ask_deepseek(messages), queries


def _split_for_qq(text: str, limit: int = 900) -> list[str]:
    """按段落切分长回答（实现搬到了 `context.split_for_qq`，这里保留同名出口）。

    `_工具链/离线验证.py` 与外部脚本按这个名字引用它，所以不做重命名。
    """
    return context.split_for_qq(text, limit)


def _polish(answer: str, question: str = "") -> str:
    """回复后处理（实现在 `context.polish`）。

    `keep_self_meta` 用**同一个判据**（`context.ask_about_self`）决定要不要保留
    "讲自己设定/机制"的句子：对方就是在问机制 → 留着（那正是他想要的答案）；
    没问 → 摘掉（读起来像在跟人解释自己的源码）。
    """
    return context.polish(
        answer, question=question, keep_self_meta=context.ask_about_self(question)
    )


async def _send(bot: Bot, event: MessageEvent, message) -> None:  # noqa: ANN001
    """统一的发送出口。

    以前只用 matcher.send / matcher.finish，但注意力机制触发时并没有 matcher
    可用（它是在 recorder 里评估出来的），所以把发送抽出来，两条路径共用。
    """
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        await bot.send_group_msg(group_id=int(group_id), message=message)
    else:
        await bot.send_private_msg(user_id=int(event.user_id), message=message)


async def _push_dsh_result(bot: Bot, event: MessageEvent, task_id: str, timeout: float) -> None:
    """后台等 DSH 结果并**主动推送**（`/dsh` 的异步回执）。

    为什么不在指令层同步等：那会把整条消息处理堵住最多 60 秒 ——
    群聊里"她愣了一分钟才回话"比"分两条说"更糟。所以指令层立刻回执，
    结果由这里轮询取到后再发一条。

    超时**不报错**，只是不推送：任务仍在本机队列/结果里躺着，
    下次 `/dsh run` 时本机 agent 还会把它跑掉 —— 比发一条"失败了"更诚实。
    """
    deadline = time.monotonic() + max(5.0, float(timeout))
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        try:
            got = dsh_bridge.read_result(task_id)
        except Exception:  # noqa: BLE001 - 读结果失败不该把后台任务打挂
            logger.debug("DSH 结果读取异常 id=%s", task_id, exc_info=True)
            continue
        if not got:
            continue
        text = dsh_bridge.render_result(got)
        logger.info("DSH 结果推送 id=%s status=%s", task_id, got.get("status"))
        try:
            group_id = getattr(event, "group_id", None)
            if group_id is not None:
                await bot.send_group_msg(group_id=int(group_id), message=text)
            else:
                await bot.send_private_msg(user_id=int(event.user_id), message=text)
        except Exception:  # noqa: BLE001 - 推送失败只记日志
            logger.exception("DSH 结果推送失败 id=%s", task_id)
        return
    logger.info("DSH 结果等待超时（不推送）id=%s timeout=%.0fs", task_id, timeout)


async def _maybe_send_sticker(bot: Bot, event: MessageEvent, conv: str = "") -> None:
    """按概率附一张表情包 —— **必须贴题**，不贴题就不发。

    改造前这里是 `stickers.pick_as_segment()`：从库里随机抽一张发出去。
    那正是"图文无关"的来源 —— 群里在认真讨论报错，它回一张卖萌图。
    现在改成 `pick_for_context()`：把当前对话（最近若干行原文）连同几张候选图的
    **真实画面**交给模型，由它挑贴题的那张，**并且允许它说不合适、这次不发**。

    这个功能的失败方向必须是"不发图"：调模型出错、返回解析不了、序号越界，
    一律不发。绝不退回随机。
    """
    if not settings.get("sticker_enabled"):
        return
    # 会话被设成「不保存图片」时也不再主动发图 —— 对方表达的是"别折腾图了"
    if conv and not state.may_store(conv):
        return
    if random.random() > float(settings.get("sticker_chance")):
        return
    # 模式缩放：专注/安慰时压低发图概率，但不清零 —— 模式是"少发一点"，不是开关
    scale = mode.scale_for(conv, "sticker") if conv else 1.0
    if scale <= 0 or random.random() > scale:
        return

    context = await _sticker_context(conv)
    segment, reason = await stickers.pick_for_context(
        context, conv=conv, is_master=_is_master(event)
    )
    if segment is None:
        logger.info("这次不发表情包 conv=%s（%s）", conv, reason)
        return
    try:
        await _send(bot, event, segment)
    except Exception:  # noqa: BLE001
        logger.exception("发送表情包失败")
        return
    # **发出去之后要留一条自己能看见的记录**。
    #
    # 改造前这支路发完就结束：聊天记录里**没有任何"我发过一张图"的痕迹**。
    # 于是群里说「这张图是你自己发的」时，它只能回「我什么时候发的，一点印象都没有」——
    # 不是它说谎，是**它对自己的行为没有记录可依**。
    # 记录用中性标记而不是具体画面描述：它不需要知道图长什么样，只需要知道"我做过这件事"。
    try:
        await stickers.record_sent_image(bot, conv, str(getattr(event, "user_id", "") or ""))
    except Exception:  # noqa: BLE001 - 记录失败不能影响已经发出去的图
        logger.exception("记录自己发的表情包失败 conv=%s", conv)


async def _sticker_context(conv: str) -> str:
    """给"挑图"用的当前语境：**最近若干行原文**，含机器人自己刚说的那句。

    刻意用「更早背景 + 未读」而不是只取当前消息 —— 一句"哈哈哈"单看是没有
    语境的，必须连着上面几句才能判断该配什么图。
    这里也包含机器人自己刚发出去的回复（它刚被 append 进记录），
    所以挑图能跟"我刚才说了什么"对上。
    """
    try:
        log = await chatlog.get_log(conv)
    except Exception:  # noqa: BLE001 - 拿不到语境就别发图
        logger.info("取不到语境，不发表情包 conv=%s", conv)
        return ""
    backend_budget = max(400, int(settings.get("sticker_pick_context_chars")) // 2)
    parts = [
        log.render_background(budget=backend_budget, clip=100),
        log.render_unread(clip=100),
    ]
    return "\n".join(x for x in parts if x)


async def _load_first_image(segments: list[dict]) -> bytes | None:
    """下载消息里的第一张图，供「看图回应」用。

    只取一张：多图会让请求体和视觉 token 一起膨胀。
    失败返回 None，调用方退回纯文本路径，不让回复整个挂掉。
    """
    limit_kb = float(settings.get("sticker_vision_max_kb"))
    for seg in segments:
        data = await stickers.download_image(seg)
        if not data:
            continue
        size_kb = len(data) / 1024
        if size_kb > limit_kb:
            logger.info("读图：图片 %.0fKB 超过上限 %.0fKB，跳过", size_kb, limit_kb)
            continue
        return data
    return None


async def _resolve_reply(
    event: MessageEvent,
) -> tuple[str, list[dict], list[dict], bool]:
    """解析 QQ 的「引用 / 回复」。

    返回 (被引用内容的文字描述, 其中的图片段, 其中的文件段, 被引用的是不是机器人自己发的)。

    **别再自己遍历 message 找 reply 段，也别再自己调 get_msg。**
    NoneBot2 的 OneBot v11 适配器在事件预处理里（`adapters/onebot/v11/bot.py` 的
    `_check_reply`）已经调过 `get_msg`、把结果塞进 `event.reply`，**并且把那个
    reply 段从 `event.message` 里 `del` 掉了**。

    这正是「引用什么都取不到」的根因：自己遍历 `event.message` 永远找不到 reply 段，
    而且那条路径一条日志都不留，从外面看就是彻底静默失效 —— 之前几轮都在对着一个
    已经被移走的东西使劲。

    取不到（消息太旧、超出 NapCat 缓存）时 NoneBot2 会打一条 WARNING 并把 reply
    留成 None，这里就返回空，绝不阻塞正常回复。
    """
    reply = getattr(event, "reply", None)
    if reply is None:
        return "", [], [], False

    raw_segments = list(getattr(reply, "message", None) or [])
    sender = getattr(reply, "sender", None)

    texts: list[str] = []
    images: list[dict] = []
    quoted_files: list[dict] = []
    seen_kinds: list[str] = []
    for raw in raw_segments:
        # 兼容两种形态：原始 dict，以及 NoneBot2 转换后的 MessageSegment
        if isinstance(raw, dict):
            kind = raw.get("type")
            data = raw.get("data") or {}
        else:
            kind = getattr(raw, "type", None)
            data = getattr(raw, "data", None) or {}
        if not kind:
            continue
        seen_kinds.append(str(kind))
        if kind == "text":
            texts.append(str(data.get("text", "")))
        elif kind == "image":
            images.append(dict(data))
        elif kind == "file":
            quoted_files.append(dict(data))
        elif kind == "at":
            qq = data.get("qq")
            texts.append("@全体成员" if str(qq) == "all" else f"@{qq}")
        elif kind in ("face", "mface"):
            texts.append("[表情]")
        # 嵌套引用不再递归，避免无限套娃

    logger.info(
        "引用解析 段=%s → 文本 %d / 图片 %d / 文件 %d",
        seen_kinds,
        len(texts),
        len(images),
        len(quoted_files),
    )

    who = str(
        getattr(sender, "card", "")
        or getattr(sender, "nickname", "")
        or getattr(sender, "user_id", "")
        or "某人"
    )
    # 引用的是机器人自己发过的消息 —— 那等于在跟它说话，不该再靠概率才敢开口
    from_bot = str(getattr(sender, "user_id", "") or "") == str(event.self_id)
    body = "".join(texts).strip()
    if images:
        body = f"{body} [图片×{len(images)}]".strip()
    if quoted_files:
        body = f"{body} [文件×{len(quoted_files)}]".strip()
    return (f"{who}：{body}" if body else ""), images, quoted_files, from_bot


# ----------------------------------------------------------- handler 1：记录
recorder = on_message(priority=1, block=False)


async def _ingest_image(
    event: MessageEvent,
    seg: dict,
    conv: str,
    conv_label: str,
    name: str,
    is_master: bool,
    file_sent: bool = False,
) -> None:
    """后台收图：下载 → 记进「最近图片」→ 人格打分 → 入库。全程不阻塞消息处理。

    「最近图片」这一份缓存同时也是「不要保存这张图片」能生效的前提 ——
    它按内容哈希记，所以主人点名时能精确定位到是哪一张。

    `file_sent=True` 表示这是"以文件形式发来的图"，会一路带到入库流程里降权。
    """
    try:
        # 会话被设成「完全不看图」时连下载都不做
        if not state.may_download(conv):
            logger.debug("会话图片策略为 off，不下载图片 conv=%s", conv)
            return

        # 先下载一次：既喂给入库打分，也存进「最近图片」——
        # 群友发完图、隔一条再问「这个是什么」时，它才记得那张图。
        data: bytes | None = None
        source = stickers.image_source(seg)
        if source:
            try:
                data = await asyncio.to_thread(stickers._download, source)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                logger.info("下载图片失败（链接可能已过期）conv=%s", conv)
        if not data:
            return

        _remember_image(conv, data)
        # 记哈希，供「这张图」指代与单张豁免
        digest = hashlib.sha256(data).hexdigest()[:16]
        state.remember_image(conv, digest)

        # 会话策略说不存，就到此为止（哈希已经记下了，点名依然有效）
        if not state.may_store(conv):
            logger.info("会话图片策略为 %s，图片只记哈希不入库 conv=%s", state.image_mode(conv), conv)
            return

        log = await chatlog.get_log(conv)
        # 这里的上下文是喂给"表情包打分"的，也走带标记的渲染 ——
        # 否则打分模型会把机器人自己说过的话当成别人在说话，判据就偏了。
        context_text = log.render_unread(clip=60) or log.render_background(budget=400, clip=60)
        item = await stickers.store_from_message(
            conv=conv,
            conv_label=conv_label,
            uid=event.user_id,
            name=name,
            is_master=is_master,
            seg=seg,
            context=context_text[-600:],
            persona=config.SYSTEM_PROMPT,
            preloaded=data,
            file_sent=file_sent,
        )
        if item is not None:
            logger.info(
                "表情包入库 hash=%s 分数=%.2f 权重=%.2f 理由=%s%s",
                item["hash"],
                item["score"],
                float(item.get("weight", 1.0)),
                item["reason"],
                "（以文件发送）" if file_sent else "",
            )
    except Exception:  # noqa: BLE001 - 收图失败绝不能影响聊天
        logger.exception("处理图片消息失败 conv=%s", conv)


@recorder.handle()
async def record_message(bot: Bot, event: MessageEvent) -> None:
    """把所有消息追加进聊天记录；图片另送表情包库。block=False，绝不拦截后续 handler。"""
    if not _allowed(event):
        return

    group_id = getattr(event, "group_id", None)
    conv = chatlog.conversation_id(group_id, event.user_id)
    name = _display_name(event)
    master = _is_master(event)

    # 这条消息是不是机器人自己发的？
    #
    # 正常路径下它收不到自己的消息，所以这里主要是**兜底**：某些部署里
    # 机器人自己（或同一 QQ 的另一个连接 / 别的客户端）发的消息会被回灌进来。
    # 不拦的话有两个后果：
    #   ① 它的发言被当成"别人说的"落盘 —— 下一轮模型就把自己说过的话当别人的话；
    #   ② 它说的话里要是带了唤醒词，它会**被自己叫出来**，对着自己接话。
    # 判据只用发送者 QQ，不看内容。
    from_bot = str(getattr(event, "user_id", "") or "") == str(getattr(bot, "self_id", "") or "")

    text = event.get_plaintext().strip()
    if text and (config.RECORD_ALL or _is_addressed(event)):
        try:
            msg, rolled = await chatlog.append_message(
                conv,
                event.user_id,
                name,
                text,
                is_bot=from_bot,
                bot_uid=int(bot.self_id) if from_bot else None,
            )
            # 会话刚切分 → 给「刚刚结束的那一轮」生成摘要（非阻塞）。
            # 放在这里而不是回复路径，是因为**没人 @ 机器人的那些轮次同样需要摘要** ——
            # 而它们只会经过 record_message 这一个落盘点。
            if rolled:
                summaries.on_session_rolled(conv, int(msg.get("session", 0)), _spawn)
        except Exception:  # 记录失败不能影响机器人回复
            logger.exception("写入聊天记录失败 conv=%s", conv)

    if from_bot:
        # 自己发的：记下（并标成已读）就够，不触发任何"该它说话"的判断
        logger.debug("收到自己发的消息，只记录为已读 conv=%s", conv)
        return

    images = stickers.extract_images(event)
    if images:
        conv_label = f"群 {group_id}" if group_id else "私聊"
        for seg in images:
            _spawn(_ingest_image(event, seg, conv, conv_label, name, master))

    # 以「文件」形式发来的图片单独走一条：它们要被打低权重，所以必须跟真·表情包
    # 分开传（混在一起就分不出谁是文件了）。QQ 的 PC 端拖拽图片就是这条路径，
    # 上报的是 file 段、sub_type 全是 0 —— 看不出是不是表情包。
    image_files = stickers.extract_image_files(event)
    for seg in image_files:
        _spawn(
            _ingest_image(
                event, seg, conv, f"群 {group_id}" if group_id else "私聊", name, master, file_sent=True
            )
        )

    # 群聊消息计入"按消息数触发主动发言"的计数器（异步，不阻塞）
    if group_id is not None and text:
        _spawn(proactive.on_group_message(conv))
        # 注意力：若这个会话正处于「被唤醒后的活跃期」，评估这条新消息跟话题的相关度。
        # 已经在跟它说话的（被 @ / 提到唤醒词）不必再走这条。
        if not _is_addressed(event) and not _mentions_wake_word(event):
            _spawn(_attention_check(bot, event, conv, text))


# ----------------------------------------------------------- handler 2：回复
def _random_reply_eligible(event: MessageEvent) -> bool:
    """是否属于「可能随机插话」的消息。纯判断，不掷骰。

    只对**群聊的、有实质正文的**消息开放：私聊本来就算在跟它说话；
    而「哈哈」「?」这类短句没什么可接的，跳过。
    """
    if not settings.get("random_reply_enabled"):
        return False
    if getattr(event, "group_id", None) is None:
        return False
    text = event.get_plaintext().strip()
    return len(text) >= int(settings.get("random_reply_min_chars"))


def _should_reply(event: MessageEvent) -> bool:
    """被 @ / 命中前缀 / 提到唤醒词 / **在对话租约内** / 命中随机插话条件。

    这里只做**纯判断**：概率与冷却一律放到 handler 里。
    rule 一旦带副作用，被评估几次就会多掷几次骰，概率直接失控 ——
    同理，租约在这里只能用 `lease_peek()`（只读），不能在门里消耗轮数。
    """
    if not _allowed(event):
        return False
    if _is_addressed(event) or _mentions_wake_word(event):
        return True
    # 对话租约：刚叫过它，接下来的追问也算在跟它说话
    conv = chatlog.conversation_id(getattr(event, "group_id", None), event.user_id)
    if attention.lease_peek(
        conv,
        str(getattr(event, "user_id", "") or ""),
        is_master=_is_master(event),
        text=event.get_plaintext(),
    ):
        return True
    return _random_reply_eligible(event)


ai_chat = on_message(rule=Rule(_should_reply), priority=50, block=True)


@ai_chat.handle()
async def handle_ai_chat(bot: Bot, event: MessageEvent) -> None:
    """判定这次是哪条来路（被 @ / 唤醒词 / 随机插话），再交给 `_reply`。

    概率与冷却一律在这里做，`_should_reply` 只负责纯判断 ——
    否则 rule 被评估几次就会多掷几次骰。
    """
    conv = chatlog.conversation_id(getattr(event, "group_id", None), event.user_id)

    # 引用机器人自己发过的消息 = 在跟它说话，不必再靠概率才敢回
    reply = getattr(event, "reply", None)
    sender = getattr(reply, "sender", None) if reply is not None else None
    quoted_from_bot = str(getattr(sender, "user_id", "") or "") == str(event.self_id)

    if _is_addressed(event) or quoted_from_bot:
        await _reply(bot, event, "addressed")
        return

    if _mentions_wake_word(event):
        if random.random() > float(settings.get("wake_chance")):
            logger.debug("被提到但掷骰未通过，不回应 conv=%s", conv)
            return
        if not _wake_cooldown_ok(conv):
            logger.debug("唤醒冷却中，不回应 conv=%s", conv)
            return
        logger.info("被唤醒词叫出来了 conv=%s", conv)
        await _reply(bot, event, "wake")
        return

    # 对话租约：刚叫过它，这个会话接下来的追问不必再 @。
    #
    # 放在唤醒之后、随机插话之前 —— 它属于"确定在跟它说话"这一类，
    # 不该和"概率插话"混在一起掷骰子。
    # 判定纯本地（不调模型、不花 token），所以放在这里不增加任何延迟。
    lease_uid = str(getattr(event, "user_id", "") or "")
    if attention.lease_check(
        conv, lease_uid, is_master=_is_master(event), text=event.get_plaintext()
    ):
        logger.info("对话租约命中，继续回应 conv=%s", conv)
        await _reply(bot, event, "lease")
        return

    # 什么都没提：随机插话（默认关）
    if random.random() > float(settings.get("random_reply_chance")):
        logger.debug("随机插话未命中，不回应 conv=%s", conv)
        return
    if not _random_cooldown_ok(conv):
        logger.debug("随机插话冷却中，不回应 conv=%s", conv)
        return
    logger.info("随机插话命中 conv=%s", conv)
    await _reply(bot, event, "random")


async def _attention_check(bot: Bot, event: MessageEvent, conv: str, text: str) -> None:
    """注意力评估：跟当前话题够相关就不等 @，自己接话。"""
    try:
        should, value = await attention.should_speak(conv, text)
        if not should:
            return
        logger.info("注意力触发主动接话 conv=%s value=%.2f", conv, value)
        await _reply(bot, event, "attention")
    except Exception:  # noqa: BLE001 - 评估出错不能影响群聊
        logger.exception("注意力评估出错 conv=%s", conv)


def _instruction_context(action: instructions.Action) -> str:
    """把指令的 kind 翻译成给模型看的场景说明。"""
    return {
        "image": "对方刚提了个关于图片怎么处理的要求，系统已经照办了。",
        "image_ignore_this": "对方刚说不要保存某张图片，系统已经照办了。",
        "image_ignore_topic": "对方刚说以后不要存图，系统已经照办了。",
        "image_status": "对方在问图片当前是怎么处理的。",
        "style": "对方刚提了个说话方式上的要求，系统已经记下了。",
        "style_call": "对方刚要求换个称呼，系统已经改好了。",
        "style_no_call": "对方刚要求别用某个称呼，系统已经记下了。",
        "style_length": "对方刚要求改回复长短，系统已经改好了。",
        "memory_remember": "对方刚让你记住一件事，系统已经存进长期记忆了。",
        "memory_forget": "对方刚让你忘掉一件事，系统已经删掉了。",
    }.get(action.kind, "对方刚提了个要求，系统已经处理完了。")


async def _send_instruction_reply(
    bot: Bot,
    event: MessageEvent,
    action: instructions.Action,
) -> None:
    """把指令结果发出去。

    分两条路，区别很重要：

    * `action.reply` 非空 —— **原样发**，不过模型。指令回执（比如 `/记忆 列表` 的清单、
      `/图 状态` 的当前策略）必须一个数字都不能被文风改写。
    * 只有 `prompt_note` —— 状态已经改好了，但让模型用人设口吻说一句，
      这样"答应了"听起来像她说的，而不是系统提示。
    """
    text = action.reply
    if not text and action.prompt_note:
        text = await _instruction_voice(event, action)
    if not text:
        return
    for chunk in _split_for_qq(text):
        await _send(bot, event, Message(chunk))
        await asyncio.sleep(0.4)


async def _instruction_voice(event: MessageEvent, action: instructions.Action) -> str:
    """让模型用人设口吻把"已经发生的事"说出来。失败就退回一句朴素的确认。"""
    conv = chatlog.conversation_id(getattr(event, "group_id", None), event.user_id)
    note = action.prompt_note
    messages = [
        {
            "role": "system",
            "content": context.system_prompt(
                conv=conv, is_master=_is_master(event), include_mechanism=False
            ),
        },
        {
            "role": "user",
            "content": (
                f"{_instruction_context(action)}\n\n{note}\n\n"
                "只回一句话，不要解释、不要复述上面这些说明、不要提「系统」「指令」「参数」。"
            ),
        },
    ]
    try:
        async with _semaphore:
            answer = await _ask_deepseek(messages)
    except Exception:  # noqa: BLE001 - 回执生成失败不能把指令本身咽掉
        logger.exception("指令回执生成失败 kind=%s", action.kind)
        return ""
    return _polish(answer)


async def _maybe_prefetch(
    conv: str,
    question: str,
    *,
    context_text: str = "",
) -> str:
    """本地兜底：模型还没说话之前，先判断这条消息是不是明显需要联网。

    为什么要有这一层（而不是全靠模型调工具）：

    * 工具调用在某些模型/端点上可能不可用（V4-Pro 就拒绝强制调用），
      而"我知识截止之后的事"是**一定会错**的 —— 这一层保证那种问题至少能查到东西；
    * 模型有时意识不到自己不知道（尤其是新词、梗），本地那句"提到了专名/在问带引号的词"
      是个便宜的提醒。

    只在 `needs_search()` 明确说要搜时才动，而且**结果只是"附上资料"，不是"替它回答"** ——
    最终还是模型自己决定怎么用、要不要说自己查过了。

    `context_text` 是**历史参数，现在不参与查询构造**（保留是为了不破坏调用方签名）。
    曾经它传的是整段 `user_text`，而 `query_from` 会取它的末尾当上下文 ——
    于是线上搜的是「…（这个会话记录里共 740 条，已读 738 条，未读 2 条）」。
    教训：**传给"启发式"的输入要按最坏情况假设**，它会拿你给的任何东西当关键词。
    """
    if not (settings.get("search_enabled") and settings.get("search_prefetch")):
        return ""
    # 形参已弃用（见 docstring）。显式 ignore 而不是删参数：调用方签名不变，
    # 且将来真要再用上下文时，会先撞到这行注释而不是悄悄拼进查询。
    _ = context_text
    should, why = search.needs_search(question)
    if not should:
        return ""
    # 只从 `question` 拧词，不带任何上下文 —— 理由见上面的 docstring 与 `search.query_from`。
    query = search.query_from(question)
    if not query:
        # 拧不出像样的关键词就**不搜**。这一条以前是"总归会返回点什么"，
        # 于是线上真的拿「那为什么不回应你的主人 （这个会话记录里共 740 条…）」去搜过。
        logger.info("预取放弃：拧不出搜索词 conv=%s question=%s", conv, question[:40])
        return ""

    # ---- 先看释义库里有没有查过 ----
    # 命中就**不必再联网**：省一次配额，也省一段 prompt 空间。
    # 低置信的仍然带上去，但渲染时会显眼标注"别当准的用"（见 search_memory.render）。
    #
    # 注意这里**在 `search.available()` 之前**：库里有答案时能不能联网根本无关紧要。
    # 第一版把 available() 放在前面，于是没配端点时明明库里有释义却不去用。
    from . import search_memory

    cached, blocked = search_memory.usable(query)
    if cached is not None and not blocked:
        logger.info("释义库命中，跳过联网 conv=%s query=%s", conv, query)
        return search_memory.render(query)
    cached_low = cached if blocked == "低置信（仅作参考）" else None

    if not search.available():
        # 连不上网：低置信的旧释义仍然给（总比没有强），明确标注即可
        return search_memory.render(query) if cached_low is not None else ""
    if not search.rate_consume(conv):
        logger.info("预取被限流 conv=%s（%s）", conv, why)
        return search_memory.render(query) if cached_low is not None else ""

    payload = await search.search(query)
    logger.info("本地预取搜索 conv=%s query=%s（%s）命中=%d",
                conv, query, why, len(payload["results"]))
    blocks: list[str] = []
    if cached_low is not None:
        blocks.append(search_memory.render(query))
    if payload["results"]:
        # 预取的结果直接摆在 prompt 里。**注意这里也要带"不可信"声明** ——
        # 渲染逻辑在 search.render_block 里统一做，不在这里另写一套。
        blocks.append(search.render_block(payload))
        # 判定传**原始问题**（question），不是拧过的 query —— 见 _remember_definition
        _spawn(_remember_definition(payload["query"] or query, payload["results"],
                                    by="预取", raw=question))
    return "\n\n".join(blocks)


async def _reply(bot: Bot, event: MessageEvent, trigger: str) -> None:
    """组装 prompt → 调模型 → 发消息。

    流程（改造后的顺序，每一步都有理由）：

    1. **先解析指令**。「不要保存这张图片」这类要求必须在组装 prompt 之前就变成
       真实的状态变更 —— 否则模型只在回话里"答应"，行为其实没变，
       这正是改造前最别扭的地方。
    2. 命中且是确定性回执（列表 / 状态 / 权限拒绝）→ 直接发，不调模型，省 token 也保准确。
    3. 没命中 → 走长期记忆 + 聊天记录组装 prompt，正常回复。
    4. 回复落地后**异步**抽取记忆，不占群友的等待时间。

    matcher（被 @ / 唤醒词 / 随机插话）与注意力机制**共用这一条路径**，
    区别只在 trigger 决定「现在需要你回应的发言」那段提示语怎么写。

    trigger 取值：addressed / wake / random / attention
    """
    conv = chatlog.conversation_id(getattr(event, "group_id", None), event.user_id)
    plain = event.get_plaintext().strip()
    is_master = _is_master(event)

    # 引用要用 event.reply：NoneBot2 已经调过 get_msg 并把 reply 段从 message 里删了
    quoted_text, quoted_images, quoted_files, quoted_from_bot = await _resolve_reply(event)

    # 图片段在**指令解析之前**就取好：`/头像` 要用"本条消息里的图 + 它引用的图"，
    # 而引用段已经被 NoneBot2 从 `event.message` 里删掉了，只有 `_resolve_reply` 拿得到。
    # 放在这里不额外花代价：原来是在下面才取的，只是提前了几行。
    image_segments = stickers.extract_images(event)

    if plain in RESET_WORDS:
        await chatlog.clear(conv)
        attention.clear(conv)
        attention.clear_lease(conv)
        behavior.clear(conv)
        await _send(bot, event, "好啦，之前的聊天记录我都忘掉了，重新开始吧。")
        return

    if not llm.api_key():
        await _send(bot, event, config.MSG_NO_KEY)
        return

    # ---------------------------------------------------------------- 实时指令
    # 群友也能用（/帮助、/记忆 列表 之类），但破坏性动作在指令层按 is_master 拦。
    # 自然语言兜底只对"主人私聊"或"明确叫到它"开放，避免群里一句随口话改了状态。
    natural_ok = bool(
        settings.get("command_natural")
        and (is_master or _is_addressed(event) or _mentions_wake_word(event))
    )
    action = await instructions.parse(
        plain,
        conv=conv,
        is_master=is_master,
        prefix=config.PREFIX,
        allow_natural=natural_ok,
        bot=bot,
        # 给 `/头像` 的候选图，按"最可能是他指的那张"排序：
        #   1. 本条消息里的图片段（自己发的排在引用的前面）
        #   2. 它引用的消息里的图片
        #   3. 以**文件**形式发来的图（PC 端拖张图进来就是这种，OneBot 报的是 file 段）
        # 拿不到的会被跳过，所以多列几种形态不会出错，只是多一次下载尝试。
        image_segments=(
            list(image_segments)
            + list(quoted_images)
            + stickers.extract_image_files(event)
            + [
                dict(seg)
                for seg in quoted_files
                if stickers.is_image_file(str(seg.get("file") or seg.get("name") or ""))
            ]
        ),
    )
    if action.handled:
        if action.stop:
            logger.info("指令命中 kind=%s ok=%s conv=%s", action.kind, action.ok, conv)
            # `/dsh run` 的异步回执：指令层已经落盘任务，这里起个后台任务等结果、主动推送。
            # 判据用 effect 里的 task_id（而不是 kind=dsh），因为将来 dsh 还会加别的子命令，
            # 只有"真的下发了一个任务"才需要推送。
            _dsh_task = getattr(action, "effect", {}) or {}
            if _dsh_task.get("task_id"):
                _spawn(_push_dsh_result(bot, event, str(_dsh_task["task_id"]),
                                        float(dsh_bridge.TIMEOUT_SECONDS) + 30.0))
            await _send_instruction_reply(bot, event, action)
            return
        # 状态已改，让模型用人设口吻确认 —— 落到下面正常路径，带上这个 note
        logger.info("指令已执行（转人设口吻）kind=%s conv=%s", action.kind, conv)
    else:
        action = instructions.Action()

    question = _question_text(event)
    # `image_segments` 上面已经取过（指令要用），这里不再重复调用
    file_segments = files.extract_files(event)

    if not question:
        # 只有附件、一个字正文都没有的消息。
        # 老版本这里一律回 MSG_NO_QUESTION「在的，@我想说什么？」，于是私聊里发张表情
        # 就被这句话顶回来，把正在聊的话头直接打断。
        if file_segments:
            # 发文件是明确的「想让你看这个」意图，值得读一下再说
            question = "（发来一个文件，没配文字）"
        elif image_segments and settings.get("reply_to_images"):
            question = "（发来一张图，没配文字）"
        elif action.handled:
            # 指令已经改完状态了，正文为空也要把话说完
            question = "（刚才那句话是个要求）"
        else:
            logger.debug("纯附件消息，按配置保持安静 conv=%s", conv)
            return

    # 被叫出来的时候，把这次的问题记成「话题」，启动注意力机制：
    # 之后一段时间的发言会按跟这个话题的相关度累积注意力，够了就不用再 @ 它。
    #
    # **只在被 @ / 命中前缀时聚焦，命中唤醒词时不聚焦**：
    # 唤醒词是**包含式**匹配（一个「肥鱼」命中「大肥鱼」「死肥鱼」…），
    # 一次误命中就会开一个 `attention_window` 那么长的评估窗口 —— 群里聊到就建窗口，
    # 既贵又吵。唤醒词本身仍会把它叫出来回一句（下面照样授予租约），
    # 只是不再顺带获得"接下来 10 分钟都在场"的状态。
    if trigger == "addressed":
        attention.focus(conv, f"{_speaker_label(event)}：{question}")
    if trigger in ("addressed", "wake"):
        # 对话租约：**这个会话**接下来的追问直接算在跟它说话，不必再 @。
        # 两套机制解决的是不同问题 —— 注意力管"旁听时要不要插话"（语义），
        # 租约管"刚叫过它、接着聊别断"（协议）。理由详见 attention.py 文件头。
        attention.lease_grant(conv, str(getattr(event, "user_id", "") or ""))

    log = await chatlog.get_log(conv)

    # 记录 handler 刚把这条写进去，所以"该用户最近一条未读"就是本次要回应的发言。
    current = log.last_from(event.user_id)
    current_id = int(current["id"]) if current else None

    # 消息里带的文件：读成文本一并交给模型。
    # 有大小上限，二进制只报元信息；最多看两个，免得一条消息把上下文撑爆。
    group_id = getattr(event, "group_id", None)
    file_blocks: list[str] = []
    # 引用里的文件也算 —— 群聊和私聊里最常见的用法都是「引用那个文件再问」
    for seg in (file_segments + quoted_files)[:2]:
        fname = seg.get("file") or seg.get("name") or "?"
        try:
            block = await files.read_segment(bot, seg, group_id, event.user_id)
        except Exception:  # noqa: BLE001 - 单个文件读失败不能拖垮整条回复
            logger.exception("读取文件出错：%s", fname)
            continue
        if block:
            file_blocks.append(block)
            logger.info("文件已并入上下文：%s", fname)
        else:
            logger.info("文件未读取（开关关闭或不可用）：%s", fname)

    # 把图交给模型看。
    # 关键：不能只在「纯图片消息」时才读 —— 用户 @ 它并配上一句「这是什么」，
    # 图同样必须送进去，否则它就是在对着空气回答。
    # 被引用的图同理：既然引用了，就说明想让它看见。
    #
    # 还要看**当前档案标没标「读图」**：换成本地纯文本模型之后，把 image_url 段
    # 塞过去多半是 400，而且那种失败发生在发消息那一步、看起来像"图坏了"。
    # 这里不读图，模型只会知道"有人发了图"，跟它自己说的"这个模型不看图"一致。
    vision_images: list[bytes] = []
    if settings.get("chat_vision") and llm.caps()["vision"] and state.may_view(conv):
        for group in (image_segments, quoted_images):
            data = await _load_first_image(group)
            if data is not None:
                vision_images.append(data)
        # 前三个来源都没图？看看这个会话最近有没有人发过图 ——
        # 群里最典型的问法就是「先发一张图，隔一条再问这是什么」，图不在当前消息里。
        if not vision_images:
            recent = _recent_image(conv)
            if recent is not None:
                vision_images.append(recent)
                logger.info("用上了最近发过的图 conv=%s", conv)

    # 组装 prompt：人设 → 风格要求 → 记录读法 → 时间 → 长期记忆 → 机制事实 → 聊天记录 → 本次发言
    messages, user_text = context.build(
        conv=conv,
        log=log,
        speaker=_speaker_label(event),
        question=question,
        current_id=current_id,
        trigger=trigger,
        quoted_text=quoted_text,
        file_blocks=file_blocks,
        is_master=is_master,
        instruction_note=action.prompt_note,
        bot_uid=str(getattr(bot, "self_id", "") or ""),
    )
    # 有图时最后一条 user 消息换成多模态数组
    messages[-1]["content"] = stickers.content_with_images(user_text, vision_images)

    async with _semaphore:
        try:
            # 先用本地启发式兜一次底（"这个词/这事得有新信息才能答"），
            # 再带工具问 —— 模型自己觉得该搜时还能再调 web_search。
            #
            # **这里不传上下文**（早先传的是上面的 `user_text`，那是线上故障的根源）：
            # `user_text` 是给模型看的整段 prompt，末尾是会话统计行，而 `query_from`
            # 当年会取它的末尾当"上下文" —— 于是搜索词变成了
            # 「…（这个会话记录里共 740 条，已读 738 条，未读 2 条）」。
            # 现在查询只由 `question` 拧出来，见 `search.query_from`。
            prefetch = await _maybe_prefetch(conv, question)
            if prefetch:
                messages[-1]["content"] = (
                    f"{messages[-1]['content']}\n\n{prefetch}"
                    if isinstance(messages[-1]["content"], str)
                    else messages[-1]["content"]
                )
                if not isinstance(messages[-1]["content"], str):
                    # 多模态形态：把资料作为额外的文本块追加
                    messages[-1]["content"] = list(messages[-1]["content"]) + [
                        {"type": "text", "text": prefetch}
                    ]
            answer, searched = await _ask_with_tools(messages, conv=conv, question=question)
        except asyncio.TimeoutError:
            logger.warning("DeepSeek 请求超时 conv=%s", conv)
            await _send(bot, event, config.MSG_TIMEOUT)
            return
        except Exception:  # noqa: BLE001 - 兜底，避免单次失败拖垮整条流程
            # 群里只说人设化的提示，技术细节留在日志里，不往群里抛
            logger.exception("DeepSeek 调用失败 conv=%s", conv)
            await _send(bot, event, config.MSG_ERROR)
            return

    answer = _polish(answer, question=question)
    if not answer:
        # 【判定】空回复是**故障**：要在群里**报错**，但报的必须是"错误通报"、
        # 不是人设化的俏皮话。
        #
        # 原来是发 `MSG_EMPTY`（「我没想出要说什么，换个说法问？」）。那条文案自带问句，
        # 与铁律「不作话头抛回者」冲突 —— 根子是**这个位置不该由人设接管**：
        # 模型一个字都没吐出来不是"她这会儿不想说话"。现在两件事一起做：
        #   ① `logger.error` 记 conv / trigger / 原话片段，供排查；
        #   ② 群里发一条**措辞中性的错误通报**（`config.MSG_EMPTY`），让人知道这轮出了故障，
        #      而不是被静默无视。该通报属"固定系统文案"语域，不受人格铁律约束
        #      （见 persona_traits.json 的 fixed_notice_channels）。
        logger.error(
            "模型返回空回复 conv=%s trigger=%s question=%r",
            conv, trigger, (question or "")[:60],
        )
        await _send(bot, event, config.MSG_EMPTY)
        return

    # 查过就让它说明一句。目的是**可追溯**：说错了别人也知道这是从网上来的，
    # 而不是它自己编的。用代码加前缀而不是靠提示词 —— 靠提示词它常常会忘。
    if searched and settings.get("search_show_note"):
        prefix = str(settings.get("search_note_prefix") or "").strip()
        if prefix and not answer.startswith(prefix):
            answer = f"{prefix}{answer}"
        logger.info("本轮回答基于联网结果 conv=%s 搜索词=%s", conv, searched)

    # 机器人的回复标为已读落盘；再把本次处理到的消息标记已读。
    # 注意先 append 再 mark_read_until：mark 用 id 上界，不会误伤刚写入的回复。
    _msg, _rolled = await chatlog.append_message(
        conv,
        int(bot.self_id),
        config.bot_name(),
        answer,
        is_bot=True,
        bot_uid=int(bot.self_id),
    )
    if _rolled:
        # 回复本身也可能撞上会话边界（隔了很久才回上一句）
        summaries.on_session_rolled(conv, int(_msg.get("session", 0)), _spawn)
    if current_id is not None:
        marked = await chatlog.mark_read_until(conv, current_id)
        logger.info("conv=%s 已标记 %d 条为已读", conv, marked)

    chunks = _split_for_qq(answer)
    for idx, chunk in enumerate(chunks):
        await _send(bot, event, Message(chunk))
        if idx < len(chunks) - 1:
            # 间隔：只有一条时不需要等；分多条时停得久一点，像真人打字。
            # 间隔太大显得卡，太小会被风控判成刷屏 —— 默认 0.9 秒。
            delay = (
                float(settings.get("sentence_split_delay"))
                if len(chunks) > 1
                else 0.4
            )
            await asyncio.sleep(delay)
    if len(chunks) > 1:
        logger.info("回答分 %d 条发送 conv=%s", len(chunks), conv)

    # 主观性的一环：有时候不回文字就够了，配张图更像群里的人
    await _maybe_send_sticker(bot, event, conv)

    # 注意力：接过一次话就减半 —— 后面相关度高还能接着聊，但不会没完没了
    attention.relax(conv)

    # 行为闸门：记下"这一轮我说了什么"，供下一轮判断是否"反复提时间/反复追问"。
    # 判定"反复"是数数，交给代码（模型数不准自己说过几次）——详见 behavior.py。
    behavior.note_reply(conv, answer)

    # 对话租约：这一轮真的回成功了才记账并续期。
    # 放在 relax 之后：两者互不干扰（租约不看注意力值）。
    if trigger == "lease":
        turns = attention.lease_commit(conv)
        logger.info("对话租约已用 %d 轮 conv=%s", turns, conv)

    # 长期记忆：回复已经发出去了，现在才异步提炼。
    # 放最后是有意的 —— 抽取要调一次模型，绝不能让它占群友的等待时间。
    if memory.is_enabled():
        _spawn(memory.extract_and_store(conv))


# ----------------------------------------------------------- 后台：主动发言与定时问候
_driver = get_driver()


@_driver.on_startup
async def _start_proactive() -> None:
    settings.store.load()
    # 记忆、会话图片策略、时钟偏移都是懒加载的，启动时先读一次：
    # 一是不用等第一条消息才碰盘，二是出问题时能在启动日志里看见。
    #
    # **人格不需要预热**：分层之后 `persona.render()` 每轮现读三个文件
    # （底层/禁止事项/表层），没有内存副本可预热 —— 原来这里的
    # `persona._store.ensure()` 随槽位机制一起删了。
    memory._db.ensure()  # noqa: SLF001
    state._state.ensure()  # noqa: SLF001
    # 时间校准：先把上次的偏移读回来（重启后到首次同步成功之间有段空窗，
    # 不恢复的话那几条消息会退回未校准的系统时钟），再起后台同步任务。
    clock._state.ensure()  # noqa: SLF001
    _spawn(clock.loop())
    _spawn(proactive.loop())
    _spawn(greetings.loop())
    # 后台补抽：把「有人聊过但没人 @ 它、或当时额度用完了」的记录补进记忆库。
    # 这是水位线的配套 —— 没有它，水位线只会被回复路径推着慢慢走。
    _spawn(memory.drain_loop())
    # 消息索引：增量给聊天记录建词面索引，供「翻旧账」查原话。
    # 它是**派生物**（删掉会自动重建），所以这里不关心失败。
    _spawn(msgindex.index_loop())
    # 使用计数落盘：`used` 每一轮回复都在涨，攒着批量写（不调模型、不花 token）。
    _spawn(memory.usage_loop())
    # 人格自我迭代（路线 C）：定期反思 → 候选 → 过冲突闸门 → 只写表层人设。
    # 它是三层结构里唯一有写权限的东西，且底层/铁律连写路径都不存在。
    _spawn(persona_iter.reflect_loop())
    logger.info(
        "后台任务已启动（主动发言：%s；定时问候：%s；长期记忆：%s/%s 条；"
        "会话摘要：%s/%s 轮；聊天记录索引：%s/%s 条；人格自我迭代：%s；当前时间：%s）",
        "开" if settings.get("proactive_enabled") else "关",
        "开" if settings.get("greet_enabled") else "关",
        "开" if settings.get("memory_enabled") else "关",
        memory.stats()["facts"],
        "开" if settings.get("summary_enabled") else "关",
        summaries.stats()["items"],
        "开" if settings.get("msgindex_enabled") else "关",
        msgindex.stats()["messages"],
        "开" if settings.get("persona_iter_enabled") else "关",
        config.now_stamp(),
    )
    # 记忆库后端与只读状态一并报出来：**后端换了**是排查时第一个要知道的事
    # （SQLite / JSON 的数据文件不同），只读则是"它答应记住却总是忘"的静默故障根因。
    logger.info(
        "记忆库后端：%s（配置值 %s）；排序证据：%d 条被想起过、%d 条被反复提到过",
        memory._db.backend_name(),  # noqa: SLF001 - 同包内诊断
        settings.get("memory_store"),
        memory.stats()["used_once"],
        memory.stats()["confirmed"],
    )
    # 人设信号账本：**只观察、不改人设**（自我迭代第 1 步）。
    # 启动时报一下条数，好知道它有没有在积累。
    _sig = signals.stats()
    logger.info(
        "人设信号账本：%s（%d 条，%d 类；只记账不改人设）",
        "开" if _sig["enabled"] else "关", _sig["items"], _sig["kinds"],
    )
    # 记忆库进入只读是**静默故障**：现象是「它答应记住却总是忘」。
    # 所以在启动日志里显式点名，别等人去翻日志才发现。
    if memory._db.readonly_reason:  # noqa: SLF001 - 同包内诊断
        logger.error(
            "⚠ 记忆库处于只读模式（原因：%s）—— 本次运行不会写入任何记忆。"
            "请检查 data/ 下的 *.corrupt-* 文件并处置后重启（后端=%s）",
            memory._db.readonly_reason,  # noqa: SLF001
            memory._db.backend_name(),  # noqa: SLF001
        )
    # 时间来源单独一条：宿主时钟漂了、或 NTP 没通，这里是唯一的线索。
    # 定时问候（到点问早/午/晚安）完全依赖这个钟，所以值得单独可见。
    _time_st = clock.status()
    logger.info(
        "时间来源：%s（偏移 %+.3f 秒；服务器 %s）",
        {
            "ok": "NTP 已校准" if _time_st["offset_seconds"] else "系统时钟（NTP 校验一致）",
            "pending": "系统时钟（还没同步）",
            "failed": "系统时钟（NTP 同步失败）",
            "disabled": "系统时钟（NTP 已关闭）",
        }.get(_time_st["status"], _time_st["status"]),
        _time_st["offset_seconds"],
        _time_st["server"] or "—",
    )
    if _time_st["status"] in ("failed",) or (
        _time_st["enabled"] and abs(_time_st["offset_seconds"]) > 60
    ):
        logger.warning(
            "时间可能不可靠！宿主时钟与真实时间相差 %.0f 秒 —— 定时问候会在错误的钟点触发。"
            "检查 _工具链\\诊断状态.py 或 /时间 校准。",
            abs(_time_st["offset_seconds"]),
        )
    # 表层人设的**播种**：先把镜像里的模板落到 data/（卷内），之后自我迭代写在那里。
    # **必须在下面的三层日志之前**，否则日志反映的是"还没播种"的状态。
    # 幂等：data/ 里已有就不动它 —— 这正是"自我学习不被重建覆盖"的保证。
    logger.info(config.seed_surface())
    # 人格三层：**每一层的字数与文件路径都要可见**。
    # 分层之后"它现在到底是什么性格"取决于三个文件，日志里只说一个来源是不够的；
    # 而且自动迭代会改表层，所以那一层的字数变化是最直观的"它有没有在学"的证据。
    _pst = persona.stats()
    logger.info(
        "人格三层：底层人设 %d 字（%s）｜禁止事项 %d 字/%d 条（%s）｜表层人设 %d 字（%s）",
        _pst["base_chars"], Path(_pst["base_file"]).name,
        _pst["forbidden_chars"], len(persona.forbidden_items()), Path(_pst["forbidden_file"]).name,
        _pst["surface_chars"], Path(_pst["surface_file"]).name,
    )
    _pit = persona_iter.stats()
    logger.info(
        "人格自我迭代：%s（每 %d 秒一次，最多 %d 条/次）｜至今写入 %d 条、被闸门拦下 %d 条",
        "开" if _pit["enabled"] else "关",
        _pit["interval"], _pit["max"], _pit["written_total"], _pit["rejected_total"],
    )
    if not _pst["base_chars"]:
        logger.warning(
            "底层人设文件是空的，当前用的是 AI_CHAT_SYSTEM_PROMPT（%s）。"
            "**注意：底层为空时自我迭代会拒绝运行** —— 没有约束就没有闸门的依据。",
            config.PERSONA_SOURCE,
        )
