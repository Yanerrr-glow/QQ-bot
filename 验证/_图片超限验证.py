"""图片超限防护的离线验证：**纯逻辑，不联网、不调模型**。

对应的线上故障（2026-10-02，群 100000003「反复无响应」）：

    QQ 上报 file_size=17942993（17.1MB）的 jpeg →
    `stickers._download` 用 `resp.read(_DOWNLOAD_LIMIT)` 只读到前 8MB →
    截断的 JPEG **头部完好、尾部缺失**，`_guess_ext` 照样认成 jpg →
    存进 `_recent_images`（那时**没有**尺寸闸门）→
    之后这个会话每一轮对话都把这张图塞进 `image_url` →
    DeepSeek 一律回 `400 .messages[1].image[0]: You have uploaded an unsupported image` →
    `_ask_with_tools` 的「退回普通问答」带着同一张图再撞一次 400 →
    群里表现成「@ 它反复没反应」。

这里把三处修补钉成回归用例：

1. `_download` 超限**返回空字节**，不再安静地返回半张图；
2. `_remember_image` 超限**不入缓存**（`_load_first_image` 有尺寸闸门，
   但 `_recent_image` 那条路径没有 —— 这才是毒缓存真正的入口）；
3. `_is_vision_reject` / `_strip_images` 让"图被拒"能触发**摘图重试**。

为什么用「按文件直接加载 + 舱内执行」而不是 import：
`ai_chat/__init__.py` 会拉 nonebot driver，整包导入在无 nonebot 的机器上必失败；
`stickers.py` 顶层还依赖 netguard / phash / cv2。本验证只关心上面几个纯函数，
所以把函数体**从源码里取出来**在受控命名空间里执行 —— 既跑的是真源码
（不是抄一遍的副本），也不背整棵依赖树。

用法（在项目根下）：
    python 验证/_图片超限验证.py
退出码 0 = 全过。
"""
from __future__ import annotations

import ast
import io
import logging
import os
import pathlib
import re
import sys
import time
import urllib  # 注意：stickers.py 里用的是 `urllib.request.Request`，
import urllib.request  # 所以舱内要给**包** `urllib`，只给子模块会 AttributeError

# 与 `离线验证_桩.py` 同一条：输出被管道捕获时 Python 用系统区域编码，
# 英文区域的 Windows 打中文会 UnicodeEncodeError 崩掉、退出码 1。
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parent
PKG = PROJ / "plugins" / "ai_chat"

FAILED: list[str] = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  [OK] " + name)
    else:
        FAILED.append(name)
        print("  [FAIL] " + name + ((" —— " + str(detail)) if detail else ""))


def load_source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def extract(tree: ast.Module, names: set[str]) -> list[ast.stmt]:
    """取出指定名字的顶层函数/赋值节点（保持原样，不重写）。"""
    got: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            got.append(node)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in names:
                    got.append(node)
        elif isinstance(node, ast.AnnAssign):
            # `_recent_images: dict[...] = {}` 走的是带注解的赋值，别漏。
            tgt = node.target
            if isinstance(tgt, ast.Name) and tgt.id in names:
                got.append(node)
    return got


def exec_into(nodes: list[ast.stmt], ns: dict) -> None:
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, "<extracted>", "exec"), ns)  # noqa: S102 - 受控源码


# --------------------------------------------------------------- 桩
# 从 settings.py 里**读出真实默认值**，不要在这里抄一份 —— 抄的那份会在调参后
# 悄悄漂移，于是验证还在用旧默认值跑，绿色的结论失去意义。
SETTINGS_SRC = (PKG / "settings.py").read_text(encoding="utf-8")
_m = re.search(
    r'Spec\(\s*"sticker_vision_max_kb"\s*,\s*int\s*,\s*"[^"]+"\s*,\s*(\d+)',
    SETTINGS_SRC,
)
REAL_VISION_MAX_KB = int(_m.group(1)) if _m else None


class _Settings:
    """只提供被测代码会读的那几个键；缺省值取自 settings.py 的真实默认值。"""

    def __init__(self, **over):
        self._d = {"sticker_vision_max_kb": REAL_VISION_MAX_KB, "chat_vision": True}
        self._d.update(over)

    def get(self, key, default=None):
        return self._d.get(key, default)


class _Logger:
    def __init__(self):
        self.lines: list[str] = []

    def _rec(self, level, msg, *a):
        try:
            self.lines.append(f"{level} " + (msg % a if a else str(msg)))
        except Exception:  # noqa: BLE001
            self.lines.append(f"{level} {msg}")

    def info(self, msg, *a):
        self._rec("INFO", msg, *a)

    def warning(self, msg, *a):
        self._rec("WARN", msg, *a)

    def error(self, msg, *a):
        self._rec("ERROR", msg, *a)

    def exception(self, msg, *a):
        self._rec("EXC", msg, *a)

    def debug(self, msg, *a):
        self._rec("DEBUG", msg, *a)


# =========================================================== 1. _download 超限
print("\n[1] stickers._download：超限不再返回截断的图")

stickers_src = load_source(PKG / "stickers.py")
stickers_tree = ast.parse(stickers_src)
nodes = extract(stickers_tree, {"_download", "_DOWNLOAD_LIMIT"})
check("能从 stickers.py 取到 _download 与 _DOWNLOAD_LIMIT", len(nodes) == 2, len(nodes))

# `_DOWNLOAD_LIMIT` 是表达式（8 * 1024 * 1024）不是字面量，执行一遍取值。
_limit_ns: dict = {}
exec_into(nodes, _limit_ns)
LIMIT = _limit_ns["_DOWNLOAD_LIMIT"]
check("_DOWNLOAD_LIMIT 是 8MB", LIMIT == 8 * 1024 * 1024, LIMIT)


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_download(payload: bytes):
    """用受控命名空间跑**真源码**的 _download，喂一段假的 HTTP 响应。"""
    logger = _Logger()

    class _Netguard:
        @staticmethod
        def check_url(url):
            return True, ""

        @staticmethod
        def open_checked(req, timeout=15.0):
            return _FakeResp(payload)

    ns = {
        "netguard": _Netguard,
        "urllib": urllib,
        "logger": logger,
    }
    exec_into(nodes, ns)
    return ns["_download"], ns["_DOWNLOAD_LIMIT"], logger


# 17.1MB 假 JPEG：这正是线上那张（file_size=17942993）
big = b"\xff\xd8\xff" + b"\x00" * (17_942_993 - 3)
dl, limit, log = make_download(big)
out = dl("https://example.invalid/big.jpg")
check("17.1MB 的图返回空字节（不再截断）", out == b"", f"len={len(out)}")
check("并且留下了一条可排查的日志", any("上限" in ln for ln in log.lines), log.lines)

# 边界：正好等于上限要**保留**（不能把合法图误杀）
exact = b"\xff\xd8\xff" + b"\x00" * (limit - 3)
dl, limit, _ = make_download(exact)
check("正好 8MB 的图原样返回", dl("https://example.invalid/ok.jpg") == exact)

# 小图必须毫发无损
small = b"\xff\xd8\xff" + b"x" * 5000
dl, _, _ = make_download(small)
check("小图字节完全一致", dl("https://example.invalid/s.jpg") == small)

# 空响应
dl, _, _ = make_download(b"")
check("空响应返回空字节", dl("https://example.invalid/e.jpg") == b"")


# ================================================= 2. _remember_image 尺寸闸门
print("\n[2] __init__._remember_image：超限不入「最近图片」缓存")

init_src = load_source(PKG / "__init__.py")
init_tree = ast.parse(init_src)
rm_nodes = extract(
    init_tree,
    {"_remember_image", "_recent_image", "_recent_images", "_RECENT_IMAGE_TTL"},
)
check(
    "能从 __init__.py 取到缓存三件套",
    len(rm_nodes) == 4,
    [getattr(n, "name", "assign") for n in rm_nodes],
)


def make_cache(max_kb=None):
    ns = {
        "time": time,
        "settings": _Settings(
            **({} if max_kb is None else {"sticker_vision_max_kb": max_kb})
        ),
        "logger": _Logger(),
    }
    exec_into(rm_nodes, ns)
    return ns


check(
    "读到了 settings.py 的真实默认值",
    REAL_VISION_MAX_KB is not None and REAL_VISION_MAX_KB > 0,
    REAL_VISION_MAX_KB,
)

ns = make_cache()
ns["_remember_image"]("g1", b"\xff\xd8\xff" + b"a" * 100_000)  # ~98KB，合法
check("98KB 的图正常入缓存", ns["_recent_image"]("g1") is not None)

ns["_remember_image"]("g1", big)  # 17.1MB，超限
check("17.1MB 的图不入缓存", ns["_recent_image"]("g1") is None)
check(
    "日志写明了拒绝原因",
    any("最近图片不缓存" in ln for ln in ns["logger"].lines),
    ns["logger"].lines,
)

# 用真实默认值的两侧做边界：默认值本身要**能**入缓存（否则等于把上限白调了），
# 超过它一个字节就要被拒。
_default_bytes = int(REAL_VISION_MAX_KB * 1024)
ns_d = make_cache()
ns_d["_remember_image"]("gd", b"\xff\xd8\xff" + b"d" * (_default_bytes - 3))
check(
    f"正好默认上限 {REAL_VISION_MAX_KB}KB 的图能入缓存",
    ns_d["_recent_image"]("gd") is not None,
)
ns_d["_remember_image"]("gd", b"\xff\xd8\xff" + b"d" * (_default_bytes + 1024))
check(f"超过默认上限 {REAL_VISION_MAX_KB}KB 的图被拒", ns_d["_recent_image"]("gd") is None)

# 线上那张 17.1MB 在旧默认值(800KB)下也是被拒的 —— 这条钉住"调大上限不等于放行一切"
ns_old = make_cache(max_kb=800)
ns_old["_remember_image"]("go", big)
check("调大上限前后，17.1MB 都入不了缓存", ns_old["_recent_image"]("go") is None)

# 关键回归：超限图**必须把同会话的旧缓存也清掉**，否则"最后一张好图"会一直留着，
# 而它已经不是用户当下问的那张 —— 但那属于语义问题，这里只钉"不会留下超限的图"。
# ⚠ 这里**显式**传 800：要验的是"上限是多少就按多少挡"，不是当前默认值。
# （默认值本身另有用例覆盖，见上面按 REAL_VISION_MAX_KB 生成的那两条。）
ns2 = make_cache(max_kb=800)
ns2["_remember_image"]("g2", b"\xff\xd8\xff" + b"b" * 900_000)  # ~879KB > 800KB
check("上限设 800KB 时，879KB 的图不入缓存", ns2["_recent_image"]("g2") is None)
ns2["_remember_image"]("g2", b"\xff\xd8\xff" + b"b" * 700_000)  # ~684KB < 800KB
check("同一上限下，684KB 的图照常入缓存", ns2["_recent_image"]("g2") is not None)

ns3 = make_cache(max_kb=2000)
ns3["_remember_image"]("g3", b"\xff\xd8\xff" + b"c" * 900_000)
check("上限调到 2000KB 后同样的图可以入缓存", ns3["_recent_image"]("g3") is not None)


# ============================================ 3. 视觉 400 的识别与摘图重试
print("\n[3] 视觉 400 必须能触发「摘图重试」")

helper_nodes = extract(init_tree, {"_strip_images", "_is_vision_reject"})
check("能取到两个兜底辅助函数", len(helper_nodes) == 2)
hns: dict = {}
exec_into(helper_nodes, hns)
is_reject = hns["_is_vision_reject"]
strip = hns["_strip_images"]

# 线上那条 400 的原文
REAL_400 = (
    "Error code: 400 - {'error': {'message': "
    "'.messages[1].image[0]: You have uploaded an unsupported image. "
    "Please make sure your image is valid and has one of the following formats: "
    "webp, png, jpeg, and gif.', 'type': 'invalid_request_error'}}"
)
check("认得线上那条 400 原文", is_reject(Exception(REAL_400)) is True)

check(
    "不把不相关的 400 误判成图片问题",
    is_reject(Exception("Error code: 400 - context length exceeded")) is False,
)
check(
    "不把超时误判成图片问题",
    is_reject(TimeoutError("timed out")) is False,
)

msgs = [
    {"role": "system", "content": "人设"},
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "这是什么"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        ],
    },
]
stripped = strip(msgs)
check("摘图后没有 image_url 段", "image_url" not in repr(stripped))
check("摘图后文本保留", stripped[1]["content"] == "这是什么")
check("纯文本消息不受影响", strip([{"role": "system", "content": "人设"}])[0]["content"] == "人设")

# 多图 + 多个文本块
multi = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "A"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}},
            {"type": "text", "text": "B"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BB"}},
        ],
    }
]
check("多图多文本块全摘干净", strip(multi)[0]["content"] == "A\nB")


# ============================================================ 4. 源码级守卫
print("\n[4] 源码级守卫（防止将来被改回去）")

check(
    "_download 多读 1 字节做超限探测",
    "resp.read(_DOWNLOAD_LIMIT + 1)" in stickers_src,
)
check(
    "_ask_with_tools 的退回分支会摘图",
    "_strip_images(messages)" in init_src,
)
check(
    "_reply 的异常兜底会摘图重试",
    "摘图后最后重试一次" in init_src,
)
check(
    "兜底重试没有重入 _semaphore（那会自锁）",
    "不要再 `async with _semaphore`" in init_src,
)


print()
if FAILED:
    print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项：")
    for name in FAILED:
        print("  - " + name)
    sys.exit(1)
print(f"全部通过（{PASSED} 项）")
sys.exit(0)
