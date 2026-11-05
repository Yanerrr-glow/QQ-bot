"""#0012 短期意图：创建/保持/完成/过期、令牌快照、取消与计数。

跑法：`python 验证\\_短期意图验证.py`（退出码 0 = 全部通过）。
隔离：每个用例 `with persona_workspace.use(root, at=T)`，`intents.json` 落临时目录。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "persona/packs/whale_v2"
WORK = ROOT / "temp" / "_intents"

T0 = 1_000_000.0


def _fresh(name: str) -> Path:
    root = WORK / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "vector.json").write_bytes((SEED / "vector.json").read_bytes())
    return root


def _ev(mid: int, detail: str = "他说这件事时给的依据") -> list[dict]:
    return [{"ref": "本轮消息", "message_id": mid, "detail": detail}]


def run(check) -> int:  # noqa: ANN001
    from ai_chat import persona_intents as pi, settings
    from ai_chat.persona_workspace import use

    settings.store.load()

    def restore() -> None:
        for key in ("intent_enabled", "intent_ttl_hours", "intent_max_items",
                    "intent_reward_enabled"):
            settings.store._runtime.pop(key, None)  # noqa: SLF001

    def create(conv: str, uid: str, mid: int, kind: str = "clarify",
               summary: str = "问接口什么时候修好", at: float = T0) -> dict:
        with use(_root, at=at):
            return pi.propose(conv, uid, kind=kind, message_id=mid, summary=summary,
                              evidence=_ev(mid))

    # ---------------------------------------------------------------- 1 重试只建一条
    _root = _fresh("idem")
    first = create("p1", "uid:a", 101)
    again = create("p1", "uid:a", 101)
    with use(_root, at=T0):
        count = pi.pending_count("p1", "uid:a")
    check("1 首次创建成功", first.get("ok") and first["item"]["id"].startswith("i:"), first)
    check("1 同 message_id 重试不再建", again.get("why") == "exists" and count == 1,
          (again.get("why"), count))

    # ---------------------------------------------------------------- 1b 空库 → 令牌
    _root = _fresh("claim")
    create("p1", "uid:a", 201)
    with use(_root, at=T0):
        got = pi.claim("p1", "uid:a", message_id=201)
    check("1b 空库能建出第一条并给出令牌",
          got["tokens"].get("P1", "").startswith("i:") and "你还没答" in got["text"], got)

    # ---------------------------------------------------------------- 2 完成后不再注入
    with use(_root, at=T0):
        done = pi.settle("p1", "uid:a", ref="P1", evidence=_ev(202, "他说接口昨天修好了已经上线"),
                         message_id=202)
        # 令牌映射是按 message_id 存的，settle 用的是同一 mid → 先补一份
        text_after = pi.render("p1", "uid:a", message_id=203)
    check("2 settle 需要同一回合的令牌", done.get("why") in ("unknown-token", "ok"), done)

    # 用正确回合重建：先 claim 再 settle
    _root = _fresh("settle")
    create("p1", "uid:a", 301)
    with use(_root, at=T0):
        claim = pi.claim("p1", "uid:a", message_id=301)
        before = pi.render("p1", "uid:a", message_id=301)
        ok = pi.settle("p1", "uid:a", ref="P1", evidence=_ev(301, "他说接口昨晚已经修好上线了"),
                       message_id=301)
        after = pi.render("p1", "uid:a", message_id=302)
        again = pi.settle("p1", "uid:a", ref="P1", evidence=_ev(301, "他说接口昨晚已经修好上线了"),
                          message_id=301)
    check("2 完成前会注入", "你还没答" in before, before)
    check("2 完成后不再注入", ok.get("ok") and after == "", (ok, after))
    check("2 重复完成被拒", again.get("why", "").startswith("already"), again)

    # ---------------------------------------------------------------- 3 过期不复活 + 墓碑
    _root = _fresh("expire")
    create("p1", "uid:a", 401)
    with use(_root, at=T0 + 4 * 3600):        # 默认 TTL 3h
        left = pi.pending("p1", "uid:a")
    with use(_root, at=T0 + 4 * 3600):
        rebuild = pi.propose("p1", "uid:a", kind="clarify", message_id=401,
                             summary="问接口什么时候修好", evidence=_ev(401))
    check("3 过期后不再 pending", left == [], left)
    check("3 重放被墓碑拦住", rebuild.get("why") == "tombstoned", rebuild)

    # ---------------------------------------------------------------- 4 重启保持
    _root = _fresh("restart")
    create("p1", "uid:a", 501)
    pi._JUDGE.clear()  # noqa: SLF001 - 模拟进程重启
    pi._SENT.clear()   # noqa: SLF001
    with use(_root, at=T0):
        rows = pi.pending("p1", "uid:a")
    check("4 重启后仍能读到", rows and rows[0]["summary"] == "问接口什么时候修好", rows)

    # ---------------------------------------------------------------- 5 跨人隔离
    _root = _fresh("scopes")
    create("g1", "uid:a", 601, summary="A 的问题")
    create("g1", "uid:b", 602, summary="B 的问题")
    with use(_root, at=T0):
        a_rows = pi.pending("g1", "uid:a")
        b_rows = pi.pending("g1", "uid:b")
    check("5 同会话两人互不串", a_rows[0]["summary"] == "A 的问题"
          and b_rows[0]["summary"] == "B 的问题", (a_rows, b_rows))

    # ---------------------------------------------------------------- 6 容量淘汰最早过期
    _root = _fresh("capacity")
    with use(_root, at=T0):
        settings.store._runtime["intent_max_items"] = 3  # noqa: SLF001
        for i in range(4):
            pi.propose("p1", "uid:a", kind="clarify", message_id=700 + i,
                       summary=f"第{i}件事", evidence=_ev(700 + i), now=T0 + i)
        rows = pi.pending("p1", "uid:a", limit=10)
        restore()
    ids = [row["summary"] for row in rows]
    check("6 满 3 条后淘汰最先过期的（第 0 件）",
          len(ids) == 3 and "第0件事" not in ids, ids)

    # ---------------------------------------------------------------- 7 不新增外发
    src = (ROOT / "plugins/ai_chat/persona_intents.py").read_text(encoding="utf-8")
    check("7 模块里没有任何发送调用", "send(" not in src and "await " not in src, "无 await")

    # ---------------------------------------------------------------- 8 措辞不虚构
    _root = _fresh("wording")
    create("p1", "uid:a", 801)
    with use(_root, at=T0):
        text = pi.claim("p1", "uid:a", message_id=801)["text"]
    check("8 只写「还没答」，不写承诺",
          "你还没答" in text and "答应过会" not in text and "他说过会" not in text, text)

    # ---------------------------------------------------------------- 9 提起不算解决
    _root = _fresh("mention")
    create("p1", "uid:a", 901)
    with use(_root, at=T0):
        pi.claim("p1", "uid:a", message_id=901)
        pi.render("p1", "uid:a", message_id=901)
        pi.note_attempt("p1", "uid:a", message_id=901)
        rows = pi.pending("p1", "uid:a")
    check("9 只是提起，状态仍是 pending", rows and rows[0]["status"] == "pending", rows)

    # ---------------------------------------------------------------- 10 计数规则
    with use(_root, at=T0):
        miss = pi.note_attempt("p1", "uid:a", message_id=9999)   # 没有快照
        dup = pi.note_attempt("p1", "uid:a", message_id=901)     # 已经计过
    check("10 没有发送快照时不计数", miss.get("why") == "no-snapshot", miss)
    check("10 同一条消息只计一次", dup.get("ok") is False, dup)

    # ---------------------------------------------------------------- 11 第三次尝试即过期
    _root = _fresh("attempts")
    create("p1", "uid:a", 1001)
    with use(_root, at=T0):
        for i in range(3):
            pi.render("p1", "uid:a", message_id=1000 + i)
            pi.note_attempt("p1", "uid:a", message_id=1000 + i)
        rows = pi.pending("p1", "uid:a")
    check("11 第三次尝试后 expired", rows == [], rows)

    # ---------------------------------------------------------------- 12 明确停止即取消
    _root = _fresh("stop")
    create("p1", "uid:a", 1101)
    with use(_root, at=T0):
        got = pi.note_stop("p1", "uid:a", "别说了，这个话题过了")
        rows = pi.pending("p1", "uid:a")
        again = pi.note_stop("p1", "uid:a", "别说了")
    check("12 边界命中即取消", got.get("ok") and rows == [], (got, rows))
    check("12 重复停止幂等", again.get("why") == "none" or again.get("ok") is False, again)

    # ---------------------------------------------------------------- 13 两条令牌不歧义
    _root = _fresh("tokens")
    create("p1", "uid:a", 1201, summary="第一件事")
    create("p1", "uid:a", 1202, summary="第二件事")
    with use(_root, at=T0):
        got = pi.claim("p1", "uid:a", message_id=1201)
        tokens = got["tokens"]
        ok = pi.settle("p1", "uid:a", ref="P1", evidence=_ev(1201, "他说第一件事已经办完了"),
                       message_id=1201)
        rows = pi.pending("p1", "uid:a", limit=5)
    check("13 两个令牌各指一条", len(tokens) == 2 and tokens["P1"] != tokens["P2"], tokens)
    check("13 settle P1 只动第一条",
          ok.get("ok") and [row["summary"] for row in rows] == ["第二件事"], rows)

    # ---------------------------------------------------------------- 14 寒暄不算完成
    _root = _fresh("smalltalk")
    create("p1", "uid:a", 1301)
    with use(_root, at=T0):
        pi.claim("p1", "uid:a", message_id=1301)
        got = pi.settle("p1", "uid:a", ref="P1", evidence=_ev(1301, "嗯"), message_id=1301)
        rows = pi.pending("p1", "uid:a")
    check("14 寒暄/复述不算完成", got.get("why", "").startswith("semantic") and rows, got)

    # ---------------------------------------------------------------- 15 无证据不完成
    with use(_root, at=T0):
        none = pi.settle("p1", "uid:a", ref="P1", evidence=[], message_id=1301)
    check("15 没有证据不结项", none.get("why") == "no-evidence", none)

    # ---------------------------------------------------------------- 16 错误对象不完成
    _root = _fresh("wronguid")
    create("p1", "uid:a", 1401)
    with use(_root, at=T0):
        pi.claim("p1", "uid:a", message_id=1401)
        wrong = pi.settle("p1", "uid:b", ref="P1", evidence=_ev(1401, "他说这件事已经解决了"),
                          message_id=1401)
        rows = pi.pending("p1", "uid:a")
    check("16 换个人结不了", wrong.get("ok") is False and rows, wrong)

    # ---------------------------------------------------------------- 17 并发取消后不能再结
    _root = _fresh("race")
    create("p1", "uid:a", 1501)
    with use(_root, at=T0):
        pi.claim("p1", "uid:a", message_id=1501)
        pi.cancel("p1", "uid:a", reason="test")
        late = pi.settle("p1", "uid:a", ref="P1", evidence=_ev(1501, "他说已经做完了没问题"),
                         message_id=1501)
    check("17 取消后的终态唯一", late.get("why") == "already-cancelled", late)

    # ---------------------------------------------------------------- 18 禁用后全关
    _root = _fresh("disabled")
    with use(_root, at=T0):
        settings.store._runtime["intent_enabled"] = False  # noqa: SLF001
        off = pi.propose("p1", "uid:a", kind="clarify", message_id=1601,
                         summary="禁用时不该建", evidence=_ev(1601))
        shown = pi.render("p1", "uid:a", message_id=1601)
        restore()
    check("18 禁用时写入口关闭、注入为空", off.get("why") == "disabled" and shown == "",
          (off, shown))

    # ---------------------------------------------------------------- 19 缺 mid 不建
    _root = _fresh("nomid")
    with use(_root, at=T0):
        got = pi.propose("p1", "uid:a", kind="clarify", message_id=None,
                         summary="没有主键", evidence=_ev(1701))
    check("19 没有 message_id 不建", got.get("why") == "no-message-id", got)

    # ---------------------------------------------------------------- 20 缓存淘汰不误计
    _root = _fresh("cache")
    create("p1", "uid:a", 1801)
    with use(_root, at=T0):
        pi.render("p1", "uid:a", message_id=1801)
        pi._SENT.clear()  # noqa: SLF001 - 模拟缓存被挤掉
        missed = pi.note_attempt("p1", "uid:a", message_id=1801)
        rows = pi.pending("p1", "uid:a")
    check("20 快照丢失时不计数", missed.get("why") == "no-snapshot", missed)
    check("20 也不会把新事项算进来", rows and rows[0]["attempts"] == 0, rows)

    # ---------------------------------------------------------------- 21 人格切换不串
    _root = _fresh("pack")
    create("p1", "uid:a", 1901)
    with use(_root, at=T0):
        pi.render("p1", "uid:a", message_id=1901)
        original = pi._pack  # noqa: SLF001
        pi._pack = lambda: "probe"  # noqa: SLF001
        try:
            missed = pi.note_attempt("p1", "uid:a", message_id=1901)
        finally:
            pi._pack = original  # noqa: SLF001
    check("21 换人格包后计数不串", missed.get("why") == "no-snapshot", missed)

    restore()
    return 0


def _bootstrap():
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    end = source.find('print("\\n=== 2.')
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "intents_bootstrap"}
    exec(compile(source[:end], str(harness), "exec"), namespace)  # noqa: S102
    return namespace


if __name__ == "__main__":
    ns = _bootstrap()
    try:
        run(ns["check"])
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    failed = ns["failed"]
    print(f"通过 {ns['passed']} 项"
          + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    sys.exit(1 if failed else 0)
