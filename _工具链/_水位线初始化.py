#!/usr/bin/env python3
"""一次性：给老会话初始化「记忆抽取水位线」。

## 为什么需要这个脚本

`memory.py` 从 2026-09-25 起用**水位线**记录「每个会话抽到哪条了」，好处是
「没人 @ 机器人的长对话」和「当天额度用完的那批消息」不再永久丢（见 README 5.6.6.1）。

但升级当场水位线是空的，语义上等于「一条都没抽过」—— 后台补抽会从**远古第一条**
开始，把已经抽过的历史整批重抽一遍。实测一个活跃群里就有 **585 条**待抽：
按 `memory_extract_batch=40` 算要 15 次模型调用，直接把当天 200 条的额度烧掉大半，
换来的只是把已经记住的东西再记一遍。

这个脚本把起点对齐到**最后一次会话切分点**：只覆盖「最近这一轮里还没被抽的」，
既不重复抽旧账，又能补上真正的漏。

## 用法

```bash
# 先看会怎么改（不写盘）
docker exec -w /app ai-chat-bot python /app/_工具链/_水位线初始化.py --dry-run

# 确认后写盘
docker exec -w /app ai-chat-bot python /app/_工具链/_水位线初始化.py
```

**幂等**：已经有水位线的会话一律跳过，重复跑不会有任何变化。

## 它做了什么、没做什么

* 只写 `data/memory_extract_state.json`，**不碰** `memories.json`、不碰聊天记录；
* 不调用模型（唯一花钱的抽取动作仍然由机器人自己按额度做）；
* 已有水位的会话不动 —— 所以机器人跑起来之后再执行这个脚本也是安全的。

## 想连「远古历史」一起补怎么办

那是另一件事，用 `_补提取.py`：

```bash
docker exec -w /app ai-chat-bot python /app/_工具链/_补提取.py --from 2026-09-21 --to 2026-09-23
```

它按指定日期区间重抽，并且会把每条记忆写回**当时的时刻**（`add_fact(ts=…)`）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 这些模块在导入期就要 `get_driver().config`（settings.py 的 _env_defaults），
# 所以必须先 init 一遍 NoneBot —— 与 `离线验证.py` 的做法一致。
os.environ.setdefault("DRIVER", "~fastapi")
os.environ.setdefault("AI_CHAT_LOG_DIR", "/app/data")

import nonebot  # noqa: E402

nonebot.init()


async def main() -> int:
    from plugins.ai_chat import chatlog, memory

    parser = argparse.ArgumentParser(description="初始化记忆抽取水位线")
    parser.add_argument("--dry-run", action="store_true", help="只算不写")
    args = parser.parse_args()

    # 把磁盘上的会话都读进来（`all_logs()` 只看内存里已加载的那些）
    import glob
    from pathlib import Path

    from plugins.ai_chat import config

    files = sorted(Path(config.LOG_DIR).glob("chatlog_*.json"))
    loaded = 0
    for path in files:
        conv = path.stem[len("chatlog_"):]
        try:
            await chatlog.get_log(conv)
            loaded += 1
        except Exception as exc:  # noqa: BLE001 - 单个会话读不动不该中断
            print(f"  ! 读不动 {path.name}：{exc}")

    print(f"已加载 {loaded} 个会话（候选：{len(files)} 个文件）")
    print()

    result = memory.baseline_watermarks(apply=not args.dry_run)
    if not result:
        print("没有需要初始化的会话 —— 要么都已经有水位线了，要么还没有跨过会话边界。")
        return 0

    print(f"{'会话':<24}{'切分点':>8}{'最新':>8}{'将跳过':>8}")
    print("-" * 50)
    for conv, info in sorted(result.items()):
        skip = info["max_id"] - info["last_id"]
        print(f"{conv:<24}{info['last_id']:>8}{info['max_id']:>8}{skip:>8}")

    total_skip = sum(v["max_id"] - v["last_id"] for v in result.values())
    print()
    if args.dry_run:
        print(f"[dry-run] 将给 {len(result)} 个会话建水位线，合计跳过 {total_skip} 条已抽过的历史。")
        print("确认无误后去掉 --dry-run 再跑一次。")
    else:
        print(f"已给 {len(result)} 个会话建好水位线，跳过 {total_skip} 条已抽过的历史。")
        st = memory.extract_state_stats()
        print(f"当前抽取状态：{st}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
