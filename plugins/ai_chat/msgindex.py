"""聊天记录索引：把「盘上那份全量记录」变成**能翻的旧账**。

## 为什么需要它

`chatlog` 一直把每句话都留在盘上，但除了 `render_background()` 那一小段，
**没有任何路径能把旧记录捞回 prompt**（README §7.4 自述过这件事）。
于是「你上周是不是说过…」这类问题它只能靠长期记忆里那几条提炼过的事实回答，
而「原话怎么说的」永远答不出来。

这个模块只做一件事：给 `chatlog_<会话>.json` 建**倒排索引**，让「翻旧账」可查。

## 三个刻意的设计

1. **索引是派生物，不是数据源**。任何时刻删掉 `memory.db` 里的索引表都能从
   `chatlog_*.json` + `.archive.jsonl` 重建。所以它坏掉/落后都不算事故。
2. **用字符 2-gram，不上分词器**。跟 `memory.py` 的检索同一套取舍：
   中文不引入 jieba 这类依赖，词面命中已经够用，跨语言的同义交给以后的可选 embedding。
3. **归档文件一起进索引**。`chatlog_*.archive.jsonl` 里是超上限被挪走的老消息 ——
   把"翻旧账"翻不到它们，等于刚刚白做了归档。

## 容量与代价

一条消息的索引 = 它的 2-gram 集合。中文短消息通常十几个 gram，
按每条 ~200 字节估，**1 万条约 2 MB**，SQLite 完全放得下。
写入是增量、批量的（`index_pending` 一次一批），不会跟着每条消息同步落盘。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import chatlog, config, settings

logger = logging.getLogger("ai_chat.msgindex")

# 索引表（与记忆共用一个 db，但**与记忆业务无关**：删掉它只影响"翻旧账"）
_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages_index (
    conv     TEXT    NOT NULL,
    msg_id   INTEGER NOT NULL,
    ts       REAL    NOT NULL DEFAULT 0,
    uid      INTEGER,
    name     TEXT    NOT NULL DEFAULT '',
    text     TEXT    NOT NULL,
    is_bot   INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (conv, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msgs_ts ON messages_index(ts);
CREATE INDEX IF NOT EXISTS idx_msgs_conv ON messages_index(conv);

CREATE TABLE IF NOT EXISTS message_grams (
    gram     TEXT    NOT NULL,
    conv     TEXT    NOT NULL,
    msg_id   INTEGER NOT NULL,
    PRIMARY KEY (gram, conv, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_grams_gram ON message_grams(gram);
"""

_conn: sqlite3.Connection | None = None
_indexed_upto: dict[str, int] = {}   # 每个会话已索引到哪条（内存缓存，权威值在库里）
_loaded = False

# 单条消息最多取多少个 gram（长消息截断）：防一条超长文本把索引撑爆
_MAX_GRAMS = 120
_MAX_TEXT = 500


# --------------------------------------------------------------------- 连接
def _db_path() -> Path:
    return config.LOG_DIR / "memory.db"


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), timeout=10)
        _conn.row_factory = sqlite3.Row
        try:
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            pass
        _conn = None


def available() -> bool:
    """索引能不能用（打不开就返回 False，调用方降级为"没索引"）。"""
    try:
        _connect()
        return True
    except sqlite3.Error:
        logger.warning("消息索引不可用（打不开 %s）", _db_path())
        return False


# --------------------------------------------------------------------- 分词
def grams(text: str) -> set[str]:
    """字符 2-gram + 拉丁词。与 `memory._bigrams` 同一套取舍（不引分词器）。"""
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
    if len(out) > _MAX_GRAMS:
        # 超长消息：保留前 _MAX_GRAMS 个（set 无序，但只影响"长文的尾部词搜不到"，可接受）
        out = set(list(out)[:_MAX_GRAMS])
    return out


# --------------------------------------------------------------------- 写入
def _indexed_max(conv: str) -> int:
    if conv not in _indexed_upto:
        try:
            row = _connect().execute(
                "SELECT MAX(msg_id) AS m FROM messages_index WHERE conv=?", (conv,)
            ).fetchone()
            _indexed_upto[conv] = int(row["m"] or 0) if row else 0
        except sqlite3.Error:
            _indexed_upto[conv] = 0
    return _indexed_upto[conv]


def _insert_rows(rows: list[tuple], grams_rows: list[tuple]) -> None:
    conn = _connect()
    conn.executemany(
        "INSERT INTO messages_index(conv,msg_id,ts,uid,name,text,is_bot,archived) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(conv,msg_id) DO UPDATE SET text=excluded.text, ts=excluded.ts",
        rows,
    )
    conn.executemany(
        "INSERT OR IGNORE INTO message_grams(gram,conv,msg_id) VALUES(?,?,?)", grams_rows
    )
    conn.commit()


def _rows_for(conv: str, messages: list[dict[str, Any]], *, archived: bool) -> tuple[list, list]:
    rows: list[tuple] = []
    grams_rows: list[tuple] = []
    for msg in messages:
        text = chatlog.ConversationLog.clip_text(msg.get("text", ""), _MAX_TEXT)
        if not text:
            continue
        try:
            mid = int(msg.get("id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if mid <= 0:
            continue
        rows.append(
            (
                conv, mid, float(msg.get("ts", 0) or 0),
                int(msg["uid"]) if msg.get("uid") not in (None, "") else None,
                str(msg.get("name", "") or ""), text,
                1 if msg.get("is_bot") else 0, 1 if archived else 0,
            )
        )
        for gram in grams(text):
            grams_rows.append((gram, conv, mid))
    return rows, grams_rows


def index_conv(conv: str, *, batch: int = 400, include_archived: bool = True) -> int:
    """把一个会话「还没进索引」的消息补进去。返回本次索引条数。"""
    log = chatlog.get_log_sync(conv)
    if log is None:
        return 0
    upto = _indexed_max(conv)
    todo = [m for m in log.messages if int(m.get("id", 0) or 0) > upto]
    if not todo:
        if include_archived:
            _index_archive(conv)
        return 0
    todo = todo[:batch]
    rows, grams_rows = _rows_for(conv, todo, archived=False)
    if rows:
        _insert_rows(rows, grams_rows)
        _indexed_upto[conv] = max(int(r[1]) for r in rows)
    if include_archived:
        _index_archive(conv)
    return len(rows)


def _index_archive(conv: str) -> int:
    """把归档文件（超上限被挪走的旧消息）也纳入索引。幂等（按 conv+msg_id 覆盖）。"""
    path = config.LOG_DIR / f"chatlog_{conv}.archive.jsonl"
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    messages: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("text"):
            messages.append(item)
    if not messages:
        return 0
    rows, grams_rows = _rows_for(conv, messages, archived=True)
    if rows:
        _insert_rows(rows, grams_rows)
    return len(rows)


def index_all(*, batch: int = 400) -> dict[str, int]:
    """把所有**已加载**的会话补进索引。给后台循环用。"""
    out: dict[str, int] = {}
    for conv in list(chatlog.all_logs().keys()):
        try:
            n = index_conv(conv, batch=batch)
        except sqlite3.Error:
            logger.exception("索引会话失败 conv=%s", conv)
            continue
        if n:
            out[conv] = n
    return out


def _conv_from_path(path: Path) -> str:
    return path.stem[len("chatlog_"):]


def backfill_from_disk(*, max_convs: int = 0, batch: int = 2000) -> dict[str, int]:
    """**从磁盘**把还没索引过的会话补进来。

    为什么必须有这一步：`index_all()` 只处理"已经在内存里加载过的会话"
    （`chatlog.all_logs()`），而进程刚启动时一个都没加载 ——
    于是**重启之后，旧记录永远不会被索引**，翻旧账在旧聊天上完全无效。
    实测就是这么发现的：索引表建好了，行数是 0。

    这里主动按文件名加载（`chatlog.get_log` 走的是异步加载路径，
    用 `get_log_sync` 一次性读入即可），代价是一次启动时的顺序读盘 ——
    实测单群 170 KB、4 个会话毫秒级完成。
    """
    changed: dict[str, int] = {}
    files = sorted(config.LOG_DIR.glob("chatlog_*.json"))
    done = 0
    for path in files:
        if max_convs > 0 and done >= max_convs:
            break
        conv = _conv_from_path(path)
        try:
            log = chatlog.get_log_sync(conv)
            if log is None:
                continue
            n = index_conv(conv, batch=batch)
        except sqlite3.Error:
            logger.exception("补索引失败 conv=%s", conv)
            continue
        done += 1
        if n:
            changed[conv] = n
    if changed:
        logger.info("启动补索引完成：%s", changed)
    return changed


async def index_loop() -> None:
    """后台增量索引：每 `msgindex_interval` 秒把新消息补进索引。

    刻意**不在每条消息上同步索引** —— 那会让落盘路径多一次 DB 写入，
    而"翻旧账"晚几分钟可查完全无所谓。

    **第一次跑之前先从磁盘补一遍**（`backfill_from_disk`），否则重启后
    "内存里没有旧会话"会让索引永久为空。
    """
    import asyncio

    await asyncio.sleep(90)
    if settings.get("msgindex_enabled"):
        try:
            backfill_from_disk(max_convs=int(settings.get("msgindex_bootstrap")))
        except Exception:  # noqa: BLE001
            logger.exception("启动补索引失败（后台循环会继续尝试）")
    while True:
        try:
            if settings.get("msgindex_enabled"):
                added = index_all()
                if added:
                    logger.info("消息索引已更新：%s", added)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("消息索引循环出错（下一轮继续）")
        try:
            await asyncio.sleep(max(120, int(settings.get("msgindex_interval"))))
        except asyncio.CancelledError:
            raise


# --------------------------------------------------------------------- 检索
def search(query: str, *, conv: str = "", limit: int = 5,
           days: int = 0) -> list[dict[str, Any]]:
    """按词面翻旧账。返回按「命中 gram 数 + 时间新近」排序的消息。

    `conv` 为空时跨全部会话搜（主人问"上次谁说过"时有用）；否则只搜这个会话。
    `days>0` 时只看最近这些天。
    """
    q_grams = grams(query)
    if not q_grams:
        return []
    if not available():
        return []
    try:
        conn = _connect()
        placeholders = ",".join("?" for _ in q_grams)
        sql = (
            "SELECT g.conv, g.msg_id, COUNT(*) AS hits, m.ts, m.name, m.text, m.is_bot "
            "FROM message_grams g JOIN messages_index m "
            "  ON m.conv=g.conv AND m.msg_id=g.msg_id "
            f"WHERE g.gram IN ({placeholders})"
        )
        params: list[Any] = list(q_grams)
        if conv:
            sql += " AND g.conv=?"
            params.append(conv)
        if days > 0:
            sql += " AND m.ts>=?"
            params.append(time.time() - days * 86400)
        sql += " GROUP BY g.conv, g.msg_id ORDER BY hits DESC, m.ts DESC LIMIT ?"
        params.append(max(1, int(limit)) * 3)
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        logger.exception("翻旧账查询失败：%s", query)
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        # 用「命中比例」而不是绝对命中数：长消息天然命中更多 gram，
        # 只看绝对数会让"什么都沾一点"的长消息排在真正相关的那条前面。
        total = max(1, len(q_grams))
        score = min(1.0, float(row["hits"]) / total)
        out.append(
            {
                "conv": str(row["conv"]),
                "id": int(row["msg_id"]),
                "ts": float(row["ts"] or 0),
                "name": str(row["name"] or ""),
                "text": str(row["text"] or ""),
                "is_bot": bool(row["is_bot"]),
                "score": round(score, 3),
            }
        )
    out.sort(key=lambda x: (-x["score"], -x["ts"]))
    return out[: max(1, int(limit))]


def render(query: str, *, conv: str = "", limit: int = 5, days: int = 0) -> str:
    """把翻到的旧账渲染成 prompt 块；没有命中返回空串。

    **带原始时间**：旧账的价值一半在"什么时候说的"，只给内容会让模型把它当成刚发生的。
    """
    hits = search(query, conv=conv, limit=limit, days=days)
    if not hits:
        return ""
    lines: list[str] = []
    for item in hits:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["ts"])) if item["ts"] else "?"
        who = "你" if item["is_bot"] else (item["name"] or "某人")
        where = "" if conv else f"（{item['conv']}）"
        lines.append(f"- [{when} {who}{where}] {item['text']}")
    return (
        "【翻到的旧记录】（这是从聊天记录里按关键词找出来的，不是你的记忆条目）\n"
        + "\n".join(lines)
    )


def stats() -> dict[str, Any]:
    if not available():
        return {"enabled": bool(settings.get("msgindex_enabled")), "messages": 0, "grams": 0}
    try:
        conn = _connect()
        msgs = conn.execute("SELECT COUNT(*) AS c FROM messages_index").fetchone()["c"]
        grs = conn.execute("SELECT COUNT(*) AS c FROM message_grams").fetchone()["c"]
    except sqlite3.Error:
        return {"enabled": bool(settings.get("msgindex_enabled")), "messages": 0, "grams": 0}
    return {
        "enabled": bool(settings.get("msgindex_enabled")),
        "messages": int(msgs or 0),
        "grams": int(grs or 0),
        "conversations": len(_indexed_upto),
    }


def rebuild() -> dict[str, int]:
    """清空索引重建（维护用：怀疑索引落后或损坏时跑一次）。"""
    try:
        conn = _connect()
        conn.execute("DELETE FROM messages_index")
        conn.execute("DELETE FROM message_grams")
        conn.commit()
    except sqlite3.Error:
        logger.exception("清空消息索引失败")
        return {}
    _indexed_upto.clear()
    return index_all(batch=100000)


# --------------------------------------------------------------------- 与记忆的关系
def index_note() -> str:
    """一句话说明索引与长期记忆的分工，供 `/机制` 自述用。"""
    st = stats()
    return (
        f"聊天记录索引：{st['messages']} 条消息可翻（{st['grams']} 个词条）。"
        "它管「原话怎么说的」，长期记忆管「我一直知道什么」。"
    )
