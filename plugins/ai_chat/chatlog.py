"""群聊记录存储：消息落盘到 chatlog_<会话>.json，并维护已读 / 未读与会话切分。

三条设计要点（都是为了"不靠对话上下文记忆，而是靠聊天记录"这个前提）：

1. **每个会话一个文件**：群用 `chatlog_g<群号>.json`，私聊用 `chatlog_u<QQ号>.json`。
   避免单文件无限膨胀，也避免多群写入互相阻塞。

2. **会话按固定间隔切分，切分时归档未读**：
   两条消息间隔超过 `SESSION_GAP` 就算新的一轮；开新一轮时，把上一轮遗留的未读
   全部标成已读。这是省 token 的关键 —— 没人 @ 机器人的那些闲聊不会以"未读"
   身份被原样灌进模型，而是降级成可压缩的背景。

3. **已读背景按预算压缩**：从最新往回累加字符，装不下的更早记录直接丢弃。
   所以越早的聊天越先被舍弃，最近的事始终保留。

## 不丢数据：三条边界

上一条的「丢弃」是**只在当轮 prompt 里丢弃**，盘上仍是全量 —— 这点必须保持。
所以本模块有两条硬规矩：

| 规矩 | 为什么 |
|---|---|
| **`MAX_PER_GROUP` 不再真删** | 原来是 `del self.messages[:n]`。默认 0（不限）所以没触发，但**一旦有人配了就永久丢**。现在改成「归档」：挪进 `chatlog_<conv>.archive.jsonl`，索引与检索都还能查到 |
| **损坏的文件不改名不留证** | 原来 `load()` 失败就静默从空记录继续，下一次 `save()` 把损坏但可能可修的文件**原地覆盖**。现在先改名成 `.corrupt-<ts>` 并**拒绝写入**（只读运行），等人工处置 |

另外 `append()` 会顺手在返回值里标一个 `_session_rolled`：**会话切分是天然的时间轴锚点**，
摘要（`summaries.py`）与记忆抽取（`memory.py`）都靠它定位「刚结束的那一轮」，不需要另建索引。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from . import clock, config, settings

logger = logging.getLogger("ai_chat.chatlog")

# 会话键 -> GroupLog / 锁。并发写在群聊里是常态，必须串行化。
_logs: dict[str, "ConversationLog"] = {}
_locks: dict[str, asyncio.Lock] = {}


def conversation_id(group_id: int | None, user_id: int) -> str:
    """群聊用 `g<群号>`，私聊退化用 `u<QQ号>`。"""
    return f"g{group_id}" if group_id else f"u{user_id}"


def _path_for(conv: str) -> Path:
    return config.LOG_DIR / f"chatlog_{conv}.json"


def _archive_path_for(conv: str) -> Path:
    """溢出归档文件。用 `.jsonl`：一行一条，追加即可，不需要读回全量再重写。"""
    return config.LOG_DIR / f"chatlog_{conv}.archive.jsonl"


def _lock_for(conv: str) -> asyncio.Lock:
    lock = _locks.get(conv)
    if lock is None:
        lock = asyncio.Lock()
        _locks[conv] = lock
    return lock


class ConversationLog:
    """一个会话的完整聊天记录，常驻内存 + 变更即落盘。"""

    def __init__(self, conv: str, path: Path) -> None:
        self.conv = conv
        self.path = path
        self.messages: list[dict[str, Any]] = []
        self.next_id = 1
        self.session = 0
        # 损坏保护：load() 判定文件坏掉时置位，此后进程内**只读不写** ——
        # 宁可这一轮没有记录，也不能拿空库把「坏了但可能能修」的文件覆盖掉。
        self.broken_reason = ""
        # 已经挪进归档文件的消息 id，避免「归档 → 又被 load 回来 → 再归档」来回搬
        self.archived_ids: set[int] = set()

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        if self.broken_reason:
            return
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            self.broken_reason = f"{type(exc).__name__}"
            self._quarantine()
            return
        if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
            self.broken_reason = "结构不对（顶层不是带 messages 的对象）"
            self._quarantine()
            return
        self.messages = [m for m in (raw.get("messages") or []) if isinstance(m, dict)]
        for m in self.messages:
            m.pop("_rolled", None)  # 内部字段不留在内存对象里（也防旧文件带进来）
        self._load_archived_ids()
        self.next_id = int(raw.get("next_id", len(self.messages) + 1))
        self.session = int(raw.get("session", 0))
        if self.messages:
            highest = max(int(m.get("session", 1)) for m in self.messages)
            self.session = max(self.session, highest)
            self.next_id = max(self.next_id, max(int(m.get("id", 0)) for m in self.messages) + 1)

    def _quarantine(self) -> None:
        """把损坏的记录改名留证，并保持只读。

        **不改名就等于没有保护**：原来的写法是从空记录继续，下一次 `save()` 直接把
        损坏文件原地覆盖 —— 那份文件再也没了。改名之后它还能被人捞出来手工修。
        """
        try:
            bak = self.path.with_name(
                f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            self.path.replace(bak)
            logger.error(
                "聊天记录损坏（%s），已改名留证并进入只读：%s → %s",
                self.broken_reason, self.path.name, bak.name,
            )
        except OSError:
            logger.error(
                "聊天记录损坏（%s）且改名失败，仍进入只读：%s",
                self.broken_reason, self.path,
            )

    def _load_archived_ids(self) -> None:
        """扫一遍归档文件，记下已归档的 id。归档是追加式的，只读 id 字段。"""
        path = _archive_path_for(self.conv)
        if not path.exists():
            return
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("id") is not None:
                    try:
                        self.archived_ids.add(int(item["id"]))
                    except (TypeError, ValueError):
                        continue
        except OSError:
            logger.warning("读归档文件失败（不影响运行）：%s", path.name)

    def _archive_messages(self, items: list[dict[str, Any]]) -> None:
        """把一批消息**追加**进归档文件。失败不抛 —— 大不了这批留在内存里下轮再试。"""
        if not items:
            return
        path = _archive_path_for(self.conv)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                for item in items:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            for item in items:
                try:
                    self.archived_ids.add(int(item.get("id", 0)))
                except (TypeError, ValueError):
                    pass
            logger.info(
                "聊天记录超出上限，已归档（未删除）%d 条 → %s", len(items), path.name
            )
        except OSError:
            logger.warning("归档失败，这 %d 条仍保留在内存里：%s", len(items), path.name)

    def save(self) -> None:
        if self.broken_reason:
            return  # 只读模式：绝不覆盖那个已改名留证的文件
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "conv": self.conv,
            "session_gap_seconds": settings.get("session_gap"),
            "next_id": self.next_id,
            "session": self.session,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock.now())),
            "messages": self.messages,
        }
        text = json.dumps(
            payload,
            ensure_ascii=False,
            indent=1 if config.LOG_PRETTY else None,
            separators=None if config.LOG_PRETTY else (",", ":"),
        )
        # 先写临时文件再原子替换，避免写一半崩了把记录毁掉。
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ 写入
    def append(
        self,
        uid: int,
        name: str,
        text: str,
        is_bot: bool = False,
        bot_uid: int | None = None,
    ) -> dict[str, Any]:
        # 时间戳走校准后的时钟：记录里的"几点"与机器人回答的"几点"必须是同一个钟，
        # 否则 NTP 一校准，历史记录与当前时间就会差出那个偏移量。
        # 而下面的会话切分间隔用 `clock.raw()` 就够 —— 两个时间点都加同一偏移，差值不变。
        now = clock.now()
        wall = clock.raw()

        last_ts = float(self.messages[-1]["ts"]) if self.messages else None
        rolled = False
        if last_ts is not None and (wall - last_ts) > settings.get("session_gap"):
            self._archive_unread()  # 上一轮到此为止：未读降级为已读
            self.session += 1
            rolled = True  # ★ 会话边界的唯一标记：摘要与记忆抽取都靠它定位「刚结束的那一轮」
        if self.session <= 0:
            self.session = 1

        msg = {
            "id": self.next_id,
            "ts": round(now, 3),
            "time": time.strftime("%H:%M", time.localtime(now)),
            "uid": uid,
            "name": name,
            "text": text,
            "is_bot": bool(is_bot),
            # 存下"当时机器人的 QQ 号"。**光靠名字认自己是不够的**：
            # 名字会随人设更换而变（鲸鱼女孩 → 鲸鱼娘），历史记录里就认不出来了，
            # 于是模型会把机器人以前说过的话当成别人说的。
            "bot_uid": int(bot_uid) if (is_bot and bot_uid is not None) else None,
            "session": self.session,
            # 机器人自己的发言直接算已读，否则下次会被当成"新发言"再回应一遍。
            "read": bool(is_bot),
            # 私有用字段（下划线开头）：**不落盘**，只用来把「这一条正好跨了会话边界」
            # 这件事带给调用方。落盘的是上面那批稳定字段，格式不受影响。
            "_rolled": rolled,
        }
        self.next_id += 1
        self.messages.append(msg)

        if config.MAX_PER_GROUP > 0 and len(self.messages) > config.MAX_PER_GROUP:
            self._overflow_to_archive()
        return msg

    def take_rolled(self, msg: dict[str, Any]) -> bool:
        """读出并清掉 `_rolled` 标记。`save()` 前由调用方走一遍，保证不落盘。"""
        return bool(msg.pop("_rolled", False))

    def _overflow_to_archive(self) -> int:
        """超出 `MAX_PER_GROUP` 的最旧消息**挪进归档文件，不删除**。返回挪走几条。

        为什么不能像原来那样 `del self.messages[:n]`：
        「聊天记录」这件事上，删掉就是真没了 —— 而它恰恰是后面补记忆、翻旧账的唯一原料。
        归档文件是 `.jsonl`（一行一条，只追加），既不会把主文件撑大，也不会丢。
        """
        extra = len(self.messages) - config.MAX_PER_GROUP
        if extra <= 0:
            return 0
        moved: list[dict[str, Any]] = []
        keep: list[dict[str, Any]] = []
        for m in self.messages:
            try:
                seen = int(m.get("id", 0)) in self.archived_ids
            except (TypeError, ValueError):
                seen = False
            # 已经归档过的不再重复搬（正常路径不会出现，防脏数据）
            if len(moved) < extra and not seen:
                moved.append({k: v for k, v in m.items() if not k.startswith("_")})
            else:
                keep.append(m)
        if not moved:
            return 0
        self._archive_messages(moved)
        self.messages = keep
        return len(moved)

    def _archive_unread(self) -> int:
        n = 0
        for m in self.messages:
            if not m.get("read"):
                m["read"] = True
                n += 1
        return n

    def mark_read_until(self, msg_id: int) -> int:
        """只把 id <= msg_id 的未读标为已读。

        用 id 上界而不是"全部标已读"，是为了不误伤机器人回复期间新到的消息。
        """
        n = 0
        for m in self.messages:
            if int(m.get("id", 0)) <= msg_id and not m.get("read"):
                m["read"] = True
                n += 1
        return n

    def clear(self) -> None:
        self.messages.clear()
        self.next_id = 1
        self.session = 0

    # ------------------------------------------------------------ 读取
    def unread(self, exclude_id: int | None = None) -> list[dict[str, Any]]:
        return [
            m
            for m in self.messages
            if not m.get("read") and (exclude_id is None or int(m.get("id", 0)) != exclude_id)
        ]

    def last_from(self, uid: int) -> dict[str, Any] | None:
        """找该用户最近一条未读消息 —— 即"这次 @ 我的那条"。"""
        for m in reversed(self.messages):
            if not m.get("read") and int(m.get("uid", 0)) == uid:
                return m
        return None

    # ------------------------------------------------------------ 增量取数
    # 下面三个是**只读**接口，给「记忆抽取水位线」和「会话摘要」用的。
    # 它们不参与 prompt 渲染，所以加进来不会改变任何现有行为。
    def since_id(self, after_id: int, *, include_bot: bool = False,
                 limit: int = 0) -> list[dict[str, Any]]:
        """取 id 严格大于 `after_id` 的消息，按 id 升序。

        记忆抽取的水位线就靠它：之前只取「最近 14 条」，于是
        ① 一段没人 @ 机器人的长对话永远不会被提炼；
        ② 当天抽取额度用完的那批消息**永久不入库**。
        有了它就能「从上次抽到的地方接着抽」，额度不够也只是推迟，不是丢。
        """
        out: list[dict[str, Any]] = []
        for m in self.messages:
            try:
                mid = int(m.get("id", 0))
            except (TypeError, ValueError):
                continue
            if mid <= after_id:
                continue
            if not include_bot and m.get("is_bot"):
                continue
            out.append(m)
            if limit > 0 and len(out) >= limit:
                break
        return out  # messages 天然按 id 升序

    def max_id(self) -> int:
        """当前最大消息 id（没有消息时返回 0）。给水位线做「全抽完」判断用。"""
        best = 0
        for m in self.messages:
            try:
                best = max(best, int(m.get("id", 0)))
            except (TypeError, ValueError):
                continue
        return best

    def last_session_boundary_id(self, upto_id: int = 0) -> int:
        """返回「最后一次会话切分发生在哪条消息之后」 —— 也就是上一个 `_rolled` 的 id。

        记忆抽取用它避开**正在进行中的那一轮**：当前轮的上下文还在变化，
        由「水位线之后的一批」去提炼更准；后台滴取只处理已经定型的旧轮次。
        没有发生过切分就返回 0（表示从头都是同一轮）。
        """
        boundary = 0
        prev_session: int | None = None
        prev_msg: dict[str, Any] | None = None
        for m in self.messages:
            try:
                mid = int(m.get("id", 0))
            except (TypeError, ValueError):
                continue
            if upto_id and mid > upto_id:
                break
            cur = int(m.get("session", 0) or 0)
            if prev_session is not None and cur != prev_session and prev_msg is not None:
                # 会话号在这一条变了 → 上一条就是边界
                boundary = int(prev_msg.get("id", 0))
            prev_session = cur
            prev_msg = m
        return boundary

    def session_range(self, session: int, *, include_bot: bool = False,
                      newest_first: bool = False) -> list[dict[str, Any]]:
        """取某一轮会话（`session` 字段等于给定值）的消息。

        会话摘要用它定位「刚刚结束的那一轮」 —— 切分边界本来就写在每条消息的
        `session` 上，所以不需要为摘要另建索引。
        """
        out = [
            m
            for m in self.messages
            if int(m.get("session", 0) or 0) == int(session)
            and (include_bot or not m.get("is_bot"))
        ]
        if newest_first:
            out.reverse()
        return out

    def stats(self) -> dict[str, int]:
        total = len(self.messages)
        unread = sum(1 for m in self.messages if not m.get("read"))
        return {"total": total, "read": total - unread, "unread": unread, "session": self.session}

    # ------------------------------------------------------------ 渲染
    @staticmethod
    def clip_text(text: str, clip: int) -> str:
        flat = " ".join(str(text).split())
        if clip > 0 and len(flat) > clip:
            return flat[:clip] + "…"
        return flat

    def _speaker(self, m: dict[str, Any], bot_uid: str = "") -> str:
        """一行的发言人标注。**这是"分不清谁说的话"的正面解法。**

        三种身份在 prompt 里必须长得不一样，否则模型只能靠名字猜：

        | 谁 | 渲染成 | 为什么 |
        |---|---|---|
        | 机器人自己 | `鲸鱼娘（你）` | 明确不是别人说的，别回应自己 |
        | 主人 | `魔王（主人）` | 与 persona 里的「（主人）」呼应 |
        | 其他人 | `张三` | 原样 |
        | 不确定是不是自己 | `鲸鱼娘（可能是你）` | 老记录缺字段，宁可提醒也别当别人 |

        「自己」的判定按**可靠性从高到低**：

        1. `is_bot` 标记（写入时定的，最可靠）；
        2. `bot_uid` == 当前机器人 QQ；
        3. **`uid` == 当前机器人 QQ** —— `uid` 是每条都有的发送者 QQ，
           从第一版记录起就在，而 `bot_uid` 是后来才加的字段。
           少了这一条，**早期记录里机器人自己的发言会被渲染成"别人说的"** ——
           现象就是"它把自己说过的话认知到对话对象身上"。

        > 这里**不做"名字等于机器人显示名"的兜底**：改名前的记录对不上，
        > 而群里真有人叫同名时会把**别人的话标成自己的** —— 那是更危险的错。
        > 名字不可信，QQ 号才可信。
        """
        name = str(m.get("name", ""))
        if m.get("is_bot"):
            return f"{name}{config.BOT_SELF_LABEL}"
        stored_uid = m.get("bot_uid")
        if stored_uid is not None and bot_uid and str(stored_uid) == str(bot_uid):
            return f"{name}{config.BOT_SELF_LABEL}"
        # 权威兜底：发送者 QQ 就是机器人自己（老记录没写 bot_uid 也认得出）
        sender_uid = m.get("uid")
        if bot_uid and sender_uid is not None and str(sender_uid) == str(bot_uid):
            return f"{name}{config.BOT_SELF_LABEL}"
        if ConversationLog._is_master_uid(m.get("uid")):
            return f"{name}（{settings.get('master_title')}）"
        # 无法确定：没有 bot_uid、没有 is_bot,名字又和机器人显示名相同 ——
        # 可能是改名前的自己，也可能是同名群友。**显式标注**，让模型别当成确定的事实。
        if (not stored_uid and not m.get("is_bot")
                and name and name == config.bot_name()):
            return f"{name}（可能是你）"
        return name

    def _is_master_uid(uid: Any) -> bool:
        try:
            return int(uid) == int(settings.get("master_qq"))
        except (TypeError, ValueError):
            return False

    def stamp_for(self, m: dict[str, Any], now: float | None = None) -> str:
        """一行的**时间前缀**。这是"机器人读时间"最关键的一处。

        改造前只有 `[03:38]` 三个字。问题在于记录是**跨天累积**的：
        三天前 03:38 说的一句和刚刚说的，渲染出来一模一样，
        于是「上次说到哪」「多久之前说的」「昨天那事」全都无从判断 ——
        模型只能把它当成"刚发生的"。

        现在的规则（每条都为了某种真实问法）：

        | 消息距今 | 前缀 | 为什么 |
        |---|---|---|
        | 今天 | `03:38` | 当天的行不必报日期，报了就全是噪声 |
        | 昨天 | `昨天 03:38` | 「昨天那事」最常见的说法 |
        | 前天到 13 天内 | `3 天前 03:38` | 「前几天说过」 |
        | 更早 / 跨年 | `2026-09-12 03:38` | 天数太大就没意义了，给绝对日期 |

        另外**每天第一行**会带一次完整日期锚点（`2026-09-20 · 03:38`）——
        只在行内标"3 天前"的话，模型算不出那是几号，而人经常会问"那是几号来着"。
        """
        now = clock.now() if now is None else now
        try:
            when = float(m.get("ts") or 0)
        except (TypeError, ValueError):
            when = 0.0
        if when <= 0:  # 老记录可能没有 ts，退回原来只有时分的写法
            return str(m.get("time", ""))

        hhmm = str(m.get("time", "")) or time.strftime("%H:%M", time.localtime(when))
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        that_day = time.strftime("%Y-%m-%d", time.localtime(when))
        if that_day == today:
            return hhmm
        # 同一日历天内差 1 天 = 昨天
        day_gap = int(
            (time.mktime(time.strptime(today, "%Y-%m-%d"))
             - time.mktime(time.strptime(that_day, "%Y-%m-%d"))) // 86400
        )
        if day_gap == 1:
            return f"昨天 {hhmm}"
        if 1 < day_gap < 14:
            return f"{day_gap} 天前 {hhmm}"
        return f"{that_day} {hhmm}"

    def line(self, m: dict[str, Any], clip: int, bot_uid: str = "", now: float | None = None) -> str:
        return (
            f"[{self.stamp_for(m, now)} {self._speaker(m, bot_uid)}] "
            f"{self.clip_text(m.get('text', ''), clip)}"
        )

    def _with_day_anchors(
        self, lines: list[tuple[dict[str, Any], str]], now: float | None = None
    ) -> list[str]:
        """在每天的第一行前插一条日期锚点。

        行内写「3 天前」回答了"多久之前"，但回答不了"那是几号"。
        两个都给人经常会问，所以按天插锚点 —— 一天最多多花一行。
        """
        now = clock.now() if now is None else now
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        out: list[str] = []
        last_day = ""
        for m, line in lines:
            try:
                day = time.strftime("%Y-%m-%d", time.localtime(float(m.get("ts") or now)))
            except (TypeError, ValueError):
                day = ""
            if day and day != last_day:
                if day != today or last_day:  # 今天的第一条不插锚点（前缀已经够清楚）
                    out.append(f"—— {day} ——")
                last_day = day
            out.append(line)
        return out

    def render_background(
        self, budget: int | None = None, clip: int | None = None, bot_uid: str = ""
    ) -> str:
        """把已读记录压成背景文本：从最新往回累加预算，装不下就丢弃更早的。"""
        budget = settings.get("read_budget") if budget is None else budget
        clip = settings.get("msg_clip") if clip is None else clip

        read_msgs = [m for m in self.messages if m.get("read")]
        if not read_msgs:
            return ""

        now = clock.now()
        kept: list[tuple[dict[str, Any], str]] = []
        used = 0
        dropped = 0
        for m in reversed(read_msgs):
            line = self.line(m, clip, bot_uid, now)
            if budget > 0 and used + len(line) > budget and kept:
                dropped += 1
                continue
            kept.append((m, line))
            used += len(line) + 1
        kept.reverse()

        # 这句计数是「这一轮 prompt 里丢了多少」——**盘上一条都没删**。
        # 被丢弃的那段时间由 `summaries.py` 的会话摘要兜底（prompt 第 4.5 段），
        # 所以这里的措辞要点明"只是不在眼前"，免得模型把它理解成"那些事没发生过"。
        header = (
            f"（更早的 {dropped} 条原文没有放进本节，它们仍在记录里；"
            f"下面这几轮的摘要见 system 的「更早几轮的摘要」）\n"
            if dropped
            else ""
        )
        return header + "\n".join(self._with_day_anchors(kept, now))

    def render_unread(
        self,
        exclude_id: int | None = None,
        clip: int | None = None,
        bot_uid: str = "",
        skip_bot: bool = True,
    ) -> str:
        """未读发言。

        `skip_bot=True` 会跳过机器人自己写的行。「刚才的新发言」的含义是
        **别人说了什么**，把自己上一轮的回复混进去只会让它对着自己接话。
        （正常路径上机器人的发言本来就标了已读，这里是防脏数据 / 防旧版本的记录。）
        """
        clip = settings.get("msg_clip") if clip is None else clip
        now = clock.now()
        kept: list[tuple[dict[str, Any], str]] = []
        for m in self.unread(exclude_id):
            if skip_bot and m.get("is_bot"):
                continue
            kept.append((m, self.line(m, clip, bot_uid, now)))
        return "\n".join(self._with_day_anchors(kept, now))


# ---------------------------------------------------------------- 对外接口
async def get_log(conv: str) -> ConversationLog:
    log = _logs.get(conv)
    if log is None:
        log = ConversationLog(conv, _path_for(conv))
        await asyncio.to_thread(log.load)  # 文件 IO 不阻塞事件循环
        _logs[conv] = log
    return log


def get_log_sync(conv: str) -> ConversationLog | None:
    """同步取日志，**只在已经加载过时**返回；否则同步加载一次。

    给记忆抽取（`memory.drain_extraction`）用：它是在回复发完之后跑的异步任务，
    需要一个"从水位线往后"的只读切片。这里刻意不新建日志对象去走 to_thread ——
    抽取失败无所谓（返回空列表），但不该因为它产生磁盘竞争。

    真没加载过时同步读一次是安全的：调用方在事件循环里，一次几十 KB 的 JSON
    读盘（毫秒级）不会造成可感知的卡顿，而且只在进程启动后的第一次抽取发生。
    """
    log = _logs.get(conv)
    if log is not None:
        return log
    path = _path_for(conv)
    if not path.exists():
        return None
    log = ConversationLog(conv, path)
    try:
        log.load()
    except Exception:  # noqa: BLE001 - 读不动就当没有
        logger.info("同步读聊天记录失败 conv=%s", conv)
        return None
    _logs[conv] = log
    return log


def all_logs() -> dict[str, "ConversationLog"]:
    """当前已加载的会话（内存里那份）。**只读用途**：给后台滴取挑「有积压的会话」。

    刻意不返回「磁盘上有记录的每个会话」—— 那要把所有文件读一遍，而它每几十分钟
    才跑一次，读盘纯属浪费。已加载的会话覆盖了「最近活跃过的」，正好是抽取要关心的。
    """
    return dict(_logs)


async def append_message(
    conv: str,
    uid: int,
    name: str,
    text: str,
    is_bot: bool = False,
    bot_uid: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """落一条消息。返回 `(消息, 是否刚好跨了会话边界)`。

    为什么把「跨边界」也返回出去：会话切分是**天然的时间轴锚点**，
    摘要（`summaries.py`）需要在这一刻对「刚结束的那一轮」做一次归纳。
    调用方拿到 `True` 就可以顺手触发，不需要自己去比对时间戳。
    """
    log = await get_log(conv)
    async with _lock_for(conv):
        # 幂等去重：同一会话里**最后一条**与本次完全同源（uid + 时间点 + 正文）就不再落。
        #
        # 为什么需要它：写落盘有**两条路** —— `record_message`（收到自己的消息回灌时，
        # 带 `from_bot=True`）与 `_reply()`（回复发完主动 append）。正常部署下回灌不会发生，
        # 所以实测没有重复；但两条路各自防各自的边界，一旦部署形态变了
        # （换了适配器 / 多开一个客户端）同一条发言就会落两次，
        # 而重复的"自己说过的话"会让模型以为自己说过两遍。
        # 只在**最后一条**上比，不做全表扫描 —— 正常路径零开销。
        if log.messages:
            last = log.messages[-1]
            same = (
                int(last.get("uid", 0) or 0) == int(uid)
                and str(last.get("text", "")) == str(text)
                and bool(last.get("is_bot")) == bool(is_bot)
                and abs(float(last.get("ts", 0) or 0) - clock.now()) < 5.0
            )
            if same:
                logger.debug("跳过重复落盘（同源同文，5 秒内）conv=%s", conv)
                return dict(last), False
        msg = log.append(uid, name, text, is_bot, bot_uid=bot_uid)
        # 先取标记再落盘：`save()` 之后消息对象已经过序列化，内部字段一律不保留。
        rolled = log.take_rolled(msg)
        await asyncio.to_thread(log.save)
    return msg, rolled


async def mark_read_until(conv: str, msg_id: int) -> int:
    log = await get_log(conv)
    async with _lock_for(conv):
        n = log.mark_read_until(msg_id)
        if n:
            await asyncio.to_thread(log.save)
    return n


async def clear(conv: str) -> None:
    log = await get_log(conv)
    async with _lock_for(conv):
        log.clear()
        await asyncio.to_thread(log.save)
