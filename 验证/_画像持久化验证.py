"""画像持久化、原子归并与批次提交的离线验证。

不需要 nonebot / openai：config、clock、settings、llm 全部用桩。
跑法：python 验证/_画像持久化验证.py
退出码：0 = 全通过。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import types
from pathlib import Path

PASSED = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [OK] {label}" + (f" —— {detail}" if detail else ""))
    else:
        FAILED.append(label)
        print(f"  [失败] {label}" + (f" —— {detail}" if detail else ""))


ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "plugins" / "ai_chat"

# memstore 不依赖 nonebot，但仍然要绕过 plugins/ai_chat/__init__.py 的
# nonebot 导入，因此按文件加载成 ai_chat.memstore。
_bootstrap = types.ModuleType("ai_chat")
_bootstrap.__path__ = [str(PKG)]
sys.modules["ai_chat"] = _bootstrap
_spec = importlib.util.spec_from_file_location("ai_chat.memstore", PKG / "memstore.py")
store_mod = importlib.util.module_from_spec(_spec)
sys.modules["ai_chat.memstore"] = store_mod
_spec.loader.exec_module(store_mod)


def _tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="profile-persist-"))


def _load_memory(log_dir: Path):
    """按临时目录加载一份新的 memory 模块（全局 _db 隔离）。"""
    stub_config = types.ModuleType("ai_chat.config")
    stub_config.API_KEY = ""
    stub_config.BASE_URL = "https://api.deepseek.com"
    stub_config.LOG_DIR = log_dir
    stub_config.SYSTEM_PROMPT = ""
    stub_config.BOT_NAME = ""
    stub_config.TIMEOUT = 5.0

    stub_clock = types.ModuleType("ai_chat.clock")
    import time as _t
    stub_clock.now = _t.time
    stub_clock.localtime = lambda ts=None: _t.localtime(ts)

    defaults = {
        "memory_store": "sqlite",
        "memory_half_life_days": 30,
        "memory_extract_per_day": 200,
        "memory_extract_max": 3,
        "memory_max_items": 800,
        "memory_enabled": True,
        "memory_extract": True,
    }
    stub_settings = types.ModuleType("ai_chat.settings")
    stub_settings.get = lambda k, d=None: defaults.get(k, d)

    stub_llm = types.ModuleType("ai_chat.llm")
    stub_llm.api_key = lambda *a, **k: ""
    stub_llm.active = lambda *a, **k: {}

    async def _no_chat(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("测试不调用模型")

    stub_llm.chat = _no_chat

    pkg = types.ModuleType("ai_chat")
    pkg.__path__ = [str(PKG)]
    sys.modules["ai_chat"] = pkg
    sys.modules["ai_chat.config"] = stub_config
    sys.modules["ai_chat.clock"] = stub_clock
    sys.modules["ai_chat.settings"] = stub_settings
    sys.modules["ai_chat.llm"] = stub_llm
    sys.modules["ai_chat.memstore"] = store_mod

    spec = importlib.util.spec_from_file_location("ai_chat.memory", PKG / "memory.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ai_chat.memory"] = mod
    spec.loader.exec_module(mod)
    return mod


def _profile(key: str) -> dict:
    return {
        "display": "测试人",
        "love": ["喜欢甲"],
        "dislike": ["讨厌乙"],
        "habit": ["习惯丙"],
        "note": "临时状态",
        "updated_at": "2026-10-22 10:00:00",
    }


def _roundtrip(backend: str) -> None:
    print(f"\n=== {backend}：写入后重新打开 ===")
    tmp = _tmpdir()
    path = tmp / ("memories.json" if backend == "json" else "memory.db")
    store_mod.open_store(tmp, prefer=backend)
    store = store_mod.open_store(tmp, prefer=backend)
    store.load()
    store.write_profile("uid:1", _profile("uid:1"))
    reopened = store_mod.open_store(tmp, prefer=backend)
    reopened.load()
    check("画像重新打开后仍在", reopened.profile.get("uid:1", {}).get("love") == ["喜欢甲"],
          str(reopened.profile.get("uid:1")))
    check("note 一起落盘", reopened.profile.get("uid:1", {}).get("note") == "临时状态")
    store.close()
    reopened.close()


def _readonly(backend: str) -> None:
    print(f"\n=== {backend}：只读写入必须失败 ===")
    tmp = _tmpdir()
    store = store_mod.open_store(tmp, prefer=backend)
    store.load()
    store.readonly_reason = "测试只读"
    try:
        store.write_profile("uid:1", _profile("uid:1"))
        check("只读写入抛错", False)
    except store_mod.MemoryWriteError:
        check("只读写入抛 MemoryWriteError", True)
    try:
        store.delete_profile("uid:1")
        check("只读删除抛错", False)
    except store_mod.MemoryWriteError:
        check("只读删除抛 MemoryWriteError", True)
    store.close()


def _storage_roundtrip(backend: str) -> None:
    print(f"\n=== {backend}：临时库往返 ===")
    tmp = _tmpdir()
    store = store_mod.open_store(tmp, prefer=backend)
    store.load()
    store.write_profile("uid:1", {"display": "测试人", "love": ["喜欢甲"], "dislike": [],
                                  "habit": [], "note": "", "updated_at": "2026-10-22 10:00:00"})
    reopened = store_mod.open_store(tmp, prefer=backend)
    reopened.load()
    check("写后重开仍在", reopened.profile.get("uid:1", {}).get("love") == ["喜欢甲"])
    store.close()
    reopened.close()


def _batch_roundtrip(backend: str) -> None:
    print(f"\n=== {backend}：批次提交与回滚 ===")
    tmp = _tmpdir()
    store = store_mod.open_store(tmp, prefer=backend)
    store.load()
    store.begin_batch()
    store.write_profile("uid:1", _profile("uid:1"))
    store.commit_batch("batch-1", "u1", 10, 2, "2")
    reopened = store_mod.open_store(tmp, prefer=backend)
    reopened.load()
    check("批次提交后画像存在", reopened.profile.get("uid:1", {}).get("love") == ["喜欢甲"])
    check("批次标记可查", (reopened.latest_committed_batch("u1") or {}).get("last_message_id") == 10)
    store.close()
    reopened.close()

    tmp2 = _tmpdir()
    store2 = store_mod.open_store(tmp2, prefer=backend)
    store2.load()
    store2.begin_batch()
    store2.write_profile("uid:2", _profile("uid:2"))
    store2.rollback_batch()
    check("回滚后内存无该条目", "uid:2" not in store2.profile)
    reopened2 = store_mod.open_store(tmp2, prefer=backend)
    reopened2.load()
    check("回滚后磁盘无该条目", "uid:2" not in reopened2.profile)
    store2.close()
    reopened2.close()


def _memory_api() -> None:
    print("\n=== memory.set_profile：落盘与 replace/clear 语义 ===")
    tmp = _tmpdir()
    mem = _load_memory(tmp)
    mem.set_profile("uid:1", display="测试人", love=["喜欢甲"], note="最近在找工作")
    st = store_mod.SqliteStore(tmp / "memory.db")
    st.load()
    check("set_profile 已落盘", st.profile.get("uid:1", {}).get("love") == ["喜欢甲"])
    check("note 已落盘", st.profile.get("uid:1", {}).get("note") == "最近在找工作")

    mem.set_profile("uid:1", love=["喜欢乙"], mode="replace")
    check("replace 替换而不是合并", mem._db.profile["uid:1"]["love"] == ["喜欢乙"],
          str(mem._db.profile["uid:1"]["love"]))
    mem.set_profile("uid:1", love=[], mode="replace")
    check("replace 传空列表可清空", mem._db.profile["uid:1"]["love"] == [])
    mem.set_profile("uid:1", mode="clear")
    check("clear 清空 note", mem._db.profile["uid:1"]["note"] == "")

    mem.set_profile("人名", display="人名", love=["喜欢合并"])
    mem.set_profile("uid:9", display="人名", dislike=["讨厌合并"])
    mem.consolidate_profile("人名", "uid:9")
    st2 = store_mod.SqliteStore(tmp / "memory.db")
    st2.load()
    merged = st2.profile.get("uid:9", {})
    check("归并后目标键落盘", "喜欢合并" in (merged.get("love") or []), str(merged))
    check("归并后源键已删除", "人名" not in st2.profile)
    st.close()
    st2.close()

    # 写入失败时内存必须保留旧值
    tmp2 = _tmpdir()
    mem2 = _load_memory(tmp2)
    mem2.set_profile("uid:1", display="测试人", love=["旧值"])
    mem2._db.store.readonly_reason = "测试只读"
    try:
        mem2.set_profile("uid:1", love=["新值"])
        check("set_profile 只读失败", False)
    except store_mod.MemoryWriteError:
        check("set_profile 只读抛错", True)
    check("失败后内存仍是旧值", mem2._db.profile.get("uid:1", {}).get("love") == ["旧值"],
          str(mem2._db.profile.get("uid:1", {}).get("love")))


def _memory_batch() -> None:
    print("\n=== memory._db.batch：提交与回滚 ===")
    tmp = _tmpdir()
    mem = _load_memory(tmp)
    with mem._db.batch(batch_id="b1", conv="u1", last_message_id=5, message_count=2):
        mem.set_profile("uid:1", display="测试人", love=["喜欢批次"])
        mem.add_fact("测试事实", subject="测试人", uid=1)
    st = store_mod.SqliteStore(tmp / "memory.db")
    st.load()
    check("批次内画像已提交", st.profile.get("uid:1", {}).get("love") == ["喜欢批次"])
    check("批次内事实已提交", any(f.get("text") == "测试事实" for f in st.facts))
    check("批次标记已提交", (st.latest_committed_batch("u1") or {}).get("last_message_id") == 5)
    st.close()

    before = mem._db.profile.get("uid:2")
    try:
        with mem._db.batch(batch_id="b2", conv="u1", last_message_id=9, message_count=1):
            mem.set_profile("uid:2", display="回滚人", love=["不应留下"])
            raise RuntimeError("模拟失败")
    except RuntimeError:
        pass
    check("回滚后内存无 uid:2", "uid:2" not in mem._db.profile)
    st2 = store_mod.SqliteStore(tmp / "memory.db")
    st2.load()
    check("回滚后磁盘无 uid:2", "uid:2" not in st2.profile)
    st2.close()

    # 批次内归并：不能因为嵌套 BEGIN 而失败
    mem.set_profile("人名", display="人名", love=["喜欢合并"])
    with mem._db.batch(batch_id="b3", conv="u1", last_message_id=10, message_count=1):
        mem.consolidate_profile("人名", "uid:9")
    st3 = store_mod.SqliteStore(tmp / "memory.db")
    st3.load()
    check("批次内归并成功", "喜欢合并" in (st3.profile.get("uid:9", {}).get("love") or []),
          str(st3.profile.get("uid:9")))
    check("批次内归并删除源键", "人名" not in st3.profile)
    st3.close()


def _reconcile() -> None:
    print("\n=== 水位线补齐：批次已提交、JSON 未推进 ===")
    tmp = _tmpdir()
    mem = _load_memory(tmp)
    with mem._db.batch(batch_id="b-crash", conv="u1", last_message_id=7, message_count=3):
        mem.set_profile("uid:1", display="测试人", love=["喜欢崩溃恢复"])
    check("补齐前水位线为 0", mem.watermark("u1") == 0, str(mem.watermark("u1")))
    mem._reconcile_watermark("u1")
    check("补齐后水位线到 7", mem.watermark("u1") == 7, str(mem.watermark("u1")))
    check("补齐时计入消息数", mem.extract_state_stats()["day_messages"] == 3,
          str(mem.extract_state_stats()["day_messages"]))
    mem._reconcile_watermark("u1")
    check("重复补齐不重复计数", mem.extract_state_stats()["day_messages"] == 3,
          str(mem.extract_state_stats()["day_messages"]))


def _clear_bucket(backend: str) -> None:
    print(f"\n=== {backend}：整类清空先落盘 ===")
    tmp = _tmpdir()
    store = store_mod.open_store(tmp, prefer=backend)
    store.load()
    store.write_profile("uid:1", _profile("uid:1"))
    store.clear_bucket("profile")
    reopened = store_mod.open_store(tmp, prefer=backend)
    reopened.load()
    check("清空后重开为空", reopened.profile == {}, str(reopened.profile))
    store.close()
    reopened.close()


def _memory_clear() -> None:
    print("\n=== memory.clear：清空画像后重开 ===")
    tmp = _tmpdir()
    mem = _load_memory(tmp)
    mem.set_profile("uid:1", display="测试人", love=["喜欢清空"])
    check("清空前有 1 条", len(mem._db.profile) == 1, str(len(mem._db.profile)))
    mem.clear("profile")
    check("清空后内存为空", len(mem._db.profile) == 0)
    st = store_mod.SqliteStore(tmp / "memory.db")
    st.load()
    check("清空后磁盘为空", st.profile == {}, str(st.profile))
    st.close()


def _extraction_alignment() -> None:
    print("\n=== P0-C：提示词字段、现有画像上下文、note 落库 ===")
    tmp = _tmpdir()
    mem = _load_memory(tmp)
    mem.set_profile("uid:1", display="甲", love=["喜欢甲"])
    mem.add_fact("甲 正在做某个项目", subject="甲", uid=1)
    messages = [{"id": 1, "uid": 1, "name": "甲", "text": "随便聊聊", "is_bot": False}]
    context = mem._extraction_context(messages)
    check("上下文带现有画像", "喜欢甲" in context, context[:120])
    check("上下文带相关事实", "某个项目" in context, context[:120])
    check("schema 支持 note", '"note"' in mem._EXTRACT_PROMPT)
    check("schema 支持 line", '"line"' in mem._EXTRACT_PROMPT)
    check("越界 line 被拒绝", mem._ts_map_pick({1: 100.0}, {"line": 9}) is None)
    check("有效 line 正常回填", mem._ts_map_pick({1: 100.0}, {"line": 1}) == 100.0)

    asyncio.run(mem._apply_extraction(
        {"profile": [{"who": "甲", "love": ["常去爬山"], "note": "最近在找工作"}],
         "facts": [], "events": []},
        conv="u1", senders={"甲": 1}, bot_ids=set(), bot_names=set(),
    ))
    info = mem._db.profile.get("uid:1", {})
    check("note 已写入", info.get("note") == "最近在找工作", str(info))
    check("love 走合并", "常去爬山" in (info.get("love") or []), str(info.get("love")))


def main() -> int:
    for backend in ("sqlite", "json"):
        _roundtrip(backend)
        _readonly(backend)
        _storage_roundtrip(backend)
        _batch_roundtrip(backend)
        _clear_bucket(backend)
    _memory_api()
    _memory_batch()
    _reconcile()
    _memory_clear()
    _extraction_alignment()
    print(f"\n=== 结果：通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
    for name in FAILED:
        print("  失败：", name)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
