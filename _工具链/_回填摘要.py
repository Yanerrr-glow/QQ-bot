"""回填历史轮次的会话摘要：把摘要功能**上线之前**那些轮次补齐。

## 为什么需要它

`summaries.py` 的触发点是**会话切分**（`on_session_rolled`）—— 也就是说，
只有"功能上线之后新发生的那一轮结束"才会被总结。上线之前盘上已有的那些轮次
永远不会被总结，而 `render_background()` 又确实会把它们从 prompt 里丢掉，
于是那段时间只有一句「更早的 N 条原文没有放进本节」和**一片没有摘要的空白**。

这个断层不会自愈：会话切分只处理"刚结束的那一轮"，没有任何路径往回看。
所以它和 `_补提取.py`（补长期记忆的历史真空）是同一类一次性脚本。

## 它怎么工作

1. 扫 `data/chatlog_*.json`，取出每个会话的**轮次号**（`messages[].session`）；
2. 跳过已经有摘要的轮次（`summaries.recent()` 里出现过的 session）；
3. 对剩下的逐轮调用 `summaries.summarize_session()` —— **复用线上逻辑**，
   所以提示词、"只写记录里确实发生的事"那些纪律、"失败不抛"的行为全都一致；
4. `summarize_session()` 自己会跳过消息数不足的轮次（`summary_min_messages`），
   所以空轮/寒暄轮不会白花一次请求。

## 用法（在容器内跑，因为它要读真实 data 目录与 API Key）

    docker exec -w /app ai-chat-bot python /app/_工具链/_回填摘要.py --dry-run
    docker exec -w /app ai-chat-bot python /app/_工具链/_回填摘要.py

参数：
    --conv g123456      只处理某个会话（默认全部）
    --limit 20          每个会话最多补几轮（默认 0 = 不限）
    --max-calls 200     本次最多调几次模型（防一次性烧额度；默认 300）
    --dry-run           只列出"哪几轮缺摘要"，不调模型

退出码：0 = 跑完（含"本来就没有缺的"）；1 = 有轮次生成失败。
"""

from __future__ import annotations

import argparse
import asyncio
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

chatlog = _pkg.chatlog
summaries = _pkg.summaries
config = _pkg.config

logger = logging.getLogger("backfill_summaries")


def _window(conv: str, session: int) -> tuple[int, float, float]:
    """这一轮的 `(消息数, 起始 ts, 结束 ts)` —— 给人看进度用。"""
    log = chatlog.get_log_sync(conv)
    if log is None:
        return 0, 0.0, 0.0
    msgs = log.session_range(session, include_bot=True)
    ts = [float(m.get("ts") or 0) for m in msgs]
    return len(msgs), (min(ts) if ts else 0.0), (max(ts) if ts else 0.0)


def scan(conv_filter: str = "") -> list[tuple[str, list[int]]]:
    """扫出每个会话「有记录但没有摘要」的轮次。

    缺口判定复用插件里的 `summaries.missing_sessions()` —— 同一份逻辑放两处
    迟早会漂移，而这类"两边算法不一样"的问题表现是"漏补了几轮"，很难发现。
    """
    result = []
    for conv in sorted(chatlog.all_logs()):
        if conv_filter and conv != conv_filter:
            continue
        missing = summaries.missing_sessions(conv)
        if missing:
            result.append((conv, missing))
    return result


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


async def run(*, conv_filter: str, limit: int, max_calls: int, dry_run: bool) -> int:
    plan = scan(conv_filter)
    if not plan:
        print("没有需要补摘要的轮次（要么本来就都补齐了，要么聊天记录是空的）。")
        return 0

    need = sum(len(x[1]) for x in plan)
    print(f"扫描到 {len(plan)} 个会话、共 {need} 个轮次缺摘要。")
    print(f"闸门：`summary_min_messages` = {_pkg.settings.get('summary_min_messages')}"
          f"（消息数不足的轮次会被跳过，不花请求）；本次调用上限 {max_calls}。\n")

    for conv, missing in plan:
        print(f"· {conv}：缺 {len(missing)} 轮")
        if dry_run:
            for sid in missing[-20:]:
                n, t0, t1 = _window(conv, sid)
                print(f"    #{sid:<4} {n:>4} 条   {_fmt_ts(t0)} ~ {_fmt_ts(t1)}")
            if len(missing) > 20:
                print(f"    ……另有 {len(missing) - 20} 轮（这里只列最近 20 轮）")
    if dry_run:
        print("\n（--dry-run：一次模型都没调，什么都没写。去掉它才会真的生成。）")
        return 0

    calls = 0
    done = 0
    skipped = 0
    failed: list[tuple[str, int]] = []

    for conv, missing in plan:
        # 从**最新**往回补：最近的轮次对回顾最有用，万一中途到上限了，
        # 缺的是最久远的那几轮，而不是"最近这几十轮全都没有"。
        #
        # `--limit` 是**每会话**的上限，所以计数必须按会话重置。
        # 第一版写成用全局 done 判断，结果第一个会话补满 limit 之后，
        # 后面所有会话一条都不补、而且不报错 —— 静默少干活是最难发现的那种错。
        done_here = 0
        for sid in sorted(missing, reverse=True):
            if limit and done_here >= limit:
                print(f"  （{conv} 已达本会话上限 {limit} 轮，换下一个会话）")
                break
            if calls >= max_calls:
                print(f"\n已达到调用上限 {max_calls}，停止。")
                print("剩余未处理轮次可下次再跑（本脚本幂等，已生成的会跳过）。")
                return 0 if not failed else 1
            n, t0, t1 = _window(conv, sid)
            calls += 1
            print(f"  [{calls}/{max_calls}] {conv} #{sid}（{n} 条，{_fmt_ts(t0)}）… ",
                  end="", flush=True)
            try:
                item = await summaries.summarize_session(conv, sid)
            except Exception as exc:  # noqa: BLE001 - 单轮失败不该中断整批
                print(f"失败：{type(exc).__name__}: {exc}")
                failed.append((conv, sid))
                continue
            if item is None:
                # 两种可能：消息数不足（正常跳过）或模型没给出内容
                skipped += 1
                print("跳过（消息数不足，或这一轮没有值得回顾的内容）")
            else:
                done += 1
                done_here += 1
                print(f"OK —— {str(item.get('text', ''))[:48]}")

    print(f"\n完成：生成 {done} 条、跳过 {skipped} 条、失败 {len(failed)} 条，共调用 {calls} 次。")
    if failed:
        print("失败的轮次（下次重跑会再试）：")
        for conv, sid in failed[:20]:
            print(f"  · {conv} #{sid}")
        return 1
    print("\n补好的摘要会出现在下一轮回复的 prompt 里"
          "（「更早几轮的摘要」那一段），不用重启。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="回填历史轮次的会话摘要")
    parser.add_argument("--conv", default="", help="只处理某个会话（默认全部）")
    parser.add_argument("--limit", type=int, default=0, help="每个会话最多补几轮（0 = 不限）")
    parser.add_argument("--max-calls", type=int, default=300, help="本次最多调几次模型")
    parser.add_argument("--dry-run", action="store_true", help="只列出缺口，不调模型")
    args = parser.parse_args()

    if not config.API_KEY and not args.dry_run:
        print("[阻塞] 没配 DEEPSEEK_API_KEY —— 回填需要调模型。先配好再跑，"
              "或者用 --dry-run 只看缺口。")
        return 1
    return asyncio.run(
        run(
            conv_filter=args.conv.strip(),
            limit=max(0, args.limit),
            max_calls=max(1, args.max_calls),
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
