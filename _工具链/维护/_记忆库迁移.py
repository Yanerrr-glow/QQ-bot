#!/usr/bin/env python3
"""记忆库迁移 / 回滚：`memories.json` ↔ `memory.db`。

## 为什么要单独一个脚本

记忆是**唯一一份不可再生**的数据（聊天记录能重建索引、表情包能重下，
但"它记得什么"丢了就真没了）。所以迁移这件事必须：

* **看得见**：先 dry-run 打印条数，确认无误再写；
* **可回滚**：原 JSON **原地保留**，任何时候都能切回去；
* **幂等**：重复跑不会翻倍，也不会覆盖成半份数据。

## 用法

```bash
# ① 先看会迁什么（不写任何文件）
docker exec -w /app ai-chat-bot python /app/_工具链/维护/_记忆库迁移.py --dry-run

# ② 迁到 SQLite（默认方向）
docker exec -w /app ai-chat-bot python /app/_工具链/维护/_记忆库迁移.py

# ③ 万一要回滚：把 db 导回 JSON，并把后端设回 json
docker exec -w /app ai-chat-bot python /app/_工具链/维护/_记忆库迁移.py --to json
#   然后在控制台「长期记忆」组把「记忆库后端」改成 json，或改 .env：
#   AI_CHAT_MEMORY_STORE=json
```

迁移**只做搬运**：不重新抽取、不重新打分、不改任何条目的内容与时间。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# settings.py 在导入期就要 get_driver().config，所以必须先 init 一遍 NoneBot
os.environ.setdefault("DRIVER", "~fastapi")
os.environ.setdefault("AI_CHAT_LOG_DIR", "/app/data/runtime")

import nonebot  # noqa: E402

nonebot.init()


def main() -> int:
    from plugins.ai_chat import config, memstore, settings

    log_dir = config.LOG_DIR
    parser = argparse.ArgumentParser(description="记忆库在 JSON 与 SQLite 之间迁移")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写任何文件")
    parser.add_argument("--to", choices=("sqlite", "json"), default="sqlite",
                        help="目标后端（默认 sqlite）")
    parser.add_argument("--backfill-vis", action="store_true",
                        help="顺手给老数据回填「可见范围」（迁移后建议跑一次，见下）")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 输出结果（给脚本消费）")
    args = parser.parse_args()

    print(f"数据目录：{log_dir}")
    print(f"现有文件："
          f"{'memories.json ' if (log_dir / 'memories.json').exists() else ''}"
          f"{'memory.db' if (log_dir / 'memory.db').exists() else ''}".strip() or "（都没有）")
    print()

    if args.backfill_vis:
        store = memstore.open_store(log_dir)
        stats = memstore.backfill_vis(
            store, master_qq=int(settings.get("master_qq") or 0), apply=not args.dry_run
        )
        print("[回填可见范围]" + ("（dry-run，未写盘）" if args.dry_run else ""))
        print(f"  事实总数 {stats['facts']}，需要改动 {stats['changed']} 条")
        for label, n in stats["breakdown"].items():
            print(f"    {label}: {n} 条")
        if args.dry_run:
            print("\n去掉 --dry-run 再跑一次即可写盘（幂等，只动 vis 一个字段）。")
        else:
            print("\n已写盘。只动了 vis 字段，其它内容与时间戳未变。")
        return 0

    if args.to == "sqlite":
        stats = memstore.migrate_json_to_sqlite(log_dir, dry_run=args.dry_run)
    else:
        stats = memstore.export_sqlite_to_json(log_dir)

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
        return 0 if stats.get("ok") else 1

    if not stats.get("ok"):
        print(f"迁移失败：{stats.get('error')}")
        return 1

    if args.to == "sqlite":
        print("将要迁移：" if args.dry_run else "已迁移：")
        print(f"  事实     {stats['facts']} 条")
        print(f"  群事件   {stats['events']} 条")
        print(f"  人物画像 {stats['profile']} 个")
        print(f"  名字索引 {stats['entities']} 个")
        print(f"  → {stats['to']}")
        if args.dry_run:
            print()
            print("原 JSON **不会被改动**。确认无误后去掉 --dry-run 再跑一次。")
        else:
            print()
            print("原 JSON 原地保留 —— 要回滚就 --to json 再导出，或把 memory_store 改回 json。")
            print("**下一步建议**：`--backfill-vis --dry-run` 看看会给老数据回填哪些可见范围"
                  "（不回填的话，老数据全是「全局可见」，会话隔离对它们不生效）。")
    else:
        print(f"已从 SQLite 导出回 JSON：{stats['facts']} 条事实 / {stats['events']} 条群事件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
