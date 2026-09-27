"""补提取：把**没有记忆那几天**的聊天记录补进长期记忆。

## 为什么需要它

长期记忆功能是 2026-09-23 04:17 才部署的（`memory.py` 的文件创建时间），而聊天记录
从 09-21 15:06 就开始了 —— 所以 09-21 / 09-22 两天的对话（群里 286 条 + 私聊 30 条）
**从来没有被提取过**，成了记忆里的一段真空。

而 `extract_and_store()` 只会看"最近 14 条"，永远不回溯历史，所以这个断层不会自愈。
这个脚本就是一次性把那段历史补上。

## 它怎么工作

1. 从 `chatlog_*.json` 里筛出目标时间范围内的消息，按会话分组；
2. 每批取 `--batch` 条（带少量重叠，给模型上下文），用**和线上完全相同的**提示词调模型；
3. 结果用 `memory.add_fact` / `set_profile` / `add_event` 落库 —— 复用线上逻辑，
   所以 `_find_similar` 去重、`prev_text` 更正、`_merge_unique` 画像合并都自动生效；
4. **幂等**：写入前用 `_find_by_text` 查重，重复跑不会产生重复条目。

## 用法（在容器内跑，因为它要读真实的 data 目录与 API Key）

    docker exec -w /app ai-chat-bot python /app/_工具链/_补提取.py --from 2026-09-21 --to 2026-09-23
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DRIVER", "~fastapi")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import nonebot  # noqa: E402

nonebot.init()
logging.basicConfig(level=logging.WARNING)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
nonebot.load_plugin("plugins.ai_chat")
_pkg = sys.modules["plugins.ai_chat"]
mem = _pkg.memory
log = _pkg.chatlog

logger = logging.getLogger("backfill")


def load_range(path: Path, ts_from: float, ts_to: float) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  [跳过] {path.name}: {exc}")
        return []
    msgs = [m for m in (data.get("messages") or []) if ts_from <= float(m.get("ts") or 0) < ts_to]
    return msgs


async def extract_batch(lines: list[str], existing: str) -> dict:
    """调一次模型。与线上 `memory._extract()` 用同一个提示词。

    `max_tokens` 给得比线上大（线上 800）：补提取的批次更长，800 会把 JSON 截断
    —— 实测出现过 `Unterminated string` 的批次失败。失败还会重试一次。
    """
    prompt = mem._EXTRACT_PROMPT.replace("{lines}", "\n".join(lines)).replace(
        "{existing}", existing or "（还没有）"
    )
    for attempt in range(2):
        try:
            resp = await asyncio.wait_for(
                mem._client.chat.completions.create(
                    model=_pkg.settings.get("model"),
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    max_tokens=2000,
                ),
                timeout=120,
            )
            raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            if not raw:
                continue
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as exc:
            # 截断/多余文本：截到最后一个完整的 } 再试
            print(f"[JSON坏({exc.msg[:30]})]", end="", flush=True)
            try:
                cut = raw.rindex("}")
                data = json.loads(raw[: cut + 1])
                if isinstance(data, dict):
                    return data
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            print(f"[批次失败 {type(exc).__name__}]", end="", flush=True)
        await asyncio.sleep(1.0)
    return {}


def existing_digest() -> str:
    recent = sorted(mem._db.facts, key=lambda f: float(f.get("ts", 0)), reverse=True)[:20]
    return "\n".join(f"- {f.get('text','')}" for f in recent)


def apply_result(data: dict, ts: float, conv: str) -> tuple[int, int, int]:
    """落库。返回 (新增事实, 更正事实, 新增事件)。

    `ts` 是这批消息的**原始时间**：补出来的事实必须回到当时的时刻，
    否则全都挤在"今天"，时间线反而更乱。
    """
    n_fact = n_fix = n_event = 0

    for person in (data.get("profile") or [])[:4]:
        if not isinstance(person, dict):
            continue
        who = mem._clean(person.get("who"), mem._MAX_NAME)
        if not who:
            continue

        def _lst(name: str) -> list[str]:
            raw = person.get(name)
            if isinstance(raw, str):
                raw = [raw]
            return [mem._clean(x, mem._MAX_PROFILE_ITEM) for x in (raw or []) if str(x or "").strip()]

        mem.set_profile(who, display=who, love=_lst("love"),
                        dislike=_lst("dislike"), habit=_lst("habit"))

    for fact in (data.get("facts") or [])[: int(_pkg.settings.get("memory_extract_max")) + 2]:
        if not isinstance(fact, dict):
            continue
        text = mem._clean(fact.get("text"))
        if len(text) < 4:
            continue
        try:
            importance = float(fact.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        subject = mem._clean(fact.get("subject"), mem._MAX_NAME) or "群友"
        prev = mem._clean(fact.get("prev_text"), mem._MAX_TEXT)
        if prev:
            target = mem._find_by_text(prev)
            if target is not None:
                target["prev_text"] = prev
                target["text"] = text
                target["updated_at"] = mem._now()
                n_fix += 1
                continue
        # 幂等：已经记过就不再加
        if mem._find_by_text(text, limit=0.5) is not None:
            continue
        _, created = mem.add_fact(text, scope="global", subject=subject, name=subject,
                                  conv=conv, importance=importance, source="extract", ts=ts)
        n_fact += 1 if created else 0

    for event in (data.get("events") or [])[: int(_pkg.settings.get("memory_extract_max")) + 2]:
        if not isinstance(event, dict):
            continue
        text = mem._clean(event.get("text"))
        if len(text) < 4:
            continue
        if mem._find_by_text(text, limit=0.5) is not None:
            continue
        try:
            importance = float(event.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        mem.add_event(text, conv=conv, importance=importance, ts=ts)
        n_event += 1

    return n_fact, n_fix, n_event


async def main_async(args) -> int:
    ts_from = time.mktime(time.strptime(args.start, "%Y-%m-%d"))
    ts_to = time.mktime(time.strptime(args.end, "%Y-%m-%d"))

    mem._db.ensure()
    print(f"目标区间：{args.start} ~ {args.end}")
    print(f"补提取前：facts={len(mem._db.facts)} events={len(mem._db.events)} profile={len(mem._db.profile)}")

    data_dir = ROOT / "data"
    total = {"fact": 0, "fix": 0, "event": 0, "batch": 0}
    for path in sorted(data_dir.glob("chatlog_*.json")):
        msgs = load_range(path, ts_from, ts_to)
        if not msgs:
            continue
        print(f"\n=== {path.name}：{len(msgs)} 条消息 ===")
        if args.limit:
            msgs = msgs[: args.limit]

        batches = 0
        i = 0
        while i < len(msgs) and batches < args.max_batches:
            chunk = msgs[i : i + args.batch]
            lines = []
            for m in chunk:
                who = "你" if m.get("is_bot") else str(m.get("name", ""))
                text = log.clip_text(m.get("text", ""), 120) if hasattr(log, "clip_text") else str(m.get("text", ""))[:120]
                if text:
                    lines.append(f"[{m.get('time', '')} {who}] {text}")
            if len(lines) < 3:
                i += args.batch
                continue
            # 这批消息的**原始时间**（取最后一条）：事实/事件要回到当时的时刻
            batch_ts = float(chunk[-1].get("ts") or 0) or time.time()
            conv_name = path.stem.replace("chatlog_", "")
            when = time.strftime("%m-%d %H:%M", time.localtime(batch_ts))
            print(f"  批次 {batches + 1}（{when}）：第 {i + 1}-{i + len(chunk)} 条 …",
                  end="", flush=True)
            got = await extract_batch(lines, existing_digest())
            nf, nx, ne = apply_result(got, ts=batch_ts, conv=conv_name)
            total["fact"] += nf
            total["fix"] += nx
            total["event"] += ne
            total["batch"] += 1
            print(f" → 事实+{nf} 更正{nx} 事件+{ne}")
            batches += 1
            # 重叠 3 条，给下一批一点上下文
            i += max(1, args.batch - 3)

    mem._db.save()
    print(f"\n补提取后：facts={len(mem._db.facts)} events={len(mem._db.events)} profile={len(mem._db.profile)}")
    print(f"合计：批次 {total['batch']}，新增事实 {total['fact']}，更正 {total['fix']}，新增事件 {total['event']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从 chatlog 补提取历史记忆")
    ap.add_argument("--from", dest="start", required=True, help="起始日期 YYYY-MM-DD")
    ap.add_argument("--to", dest="end", required=True, help="结束日期 YYYY-MM-DD（不含）")
    ap.add_argument("--batch", type=int, default=14, help="每批发给模型多少条消息")
    ap.add_argument("--max-batches", type=int, default=40, help="每个会话最多跑多少批")
    ap.add_argument("--limit", type=int, default=0, help="每个会话最多处理多少条消息（0=全部）")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
