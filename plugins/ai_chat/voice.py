"""显式语音指令：`/语音 <文本>` -> TTS -> 一条独立的 record 消息。

**范围（首版）**：

* 只认斜杠指令，不接普通回复 —— 普通回复走文字链路，避免每条消息都占 GPU；
* 只放行主人、`TTS_ALLOWED_USERS`、`TTS_ALLOWED_GROUPS`；不在名单里静默忽略；
* 合成失败**回退一句文字**说明，不重试、不补发全文；
* 语音只发一条 record，不与文字/图片混排。

为什么单独一个模块：`tts.py` 只管 HTTP 与音频校验，`__init__.py` 只负责把 matcher 接上，
录音、发送、记忆落盘的顺序都在这里，改一处不影响另一处。
"""

from __future__ import annotations

import asyncio
import base64
import logging

from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from . import chatlog, config, settings, tts, voices
from .tts import TTSError

logger = logging.getLogger("ai_chat.voice")

COMMANDS = ("/语音", "／语音")

# 普通回复随机语音时用它串行：同一时刻只允许一个合成任务，
# GPU 忙就直接让这轮走文字（**不排队**——排队会让回复延迟不可预测）。
_REPLY_LOCK = asyncio.Semaphore(1)


def match(event: MessageEvent) -> str | None:
    """命中指令时返回要朗读的文本（可能为空串），否则 None。"""
    text = event.get_plaintext().strip()
    for head in COMMANDS:
        if text.startswith(head):
            return text[len(head):].strip().lstrip("：:").strip()
    return None


def allowed(event: MessageEvent, *, is_master: bool) -> bool:
    if is_master:
        return True
    try:
        uid = int(event.user_id)
        gid = int(event.group_id) if getattr(event, "group_id", None) is not None else None
    except (TypeError, ValueError):
        return False
    if uid in config.TTS_ALLOWED_USERS:
        return True
    return gid is not None and gid in config.TTS_ALLOWED_GROUPS


async def handle(bot: Bot, event: MessageEvent, send, *, is_master: bool) -> None:  # noqa: ANN001
    """处理一条 `/语音`。`send` 是插件里的 `_send`（统一出口，返回 OneBot 结果）。"""
    max_chars = int(settings.get("tts_max_chars") or config.TTS_MAX_CHARS)
    text = match(event) or ""
    if not text:
        await send(bot, event, Message("用法：/语音 <要朗读的文字>（最多 %d 字）" % max_chars))
        return
    if not bool(settings.get("tts_enabled")):
        await send(bot, event, Message("语音功能没开：在控制台「语音」页打开，或把 .env 的 TTS_ENABLED 设成 true。"))
        return
    if not config.TTS_ENDPOINT:
        await send(bot, event, Message("语音服务没配：TTS_ENDPOINT 为空。"))
        return
    if not allowed(event, is_master=is_master):
        logger.info("语音指令被拒：uid=%s gid=%s 不在白名单", event.user_id, getattr(event, "group_id", None))
        return

    profile = voices.resolve()
    try:
        result = await tts.synthesize(
            text,
            endpoint=config.TTS_ENDPOINT,
            token=config.TTS_TOKEN,
            ref_audio=profile["ref_audio"] or config.TTS_REF_AUDIO,
            prompt_text=profile["prompt_text"] or config.TTS_PROMPT_TEXT,
            prompt_lang=profile["prompt_lang"] or config.TTS_PROMPT_LANG,
            text_lang=profile["text_lang"] or config.TTS_TEXT_LANG,
            gpt_weights=profile["gpt_weights"],
            sovits_weights=profile["sovits_weights"],
            connect_timeout=config.TTS_CONNECT_TIMEOUT,
            total_timeout=config.TTS_TOTAL_TIMEOUT,
            max_chars=max_chars,
            max_bytes=int(settings.get("tts_max_bytes") or config.TTS_MAX_BYTES),
            max_seconds=float(settings.get("tts_max_seconds") or config.TTS_MAX_SECONDS),
        )
    except TTSError as exc:
        # 失败不重试；回一句不带技术细节的话，细节在日志里。
        logger.warning("语音合成失败 kind=%s detail=%s", exc.kind, exc.detail[:300])
        reason = {
            "too-long": "这句太长，语音上限是 %d 字。" % max_chars,
            "timeout": "语音合成超时了，这次先用文字回你。",
            "network": "语音服务连不上，先发文字。",
        }.get(exc.kind, "语音没合成出来，这次先用文字回你。")
        await send(bot, event, Message(reason))
        return

    conv = chatlog.conversation_id(getattr(event, "group_id", None), event.user_id)
    # 先落盘再发送：与文字回复的顺序一致（回复先入库、再标已读）。
    # 语音本身不进聊天记录，只存"我说过这句"的文本，避免 Base64 污染记忆。
    # **这一步不能省**：不进 chatlog 的话，下一轮上下文里只有用户那句 `/语音 …`，
    # 模型会不知道自己刚才"说"过什么，容易重复或答非所问。
    _msg, _rolled = await chatlog.append_message(
        conv, int(bot.self_id), config.bot_name(), text,
        is_bot=True, bot_uid=int(bot.self_id),
    )
    try:
        await chatlog.mark_read_until(conv, int(event.message_id))
    except Exception:  # noqa: BLE001 - 标已读失败不该拦住发送
        logger.exception("语音指令标记已读失败 conv=%s", conv)

    segment = MessageSegment.record("base64://" + base64.b64encode(result.data).decode("ascii"))
    sent = await send(bot, event, Message(segment))
    logger.info(
        "语音已发送 conv=%s voice=%s chars=%d bytes=%d duration=%.2fs elapsed=%.2fs saved=%s message_id=%s",
        conv, profile["id"], len(text), len(result.data), result.duration, result.elapsed,
        bool(_msg), _message_id(sent),
    )
    return _message_id(sent)


def _message_id(result):  # noqa: ANN001, ANN202
    """OneBot 适配器返回的是 dict，不是带属性的对象 —— 两种都兼容。

    实测 2026-10-22：`send_private_msg` 返回 `{'message_id': …}`，
    直接 `getattr(result, "message_id")` 永远是 None（首条真实语音就踩到了）。
    """
    if isinstance(result, dict):
        return result.get("message_id")
    return getattr(result, "message_id", None)


# ---------------------------------------------------------------- 普通回复的随机语音
def speakable(text: str, max_chars: int) -> bool:
    """这段文本适合朗读吗？代码块、链接、表格、过长都回文字。"""
    t = (text or "").strip()
    if not t or len(t) > max_chars:
        return False
    if "```" in t or "http://" in t or "https://" in t:
        return False
    if (chr(10) + "|") in t or t.count(chr(10)) > 4:
        return False
    return True


async def as_reply(text: str, event: MessageEvent):
    """按当前语音包合成一条 record 消息；不满足条件/失败时返回 None（调用方回退文字）。

    与 `/语音` 不同：这是**bot 自己的随机选择**，不套用户白名单 ——
    频率由 `voice_reply_chance` 控制，任何人发来的消息都只是"可能被语音回复"。
    """
    if not bool(settings.get("tts_enabled")) or not config.TTS_ENDPOINT:
        return None
    max_chars = int(settings.get("tts_max_chars") or config.TTS_MAX_CHARS)
    if not speakable(text, max_chars):
        return None
    if _REPLY_LOCK.locked():
        logger.info("语音回复跳过：上一段还在合成")
        return None
    profile = voices.resolve()
    try:
        async with _REPLY_LOCK:
            result = await tts.synthesize(
                text,
                endpoint=config.TTS_ENDPOINT,
                token=config.TTS_TOKEN,
                ref_audio=profile["ref_audio"] or config.TTS_REF_AUDIO,
                prompt_text=profile["prompt_text"] or config.TTS_PROMPT_TEXT,
                prompt_lang=profile["prompt_lang"] or config.TTS_PROMPT_LANG,
                text_lang=profile["text_lang"] or config.TTS_TEXT_LANG,
                gpt_weights=profile["gpt_weights"],
                sovits_weights=profile["sovits_weights"],
                connect_timeout=config.TTS_CONNECT_TIMEOUT,
                total_timeout=config.TTS_TOTAL_TIMEOUT,
                max_chars=max_chars,
                max_bytes=int(settings.get("tts_max_bytes") or config.TTS_MAX_BYTES),
                max_seconds=float(settings.get("tts_max_seconds") or config.TTS_MAX_SECONDS),
            )
    except TTSError as exc:
        logger.warning("语音回复合成失败，回退文字 kind=%s detail=%s", exc.kind, exc.detail[:200])
        return None
    logger.info("回复改用语音 voice=%s chars=%d duration=%.2fs elapsed=%.2fs",
                profile["id"], len(text), result.duration, result.elapsed)
    return Message(MessageSegment.record("base64://" + base64.b64encode(result.data).decode("ascii")))
