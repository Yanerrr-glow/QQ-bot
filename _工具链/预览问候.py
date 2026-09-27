"""离线预览问候：只调 DeepSeek 生成问候文案并打印，**不连 QQ、不发任何消息**。

用途有两个：

1. 上线前先看文案像不像人话 —— 比"部署上去、等明早 8 点再看"快得多，
   也不会因为反复重启机器人而多冒一次登录风险；
2. 顺便验证 API Key / 模型名 / 时区 —— 它会按【真正注入给模型的那个格式】
   打印当前时间，容器时区不对（差 8 小时）在这里一眼就能看出来。

用法（在项目根目录下）：
    .\\.venv\\Scripts\\python.exe '_工具链\\预览问候.py'
    .\\.venv\\Scripts\\python.exe '_工具链\\预览问候.py' -n 3            # 每个时段生成 3 条
    .\\.venv\\Scripts\\python.exe '_工具链\\预览问候.py' --slot night    # 只看晚安

服务器上同理（Linux 用 .venv/bin/python）：
    .venv/bin/python _工具链/预览问候.py -n 3

退出码：0 = 生成成功（哪怕退回了兜底话术）；1 = 没配 API Key。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402

# 读同目录的 .env（nonebot.init 默认就会读），所以不需要手动 dotenv。
nonebot.init()

from nonebot.adapters.onebot.v11 import Adapter  # noqa: E402

nonebot.get_driver().register_adapter(Adapter)
nonebot.load_plugins("plugins")

from plugins.ai_chat import config, greetings, settings  # noqa: E402


async def main(count: int, only: str) -> int:
    stamp, weekday, period = config.time_parts()
    print("=" * 62)
    print("离线预览问候（不会发出任何消息）")
    print("=" * 62)
    print(f"当前时间（注入给模型的格式）：{stamp} {weekday}，{period}")
    print(f"时区标记：{config.now_stamp()}")
    print(f"模型：{config.MODEL}    接口：{config.BASE_URL}")
    print(
        f"定时问候开关：{'开' if settings.get('greet_enabled') else '关'}    "
        f"发送对象：{settings.get('greet_target')}    "
        f"补发窗口：{settings.get('greet_window')} 分钟"
    )

    if not config.API_KEY:
        print("\n[!] .env 里没有 DEEPSEEK_API_KEY —— 下面打印的会是内置兜底话术。")
        print("    填好 Key 再跑一次，才能看到模型真正会说的话。")

    slots = [s for s in ("morning", "noon", "night") if not only or s == only]
    for slot in slots:
        label = greetings.label_of(slot)
        key = {"morning": "greet_morning", "noon": "greet_noon", "night": "greet_night"}[slot]
        print(f"\n------------- {label}（控制台里配的时间点：{settings.get(key)}）-------------")
        for i in range(count):
            text = await greetings.compose(slot)
            print(f"  {i + 1}. {text}")

    print("\n提示：这里只生成文案。真要发出去，在控制台点「现在问候一次」；")
    print("      定时发送靠 greet_enabled + 三个时间点，改完即时生效、不用重启。")
    return 0 if config.API_KEY else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="离线预览定时问候的文案（不连 QQ）")
    parser.add_argument("-n", "--count", type=int, default=2, help="每个时段生成几条（默认 2）")
    parser.add_argument("--slot", choices=("morning", "noon", "night", "all"), default="all")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(max(1, args.count), "" if args.slot == "all" else args.slot)))
