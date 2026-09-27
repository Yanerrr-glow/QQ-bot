"""记忆的持久化层：**同一份数据，两种后端（JSON / SQLite）**，算法层不感知。

## 为什么要单独一层

`memory.py` 里真正有价值的是**算法**：2-gram 相似度去重、覆盖更新、
`score_fact()` 的加权排序、半衰期衰减、抽取水位线。而「数据放在哪儿」
（一个 JSON 还是 SQLite 里的几张表）跟这些算法**没有关系**。

原来两者是耦合的：`_Db.load()` 直接把 `memories.json` 读成一个 dict、
`_Db.save()` 把三个列表整体序列化重写。于是：

* 每次 `remember` / `extract_and_store` 都是一次**全量重写**（写放大）；
* 想加一个字段（`vis`、`uid`、`entities`）就要动载入与保存两处；
* 想按会话/时间过滤，只能在 Python 里全表线性扫描。

现在把这一层抽出来：`JsonStore` 与 `SqliteStore` **实现同一组方法**，
`memory.py` 只调方法、不碰文件格式。上面那些毛病逐条消失，
而且**迁移是可回滚的**（JSON 原地留着，删掉 `.db` 就回去）。

## 两个后端的取舍

| | JsonStore | SqliteStore |
|---|---|---|
| 落盘 | 每次全量重写整份 JSON | 按行 INSERT/UPDATE |
| 迁移 | — | 首次启动时若只有 JSON 就自动迁移（幂等） |
| 检索 | Python 里线性扫描（现在的做法） | 仍走内存 + 可加 SQL/FTS |
| 用途 | **回滚用**、人工查看 | 线上默认 |

**注意**：RAM 里那份 `facts` 列表两种后端都保留 —— 这是刻意的。
现有算法的核心是对列表做 2-gram 打分，改成逐条 SQL 查询会把
「0 token 本地检索」变成几百次查询，得不偿失。SQLite 解决的是**写入放大**与
**格式演进**，不是检索性能（检索的优化在 `messages_index` 那边，见 A5）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("ai_chat.memstore")

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL DEFAULT 0,
    time        TEXT    NOT NULL DEFAULT '',
    text        TEXT    NOT NULL,
    scope       TEXT    NOT NULL DEFAULT 'global',
    vis         TEXT    NOT NULL DEFAULT '["*"]',
    subject     TEXT    NOT NULL DEFAULT '群友',
    uid         INTEGER,
    name        TEXT    NOT NULL DEFAULT '',
    conv        TEXT    NOT NULL DEFAULT '',
    importance  REAL    NOT NULL DEFAULT 0.5,
    source      TEXT    NOT NULL DEFAULT 'extract',
    protected   INTEGER NOT NULL DEFAULT 0,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit    TEXT,
    used        INTEGER NOT NULL DEFAULT 0,
    last_used   TEXT,
    prev_text   TEXT,
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_conv ON facts(conv);
CREATE INDEX IF NOT EXISTS idx_facts_uid  ON facts(uid);
CREATE INDEX IF NOT EXISTS idx_facts_ts   ON facts(ts);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         REAL    NOT NULL DEFAULT 0,
    time       TEXT    NOT NULL DEFAULT '',
    text       TEXT    NOT NULL,
    conv       TEXT    NOT NULL DEFAULT '',
    importance REAL    NOT NULL DEFAULT 0.5,
    used       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conv);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);

CREATE TABLE IF NOT EXISTS profile (
    key         TEXT NOT NULL,
    display     TEXT NOT NULL DEFAULT '',
    love        TEXT NOT NULL DEFAULT '[]',
    dislike     TEXT NOT NULL DEFAULT '[]',
    habit       TEXT NOT NULL DEFAULT '[]',
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (key)
);

-- 身份归一：一个 QQ 号可能在不同群有不同昵称（实测 100000001 在 A 群叫 小明、
-- 在 B 群叫「夜风の 旅人⭐」）。这张表把「见过这个名字 = 这个 uid」
-- 记下来，于是 facts 的 subject 能落到 uid 上；profile 的键也就能统一。
CREATE TABLE IF NOT EXISTS entities (
    name        TEXT PRIMARY KEY,
    uid         INTEGER,
    canonical   TEXT NOT NULL DEFAULT '',
    conv        TEXT NOT NULL DEFAULT '',
    seen_count  INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT NOT NULL DEFAULT '',
    last_seen   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entities_uid ON entities(uid);

CREATE TABLE IF NOT EXISTS extract_state (
    conv        TEXT PRIMARY KEY,
    last_id     INTEGER NOT NULL DEFAULT 0,
    total       INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT ''
);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(text: Any, default: Any) -> Any:
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


# --------------------------------------------------------------------- 后端
class JsonStore:
    """一个 JSON 文件装全部（改造前的行为），保留下来供回滚与人工查看。"""

    name = "json"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.profile: dict[str, dict[str, Any]] = {}
        self.facts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.entities: dict[str, dict[str, Any]] = {}
        self.next_id = 1
        self.meta: dict[str, Any] = {}
        self.readonly_reason = ""
        self.loaded = False

    # -------------------------------------------------- 生命周期
    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    def load(self) -> None:
        self.loaded = True
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.readonly_reason = f"{type(exc).__name__}"
            self._quarantine()
            return
        if not isinstance(raw, dict):
            self.readonly_reason = "顶层不是对象"
            self._quarantine()
            return
        self.profile = {
            str(k): v for k, v in (raw.get("profile") or {}).items() if isinstance(v, dict)
        }
        self.facts = [f for f in (raw.get("facts") or []) if isinstance(f, dict) and f.get("text")]
        self.events = [e for e in (raw.get("events") or []) if isinstance(e, dict) and e.get("text")]
        self.entities = {
            str(k): v for k, v in (raw.get("entities") or {}).items() if isinstance(v, dict)
        }
        self.next_id = int(raw.get("next_id", len(self.facts) + 1))
        self.meta = dict(raw.get("meta") or {})
        if self.facts:
            self.next_id = max(self.next_id, max(int(f.get("id", 0)) for f in self.facts) + 1)

    def _quarantine(self) -> None:
        try:
            bak = self.path.with_name(f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            self.path.replace(bak)
            logger.error(
                "记忆库损坏（%s），已改名留证并进入只读（不会覆盖）：%s → %s",
                self.readonly_reason, self.path.name, bak.name,
            )
        except OSError:
            logger.error("记忆库损坏（%s）且改名失败，仍进入只读：%s", self.readonly_reason, self.path)

    def save(self) -> None:
        if self.readonly_reason:
            logger.error(
                "记忆库处于只读模式（原因：%s），本次不写盘", self.readonly_reason,
            )
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "next_id": self.next_id,
            "profile": self.profile,
            "facts": self.facts,
            "events": self.events,
            "entities": self.entities,
            "meta": self.meta,
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def close(self) -> None:
        return None

    # -------------------------------------------------- 兼容旧接口（memory.py 用）
    def next_fact_id(self) -> int:
        self.ensure()
        fid = self.next_id
        self.next_id += 1
        return fid


class SqliteStore:
    """SQLite 后端：按行写入、便于加字段与按会话/时间过滤。

    **事实与事件仍常驻内存**（`self.facts` / `self.events`），只有写盘变成按行操作。
    理由见模块 docstring 末尾。
    """

    name = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: sqlite3.Connection | None = None
        # **裸容器**（不穿透）：写入穿透的包装只在 `load()` 之后套一层。
        # 为什么必须分两份：`upsert_profile()` 要写内存副本，而穿透字典的 `__setitem__`
        # 又会回调 `upsert_profile()` —— 直接写包装过的那份会无限递归（实测踩到过）。
        self._raw_profile: dict[str, dict[str, Any]] = {}
        self._raw_facts: list[dict[str, Any]] = []
        self._raw_events: list[dict[str, Any]] = []
        self.profile: dict[str, dict[str, Any]] = {}
        self.facts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.entities: dict[str, dict[str, Any]] = {}
        self.next_id = 1
        self.meta: dict[str, Any] = {}
        self.readonly_reason = ""
        self.loaded = False
        self._deleted_facts: set[int] = set()
        self._deleted_events: set[int] = set()

    # -------------------------------------------------- 生命周期
    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    def _connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), timeout=10)
            self.conn.row_factory = sqlite3.Row
            # WAL：读写不互相阻塞，机器人一边回复一边补抽时更稳
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        return self.conn

    def load(self) -> None:
        self.loaded = True
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            self.readonly_reason = f"sqlite:{type(exc).__name__}"
            logger.error("记忆库打不开（%s），进入只读：%s", self.readonly_reason, self.path)
            return
        try:
            self.meta = {
                str(r["key"]): _loads(r["value"], None)
                for r in conn.execute("SELECT key, value FROM meta")
            }
            self.next_id = int(self.meta.get("next_id") or 1)

            self.facts = [_fact_from_row(r) for r in conn.execute("SELECT * FROM facts ORDER BY id")]
            self.events = [
                _event_from_row(r) for r in conn.execute("SELECT * FROM events ORDER BY id")
            ]
            self.profile = {
                str(r["key"]): _profile_from_row(r) for r in conn.execute("SELECT * FROM profile")
            }
            self.entities = {
                str(r["name"]): {
                    "uid": r["uid"],
                    "canonical": r["canonical"],
                    "conv": r["conv"],
                    "seen_count": int(r["seen_count"] or 1),
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                }
                for r in conn.execute("SELECT * FROM entities")
            }
        except sqlite3.Error as exc:
            logger.exception("读记忆库失败：%s", exc)
            self.readonly_reason = f"read:{type(exc).__name__}"
            return
        if self.facts:
            self.next_id = max(self.next_id, max(int(f.get("id", 0)) for f in self.facts) + 1)
        # 裸容器存好，再套一层写入穿透：此后算法层的 append/remove 会自动落盘
        self._raw_facts = self.facts
        self._raw_events = self.events
        self._raw_profile = self.profile
        self.facts = _WriteThroughList(self._raw_facts, self, "facts")
        self.events = _WriteThroughList(self._raw_events, self, "events")
        self.profile = _WriteThroughDict(self._raw_profile, self)
        logger.info(
            "记忆库已载入（sqlite）：%d 条事实 / %d 条群事件 / %d 个人物 / %d 个名字",
            len(self.facts), len(self.events), len(self.profile), len(self.entities),
        )

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

    # -------------------------------------------------- 写入
    def next_fact_id(self) -> int:
        self.ensure()
        fid = self.next_id
        self.next_id += 1
        return fid

    def upsert_fact(self, fact: dict[str, Any]) -> None:
        """新增或整体更新一条事实（按 id）。"""
        self.ensure()
        if self.readonly_reason:
            return
        _upsert_fact(self._raw_facts, fact)
        try:
            conn = self._connect()
            conn.execute(_FACT_UPSERT_SQL, _fact_to_row(fact))
            conn.commit()
        except sqlite3.Error:
            logger.exception("写事实失败 id=%s", fact.get("id"))

    def touch_facts(self, facts: list[dict[str, Any]]) -> None:
        """只更新这些条目的可变字段（used / hits / last_used）。给 `mark_used` 用。"""
        if not facts or self.readonly_reason:
            return
        try:
            conn = self._connect()
            for fact in facts:
                conn.execute(
                    "UPDATE facts SET used=?, last_used=?, hits=?, last_hit=? WHERE id=?",
                    (
                        int(fact.get("used", 0) or 0),
                        fact.get("last_used"),
                        int(fact.get("hits", 0) or 0),
                        fact.get("last_hit"),
                        int(fact.get("id", 0) or 0),
                    ),
                )
            conn.commit()
        except sqlite3.Error:
            logger.exception("更新使用计数失败")

    def delete_fact(self, fact_id: int) -> bool:
        return self._delete("facts", fact_id, self._deleted_facts)

    def delete_event(self, event_id: int) -> bool:
        return self._delete("events", event_id, self._deleted_events)

    def upsert_event(self, event: dict[str, Any]) -> None:
        self.ensure()
        if self.readonly_reason:
            return
        _upsert_fact(self._raw_events, event)  # 同一个"按 id 覆盖"逻辑，复用它
        try:
            conn = self._connect()
            conn.execute(_EVENT_UPSERT_SQL, _event_to_row(event))
            conn.commit()
        except sqlite3.Error:
            logger.exception("写群事件失败 id=%s", event.get("id"))

    def delete_item(self, kind: str, item_id: int) -> bool:
        """按表名删一行。给写入穿透列表用（它只知道 "facts" / "events"）。"""
        if kind == "facts":
            return self.delete_fact(item_id)
        if kind == "events":
            return self.delete_event(item_id)
        return False

    def delete_profile(self, key: str) -> bool:
        self.ensure()
        if self.readonly_reason:
            return False
        self._raw_profile.pop(str(key), None)
        try:
            conn = self._connect()
            conn.execute("DELETE FROM profile WHERE key=?", (str(key),))
            conn.commit()
        except sqlite3.Error:
            logger.exception("删人物画像失败 key=%s", key)
            return False
        return True

    def _delete(self, table: str, item_id: int, bucket: set[int]) -> bool:
        self.ensure()
        if self.readonly_reason:
            return False
        bucket.add(int(item_id))
        try:
            conn = self._connect()
            conn.execute(f"DELETE FROM {table} WHERE id=?", (int(item_id),))
            conn.commit()
        except sqlite3.Error:
            logger.exception("删除 %s#%s 失败", table, item_id)
            return False
        return True

    def replace_all(self, *, profile: dict[str, Any], facts: list[dict[str, Any]],
                    events: list[dict[str, Any]], entities: dict[str, Any] | None = None) -> None:
        """整库重写。只在 `clear()` / 迁移这类"成批改动"时用。"""
        self.ensure()
        if self.readonly_reason:
            return
        self.profile = profile
        self.facts = facts
        self.events = events
        if entities is not None:
            self.entities = entities
        try:
            conn = self._connect()
            conn.execute("BEGIN")
            conn.execute("DELETE FROM facts")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM profile")
            conn.execute("DELETE FROM entities")
            conn.executemany(_FACT_UPSERT_SQL, [_fact_to_row(f) for f in facts])
            conn.executemany(_EVENT_UPSERT_SQL, [_event_to_row(e) for e in events])
            conn.executemany(_PROFILE_UPSERT_SQL, [_profile_to_row(k, v) for k, v in profile.items()])
            conn.executemany(
                _ENTITY_UPSERT_SQL, [_entity_to_row(k, v) for k, v in (entities or {}).items()]
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("整库重写失败")

    def upsert_profile(self, key: str, info: dict[str, Any]) -> None:
        self.ensure()
        if self.readonly_reason:
            return
        self._raw_profile[key] = info
        try:
            conn = self._connect()
            conn.execute(_PROFILE_UPSERT_SQL, _profile_to_row(key, info))
            conn.commit()
        except sqlite3.Error:
            logger.exception("写人物画像失败 key=%s", key)

    def upsert_entity(self, name: str, info: dict[str, Any]) -> None:
        self.ensure()
        if self.readonly_reason:
            return
        self.entities[name] = info
        try:
            conn = self._connect()
            conn.execute(_ENTITY_UPSERT_SQL, _entity_to_row(name, info))
            conn.commit()
        except sqlite3.Error:
            logger.exception("写名字索引失败 name=%s", name)

    def save(self) -> None:
        """兜底：把内存里的 next_id 落一次。

        与 JSON 后端不同，SQLite 后端**每次写操作已经单独落盘**，
        所以 `save()` 不再是"把整库写一遍"，只是补一个计数器。
        """
        self.ensure()
        if self.readonly_reason:
            logger.error(
                "记忆库处于只读模式（原因：%s），本次不写盘", self.readonly_reason,
            )
            return
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('next_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_dumps(self.next_id),),
            )
            conn.commit()
        except sqlite3.Error:
            logger.exception("写 meta 失败")


# --------------------------------------------------------------------- 写入穿透
class _WriteThroughList(list):
    """一个 list，但**增删改会顺带落盘**。

    为什么用这种写法而不是"把 `_db.facts.append(x)` 改成 `store.add_fact(x)`"：
    `memory.py` 里对这两个列表有 **70 多处**读写（打分、去重、淘汰、渲染都直接遍历它们），
    把每一处都改成方法调用，等于把"换个存储"变成"重写算法层"—— 那是最容易引入回归的做法。
    现在算法层**一个字都不用改**：它照旧 `_db.facts.append(item)`、`_db.facts.remove(fact)`，
    而落盘自动发生。

    代价要说清楚：**淘汰循环里连续 remove N 条会写 N 次**。实测淘汰是低频动作
    （只在抽取后触发、且要超上限才发生），N 通常是几条；用 `batch()` 包一层可以合并成一次。
    """

    __slots__ = ("_store", "_kind")

    def __init__(self, items: list[dict[str, Any]], store: Any, kind: str) -> None:
        super().__init__(items)
        self._store = store
        self._kind = kind

    def _sync(self, item: dict[str, Any]) -> None:
        if self._kind == "facts":
            self._store.upsert_fact(item)
        else:
            self._store.upsert_event(item)

    def append(self, item: dict[str, Any]) -> None:  # type: ignore[override]
        super().append(item)
        self._sync(item)

    def remove(self, item: dict[str, Any]) -> None:  # type: ignore[override]
        super().remove(item)
        self._store.delete_item(self._kind, int(item.get("id", 0) or 0))

    def pop(self, index: int = -1) -> dict[str, Any]:  # type: ignore[override]
        item = super().pop(index)
        self._store.delete_item(self._kind, int(item.get("id", 0) or 0))
        return item

    def __delitem__(self, index: Any) -> None:  # type: ignore[override]
        victims = self[index] if isinstance(index, slice) else [self[index]]
        super().__delitem__(index)
        for item in victims:
            self._store.delete_item(self._kind, int(item.get("id", 0) or 0))


class _WriteThroughDict(dict):
    """人物画像的写入穿透。键是名字（归一后的规范名）。"""

    __slots__ = ("_store",)

    def __init__(self, items: dict[str, Any], store: Any) -> None:
        super().__init__(items)
        self._store = store

    def __setitem__(self, key: str, value: Any) -> None:  # type: ignore[override]
        super().__setitem__(key, value)
        self._store.upsert_profile(str(key), value)

    def setdefault(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key: str, *args: Any) -> Any:  # type: ignore[override]
        existed = key in self
        value = super().pop(key, *args)
        if existed:
            self._store.delete_profile(str(key))
        return value


# --------------------------------------------------------------------- 行 ↔ 字典
_FACT_COLS = (
    "id", "ts", "time", "text", "scope", "vis", "subject", "uid", "name", "conv",
    "importance", "source", "protected", "hits", "last_hit", "used", "last_used",
    "prev_text", "updated_at",
)
_EVENT_COLS = ("id", "ts", "time", "text", "conv", "importance", "used")
_PROFILE_COLS = ("key", "display", "love", "dislike", "habit", "note", "updated_at")
_ENTITY_COLS = ("name", "uid", "canonical", "conv", "seen_count", "first_seen", "last_seen")


def _upsert_sql(table: str, cols: tuple[str, ...]) -> str:
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "key", "name"))
    return f"INSERT INTO {table}({','.join(cols)}) VALUES({placeholders}) " \
           f"ON CONFLICT({cols[0]}) DO UPDATE SET {updates}"


_FACT_UPSERT_SQL = _upsert_sql("facts", _FACT_COLS)
_EVENT_UPSERT_SQL = _upsert_sql("events", _EVENT_COLS)
_PROFILE_UPSERT_SQL = _upsert_sql("profile", _PROFILE_COLS)
_ENTITY_UPSERT_SQL = _upsert_sql("entities", _ENTITY_COLS)


def _fact_to_row(fact: dict[str, Any]) -> tuple:
    return (
        int(fact.get("id", 0) or 0),
        float(fact.get("ts", 0) or 0),
        str(fact.get("time", "") or ""),
        str(fact.get("text", "") or ""),
        str(fact.get("scope", "global") or "global"),
        _dumps(fact.get("vis") or ["*"]),
        str(fact.get("subject", "群友") or "群友"),
        int(fact["uid"]) if fact.get("uid") not in (None, "") else None,
        str(fact.get("name", "") or ""),
        str(fact.get("conv", "") or ""),
        float(fact.get("importance", 0.5) or 0.5),
        str(fact.get("source", "extract") or "extract"),
        1 if fact.get("protected") else 0,
        int(fact.get("hits", 0) or 0),
        fact.get("last_hit"),
        int(fact.get("used", 0) or 0),
        fact.get("last_used"),
        fact.get("prev_text"),
        fact.get("updated_at"),
    )


def _fact_from_row(row: sqlite3.Row) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "id": int(row["id"]),
        "ts": float(row["ts"] or 0),
        "time": str(row["time"] or ""),
        "text": str(row["text"] or ""),
        "scope": str(row["scope"] or "global"),
        "vis": _loads(row["vis"], ["*"]),
        "subject": str(row["subject"] or "群友"),
        "name": str(row["name"] or ""),
        "conv": str(row["conv"] or ""),
        "importance": float(row["importance"] or 0.5),
        "source": str(row["source"] or "extract"),
        "protected": bool(row["protected"]),
        "hits": int(row["hits"] or 0),
        "used": int(row["used"] or 0),
    }
    if row["uid"] is not None:
        fact["uid"] = int(row["uid"])
    for key in ("last_hit", "last_used", "prev_text", "updated_at"):
        if row[key]:
            fact[key] = row[key]
    return fact


def _event_to_row(event: dict[str, Any]) -> tuple:
    return (
        int(event.get("id", 0) or 0),
        float(event.get("ts", 0) or 0),
        str(event.get("time", "") or ""),
        str(event.get("text", "") or ""),
        str(event.get("conv", "") or ""),
        float(event.get("importance", 0.5) or 0.5),
        int(event.get("used", 0) or 0),
    )


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "ts": float(row["ts"] or 0),
        "time": str(row["time"] or ""),
        "text": str(row["text"] or ""),
        "conv": str(row["conv"] or ""),
        "importance": float(row["importance"] or 0.5),
        "used": int(row["used"] or 0),
    }


def _profile_to_row(key: str, info: dict[str, Any]) -> tuple:
    return (
        str(key),
        str(info.get("display", "") or ""),
        _dumps(list(info.get("love") or [])),
        _dumps(list(info.get("dislike") or [])),
        _dumps(list(info.get("habit") or [])),
        str(info.get("note", "") or ""),
        str(info.get("updated_at", "") or ""),
    )


def _profile_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "display": str(row["display"] or ""),
        "love": _loads(row["love"], []),
        "dislike": _loads(row["dislike"], []),
        "habit": _loads(row["habit"], []),
        "note": str(row["note"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _entity_to_row(name: str, info: dict[str, Any]) -> tuple:
    return (
        str(name),
        int(info["uid"]) if info.get("uid") not in (None, "") else None,
        str(info.get("canonical", "") or ""),
        str(info.get("conv", "") or ""),
        int(info.get("seen_count", 1) or 1),
        str(info.get("first_seen", "") or ""),
        str(info.get("last_seen", "") or ""),
    )


def _upsert_fact(facts: list[dict[str, Any]], fact: dict[str, Any]) -> None:
    """在内存列表里按 id 覆盖或追加（保持列表与库一致）。"""
    for idx, old in enumerate(facts):
        if int(old.get("id", 0) or 0) == int(fact.get("id", 0) or 0):
            facts[idx] = fact
            return
    facts.append(fact)


# --------------------------------------------------------------------- 挑选后端
def open_store(log_dir: Path, *, prefer: str = "sqlite") -> JsonStore | SqliteStore:
    """按偏好打开后端。`prefer="json"` 用于回滚与人工查看。"""
    json_path = log_dir / "memories.json"
    db_path = log_dir / "memory.db"
    if prefer == "json":
        return JsonStore(json_path)
    if prefer == "sqlite":
        return SqliteStore(db_path)
    # auto：有 db 用 db，否则用 json（迁移由调用方决定，见 migrate_json_to_sqlite）
    if db_path.exists():
        return SqliteStore(db_path)
    return JsonStore(json_path)


def backfill_vis(store: Any, *, master_qq: int = 0, apply: bool = False) -> dict[str, Any]:
    """给**老数据**回填可见范围（`vis`）。

    为什么需要：迁移（或升级）过来的老事实没有 `vis` 字段，而 `_visible_in()`
    对"没有 vis"的条目只能按 `conv` 推断；但迁移进 SQLite 时列有默认值 `["*"]`，
    于是**看上去每条都有 vis，实际全是"全局可见"** —— 隔离等于没生效。
    实测：迁移后 102 条事实全是 `["*"]`。

    回填规则与 `memory._vis_for()` 一致（一处规则两处用，改的时候要一起改）：

    | 事实来自 | vis |
    |---|---|
    | 群 `g123` | `["g123"]` —— 只在那个群可见 |
    | **主人私聊** `u<master>` | `["*"]` —— 主人说的，跨会话记得是本该有的能力 |
    | 别人私聊 `u456` | `["u456"]` —— 只在那条私聊里 |
    | 没有 `conv`（推不出来） | 保持 `["*"]` —— 宁可不隔离，也不误删可回忆的内容 |

    `apply=False` 只统计不写（审计用）。
    """
    store.ensure()
    changed = 0
    kinds: dict[str, int] = {}
    for fact in store.facts:
        if fact.get("vis") not in (None, [], ["*"]):
            continue  # 已经是窄范围，不动
        conv = str(fact.get("conv") or "")
        if not conv:
            kinds["无 conv（保持全局）"] = kinds.get("无 conv（保持全局）", 0) + 1
            continue
        if conv.startswith("u"):
            try:
                is_master = master_qq and int(conv[1:]) == int(master_qq)
            except (TypeError, ValueError):
                is_master = False
            new_vis = ["*"] if is_master else [conv]
            label = "主人私聊（保持全局）" if is_master else "别人私聊（收到该私聊）"
        elif conv.startswith("g"):
            new_vis = [conv]
            label = "群（收到该群）"
        else:
            kinds["无法判断（保持全局）"] = kinds.get("无法判断（保持全局）", 0) + 1
            continue
        kinds[label] = kinds.get(label, 0) + 1
        if new_vis != fact.get("vis"):
            changed += 1
            if apply:
                fact["vis"] = new_vis
                store.upsert_fact(fact)
    return {
        "ok": True,
        "apply": apply,
        "facts": len(store.facts),
        "changed": changed,
        "breakdown": kinds,
    }


def migrate_json_to_sqlite(log_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """把 `memories.json` 迁进 `memory.db`。**幂等**：已有同 id 的行会被覆盖成 JSON 的值。

    返回统计信息（条数与目标路径），`dry_run=True` 时只读不写。
    原 JSON **原地保留** —— 它是回滚的唯一凭据，不能删。
    """
    json_path = log_dir / "memories.json"
    db_path = log_dir / "memory.db"
    src = JsonStore(json_path)
    src.load()
    if src.readonly_reason:
        return {"ok": False, "error": f"源文件损坏：{src.readonly_reason}"}
    stats = {
        "ok": True,
        "from": str(json_path),
        "to": str(db_path),
        "facts": len(src.facts),
        "events": len(src.events),
        "profile": len(src.profile),
        "entities": len(src.entities),
        "dry_run": dry_run,
    }
    if dry_run:
        return stats
    dst = SqliteStore(db_path)
    dst.load()
    dst.replace_all(
        profile=src.profile, facts=src.facts, events=src.events, entities=src.entities,
    )
    dst.meta = dict(src.meta)
    dst.next_id = max(int(src.next_id), dst.next_id)
    dst.save()
    dst.close()
    logger.info("记忆库已迁移到 SQLite：%s", stats)
    return stats


def export_sqlite_to_json(log_dir: Path) -> dict[str, Any]:
    """反向导出：把 `memory.db` 写回 `memories.json`（备份/交付用）。"""
    src = SqliteStore(log_dir / "memory.db")
    src.load()
    if src.readonly_reason:
        return {"ok": False, "error": src.readonly_reason}
    dst = JsonStore(log_dir / "memories.json")
    dst.profile = src.profile
    dst.facts = src.facts
    dst.events = src.events
    dst.entities = src.entities
    dst.next_id = src.next_id
    dst.meta = src.meta
    dst.loaded = True
    dst.save()
    src.close()
    return {"ok": True, "facts": len(dst.facts), "events": len(dst.events)}
