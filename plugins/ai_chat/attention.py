"""注意力机制 + 对话租约：前者决定「要不要主动搭话」，后者保证「一连串问答不被打断」。

## 两件不同的事，别混在一起看

| | 注意力 value | 对话租约 lease |
|---|---|---|
| 解决什么 | 话题还在不在、值不值得**主动**接 | 刚才在跟**我**说话，接下来几句该继续回 |
| 本质 | **语义**判断（模型打相关性分） | **协议**判断（不调模型，看时间与轮数） |
| 花不花 token | 会（每条最多一次轻量调用） | **不会**（纯本地判断） |

**为什么必须有租约**：注意力只能让机器人在"旁听"时插话，它做不到"连续问答"。
原因是算术上的 —— 回复末尾会调 `relax()` 减半，所以刚被 @ 完的有效值是
`0.7 × 0.5 = 0.35`，而阈值是 0.8：

```
0.35  → 一条满分 → 0.35×0.5 + 0.55 = 0.725 < 0.80   ✗ 不接
0.725 → 再来一条满分 → 0.9125 ≥ 0.80                 ✓ 才接
```

**一问一答必定在第二轮断掉**，要连发两条高度相关的消息才会被重新接上。
这不是 bug，是"旁听机制被当对话机制用"的错配；因此单开一层租约，
按 AstrBot `SessionController` 的思路：**显式授予 + 超时 + 可主动结束**。

## 注意力本身的工作方式

被唤醒时把「当前话题」记下来，之后每条新消息让模型打个相关度分，
按 `value = value * DECAY + score * GAIN` 更新注意力值：

* score = 1.0（明显还在聊同一件事）→ 涨
* score ≈ 0.5（沾点边）→ 基本持平
* score = 0.0（换话题了）→ 掉

超过 `attention_threshold` 就自己接话（不用再 @）；接过一次后注意力减半，
还能继续跟，但不会没完没了。超过 `attention_window` 秒没动静则直接清零。

> **主约束是 `relax()` 减半，不是阈值抬升**：阈值最多升到 0.95，而 value 有
> `min(1.0, ·)` 封顶、满分连击能顶到 1.0，所以"越到后面一条满分也不够"并不成立。

跟另外三套触发的分工：

| 触发 | 依据 | 与内容有关吗 |
|---|---|---|
| 被 @ / 唤醒词 / **租约** | 有没有叫它（租约=刚叫过） | 无关 |
| 随机插话 | 概率 | 无关 |
| 主动发言 | 定时 / 消息数 | 无关 |
| **注意力（本模块）** | 新发言**跟当前话题的相关性** | **有关** |
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path


from . import config, llm, settings

logger = logging.getLogger("ai_chat.attention")


# 衰减模型的内置系数：不暴露成参数，够用且少几个旋钮。
#
# GAIN 取 0.55 而不是 0.6 是有讲究的：初始 0.7 时 score=1 得 0.90 稳过线；
# 而 relax() 减半后（0.45）再来一条满分只有 0.775，**必须连续两条高度相关**才会再触发。
# 若取 0.6，减半后 0.825 仍然过线，relax 就形同虚设，它会一直插话。
_DECAY = 0.5  # 每评估一次先衰减一半
_GAIN = 0.55  # 相关性满分时的增量
# 阈值随时间升高：刚被唤醒时最好接话，越接近活跃期末尾越挑。
# 0.15 意味着 10 分钟活跃期的末尾阈值会从 0.8 抬到 0.95。
#
# **与另两个参数耦合**：它的设计意图建立在 `attention_initial`(0.7) 与
# `attention_threshold`(0.8) 的默认值上。改那两个之前先看上面的算式 ——
# 这里没有回归守卫，改错了只表现为"它变得话多/话少"，不会报错。
_THRESHOLD_RISE = 0.15

# 对话租约的落盘文件（放 data/ 下，与 greet_state.json 同级）
_LEASE_FILE = "attention_state.json"


@dataclass
class _Focus:
    topic: str
    started_at: float
    # 实际值由 focus() 按 attention_initial 显式赋值，这里只是占位
    value: float = 0.0
    last_eval: float = 0.0
    evaluated: int = 0
    # 回复之后的静默截止时刻（与租约分工：这几句交给租约，注意力别插）
    muted_until: float = 0.0


@dataclass
class _Lease:
    """一次「刚跟它说过话」的租约：到期前该会话的追问不必再 @。"""

    who: str            # 授予时的发送者 uid
    expires_at: float
    turns: int = 0      # 已用掉几轮（超出 attention_lease_max_turns 就失效）


_focus: dict[str, _Focus] = {}
_lease: dict[str, _Lease] = {}
_lease_loaded = False


# ------------------------------------------------------------------ 状态操作
def focus(conv: str, topic: str) -> None:
    """被唤醒时记下话题，并把注意力拉到初始值。"""
    if not settings.get("attention_enabled"):
        return
    topic = " ".join(str(topic).split())[:300]
    if not topic:
        return
    now = time.time()
    initial = float(settings.get("attention_initial"))
    _focus[conv] = _Focus(topic=topic, started_at=now, last_eval=now, value=initial)
    logger.info("注意力聚焦 conv=%s（初始 %.2f）话题=%.40s", conv, initial, topic)


def relax(conv: str) -> None:
    """接过话之后把注意力减半，并**静默一段时间**。

    减半是为了"不会连续插话"；静默是为了**跟对话租约分工**：
    刚回完这一轮，接下来几句本来就该由租约接着（那是"在场的对话"），
    注意力不该再对同一件事插一句 —— 既省一次评估，也避免一段对话里出现两个声音。
    """
    item = _focus.get(conv)
    if item is None:
        return
    item.value *= 0.5
    item.last_eval = time.time()
    item.muted_until = time.time() + float(settings.get("attention_reply_cooldown"))
    logger.info(
        "注意力减半并静默 %.0fs conv=%s → %.2f",
        settings.get("attention_reply_cooldown"), conv, item.value,
    )


def clear(conv: str) -> None:
    _focus.pop(conv, None)


def state(conv: str) -> dict:
    item = _focus.get(conv)
    if item is None:
        return {}
    return {
        "topic": item.topic,
        "value": round(item.value, 3),
        "age_seconds": round(time.time() - item.started_at, 1),
        "evaluated": item.evaluated,
        "muted_seconds": round(max(0.0, item.muted_until - time.time()), 1),
    }


def _expired(item: _Focus) -> bool:
    return (time.time() - item.started_at) > int(settings.get("attention_window"))


def _progress(item: _Focus) -> float:
    """话题推进程度 0~1（0 = 刚唤醒，1 = 活跃期末尾）。"""
    window = max(1, int(settings.get("attention_window")))
    return min(1.0, max(0.0, (time.time() - item.started_at) / window))


def _effective_threshold(item: _Focus) -> float:
    """当前该用的阈值：随话题推进而升高。

    「最开始接话容易、话题越久越需要高相关性」就是靠这条实现的 ——
    不去动 GAIN，而是让门槛随时间抬上去：开头 0.8 就够，末尾得 0.95。
    """
    base = float(settings.get("attention_threshold"))
    return min(1.0, base + _THRESHOLD_RISE * _progress(item))


# ------------------------------------------------------------------ 相关性打分
async def score_relevance(topic: str, text: str) -> float:
    """让模型给「这条发言跟当前话题的相关度」打分 0~1。失败按无关处理。"""
    if not llm.api_key():
        return 0.0
    prompt = (
        "下面是群里正在聊的话题，以及随后出现的一条新发言。\n"
        "判断这条新发言跟话题的相关程度。\n\n"
        f"【话题】\n{topic}\n\n"
        f"【新发言】\n{text}\n\n"
        "评分标准：\n"
        "- 1.0 = 明显还在聊同一件事，甚至是在接着这个话题问或答\n"
        "- 0.5 = 沾点边，同一个大话题下的分支\n"
        "- 0.0 = 无关（换话题了、纯闲聊、纯表情）\n"
        '只输出 JSON，不要任何多余文字：{"score": 0.0}'
    )
    try:
        resp = await asyncio.wait_for(
            llm.chat(
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                # 推理模型会先思考，max_tokens 给小了 content 会是空的
                max_tokens=800,
            ),
            timeout=config.TIMEOUT,
        )
        raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        data = json.loads(raw)
        return max(0.0, min(1.0, float(data.get("score", 0.0))))
    except Exception:  # noqa: BLE001 - 打分失败不该影响群聊
        logger.info("相关性打分失败，按无关处理")
        return 0.0


async def should_speak(conv: str, text: str) -> tuple[bool, float]:
    """评估一条新消息，返回 (是否该主动接话, 当前注意力值)。"""
    if not settings.get("attention_enabled"):
        return False, 0.0

    item = _focus.get(conv)
    if item is None:
        return False, 0.0
    if _expired(item):
        logger.info("注意力超时退出 conv=%s（age=%.0fs）", conv, time.time() - item.started_at)
        clear(conv)
        return False, 0.0

    # 静默期：刚回完这一轮，接下来几句归**对话租约**管，注意力别插
    # （省一次评估，也避免一段对话里出现两个声音）
    if time.time() < item.muted_until:
        return False, item.value

    # 限流：每条消息都调模型太贵，两次评估之间至少隔 attention_interval 秒
    if time.time() - item.last_eval < int(settings.get("attention_interval")):
        return False, item.value
    if len(text.strip()) < int(settings.get("attention_min_chars")):
        return False, item.value
    if text.strip() == item.topic:
        return False, item.value

    # **评估次数上限**：光靠 interval × window 挡不住成本
    # （线上就出现过 interval=10 + window=600 = 最坏 60 次评估）。
    # 到顶就退出这次聚焦 —— 想再让它"在场"，重新叫它一次即可。
    cap = int(settings.get("attention_max_evals"))
    if cap > 0 and item.evaluated >= cap:
        logger.info(
            "注意力已评估 %d 次（上限 %d），结束本次聚焦 conv=%s",
            item.evaluated, cap, conv,
        )
        clear(conv)
        return False, 0.0

    item.last_eval = time.time()
    item.evaluated += 1
    score = await score_relevance(item.topic, text)
    item.value = min(1.0, item.value * _DECAY + score * _GAIN)  # 封顶 1.0
    threshold = _effective_threshold(item)
    logger.info(
        "注意力更新 conv=%s score=%.2f → value=%.2f（阈值已抬到 %.2f，话题过了 %.0f%%）",
        conv, score, item.value, threshold, _progress(item) * 100,
    )
    return item.value >= threshold, item.value


# ================================================================== 对话租约
#
# 设计要点（每一条都对应一个具体的翻车方式）：
#
# 1. **只对本人的消息生效**（默认）。否则群里任何人接着说话都会被当成"在跟它聊"，
#    等于把机器人变成话痨。主人例外，见 `_lease_ok_user`。
# 2. **只在群聊生效**。私聊本来就是"每条都算在跟它说话"，不需要租约。
# 3. **必须有正文才续期**。否则有人连发几个表情包就能让租约永久有效。
# 4. **有轮数上限**。只靠超时的话，一个持续聊两小时的人能让它一直回。
# 5. **落盘**。重启/重建容器后租约还在 —— 线上是常驻服务，重启不该断正在进行的问答。
# 6. **不调模型**。判定纯本地，所以它不会增加 token 成本，也不会被限流闸门拖慢。

def _lease_path() -> Path:
    return config.LOG_DIR / _LEASE_FILE


def _push_lease() -> None:
    """按开关决定是否写盘。关掉落盘时，租约退化为纯内存态（重启即失）。"""
    if settings.get("attention_lease_persist"):
        _save_lease()


def _save_lease() -> None:
    """把租约落盘。失败只记日志 —— 租约丢了只是少一层便利，不该影响回复。"""
    try:
        _lease_path().parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "lease": {
                conv: {"who": it.who, "expires_at": it.expires_at, "turns": it.turns}
                for conv, it in _lease.items()
            },
            "updated_at": time.time(),
        }
        tmp = _lease_path().with_name(_lease_path().name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(_lease_path())
    except OSError:
        logger.warning("对话租约写盘失败（不影响回复）")


def _load_lease() -> None:
    """启动时恢复租约；**过期的直接丢并记日志**（不静默）。"""
    global _lease_loaded
    _lease_loaded = True
    path = _lease_path()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("对话租约文件损坏，已忽略：%s", path)
        return
    if not isinstance(raw, dict):
        return
    now = time.time()
    kept = dropped = 0
    for conv, item in (raw.get("lease") or {}).items():
        try:
            lease = _Lease(
                who=str(item.get("who") or ""),
                expires_at=float(item.get("expires_at") or 0),
                turns=int(item.get("turns") or 0),
            )
        except (TypeError, ValueError):
            dropped += 1
            continue
        if lease.expires_at <= now:
            dropped += 1
            continue
        _lease[str(conv)] = lease
        kept += 1
    if kept or dropped:
        logger.info(
            "对话租约已从磁盘恢复：%d 个有效，%d 个已过期丢弃（文件 %s）",
            kept, dropped, path.name,
        )


def _ensure_lease_loaded() -> None:
    if not _lease_loaded:
        _load_lease()


def _lease_on() -> bool:
    return bool(settings.get("attention_lease_enabled"))


def _lease_ttl() -> float:
    return max(10.0, float(settings.get("attention_lease_seconds")))


def _lease_ok_user(lease: _Lease, uid: str, is_master: bool) -> bool:
    """这条消息算不算"在跟它继续聊"。

    * 默认**只认发起租约的那个人**——否则群里任何人接着说话都会被当成在跟它聊；
    * `attention_lease_anyone` 打开后谁都能接（适合"整个群一起问它"的场景）；
    * **主人始终例外**：不管租约是谁发的，主人都能接着聊下去。
    """
    if not lease.who:
        return False
    if str(uid) == lease.who:
        return True
    if is_master:
        return True
    return bool(settings.get("attention_lease_anyone"))


def lease_grant(conv: str, uid: str) -> None:
    """被 @ / 唤醒词叫到时授予租约 —— 之后这个会话的追问不用再 @。"""
    if not _lease_on():
        return
    _ensure_lease_loaded()
    ttl = _lease_ttl()
    _lease[conv] = _Lease(who=str(uid), expires_at=time.time() + ttl, turns=0)
    _push_lease()
    logger.info("对话租约已授予 conv=%s uid=%s 有效期 %.0fs", conv, uid, ttl)


def lease_check(conv: str, uid: str, *, is_master: bool, text: str) -> bool:
    """这条消息是否落在租约内。

    **不消耗轮数、不改状态** —— 真正的记账在回复成功后由 `lease_commit()` 做，
    否则"回复失败了但轮数已经扣掉"会让租约凭空少一轮。
    """
    if not _lease_on():
        return False
    _ensure_lease_loaded()
    lease = _lease.get(conv)
    if lease is None:
        return False
    if time.time() >= lease.expires_at:
        logger.info(
            "对话租约到期（该会话的追问将不再自动回应）conv=%s 用了 %d 轮", conv, lease.turns
        )
        _lease.pop(conv, None)
        _push_lease()
        return False
    if lease.turns >= int(settings.get("attention_lease_max_turns")):
        logger.info("对话租约已达轮数上限 %d，作废 conv=%s", lease.turns, conv)
        _lease.pop(conv, None)
        _push_lease()
        return False
    if not text.strip():
        return False
    if not _lease_ok_user(lease, uid, is_master):
        return False
    return True


def lease_peek(conv: str, uid: str, *, is_master: bool, text: str) -> bool:
    """只读版：给 NoneBot 的 `Rule` 用。

    **绝不能有副作用**。NoneBot2 的 rule 可能被评估多次（这也正是"唤醒概率要放到
    handler 里"的原因），所以这里不清理过期项、不改状态、不写盘 ——
    只回答"现在这条消息该不该进门"。真正的判定与清理由 handler 里的
    `lease_check()` 做。

    与 `lease_check` 的分工：**门用 peek，门内用 check。** 两边判据必须一致，
    所以共用 `_lease_ok_user` 和同一批参数。
    """
    if not _lease_on():
        return False
    if not str(text or "").strip():
        return False
    lease = _lease.get(conv)
    if lease is None:
        return False
    if time.time() >= lease.expires_at:
        return False
    if lease.turns >= int(settings.get("attention_lease_max_turns")):
        return False
    return _lease_ok_user(lease, uid, is_master)


def lease_commit(conv: str) -> int:
    """回复成功后记账并续期。返回已用轮数。"""
    lease = _lease.get(conv)
    if lease is None:
        return 0
    lease.turns += 1
    lease.expires_at = time.time() + _lease_ttl()   # 每一轮都重新计时
    _push_lease()
    return lease.turns


def clear_lease(conv: str) -> bool:
    """作废租约。返回**是否真的清掉了**一条（给测试与调用方做判据）。

    **必须先 `_ensure_lease_loaded()`**：`_lease` 是惰性从磁盘加载的，
    只清内存会让"仅存在于文件里的租约"在下次 `lease_check/peek` 时被重新载入 ——
    那样 `/对话 结束` 就会看起来"说了没用"。这个坑是在离线验证里踩出来的。
    """
    _ensure_lease_loaded()
    if _lease.pop(conv, None) is not None:
        _push_lease()
        logger.info("对话租约已作废 conv=%s", conv)
        return True
    return False


def lease_state(conv: str) -> dict:
    """给 `/对话` 与机制说明看的现状。"""
    lease = _lease.get(conv)
    if lease is None:
        return {}
    now = time.time()
    return {
        "who": lease.who,
        "turns": lease.turns,
        "left_seconds": max(0.0, round(lease.expires_at - now, 1)),
        "max_turns": int(settings.get("attention_lease_max_turns")),
    }

