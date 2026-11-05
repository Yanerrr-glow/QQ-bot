"""#0015 M4 取样与反馈：事件单位、去重、游标、身份 fail-closed、反馈契约。

跑法：`python 验证\\_评估反馈验证.py`（退出码 0 = 全部通过）。

全部离线：不联网、不读真实聊天记录（用构造的会话文件）。
"""
from __future__ import annotations

import json
import shutil
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "temp" / "_eval_feedback"
T0 = time.time() - 86400.0  # 一天前：落在取样窗口内（D3 上限一周）


def _fresh(name: str) -> Path:
    root = WORK / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "vector.json").write_text("{}", encoding="utf-8")
    return root


def _messages() -> list[dict]:
    return [
        {"id": 1, "ts": T0, "uid": 111, "name": "阿明", "text": "今天好累", "is_bot": False},
        {"id": 2, "ts": T0 + 1, "uid": 0, "name": "鲸鱼娘", "text": "累了就歇会儿。", "is_bot": True},
        {"id": 3, "ts": T0 + 2, "uid": 222, "name": "小李", "text": "我也累", "is_bot": False},
        {"id": 4, "ts": T0 + 3, "uid": 0, "name": "鲸鱼娘", "text": "那就一起歇。", "is_bot": True},
    ]


def run(check) -> int:  # noqa: ANN001
    from ai_chat import eval_cases as ec, eval_sampling as es, settings, signals
    from ai_chat import instructions
    from ai_chat.persona_workspace import use

    settings.store.load()
    saved = {k: settings.get(k) for k in
             ("eval_cases_enabled", "eval_sampling_enabled", "eval_sampling_per_conv",
              "eval_sampling_seed", "eval_sampling_limit")}
    try:
        for key, value in (("eval_cases_enabled", True), ("eval_sampling_enabled", True),
                           ("eval_sampling_per_conv", 5), ("eval_sampling_seed", 0),
                           ("eval_sampling_limit", 20)):
            if any(s.key == key for s in settings._SPECS):
                settings.set_value(key, value)
            else:
                settings.store._runtime[key] = value

        # ---------------------------------------------------------- 纯函数：事件单位
        events = es.collect("g1", _messages(), context=3)
        check("只取 bot 事件", len(events) == 2, len(events))
        check("事件带 message_id", events[0]["message_id"] == 2)
        check("事件带前文（含 bot 自己）",
              any(x["is_bot"] for x in events[1]["pre_context"]),
              events[1]["pre_context"])
        check("事件带本轮问句", events[0]["question"] == "今天好累", events[0]["question"])
        check("since 过滤生效", len(es.collect("g1", _messages(), since=T0 + 2)) == 1)
        # D3：取样时效上限一周。8 天前的事件不该进来。
        old_msgs = _messages() + [{"id": 9, "ts": time.time() - 8 * 86400, "uid": 0,
                                   "name": "鲸鱼娘", "text": "很久以前的回复", "is_bot": True}]
        recent = es.collect("g1", old_msgs, since=time.time() - 168 * 3600)
        check("超过一周的事件不采（D3）",
              all(item["ts"] >= time.time() - 168 * 3600 for item in recent),
              [item["message_id"] for item in recent])
        check("一周内的旧事件仍在池子里",
              any(item["message_id"] == 9 for item in es.collect("g1", old_msgs, since=0)))

        # ---------------------------------------------------------- 入队 / 去重 / 游标
        root = _fresh("queue")
        with use(root, at=T0 + 10):
            got = es.sample("g1")
            check("sample 读不到记录时明确失败", got.get("ok") is False, got.get("why"))
            log = root / "_runtime"
            log.mkdir(parents=True, exist_ok=True)
            (log / "chatlog_g1.json").write_text(
                json.dumps({"conv": "g1", "messages": _messages()}, ensure_ascii=False),
                encoding="utf-8")

            got = es.sample("g1")
            check("sample 成功取到事件", got.get("ok") and len(got["events"]) == 2, got.get("events"))
            check("sample 回报窗口小时数（D3 = 168）", got.get("window_hours") == 168,
                  got.get("window_hours"))
            first = es.enqueue(got["events"], route="handpick", queue="diagnostic")
            check("入队成功", first.get("ok") and first["added"] == 2, first)
            check("游标推进到最晚一条", es.cursor().get("g1") == T0 + 3, es.cursor())
            again = es.sample("g1")
            check("游标生效后不再重复取", again["events"] == [], again["events"])
            dup = es.enqueue(got["events"])
            check("重复入队被跳过", dup.get("skipped") == 2, dup)
            check("非法 route 被拒", not es.enqueue(got["events"], route="乱写").get("ok"))
            check("非法 queue 被拒", not es.enqueue(got["events"], queue="乱写").get("ok"))

            due = es.due(10)
            check("due 取到待处理", len(due) == 2, len(due))
            check("due 可按队列过滤",
                  len(es.due(10, queue="diagnostic")) == 2 and es.due(10, queue="representative") == [])
            key = due[0]["key"]
            check("标记 done", es.mark(key, "done")["ok"])
            check("标记后不再 pending", all(x["key"] != key for x in es.due(10)))
            check("非法状态被拒", not es.mark(key, "乱写").get("ok"))
            info = es.stats()
            check("stats 分队列计数", info["by_queue"].get("diagnostic") == 2, info["by_queue"])
            check("stats 分状态计数", info["by_status"].get("done") == 1, info["by_status"])

        # ---------------------------------------------------------- 反馈：契约与落库
        root = _fresh("feedback")
        with use(root, at=T0):
            check("契约含目标引用", "target_reply_ref" in ec.feedback_contract())
            good = {"feedback_id": "f-1", "type": "expression", "polarity": "negative",
                    "scope": "event", "sender": "111", "status": "unconfirmed"}
            got = ec.save_feedback(good)
            check("反馈可入库", got.get("ok"), got.get("why"))
            check("默认未确认", got.get("status") == "unconfirmed", got)
            check("列表可读回", len(ec.feedback_list()) == 1)
            check("缺 sender 不能 confirmed",
                  not ec.save_feedback({**good, "feedback_id": "f-2", "status": "confirmed",
                                        "sender": ""}).get("ok"))
            check("非法类型被拒",
                  not ec.save_feedback({**good, "feedback_id": "f-3", "type": "乱写"}).get("ok"))
            check("stats 带反馈计数", ec.stats()["feedback"] == 1, ec.stats())

        # ---------------------------------------------------------- 身份链 fail-closed
        root = _fresh("identity")
        with use(root, at=T0):
            n = instructions._record_style_signal("简短点", conv="g1", uid="12345")
            check("有身份时记账", n >= 1, n)
            items = signals.all_items()
            check("记下真实 uid", any(x.get("uid") == 12345 for x in items),
                  [x.get("uid") for x in items])
            check("有身份标 confirmed",
                  all(x.get("status") == "confirmed" for x in items if x.get("uid") == 12345))

            n2 = instructions._record_style_signal("正经点", conv="g1")
            items = signals.all_items()
            unconfirmed = [x for x in items if x.get("uid") == 0]
            check("缺身份时给出 unconfirmed", unconfirmed and
                  all(x.get("status") == "unconfirmed" for x in unconfirmed), unconfirmed)
            check("缺身份不冒用主人",
                  not any(x.get("uid") == int(settings.get("master_qq") or 0) and
                          x.get("status") == "unconfirmed" for x in items))
            check("记录带 message_id 字段", all("message_id" in x for x in items))
            check("记录带 quoted_id 字段", all("quoted_id" in x for x in items))
    finally:
        for key, value in saved.items():
            if value is not None:
                if any(s.key == key for s in settings._SPECS):
                    settings.set_value(key, value)
                else:
                    settings.store._runtime[key] = value
        shutil.rmtree(WORK, ignore_errors=True)
    return 0


def _bootstrap():
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    end = source.find('print("\\n=== 2.')
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "eval_feedback_bootstrap"}
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
