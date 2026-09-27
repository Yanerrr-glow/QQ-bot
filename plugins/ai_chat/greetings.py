"""定时问候：到点主动向主人问早 / 午 / 晚安。

跟 proactive.py 的分工（两者都会"主动开口"，但依据完全不同）：

| 模块 | 何时开口 | 说什么 |
|---|---|---|
| `proactive` | 定时掷骰 / 每累计 N 条消息 / 控制台手动 | 看群里语境自由发挥，允许回 `[SKIP]` 拒绝 |
| **`greetings`（本模块）** | **到配置的钟点** | **给主人的一句问候**，不看群聊内容 |

四条设计约束：

1. **一天一次，且落盘。** 状态写 `data/greet_state.json`。进程重启、容器重建都不会把
   今天的问候再发一遍 —— 定时任务最讨嫌的就是"每重启一次就多问一句早安"。
2. **只补发一个窗口内的。** 08:00 的早安在 08:00~09:30（`greet_window`）之间上线都会补，
   超过窗口就整天不补 —— 免得下午三点突然来一句「早安」。
3. **失败不无限重试。** 发送失败只记日志，等下一轮轮询再看，同一时段当天最多试
   `_MAX_ATTEMPTS` 次。掉线一小时不该变成"每 30 秒调一次模型"的烧钱循环。
4. **长时间离线后不补一串。** 一轮只发**最晚到期**的那一个时段：第二天才重启时，
   不会一口气把早安、午安、晚安全砸给主人。

风控是首要约束：宁可少发一句，也不要因为重试把自己刷成异常账号。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from openai import AsyncOpenAI

from . import chatlog, clock, config, context, settings

logger = logging.getLogger("ai_chat.greetings")

_client = AsyncOpenAI(api_key=config.API_KEY or "sk-not-configured", base_url=config.BASE_URL)

SKIP_TOKEN = "[SKIP]"
_STATE_FILE = "greet_state.json"
# 同一时段当天最多尝试几次（含发送失败）。见模块头第 3 条。
_MAX_ATTEMPTS = 3

# 时段表：(内部键, 显示名, 时间点参数, 兜底话术)
# 兜底话术只在没配 API Key 或模型调用失败时使用 —— 到点了总不能一个字都不说。
_SLOTS: tuple[tuple[str, str, str, str], ...] = (
    ("morning", "早安", "greet_morning", "早安，主人~新的一天也一起加油吧。"),
    ("noon", "午安", "greet_noon", "午安主人，记得吃口热乎的呀。"),
    # 【语域说明】这些兜底话术是**固定时间发出的固定文本**（只在没配 Key、
    # 或模型调用失败/超时时用），不是模型说的话 —— 所以它与铁律「不提时间，也不提睡眠」
    # **分属两个语域**：那条铁律管的是"模型即兴回复里别催睡"，管不到这里。
    # 正常情况下晚安是模型按下面的 ask 现写的（ask 里明确要求"别复述时间"）。
    # 这个区分登记在 persona_traits.json 的 `fixed_notice_channels` 里。
    ("night", "晚安", "greet_night", "晚安啦主人，今天也辛苦了，早点睡~"),
)
_LABEL: dict[str, str] = {slot: label for slot, label, _, _ in _SLOTS}
_FALLBACK: dict[str, str] = {slot: text for slot, _, _, text in _SLOTS}


def is_fixed_notice(slot: str, text: str) -> bool:
    """这句话是不是**固定系统文案**（而不是她即兴说的）。

    用途见 `greet()`：固定文案**不进聊天记录**。
    判据就是"文本与这个时段的兜底话术逐字相同" —— 简单、可测、不依赖调用路径
    （不管是因为没配 Key 还是调用失败，只要落回兜底就成立）。
    """
    return str(text or "").strip() == str(_FALLBACK.get(slot) or "").strip()


def label_of(slot: str) -> str:
    """时段显示名（"早安" / "午安" / "晚安"），未知时段返回空串。"""
    return _LABEL.get(slot, "")


def parse_hm(text: object) -> tuple[int, int] | None:
    """解析时间点："08:00" / "8:00" / "08：00" / "0800" 都认；空或非法返回 None（= 该时段关闭）。"""
    raw = str(text or "").strip().replace("：", ":")
    if not raw:
        return None
    if ":" in raw:
        hh, _, mm = raw.partition(":")
    elif len(raw) == 4 and raw.isdigit():
        hh, mm = raw[:2], raw[2:]
    else:
        return None
    hh, mm = hh.strip(), mm.strip()
    if not (hh.isdigit() and mm.isdigit()):
        return None
    hour, minute = int(hh), int(mm)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


class _State:
    """每个时段最后一次发送的日期与当天尝试次数。落盘，重启不清零。"""

    def __init__(self) -> None:
        self.sent: dict[str, str] = {}
        self.attempts: dict[str, int] = {}
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
            logger.warning("问候状态文件损坏，已忽略：%s", self.path)
            return
        if not isinstance(raw, dict):
            return
        self.sent = {str(k): str(v) for k, v in (raw.get("sent") or {}).items()}
        self.attempts = {str(k): int(v) for k, v in (raw.get("attempts") or {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sent": self.sent,
            "attempts": self.attempts,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock.now())),
        }
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            logger.warning("问候状态写盘失败")

    def ensure(self) -> None:
        if not self._loaded:
            self.load()

    def sent_today(self, slot: str, day: str) -> bool:
        self.ensure()
        return self.sent.get(slot) == day

    def tries(self, slot: str, day: str) -> int:
        self.ensure()
        return int(self.attempts.get(f"{slot}:{day}", 0))

    def mark(self, slot: str, day: str) -> None:
        self.ensure()
        self.sent[slot] = day
        self._prune(day)
        self.save()

    def count_try(self, slot: str, day: str) -> int:
        """记一次尝试（成功发送也算），返回累计次数。"""
        self.ensure()
        key = f"{slot}:{day}"
        self.attempts[key] = self.attempts.get(key, 0) + 1
        self._prune(day)
        self.save()
        return self.attempts[key]

    def _prune(self, day: str) -> None:
        """只留今天的尝试计数，免得文件一年后长成一本流水账。"""
        for key in [k for k in self.attempts if not k.endswith(f":{day}")]:
            self.attempts.pop(key, None)
        self.sent = {k: v for k, v in self.sent.items() if k in _LABEL}

    def status(self) -> dict[str, Any]:
        self.ensure()
        return {"sent": dict(self.sent), "attempts": dict(self.attempts)}


_state = _State()


# ------------------------------------------------------------------ 判定
def due_slots(now: float | None = None) -> list[str]:
    """此刻该发、且今天还没发的时段（按时间点从早到晚）。

    纯判定，不调模型、不发送 —— 所以离线测试能直接喂时间戳进来验证。
    """
    if not settings.get("greet_enabled"):
        return []
    moment = clock.now() if now is None else now
    lt = time.localtime(moment)
    day = time.strftime("%Y-%m-%d", lt)
    minutes_now = lt.tm_hour * 60 + lt.tm_min
    window = max(0, int(settings.get("greet_window")))

    out: list[str] = []
    for slot, _label, key, _fallback in _SLOTS:
        hm = parse_hm(settings.get(key))
        if hm is None:  # 留空 = 该时段不发
            continue
        start = hm[0] * 60 + hm[1]
        if minutes_now < start or minutes_now > start + window:
            continue
        if _state.sent_today(slot, day):
            continue
        if _state.tries(slot, day) >= _MAX_ATTEMPTS:
            logger.debug("时段 %s 今天已尝试 %d 次，放弃", slot, _MAX_ATTEMPTS)
            continue
        out.append(slot)
    return out


# ------------------------------------------------------------------ 生成
async def compose(slot: str, now: float | None = None) -> str:
    """让模型按人设写一句问候。没配 Key、调用失败或超时都退回内置话术。"""
    label = _LABEL.get(slot, "问候")
    fallback = _FALLBACK.get(slot, "主人好呀。")
    if not config.API_KEY:
        logger.info("未配置 API Key，问候改用内置话术 slot=%s", slot)
        return fallback

    stamp, weekday, period = config.time_parts(now)
    skip_hint = ""
    if settings.get("greet_allow_skip"):
        skip_hint = f"\n不想说、或此刻说{label}很别扭，就只回 {SKIP_TOKEN}，不要输出别的内容。"

    ask = (
        f"现在是 {stamp}（{weekday}，{period}）。\n"
        f"到点了，该你主动跟主人说一句「{label}」。\n"
        "要求：一到两句，像随手发出去的那种；别客套、别复述时间、别解释你为什么发这条。"
        f"{skip_hint}"
    )

    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=settings.get("model"),
                # 问候也要带上"近期发生过什么、你知道主人什么" ——
                # 不然早晚安就是两句可以互换的空话，跟改造前一样没有"熟人感"。
                messages=context.build_simple(
                    conv="greet",
                    is_master=True,
                    body=ask,
                ),
                # 推理模型会先思考再开口，max_tokens 给小了 content 直接是空的
                max_tokens=800,
            ),
            timeout=config.TIMEOUT,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    except Exception:  # noqa: BLE001 - 生成失败不该让定时任务崩掉
        logger.exception("问候生成失败 slot=%s（改用内置话术）", slot)
        return fallback

    if not text:
        logger.info("问候生成为空 slot=%s（改用内置话术）", slot)
        return fallback

    limit = int(settings.get("greet_max_chars"))
    return text[:limit]


# ------------------------------------------------------------------ 发送
async def _groups_with_master(master: int) -> list[int]:
    """主人说过话的群，按他最后一次发言时间从新到旧。"""
    found: list[tuple[float, int]] = []
    if not config.LOG_DIR.exists():
        return []
    for path in config.LOG_DIR.glob("chatlog_g*.json"):
        try:
            group_id = int(path.stem[len("chatlog_g"):])
        except ValueError:
            continue
        try:
            log = await chatlog.get_log(f"g{group_id}")
        except Exception:  # noqa: BLE001 - 单个记录读不动不能拖垮问候
            logger.exception("读取群记录失败 conv=g%s", group_id)
            continue
        hits = [m for m in log.messages if int(m.get("uid", 0)) == master]
        if hits:
            found.append((float(hits[-1].get("ts", 0)), group_id))
    found.sort(reverse=True)
    return [group_id for _, group_id in found]


async def _send_private(bot, master: int, text: str) -> bool:  # noqa: ANN001
    try:
        await bot.send_private_msg(user_id=master, message=text)
        return True
    except Exception:  # noqa: BLE001 - 不是好友 / 被限流都走这里
        logger.info("私聊问候没发出去（可能不是好友），准备换通道 master=%s", master)
        return False


async def _send_group(bot, group_id: int, master: int, text: str) -> bool:  # noqa: ANN001
    try:
        # @ 上主人：群里不 @ 的话他多半收不到提醒，还会以为它在跟别人说话
        await bot.send_group_msg(
            group_id=group_id,
            message=Message([MessageSegment.at(master), MessageSegment.text(" " + text)]),
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception("群内问候发送失败 group=%s", group_id)
        return False


async def deliver(text: str) -> tuple[bool, str]:
    """按 `greet_target` 把问候送出去，返回 (是否发出, 落在哪个会话)。

    会话键一并返回，是为了把这条问候写进对应的聊天记录 —— 主人接着回一句
    「早啊」时，它得知道自己在说什么，不然就成了答非所问。
    """
    try:
        master = int(settings.get("master_qq"))
    except (TypeError, ValueError):
        logger.warning("主人 QQ 未配置，问候无处可发")
        return False, ""

    bots = get_bots()
    bot = next(iter(bots.values()), None)
    if bot is None:
        logger.info("问候跳过：机器人还没连上 NapCat")
        return False, ""

    mode = str(settings.get("greet_target") or "auto")
    try:
        configured_group = int(settings.get("greet_group") or 0)
    except (TypeError, ValueError):
        configured_group = 0

    if mode == "private":
        ok = await _send_private(bot, master, text)
        return (ok, f"u{master}") if ok else (False, "")

    if mode == "group":
        group_id = configured_group or next(iter(await _groups_with_master(master)), 0)
        if not group_id:
            logger.info("问候跳过：没有可用的群（greet_group=0 且主人没在任何群说过话）")
            return False, ""
        ok = await _send_group(bot, group_id, master, text)
        return (ok, f"g{group_id}") if ok else (False, "")

    # auto：先私聊（最不打扰），发不出去再退到群里 @ 他
    if await _send_private(bot, master, text):
        return True, f"u{master}"
    group_id = configured_group or next(iter(await _groups_with_master(master)), 0)
    if not group_id:
        logger.warning("私聊失败且找不到可用的群，问候未发出")
        return False, ""
    ok = await _send_group(bot, group_id, master, text)
    return (ok, f"g{group_id}") if ok else (False, "")


async def greet(slot: str, force: bool = False) -> bool:
    """发一条问候，返回是否真的发出去了。

    force=True 供控制台的「现在问候一次」使用：绕开时间点与开关，
    也**不写**"今天已发"状态 —— 手动触发多半是在试效果，不该吃掉当天的自动问候。
    """
    if slot not in _LABEL:
        logger.warning("未知的问候时段：%s", slot)
        return False

    day = time.strftime("%Y-%m-%d", clock.localtime())
    if not force:
        if not settings.get("greet_enabled"):
            return False
        _state.count_try(slot, day)

    text = await compose(slot)
    if settings.get("greet_allow_skip") and SKIP_TOKEN in text:
        # 模型自己选择不说。记成"今天已处理"，否则每 30 秒都会再问一次模型。
        logger.info("模型选择不发这条问候 slot=%s", slot)
        if not force:
            _state.mark(slot, day)
        return False

    ok, conv = await deliver(text)
    if not ok:
        logger.warning("问候发送失败 slot=%s（当天已尝试 %d 次）", slot, _state.tries(slot, day))
        return False

    if not force:
        _state.mark(slot, day)
    if conv:
        if is_fixed_notice(slot, text):
            # 【判定】**固定系统文案一律不进聊天记录** —— 与 MSG_TIMEOUT /
            # MSG_ERROR / MSG_EMPTY 同口径（那三处在 append 之前就 return 了）。
            #
            # 为什么：`chatlog.render_background()` 取的是"所有已读消息"，**不过滤机器人自己**
            # （只有 `render_unread(skip_bot=True)` 才排除）。而聊天记录的已读部分每轮都会
            # 回到 prompt 里 —— 把兜底话术记进去，就等于每轮都在给她示范"我平时这么说话"。
            # 这恰恰是 `behavior.py` 里量出"写规则无效"的那个成因。
            #
            # 依据：这些文案属于"固定系统文案"语域（persona_traits.json 的
            # fixed_notice_channels）—— 非人格语域的东西，**既不被铁律评判，
            # 也不构成人格的自我陈述**。
            logger.info("固定文案不写进聊天记录 slot=%s：%s", slot, text[:40])
        else:
            try:
                bot = next(iter(get_bots().values()), None)
                if bot is not None:
                    await chatlog.append_message(
                        conv, int(bot.self_id), config.bot_name(), text, is_bot=True
                    )
            except Exception:  # noqa: BLE001 - 写记录失败不影响已经发出的问候
                logger.exception("问候写进聊天记录失败 conv=%s", conv)
    logger.info("问候已发出（%s%s）conv=%s：%s", _LABEL[slot], "·手动" if force else "", conv, text)
    return True


def status() -> dict[str, Any]:
    _state.ensure()
    return {
        "enabled": bool(settings.get("greet_enabled")),
        "times": {label: settings.get(key) for _, label, key, _ in _SLOTS},
        "target": settings.get("greet_target"),
        "window_minutes": settings.get("greet_window"),
        "due_now": due_slots(),
        "today": time.strftime("%Y-%m-%d", clock.localtime()),
        **_state.status(),
    }


def current_slot(now: float | None = None) -> str:
    """按当前钟点挑一个时段，供控制台"现在问候一次"默认用。

    规则：早于 11 点算早安，早于 18 点算午安，其余算晚安 —— 只影响手动触发的默认选择，
    真正的定时发送仍以 `greet_morning/noon/night` 三个时间点为准。
    """
    hour = clock.localtime(clock.now() if now is None else now).tm_hour
    if hour < 11:
        return "morning"
    if hour < 18:
        return "noon"
    return "night"


# ------------------------------------------------------------------ 轮询
async def loop() -> None:
    """常驻后台任务：每 30 秒看一次到点没有。

    用固定 30 秒轮询而不是 sleep 到下一个时间点，是为了让控制台里改的时间点
    能在半分钟内生效，也不用处理"系统时间被调整"这类边缘情况。
    """
    await asyncio.sleep(25)  # 等机器人连上 NapCat
    logger.info(
        "定时问候任务已启动（开关=%s，早安=%s 午安=%s 晚安=%s，现在是 %s）",
        "开" if settings.get("greet_enabled") else "关",
        settings.get("greet_morning"),
        settings.get("greet_noon"),
        settings.get("greet_night"),
        config.now_stamp(),
    )
    while True:
        try:
            pending = due_slots()
            if pending:
                # 只发最晚到期的那个：长时间离线后（比如第二天才重启）
                # 不要一口气把早安、午安、晚安全补上。
                if len(pending) > 1:
                    logger.info("有 %d 个时段同时到期，只补发最晚的 %s", len(pending), pending[-1])
                await greet(pending[-1])
        except Exception:  # noqa: BLE001 - 后台任务绝不能崩
            logger.exception("定时问候轮询出错")
        await asyncio.sleep(30)
