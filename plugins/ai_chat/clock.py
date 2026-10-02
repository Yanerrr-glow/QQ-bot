"""统一的"现在几点"：NTP 校准 + 全项目单一时间入口。

## 为什么需要它

改造前，机器人对时间的认知**完全等于宿主机的系统时钟**。这有三个真实的坑：

1. **机器时钟会漂。** 长期挂着的机器、休眠过的笔记本、虚拟化环境，时钟差几分钟到几小时
   都很常见。而它不只是"答错时间"——**定时问候是靠它触发的**：
   `greetings` 判断"到 08:00 了吗"用的就是这个钟。差 8 小时就等于早安安在半夜。
2. **容器时区 / 时钟都不受我们控制。** 现在只有 `Dockerfile` 里一行 `TZ=Asia/Shanghai`，
   它一旦被当成"无关紧要的 ENV"删掉，或宿主时钟本身不准，全程没人发现 ——
   日志里那行 `当前时间：… +0800` 是唯一线索，得有人主动去看。
3. **示例项目早就解决了这件事。** 参考目录里的 `renderer.js` 有一整套
   `ntpOffset` + `nowCal()`：**所有时间读取都走一个校准后的入口**（第 30-47 行的注释
   写得很清楚："渲染层统一时间入口"）。本项目缺的正是这一层。

## 做法：偏移量，而不是"改系统时钟"

跟参考项目一致 —— **不去动系统时钟**（那需要管理员权限，还会影响同机其它程序），
而是取回"真实时间与本地时钟的差"，存成 `offset`，之后所有时间读取都加它：

```
真实时间 = 本地时钟 + offset
```

好处是：进程内自己一致、不需要任何权限、失败时 offset=0 就自然退化成原来的行为。

## 单一入口是硬要求

`clock.now()` / `clock.localtime()` 是**本项目唯一该用来取"现在"的地方**。
`time.time()` 只允许在两种场合直接用：

* **算时长差**（`now - msg_ts`）—— 两个时间点都加同一个 offset，差不变，用哪个钟都一样；
* **性能计时**（日志里的毫秒数）—— 跟真实时刻无关。

**取"现在时刻"却直接调 `time.time()`，就是漏掉校准。** 这条约定写在
`_工具链/启动/诊断状态.py` 的检查里，也写在 `01_接口参考` 的踩坑记录里。

## NTP 客户端是自己写的

只为这一件事引一个依赖不划算（`ntplib` 也就百来行）。用 `socket` 发一个 48 字节的
SNTP 请求（模式 3 = 客户端），按 RFC 5905 算：

```
offset = ((T2 - T1) + (T3 - T4)) / 2      ← 往返延迟被对称地抵消掉
delay  = (T4 - T1) - (T3 - T2)
```

T1=我方发送时刻、T2=服务器收到、T3=服务器发出、T4=我方收到。
请求是同步阻塞的，所以调用方用 `asyncio.to_thread` 包起来。
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import threading
import time
from typing import Any

from . import settings

logger = logging.getLogger("ai_chat.clock")

# NTP 纪元（1900-01-01）与 Unix 纪元（1970-01-01）相差 2208988800 秒
_NTP_EPOCH_DELTA = 2_208_988_800

_FILE = "clock.json"

# 单次查询的超时（秒）。国内公共 NTP 通常几十毫秒，1.5 秒已经很宽裕。
_TIMEOUT = 1.5

# 合理的 offset 上限（秒）。超过这个值基本可以断定是异常应答 ——
# 比如服务器返回了未同步的时钟（stratum 0/16）或被中间设备伪造。
# 宁可退回系统时钟，也不要被一个荒谬的 offset 把定时任务带跑偏。
_MAX_SANE_OFFSET = 48 * 3600

# 应答的 stratum 白名单：0 是 kiss-o'-death，16 是未同步，都不能用
_MIN_STRATUM = 1
_MAX_STRATUM = 15


# --------------------------------------------------------------------- 状态
class _Clock:
    def __init__(self) -> None:
        self.offset: float = 0.0          # 真实时间 - 本地时钟
        self.delay: float = 0.0           # 最近一次同步的往返延迟
        self.stratum: int = 0
        self.server: str = ""             # 最近一次成功的服务器
        self.synced_at: float = 0.0       # 本地时钟下的同步时刻
        self.attempts: int = 0
        self.failures: int = 0
        self.last_error: str = ""
        self.status: str = "pending"      # pending / ok / failed / disabled
        self.loaded = False

    def ensure(self) -> None:
        if not self.loaded:
            self.load()

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        """从盘上恢复上次的 offset。

        为什么要落盘：**进程重启后、第一次 NTP 同步成功之前**有一段空窗。
        不恢复的话，"重启后头几条消息"会退回未校准的系统时钟 ——
        而重启恰好是最常见的时间跳变时机（容器重建、机器换时区）。
        """
        self.loaded = True
        path = _path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("时间校准文件损坏，按未校准继续：%s", path)
            return
        if not isinstance(raw, dict):
            return
        try:
            offset = float(raw.get("offset") or 0.0)
        except (TypeError, ValueError):
            return
        if abs(offset) > _MAX_SANE_OFFSET:
            logger.warning("恢复到的时间偏移不合理（%.0f 秒），忽略", offset)
            return
        self.offset = offset
        self.delay = float(raw.get("delay") or 0.0)
        self.stratum = int(raw.get("stratum") or 0)
        self.server = str(raw.get("server") or "")
        self.synced_at = float(raw.get("synced_at") or 0.0)
        self.status = "ok" if offset else "pending"
        if offset:
            logger.info(
                "已恢复上次的时间校准：偏移 %+.3f 秒（%s）", offset, self.server or "未知服务器"
            )

    def save(self) -> None:
        path = _path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "offset": round(self.offset, 6),
                "delay": round(self.delay, 6),
                "stratum": self.stratum,
                "server": self.server,
                "synced_at": self.synced_at,
                "attempts": self.attempts,
                "failures": self.failures,
            }
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("时间校准写盘失败：%s", path)


_state = _Clock()
_lock = threading.Lock()


def _path():
    """校准文件放在日志目录（= data/）下，跟其它运行态数据一起。

    **这里刻意用局部 import**：`config` 需要本模块的 `now()` 来做时间格式化，
    本模块又需要 `config.LOG_DIR` 定位文件 —— 顶部互相 import 会成环。
    把 `config` 的引用收进函数体，环就断开了（`config` 只在运行时被访问，那时它早已加载完）。
    """
    from . import config

    return config.LOG_DIR / _FILE


# --------------------------------------------------------------------- 时间入口
def now() -> float:
    """**本项目取"现在"的唯一入口**：本地时钟 + NTP 校准偏移。

    等价于参考项目里的 `nowCal()`。任何需要"现在时刻"的地方都该调它，
    而不是 `time.time()` —— 直接调 `time.time()` 就是漏掉校准。
    """
    _state.ensure()
    return time.time() + _state.offset


def raw() -> float:
    """未校准的本地时钟。只在"我要看系统钟本身准不准"时用（诊断、报偏差）。"""
    return time.time()


def localtime(when: float | None = None) -> time.struct_time:
    """校准后的本地时间结构体。`when` 传的是校准后的时间戳。"""
    moment = now() if when is None else float(when)
    return time.localtime(moment)


def strftime(fmt: str, when: float | None = None) -> str:
    """校准后的 `strftime`。"""
    return time.strftime(fmt, localtime(when))


def offset() -> float:
    _state.ensure()
    return _state.offset


def calibrated() -> bool:
    """有没有可用的校准偏移（0 也算未校准 —— 它跟"偏移恰好为 0"无法区分，
    但那种情况下退回系统时钟本来就是对的）。"""
    _state.ensure()
    return _state.status == "ok" and _state.offset != 0.0


# --------------------------------------------------------------------- NTP 查询
def _servers() -> list[str]:
    raw_text = str(settings.get("ntp_servers") or "")
    out = [s.strip() for s in raw_text.replace("，", ",").split(",") if s.strip()]
    return out


def _ntp_to_unix(raw: bytes) -> float:
    """把 8 字节 NTP 时间戳转成 Unix 秒。

    **这里之前是错的，而且错得很隐蔽**：原来写的是 `struct.unpack("!d", data[32:40])`，
    也就是把 NTP 时间戳当成 IEEE-754 double 读。NTP 时间戳其实是
    **64 位定点数**：高 32 位是自 1900-01-01 起的秒数，低 32 位是秒的小数部分
    （单位 1/2^32 秒）。两者位模式完全不同，读出来就是一个天文数字。

    症状：六台服务器全部返回了**格式完全合法**的应答（48 字节、Mode=4、stratum=2/3/4），
    但算出来的偏移是 -4.2e277 这种荒谬值 —— 因为不合法的是**解析**，不是服务器。
    实测见 `验证/离线验证_桩.py` 的 NTP 用例。

    另外偏移量也算错了：`Receive` 在 32、`Transmit` 在 **40**。
    原来取 32 拿到的是 Receive，且被当 double 读，两处错叠在一起。

    2036 年问题：NTP 的 32 位秒数会在 2036-02-07 回绕。这里不做 era 推断
    （需要额外记录），但 `_MIN_PLAUSIBLE` 的合法性检查会拦住回绕后的荒谬值 ——
    宁可拒绝也不要算出一个错的时间。
    """
    seconds, fraction = struct.unpack("!II", raw)
    return seconds + fraction / 2**32 - _NTP_EPOCH_DELTA


# 合法时间的下界：2000-01-01。上界：2100-01-01。
# NTP 秒数从 1900 起算，所以"正常的现在"必然落在两者之间；
# 落在外面说明**这个字节不是我们以为的那个字段**（或服务器在回垃圾）。
_MIN_PLAUSIBLE = 946_684_800.0
_MAX_PLAUSIBLE = 4_102_444_800.0


def _query(server: str, timeout: float = _TIMEOUT) -> tuple[float, float, int]:
    """问一个 NTP 服务器。返回 (offset, delay, stratum)，失败抛异常。

    报文格式（RFC 5905）：48 字节，第一个字节 LI=0 / VN=4 / Mode=3（客户端）。

    `server` 支持 `主机名:端口` 写法（默认 123）—— 离线验证就是靠它起一个
    **本地 UDP NTP 服务**来跑真实 socket 路径，而不是 mock 掉 socket。
    """
    host, _, port_text = server.partition(":")
    port = 123
    if port_text:
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(f"端口不是数字：{server}") from None

    packet = bytearray(48)
    packet[0] = 0x1B  # 00 011 011 = LI 0, VN 4, Mode 3

    t1 = time.time()
    # 用 SOCK_DGRAM + connect，这样 send/recv 都带上了对端校验：
    # 只接受"我们问的那台"的应答，避免被无关的 UDP 包污染。
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.send(packet)
        data = sock.recv(512)
        t4 = time.time()

    if len(data) < 48:
        raise ValueError(f"应答太短（{len(data)} 字节）")

    # 第一个字节：LI(2) VN(3) Mode(3)。Mode 4 = 服务器，5 = 广播
    mode = data[0] & 0x07
    if mode not in (4, 5):
        raise ValueError(f"应答 Mode={mode}，不是服务器应答")
    stratum = data[1]
    if not _MIN_STRATUM <= stratum <= _MAX_STRATUM:
        # stratum 0 = kiss-o'-death（服务器在拒绝我们，常见于限速）
        raise ValueError(f"stratum={stratum}（0=服务器拒绝，16=未同步）")

    # Receive Timestamp 在偏移 32，Transmit Timestamp 在偏移 40
    t2 = _ntp_to_unix(data[32:40])
    t3 = _ntp_to_unix(data[40:48])

    # 两个时间戳都必须落在合理区间。少了这一步，任何畸形应答都会被当成
    # 有效数据算进去 —— 上面那个 -4.2e277 就是这么来的。
    if not (_MIN_PLAUSIBLE <= t2 <= _MAX_PLAUSIBLE and _MIN_PLAUSIBLE <= t3 <= _MAX_PLAUSIBLE):
        raise ValueError("应答里的时间戳不在合理范围内（很可能不是标准 NTP 应答）")
    # 服务器自己给出的两个时刻也不该差得太离谱
    if abs(t3 - t2) > 5.0:
        raise ValueError(f"应答自相矛盾：Receive 与 Transmit 相差 {abs(t3 - t2):.1f} 秒")

    offset = ((t2 - t1) + (t3 - t4)) / 2.0
    delay = (t4 - t1) - (t3 - t2)
    return offset, delay, stratum


def sync_blocking(retries: int | None = None) -> dict[str, Any]:
    """同步一次（阻塞）。逐个试服务器，成功即止。返回结果快照。

    失败的服务器会依次尝试；全失败时**保留上一次的 offset**，不动它 ——
    拿不到新的校准不代表旧的就作废了（时钟不会因为网络断了就变准）。
    """
    _state.ensure()
    servers = _servers()
    if not servers:
        _state.status = "disabled"
        return status()

    if retries is None:
        retries = max(1, int(settings.get("ntp_retries")))

    last_error = ""
    for attempt in range(retries):
        for server in servers:
            _state.attempts += 1
            try:
                offset, delay, stratum = _query(server)
            except Exception as exc:  # noqa: BLE001 - 网络/解析失败都要继续试下一个
                last_error = f"{server}: {type(exc).__name__} {exc}"
                logger.info("NTP 查询失败 %s（%s）", server, last_error)
                continue

            if abs(offset) > _MAX_SANE_OFFSET:
                last_error = f"{server}: 偏移 {offset:.0f} 秒不合理，已忽略"
                logger.warning("%s", last_error)
                continue

            with _lock:
                _state.offset = offset
                _state.delay = delay
                _state.stratum = stratum
                _state.server = server
                _state.synced_at = time.time()
                _state.status = "ok"
                _state.last_error = ""
                _state.save()
            logger.info(
                "时间已校准：偏移 %+.3f 秒，往返 %.0f ms，stratum %d，来源 %s",
                offset,
                delay * 1000,
                stratum,
                server,
            )
            return status()

    _state.failures += 1
    _state.last_error = last_error
    # 注意：**不改 offset**。拿不到新的校准，旧的仍然比系统时钟可信。
    if _state.offset:
        _state.status = "ok"
        logger.warning("NTP 全部失败，继续用上次的偏移 %+.3f 秒（%s）", _state.offset, last_error)
    else:
        _state.status = "failed"
        logger.warning("NTP 全部失败，退回系统时钟（%s）", last_error)
    return status()


async def sync() -> dict[str, Any]:
    """异步同步一次（`/时间 校准`、启动任务、Web 控制台都用它）。"""
    if not settings.get("ntp_enabled"):
        _state.ensure()
        _state.status = "disabled"
        logger.info("NTP 校准已关闭，使用系统时钟")
        return status()
    return await asyncio.to_thread(sync_blocking)


# --------------------------------------------------------------------- 只读视图
def _stale() -> bool:
    """上次同步是不是已经过期了。过期不代表不能用，但值得在状态里说明。"""
    _state.ensure()
    if not _state.synced_at:
        return bool(_state.offset)
    interval = max(60, int(settings.get("ntp_sync_interval")))
    return (time.time() - _state.synced_at) > interval * 3


def status() -> dict[str, Any]:
    _state.ensure()
    return {
        "status": _state.status,
        "offset_seconds": round(_state.offset, 3),
        "delay_ms": round(_state.delay * 1000, 1) if _state.delay else 0.0,
        "stratum": _state.stratum,
        "server": _state.server,
        "synced_at": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_state.synced_at))
            if _state.synced_at
            else ""
        ),
        "age_seconds": round(time.time() - _state.synced_at, 1) if _state.synced_at else None,
        "stale": _stale(),
        "attempts": _state.attempts,
        "failures": _state.failures,
        "last_error": _state.last_error,
        "enabled": bool(settings.get("ntp_enabled")),
        "servers": _servers(),
        "calibrated": calibrated(),
    }


def report(now_cal: float | None = None) -> str:
    """给 `/时间 校准` 与 `/机制 时间` 看的人话报告。"""
    st = status()
    moment = now() if now_cal is None else float(now_cal)
    raw_moment = time.time()
    lines: list[str] = []

    lines.append(f"校准后的当前时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(moment))}")
    lines.append(f"系统时钟        ：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(raw_moment))}")

    state_label = {
        "ok": "已校准" if st["offset_seconds"] else "已校准（与系统时钟一致，偏差近 0）",
        "pending": "还没校准过",
        "failed": "校准失败（退回系统时钟）",
        "disabled": (
            # 关掉开关但上次的偏移还留着：这时候说"已关闭"会让人以为时间没校准，
            # 而实际上 `clock.now()` 仍在加那个偏移。说清楚"沿用上次结果"。
            "已关闭（沿用上次的校准偏移）" if st["offset_seconds"] else "已关闭（用系统时钟）"
        ),
    }.get(st["status"], st["status"])
    lines.append(f"状态            ：{state_label}")

    if st["offset_seconds"]:
        sign = "快" if st["offset_seconds"] > 0 else "慢"
        lines.append(f"系统时钟偏差    ：{sign} {abs(st['offset_seconds']):.3f} 秒（已按此校正）")
    elif st["status"] == "ok":
        lines.append("系统时钟偏差    ：几乎为 0，不用校正")

    if st["server"]:
        lines.append(
            f"来源            ：{st['server']}（stratum {st['stratum']}，"
            f"往返 {st['delay_ms']:.0f} ms，{st['synced_at']} 同步）"
        )
    if st["stale"]:
        lines.append(f"注意            ：距上次同步已 {st['age_seconds']:.0f} 秒，可能已经过期")
    if st["last_error"]:
        lines.append(f"最近一次失败    ：{st['last_error']}")
    if not st["calibrated"]:
        lines.append("（没有可用校准，时间按系统时钟算 —— 容器时区记得设成 Asia/Shanghai）")
    return "\n".join(lines)


# --------------------------------------------------------------------- 后台任务
async def loop() -> None:
    """常驻：启动后同步一次，之后按 `ntp_sync_interval` 定期同步。

    用固定 30 秒轮询而不是 `sleep(interval)`，理由跟 `proactive.loop()` 一样：
    让控制台里改的间隔能在半分钟内生效，而不是等一个旧周期走完。
    """
    _state.ensure()
    await asyncio.sleep(3)  # 等驱动起来、日志handler挂好
    while True:
        try:
            if settings.get("ntp_enabled"):
                interval = max(60, int(settings.get("ntp_sync_interval")))
                last = _state.synced_at or 0.0
                # 从没同步过 → 立刻试；否则按间隔
                if not last or (time.time() - last) >= interval:
                    await sync()
        except Exception:  # noqa: BLE001 - 后台任务绝不能崩
            logger.exception("NTP 同步轮询出错")
        await asyncio.sleep(30)
