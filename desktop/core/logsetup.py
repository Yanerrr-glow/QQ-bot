"""日志：带脱敏的 logging 配置。

两件事必须做对，否则桌面端会变成一个凭据泄露渠道：

1. **handler 一定要挂**（和 bot.py 里那段注释同一个理由）：标准 logging 默认没有
   handler，`logger.info(...)` 会被静默丢掉，排查时一片空白。
2. **所有输出过 redact()**：日志会进控制台窗口、会被用户复制粘贴。
"""

from __future__ import annotations

import logging
import sys

from .util import redact_text

_CONFIGURED = False


class RedactingFilter(logging.Filter):
    """把最终写出的整行文本再过一遍脱敏。

    刻意做在 Filter 而不是 Formatter 上：这样无论谁往日志里塞了什么
    （异常里的 URL、requests 的错误文本），出口只有一处。
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging 的接口名
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - 格式化失败也不能让日志本身崩掉
            message = str(record.msg)
        cleaned = redact_text(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def setup_logging(level: int = logging.INFO, *, stream=None) -> logging.Logger:
    """配置根 logger 并返回桌面端自己的 logger。重复调用无副作用。"""
    global _CONFIGURED
    logger = logging.getLogger("qqbot.desktop")
    if _CONFIGURED:
        return logger

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
    _CONFIGURED = True
    return logger


def plugin_logger(plugin_id: str) -> logging.Logger:
    """插件用的 logger：名字里带插件 ID，便于"是哪个插件在刷屏"一眼可见。"""
    return logging.getLogger(f"qqbot.desktop.plugin.{plugin_id}")
