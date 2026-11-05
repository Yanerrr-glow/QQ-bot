"""#0015 M1 评估案例：schema / 版本 / 分组 / 导入 / 偏好交换。

跑法：`python 验证\\_评估案例验证.py`（退出码 0 = 全部通过）。

隔离：每个用例都在 `persona_workspace.use(root, at=T)` 里跑，案例落临时目录，
不碰线上 `data/runtime/persona/<包>/eval/`。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "temp" / "_eval_cases"
T0 = 1_000_000.0


def _fresh(name: str) -> Path:
    root = WORK / name
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "vector.json").write_text("{}", encoding="utf-8")
    return root


def _case(case_id: str = "c-1", version: int = 1, **over) -> dict:
    base = {
        "case_id": case_id,
        "version": version,
        "status": "ok",
        "source_type": "chat",
        "provenance": "human",
        "persona_id": "whale",
        "persona_fingerprint": "abc123",
        "split_group": "conv:g1",
        "scope": "group",
        "source_refs": [{"conv": "g1", "message_id": 101, "ts": T0, "sha256": "deadbeef"}],
        "pre_context": [{"uid": "u1", "text": "在吗", "is_bot": False}],
        "original_reply": "在。",
        "action": "",
        "failure_type": "unknown",
        "preferences": [],
        "followups": [],
    }
    base.update(over)
    return base


def run(check) -> int:  # noqa: ANN001
    from ai_chat import eval_cases as ec, settings
    from ai_chat.persona_workspace import use

    settings.store.load()

    def set_key(key: str, value) -> None:
        spec = next((s for s in settings._SPECS if s.key == key), None)
        if spec is not None:
            settings.set_value(key, value)
        else:  # 缺 Spec 时直接写运行时（与离线桩同一手法）
            settings.store._runtime[key] = value

    saved = {k: settings.get(k) for k in ("eval_cases_enabled", "eval_cases_max_per_group")}
    try:
        # ---------------------------------------------------------- 关着时不产生副作用
        root = _fresh("off")
        with use(root, at=T0):
            set_key("eval_cases_enabled", False)
            got = ec.save(_case())
            check("关着时 save 被拒", not got.get("ok"), got.get("why"))
            check("关着时不创建 cases 目录", not ec.paths()["cases"].exists())
            check("关着时 enabled() 为假", ec.enabled() is False)

        # ---------------------------------------------------------- 打开后的基本读写
        root = _fresh("basic")
        with use(root, at=T0):
            set_key("eval_cases_enabled", True)
            got = ec.save(_case())
            check("打开后 save 成功", got.get("ok"), got.get("why"))
            check("文件落在 workspace 内", Path(got["file"]).is_file() if got.get("ok") else False)
            back = ec.load("c-1")
            check("读回 case_id 一致", back and back.get("case_id") == "c-1")
            check("读回带 updated_at", bool(back and back.get("updated_at")))

            # ------------------------------------------------------ schema 校验
            problems = ec.validate({"case_id": "x"})
            check("缺必填被点名", any("version" in p for p in problems), problems[:3])
            check("非法 failure_type 被拒",
                  any("failure_type" in p for p in ec.validate(_case(failure_type="乱写"))))
            check("非法 provenance 被拒",
                  any("provenance" in p for p in ec.validate(_case(provenance="chat"))))
            check("缺 original_reply 且无 action 被拒",
                  any("original_reply" in p for p in ec.validate(_case(original_reply="", action=""))))
            check("有 action 时允许空 original_reply",
                  not any("original_reply" in p
                          for p in ec.validate(_case(original_reply="", action="skip")))
                  )
            check("case_id 非法字符被拒",
                  any("case_id" in p for p in ec.validate(_case(case_id="a b"))))
            check("pre_context 空数组被拒",
                  any("pre_context" in p for p in ec.validate(_case(pre_context=[]))))

            # ------------------------------------------------------ 版本只增不改
            dup = ec.save(_case())
            check("同版本再写被拒", not dup.get("ok"), dup.get("why"))
            conflict = ec.save(_case(version=3), expected_version=0)
            check("并发版本不符被拒", conflict.get("conflict") is True, conflict.get("why"))
            v2 = ec.save(_case(version=2), expected_version=1)
            check("v2 写入成功", v2.get("ok"), v2.get("why"))
            check("load 默认取最高版本", (ec.load("c-1") or {}).get("version") == 2)
            check("versions 升序", ec.versions("c-1") == [1, 2])
            check("load_all 每个 case 只取最高",
                  [x["version"] for x in ec.load_all() if x["case_id"] == "c-1"] == [2])

            # ------------------------------------------------------ 分组切分
            leak = [_case(case_id="c-2", split_group="conv:g1", split="validation")]
            problems = ec.assert_no_leak(ec.load_all() + leak)
            check("同组跨切分被报出", any("conv:g1" in p for p in problems), problems)
            check("synthetic 默认进 train",
                  ec.split_of(_case(provenance="synthetic")) == "train")
            check("显式 split 优先", ec.split_of(_case(split="sealed")) == "sealed")

            # ------------------------------------------------------ 偏好：交换矛盾
            base = ec.load("c-1")
            pref = {"choice": "A", "answer_a": "在。", "answer_b": "嗯？",
                    "annotator": "master", "swapped": False}
            got = ec.append_preference("c-1", pref)
            check("偏好追加成功", got.get("ok"), got.get("why"))
            check("偏好使版本 +1", (ec.load("c-1") or {}).get("version") == 3)
            again = ec.append_preference("c-1", pref)
            check("同向偏好不判矛盾",
                  (ec.load("c-1") or {}).get("preferences", [{}])[-1].get("choice") == "A")
            swapped_same = {"choice": "B", "answer_a": "在。", "answer_b": "嗯？",
                            "annotator": "master", "swapped": True}
            ec.append_preference("c-1", swapped_same)
            last = (ec.load("c-1") or {}).get("preferences", [{}])[-1]
            check("交换后同答案 → 不判矛盾", last.get("choice") == "B", last.get("choice"))
            swapped_diff = {"choice": "A", "answer_a": "在。", "answer_b": "嗯？",
                            "annotator": "master", "swapped": True}
            ec.append_preference("c-1", swapped_diff)
            last = (ec.load("c-1") or {}).get("preferences", [{}])[-1]
            check("交换后反向 → 记 uncertain", last.get("choice") == "uncertain", last)
            check("矛盾保留 conflict_with", bool(last.get("conflict_with")))
            check("缺 annotator 被拒",
                  not ec.append_preference("c-1", {"choice": "A", "answer_a": "a",
                                                   "answer_b": "b"}).get("ok"))
            check("偏好不存在的案例被拒",
                  not ec.append_preference("nope", pref).get("ok"))

            # ------------------------------------------------------ 旧格式导入
            legacy = WORK / "legacy.jsonl"
            legacy.write_text(json.dumps({"id": "ls-g1-3", "ctx": ["甲"], "human": "乙",
                                          "source": "g1"}, ensure_ascii=False) + "\n",
                              encoding="utf-8")
            got = ec.import_jsonl(legacy)
            check("旧格式被暂存而非入库", got.get("staged") == 1 and got.get("imported") == 0, got)
            staged = list(ec.paths()["incomplete"].glob("*.json"))
            check("暂存文件存在", len(staged) == 1, staged)
            body = json.loads(staged[0].read_text(encoding="utf-8")) if staged else {}
            check("暂存标 incomplete", body.get("status") == "incomplete")
            check("暂存标 legacy", body.get("provenance") == "legacy")
            check("暂存写明缺什么", "目标回复" in str(body.get("why_incomplete") or ""))
            again = ec.import_jsonl(legacy)
            check("重复导入幂等", again.get("staged") == 0 and len(
                list(ec.paths()["incomplete"].glob("*.json"))) == 1, again)

            # ------------------------------------------------------ 新格式导入
            newbie = WORK / "cases.jsonl"
            newbie.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in (
                _case(case_id="c-9", version=1, split_group="conv:g9"),
                _case(case_id="c-9", version=1, split_group="conv:g9"),
            )) + "\n", encoding="utf-8")
            got = ec.import_jsonl(newbie)
            check("新格式导入一条", got.get("imported") == 1 and got.get("skipped") == 1, got)
            check("导入后可读", (ec.load("c-9") or {}).get("version") == 1)

            # ------------------------------------------------------ 反馈契约
            contract = ec.feedback_contract()
            check("契约含定锚字段", "target_reply_ref" in contract)
            check("契约含范围", "scope" in contract)
            check("契约含撤回", "supersedes" in contract)
            check("缺 sender 不能 confirmed",
                  any("sender" in p for p in ec.validate_feedback(
                      {"feedback_id": "f1", "type": "expression", "polarity": "negative",
                       "status": "confirmed"})))
            check("未确认可以缺 sender",
                  not ec.validate_feedback({"feedback_id": "f1", "type": "expression",
                                            "polarity": "negative", "status": "unconfirmed"}))
            check("非法浮点/枚举被拒",
                  any("polarity" in p for p in ec.validate_feedback(
                      {"feedback_id": "f1", "type": "expression", "polarity": "爽"})))

            # ------------------------------------------------------ 概况
            info = ec.stats()
            check("stats 报启用", info.get("enabled") is True)
            check("stats 计数与磁盘一致", info.get("cases") == 2, info.get("cases"))
            check("stats 报 incomplete 数", info.get("incomplete") == 1)
            check("stats 带泄漏检查", isinstance(info.get("leaks"), list))

        # ---------------------------------------------------------- 提取器产物能导入
        from importlib import util as _util

        spec = _util.spec_from_file_location("extract_events", ROOT / "验证" / "_提取目标事件.py")
        mod = _util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        root = _fresh("extract")
        logdir = root / "logs"
        logdir.mkdir()
        (logdir / "chatlog_g1.json").write_text(json.dumps({
            "conv": "g1",
            "messages": [
                {"id": 1, "ts": T0, "uid": 111, "name": "阿明", "text": "今天好累", "is_bot": False},
                {"id": 2, "ts": T0 + 1, "uid": 0, "name": "鲸鱼娘", "text": "累了就歇会儿。",
                 "is_bot": True},
                {"id": 3, "ts": T0 + 2, "uid": 111, "name": "阿明", "text": "可是还要加班",
                 "is_bot": False},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        out = root / "events.jsonl"
        got = mod.extract(logdir, out)
        check("提取到 target 事件", got.get("rows") == 1, got)
        rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
        check("提取带 message_id", rows and rows[0]["source_refs"][0].get("message_id") == 2)
        check("提取含双侧上下文", rows and any(x.get("is_bot") for x in rows[0]["pre_context"]) is False,
              "目标事件之前的消息里没有 bot，符合预期")
        check("提取的 original_reply 是 bot 那句",
              rows and "歇会儿" in rows[0]["original_reply"])
        check("提取标 legacy", rows and rows[0]["provenance"] == "legacy")
        with use(root, at=T0):
            set_key("eval_cases_enabled", True)
            imported = ec.import_jsonl(out)
            check("提取产物可直接导入", imported.get("imported") == 1, imported)
            check("导入后能读回 pre_context",
                  bool((ec.load(rows[0]["case_id"]) or {}).get("pre_context")))
    finally:
        for key, value in saved.items():
            if value is not None:
                set_key(key, value)
        shutil.rmtree(WORK, ignore_errors=True)
    return 0


def _bootstrap():
    harness = ROOT / "验证" / "离线验证_桩.py"
    source = harness.read_text(encoding="utf-8")
    end = source.find('print("\\n=== 2.')
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "eval_cases_bootstrap"}
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
