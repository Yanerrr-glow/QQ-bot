"""由离线桩套件加载；所有状态与故障注入均限制在临时目录。"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock


def run(check, skip=None):
    # 公开副本不分发 `persona/packs/whale_v2`（导出脚本 `$SkipPrefix` 整包排除）——
    # 缺 seed 时**整组跳过并说明原因**，不能崩在读取那一行：CI 会红，而且掩盖别的问题。
    _seed = Path(__file__).resolve().parent.parent / "persona/packs/whale_v2"
    if not (_seed / "base.txt").exists() or not (_seed / "examples/examples.jsonl").exists():
        if skip is not None:
            skip("人格学习回归（v2 隔离回归）", "副本不分发 persona/packs/whale_v2")
        return

    from ai_chat import config, persona, persona_eval as ev, persona_examples as ex
    from ai_chat import persona_relation as rel, persona_vector as vec
    from ai_chat import persona_revision_batch as batch, context
    from ai_chat.persona_workspace import use

    with tempfile.TemporaryDirectory(prefix="persona-regression-") as directory:
        root = Path(directory)
        seed = Path(__file__).resolve().parent.parent / "persona/packs/whale_v2"
        for name in ("base.txt", "surface.txt", "forbidden.txt", "vector.json"):
            (root / name).write_bytes((seed / name).read_bytes())
        (root / "examples.jsonl").write_bytes((seed / "examples/examples.jsonl").read_bytes())
        with use(root, at=100000), patch.object(vec, "enabled", return_value=True):
            total = 0
            for i in range(100):
                result = vec.apply({"mood": .08 if i % 2 == 0 else -.08})
                total += abs(result.get("applied", {}).get("mood", 0))
            check("矢量往返变化不能绕过日限额", abs(total - .5) < 1e-6, str(total))
            check("非有限变化拒绝", not vec.apply({"energy": float("nan")}).get("ok"))
            check("未知轴参数拒绝", not vec.set_param("mood", "id", "oops").get("ok"))
            check("非法半衰期拒绝", not vec.set_param("mood", "half_life_h", -1).get("ok"))
            check("非有限参数拒绝", not vec.set_param("mood", "baseline", float("inf")).get("ok"))
            normal = vec._decay({"mood": 1}, 1, 24 * 3600)["mood"]
            held = vec._decay({"mood": 1}, 1, 24 * 3600, {"mood": 1})["mood"]
            check("极端持有超时加速回归", held < normal)
            with patch.object(rel, "_profile", return_value={}), patch.object(rel, "_msg_stats", return_value=(50, 99999)):
                before = rel.derived("test")
                with patch.object(rel, "_profile", return_value={"love": ["火锅"] * 10, "dislike": ["香菜"] * 10}):
                    after = rel.derived("test")
                check("喜好条数不改变亲近或逆反", before["play"] == after["play"] and before["offsets"] == after["offsets"])
            vec.observe("我今天单身，技术很菜", uid="test")
            check("提及话题不生成许可", not rel.topics("test"))
            check("未经确认的许可拒绝", not rel.note_topic("test", "skill").get("ok"))
            rel.set_manual("test", "play", .8)
            rel.note_topic("test", "skill", confirmed=True)
            check("显式确认可以授权", "skill" in rel.topics("test"))
            rel.note_stop("test")
            check("拒绝后立刻收回许可", not rel.topics("test"))
            check("冷却期间不能重新授权", not rel.note_topic("test", "skill", confirmed=True).get("ok"))
            examples = {x["id"]: x for x in ex._load()}
            hugging = examples["ex-comfort-02"]
            check("抱抱示例不向非主人开放", not ex._audience_ok(hugging, is_master=False))
            check("主人群聊不注入亲密示例", not ex._topic_ok(hugging, is_master=True, private=False, play=1, topics=[]))
            check("未知年龄不注入性话题示例", not ex._topic_ok(examples["ex-intimacy-01"], is_master=False, play=1, topics=["sex"]))
            check("示例 say 字符串拒绝", not ex.append({"id": "bad", "polarity": "good", "tags": ["scene:闲聊"], "ctx": ["hi"], "say": "hello"}).get("ok"))
            usage = root / "examples_usage.json"
            ex.block(scenes=["吐槽"], uid="test")
            check("隔离评估不累计示例使用次数", not usage.exists())

            original = (root / "surface.txt").read_text(encoding="utf-8")
            change = {"op": "replace", "target": "surface", "find": "闲聊一两句就够，复杂问题才展开。", "replace": "闲聊接住新信息，认真提问则完整回答。"}
            add = {"op": "add", "target": "examples", "id": "reg-example", "polarity": "good", "tags": ["scene:闲聊"], "ctx": ["对方：测试"], "say": ["你：收到测试。"]}
            rejected = batch.prepare([add, {**change, "find": "不存在的连续原文，不可匹配"}])
            check("整批校验失败不提前写示例", not rejected["ok"] and not any(x["id"] == "reg-example" for x in ex._load()))
            plan = batch.prepare([change, add])
            check("可在隔离副本中准备混合修订", plan.get("ok"), str(plan.get("why")))
            check("准备后线上文本不变", (root / "surface.txt").read_text(encoding="utf-8") == original)
            no = batch.commit(plan, source="test", verification={"passed": False})
            check("复核失败不提交", not no["ok"] and persona.surface_text() == original.strip())
            yes = batch.commit(plan, source="test", verification={"passed": True})
            check("混合修订整批提交", yes["ok"] and any(x["id"] == "reg-example" for x in ex._load()))
            undo = persona.undo_revision()
            check("撤回覆盖文本和示例", undo["ok"] and persona.surface_text() == original.strip() and not any(x["id"] == "reg-example" for x in ex._load()), str(undo))

            plan = batch.prepare([change, add])
            real_write = batch._write
            def broken(path, data):
                if path.resolve() == (root / "examples.jsonl").resolve():
                    raise OSError("injected write failure")
                return real_write(path, data)
            with patch.object(batch, "_write", side_effect=broken):
                result = batch.commit(plan, source="test", verification={"passed": True})
            check("混合写入失败恢复已写文本", not result["ok"] and persona.surface_text() == original.strip())
            plan = batch.prepare([change])
            assert plan.get("ok"), plan
            (root / "surface.txt").write_text(original + "\n并发编辑", encoding="utf-8")
            check("拒绝覆盖评估期间的修改", not batch.commit(plan, source="test", verification={"passed": True})["ok"])
            (root / "surface.txt").write_text(original, encoding="utf-8")
            with patch.object(ev, "likeness_samples", return_value=[]):
                check("缺少留出集时不提交", not asyncio.run(ev.verify_revision(plan))["passed"])
            with patch.object(context.mode, "hint", side_effect=AssertionError("must not mutate mode")):
                prompt = ev._evaluation_prompt({"ctx": ["今天好累"], "conv": "g-test", "uid": "test"})
            check("评估复用实际示例注入", "【这个状态下的说法】" in prompt)
            check("评估不强制只回一句", "只回一句，不要解释" not in prompt)

            chat_dir = root / "chats"
            chat_dir.mkdir()
            logs = {}
            for conv, start in (("z", 110), ("a", 120), ("b", 130)):
                (chat_dir / f"chatlog_{conv}.json").write_text("{}")
                logs[conv] = SimpleNamespace(messages=[{"ts": start+i, "text": "测试发言", "is_bot": i % 2 == 1, "name": "甲"} for i in range(4)])
            with patch.object(config, "LOG_DIR", chat_dir), patch.object(ev.chatlog, "get_log_sync", side_effect=logs.get):
                segments = ev._window_segments(100, cutoff=200, max_segments=1)
                check("窗口先处理时间最早而非文件名最后", segments[0]["conv"] == "z")
                state = {"last_eval_ts": 100}
                calls = []
                async def ask(messages, **kwargs):
                    calls.append(messages)
                    if len(calls) == 1:
                        raise ValueError("simulated judge failure")
                    logs["b"].messages.append({"ts": 250, "text": "评估期间新消息", "is_bot": True})
                    return '{"score":70,"issues":[],"examples":[]}'
                with patch.object(ev, "_load", side_effect=lambda: copy.deepcopy(state)), patch.object(ev, "_save", side_effect=lambda d: state.update(d)), patch.object(ev, "_ask", side_effect=ask), patch.object(ev, "enabled", return_value=True), patch.object(ev.time, "time", return_value=200):
                    result = asyncio.run(ev.evaluate_window(max_segments=2))
                check("失败片段保留重试游标", "z" not in state["window_cursors"])
                check("未选中的会话不推进游标", "b" not in state["window_cursors"])
                check("评估截止时间固定在扫描之前", result["to_ts"] == 200)
                pending = ev._window_segments(100, cutoff=300, cursors=state["window_cursors"])
                check("下次仍可见失败和未处理片段", {s["conv"] for s in pending} >= {"z", "b"})

            with patch.object(ev, "_ask_label", side_effect=AsyncMock(side_effect=["T", "T"])):
                check("偏好评估支持平局", asyncio.run(ev._preference(["hi"], "a", "b")) == .5)
            with patch.object(ev, "_ask_label", side_effect=AsyncMock(side_effect=["B", "A"])):
                check("交换位置后同一回答胜出", asyncio.run(ev._preference(["hi"], "a", "b")) == 1)


if __name__ == "__main__":
    # 桩初始化至正式用例开始之前，不运行旧用例，也不会连接机器人。
    import sys
    harness = Path(__file__).with_name("离线验证_桩.py")
    source = harness.read_text(encoding="utf-8")
    marker = 'print("\\n=== 2.'
    end = source.find(marker)
    if end < 0:
        raise RuntimeError("找不到离线桩初始化边界")
    namespace = {"__file__": str(harness), "__name__": "persona_test_bootstrap"}
    exec(compile(source[:end], str(harness), "exec"), namespace)
    run(namespace["check"])
    print("人格学习回归：", namespace["passed"], "通过；", len(namespace["failed"]), "失败", namespace["failed"])
    sys.exit(1 if namespace["failed"] else 0)
