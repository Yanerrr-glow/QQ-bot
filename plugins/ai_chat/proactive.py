"""主动发言：定时掷骰 + 按消息数触发两种模式，并且允许模型自己拒绝发言。

"主观性"体现在两点：
1. 掷骰决定"要不要说"（参数在 Web UI 里调）；
2. 即使掷中了，模型也可以回 `[SKIP]` 表示"没什么想说的" —— 不是掷中就必须开口。

限流手段（防止变成刷屏机器）：
* 只在"最近还有人说话"的群里开口（活跃窗口）；
* 两次主动发言之间有冷却；
* 每群每天有次数上限。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import MessageSegment
from openai import AsyncOpenAI

from . import chatlog, clock, config, context, mode, settings, stickers

logger = logging.getLogger("ai_chat.proactive")

_client = AsyncOpenAI(api_key=config.API_KEY or "sk-not-configured", base_url=config.BASE_URL)

SKIP_TOKEN = "[SKIP]"
_STATE_FILE = "proactive_state.json"


class _State:
    """每群的冷却时间与当日计数。

    落盘是因为"每天上限"如果重启就清零，等于没有上限。
    """

    def __init__(self) -> None:
        self.last_spoke: dict[str, float] = {}
        self.day: str = time.strftime("%Y-%m-%d", clock.localtime())
        self.day_count: dict[str, int] = {}
        self.msg_counter: dict[str, int] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return config.LOG_DIR / _STATE_FILE

    def load(self) -> None:
        self._loaded = True
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        self.last_spoke = {str(k): float(v) for k, v in (raw.get("last_spoke") or {}).items()}
        self.day = str(raw.get("day") or self.day)
        self.day_count = {str(k): int(v) for k, v in (raw.get("day_count") or {}).items()}
        self._rollover()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "day": self.day,
            "last_spoke": self.last_spoke,
            "day_count": self.day_count,
        }
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            logger.warning("主动发言状态写盘失败")

    def _rollover(self) -> None:
        today = time.strftime("%Y-%m-%d", clock.localtime())
        if today != self.day:
            self.day = today
            self.day_count.clear()

    def ensure(self) -> None:
        if not self._loaded:
            self.load()

    def budget_left(self, conv: str) -> bool:
        cap = int(settings.get("proactive_max_per_day"))
        return cap <= 0 or self.day_count.get(conv, 0) < cap

    def cooled_down(self, conv: str) -> bool:
        last = self.last_spoke.get(conv, 0.0)
        return (time.time() - last) >= int(settings.get("proactive_cooldown"))

    def record_speak(self, conv: str) -> None:
        self._rollover()
        self.last_spoke[conv] = time.time()
        self.day_count[conv] = self.day_count.get(conv, 0) + 1
        self.save()


_state = _State()
_last_tick = 0.0


# ---------------------------------------------------------------- 辅助
async def known_groups() -> list[int]:
    """从聊天记录文件名反推有哪些群聊过 —— 比查群列表更省事，也更稳。"""
    out: list[int] = []
    if not config.LOG_DIR.exists():
        return out
    for p in config.LOG_DIR.glob("chatlog_g*.json"):
        try:
            out.append(int(p.stem[len("chatlog_g"):]))
        except ValueError:
            continue
    return out


async def _compose(conv: str) -> str:
    """把该群最近的聊天记录交给模型，问它想不想插一句。"""
    log = await chatlog.get_log(conv)
    background = log.render_background()
    fresh = log.render_unread()

    parts: list[str] = []
    if background:
        parts.append("【群里之前聊过的（已读过）】\n" + background)
    if fresh:
        parts.append("【群里刚才说的】\n" + fresh)
    if not parts:
        return SKIP_TOKEN

    parts.append(
        "你刚看到了上面这些。以你的性格，想说一句就自然地说（一两句就够，"
        "像群里的人随口插话，不要像客服复述）；"
        f"如果没什么想说的、或者现在插话很突兀，就只回复 {SKIP_TOKEN}，不要输出任何别的内容。"
    )

    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=settings.get("model"),
                # 走统一组装：人设 + 运行时风格 + 时间 + 长期记忆都在里面。
                # 主动发言最怕"像个复述上文的客服"，而长期记忆正好给了它
                # "接着昨天那件事说一句"的素材。
                messages=context.build_simple(
                    conv=conv,
                    is_master=False,
                    body="\n\n".join(parts),
                ),
                # 推理模型会先思考再开口，token 留少了下场就是"想说却吐不出字"
                max_tokens=800,
            ),
            timeout=config.TIMEOUT,
        )
        return (resp.choices[0].message.content or "").strip() if resp.choices else ""
    except Exception:  # noqa: BLE001
        logger.exception("主动发言生成失败 conv=%s", conv)
        return ""


async def _maybe_sticker(conv: str = "") -> MessageSegment | None:
    """主动发言时配图也要贴题 —— 跟对话里走同一条"按语境挑、允许不发"的路径。"""
    if not settings.get("sticker_enabled"):
        return None
    if random.random() > float(settings.get("sticker_chance")):
        return None
    # 模式缩放：正聊着技术问题 / 在安慰人时主动配图更显得吵
    if conv and random.random() > mode.scale_for(conv, "sticker"):
        return None

    context = ""
    if conv:
        try:
            log = await chatlog.get_log(conv)
            context = "\n".join(
                x
                for x in (
                    log.render_background(budget=400, clip=100),
                    log.render_unread(clip=100),
                )
                if x
            )
        except Exception:  # noqa: BLE001 - 取不到语境就不配图
            logger.info("取不到语境，主动发言不配图 conv=%s", conv)

    segment, reason = await stickers.pick_for_context(context, conv=conv)
    if segment is None:
        logger.info("主动发言这次不配图 conv=%s（%s）", conv, reason)
    return segment


# ---------------------------------------------------------------- 发言
async def speak(group_id: int, trigger: str, force: bool = False) -> bool:
    """尝试在一个群里主动发言。返回是否真的说了。

    force=True 会忽略总开关 / 冷却 / 日上限，供控制台的「让它现在说一句」使用。
    """
    conv = f"g{group_id}"
    _state.ensure()

    if not force:
        if not settings.get("proactive_enabled"):
            return False
        if not _state.budget_left(conv):
            logger.debug("主动发言已达当日上限 conv=%s", conv)
            return False
        if not _state.cooled_down(conv):
            logger.debug("主动发言冷却中 conv=%s", conv)
            return False

    log = await chatlog.get_log(conv)
    if not log.messages:
        return False

    # 群里太久没人说话就别自作多情了（从控制台手动触发时忽略这条）
    if not force:
        idle = time.time() - float(log.messages[-1].get("ts", 0))
        if idle > int(settings.get("proactive_active_window")):
            logger.debug("群已静默 %.0f 秒，跳过主动发言 conv=%s", idle, conv)
            return False

    bots = get_bots()
    bot = next(iter(bots.values()), None)
    if bot is None:
        return False

    text = await _compose(conv)
    if not text or SKIP_TOKEN in text:
        logger.info("模型选择不发言（%s）conv=%s", trigger, conv)
        return False

    # 主动发言最多两段，避免长篇大论砸进群里
    chunks = [text] if len(text) <= 900 else [text[:900]]
    sent_any = False
    for chunk in chunks:
        try:
            await bot.send_group_msg(group_id=group_id, message=chunk)
            sent_any = True
        except Exception:
            logger.exception("主动发言发送失败 conv=%s", conv)
            return False

    if sent_any:
        _state.record_speak(conv)
        await chatlog.append_message(
            conv, int(bot.self_id), config.bot_name(), text, is_bot=True, bot_uid=int(bot.self_id)
        )
        # 配图必须在**文本落盘之后**再发、再记录。
        # 原来贴纸在 append_message 之前发，于是记录里"图和文本的先后"与实际相反；
        # 而且那张图压根不落盘 —— 群里说"这是你发的"时它无从核对（同 `_maybe_send_sticker`）。
        sticker = await _maybe_sticker(conv)
        if sticker is not None:
            try:
                await bot.send_group_msg(group_id=group_id, message=sticker)
                await stickers.record_sent_image(bot, conv)
            except Exception:
                logger.exception("主动发言配图发送失败 conv=%s", conv)
        # 插过话就算"看过了"，这些未读不必再回应；用上界标记不误伤新消息
        latest = max((int(m.get("id", 0)) for m in log.messages), default=0)
        if latest:
            await chatlog.mark_read_until(conv, latest)
        logger.info("主动发言已发出（%s）conv=%s", trigger, conv)
    return sent_any


# ---------------------------------------------------------------- 触发
async def on_group_message(conv: str) -> None:
    """按消息数触发：群友每累计 N 条消息掷一次骰。"""
    if not settings.get("proactive_enabled"):
        return
    _state.ensure()
    threshold = max(1, int(settings.get("proactive_msg_threshold")))
    _state.msg_counter[conv] = _state.msg_counter.get(conv, 0) + 1
    if _state.msg_counter[conv] < threshold:
        return
    _state.msg_counter[conv] = 0
    if random.random() > float(settings.get("proactive_msg_chance")):
        return
    if conv.startswith("g"):
        try:
            group_id = int(conv[1:])
        except ValueError:
            return
        await speak(group_id, "msg-count")


async def _tick() -> None:
    """按间隔触发：每个群各自掷一次骰。"""
    if not settings.get("proactive_enabled"):
        return
    chance = float(settings.get("proactive_chance"))
    if chance <= 0:
        return
    for group_id in await known_groups():
        if random.random() > chance:
            continue
        await speak(group_id, "interval")
        await asyncio.sleep(1)  # 多群同时掷中时错开，别一起发


async def loop() -> None:
    """常驻后台任务。每 30 秒看一次是否到了该掷骰的间隔。

    用固定 30 秒的轮询而不是 sleep(interval)，是为了让 UI 里改的间隔
    能在半分钟内生效，而不是等一个旧周期走完。
    """
    global _last_tick
    await asyncio.sleep(20)  # 等机器人连上 NapCat
    while True:
        try:
            now = time.time()
            if now - _last_tick >= int(settings.get("proactive_interval")):
                _last_tick = now
                await _tick()
        except Exception:  # noqa: BLE001 - 后台任务绝不能崩
            logger.exception("主动发言轮询出错")
        await asyncio.sleep(30)


def status() -> dict[str, Any]:
    _state.ensure()
    return {
        "day": _state.day,
        "last_spoke": {
            k: time.strftime("%H:%M:%S", clock.localtime(v))
            for k, v in _state.last_spoke.items()
        },
        "day_count": dict(_state.day_count),
        "msg_counter": dict(_state.msg_counter),
    }
