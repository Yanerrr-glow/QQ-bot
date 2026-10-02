"""QQ 群 AI 机器人入口（NoneBot2 + OneBot v11 + DeepSeek）。

启动方式：
    python bot.py
或使用项目内脚本：
    _工具链\\启动\\启动机器人.ps1

它做三件事：
1. nonebot.init() 读取同目录 .env 完成配置；
2. 注册 OneBot v11 适配器，并按 ONEBOT_WS_URLS 主动连接 NapCat；
3. 加载 plugins/ 下的全部插件（含 ai_chat）。
"""

from __future__ import annotations

import logging

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

nonebot.init()

# 标准 logging 默认不挂 handler，插件里 logger.info(...) 会被直接丢掉 ——
# 排查问题时日志一片空白，只能靠猜。这里桥接到控制台（stderr），
# 由启动脚本一并写进 data/runtime/logs/bot.log。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

# 直接用 nonebot.load_plugins 加载，避免额外依赖 nb-cli。
nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run()
