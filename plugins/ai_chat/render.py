"""渲染兜底：HTTP 抓不到正文时，用真浏览器截图 + 本地 OCR 把内容读出来。

## 为什么需要它

`fetch.py` 走的是普通 HTTP 客户端。百度百科、知乎这类站点**按机房 IP 段直接 403**
（实测：换浏览器 UA、补 Referer、改 Accept-Encoding 全都没用）。但它们拦的是
"HTTP 客户端特征"，不是内容本身 —— 真实浏览器渲染出来往往能过。

所以这里做一条**兜底通道**：真浏览器打开页面 → 截图 → 本地 OCR 出中文文本 → 进 prompt。

## 为什么是 OCR 而不是"把截图给模型看"

**先纠正一个容易写错的前提**：不是"模型不支持图片"。实测 `deepseek-flash` /
`deepseek-chat` 都能真正读图（`deepseek-v4-pro` 接口接受图片、但会回
「我无法查看这张图片」）—— 完整结论表见 `stickers.py` 文件头，**别在这里再写一份**。

真正的理由是**架构上的**：截图是本机浏览器产出的临时文件，而现行契约是
"把图片本体编码进消息内容"（`stickers.content_with_images`）。从"本地文件"
到"消息里的图片块"这条路现在**不存在**，所以先在本机把文字认出来最省事。

将来如果补上"把本地文件发给模型"的通道（见改进清单 C2），这条兜底就可以
改成直接把截图交出去，OCR 退成"连模型都读不到"时才用的最后一档。

## 代价（这也是它只做兜底、不做主路径的原因）

| | 普通 HTTP（fetch.py） | 渲染兜底（这里） |
|---|---|---|
| 单页耗时 | 0.2–2s | **5–15s** |
| 内存峰值 | 几 MB | **200–300MB** |
| 依赖 | 标准库 | Chromium + 字体 + OCR 模型 |

本机总内存 1.6GB、可用常在 700MB 上下，所以这里：
**单飞锁**（同时只允许一次渲染）、**严格超时**、**用完释放页面**。

## 浏览器只启一次

Chromium 冷启动要 1–3s。每次渲染都开关的话，一次搜索预读要白花好几秒，
所以进程内**共享一个浏览器实例**，用完不关；崩了下次重建。
`shutdown()` 用来关掉共享浏览器。**注意：目前没有任何调用方** ——
插件侧还没接停机钩子（`__init__.py` 只有 `on_startup`），所以进程退出时 Chromium 由系统回收。
接优雅停机时（`@_driver.on_shutdown`）要记得把 `render.shutdown()` 与 `msgindex.close()` 一起调上。

## 与 fetch.py 一致的三条底线

* 同样先做 SSRF 检查（复用 `fetch._check_url`）—— 浏览器能访问的东西比 HTTP 宽得多
  （`file://`、内网、`localhost`），这一步**更不能省**；
* 截图体积、页面高度、OCR 字数都有硬上限；
* 出口文本统一消毒，渲染进 prompt 时声明"这是别人写的"。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

# 渲染出来的是**外部文本**（网页 DOM 或 OCR 结果），进 prompt 前必须过统一消毒层。
# 早先这里只调 `_clean()`（那只是空白归一化），而 `fetch.fetch_page()` 又直接
# `return rendered` —— 于是"模型主动 web_fetch 一个需要渲染的页面"整条支路绕过防护。
from . import untrusted

logger = logging.getLogger("ai_chat.render")

# --------------------------------------------------------------------- 硬上限
_MAX_HEIGHT = 2600        # 截图最高像素。⚠️ OCR 耗时随文字行数**线性**涨：
                          # 1280×900 的百科页要 37s，所以高度必须压住
_SCALE_WIDTH = 900        # 缩到这个宽度再 OCR（实测明显快于 1100，识别率仍够）
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
_NAV_TIMEOUT_MS = 20000   # 页面导航超时
_SETTLE_MS = 2200         # 导航完成后再等一会儿，让异步内容渲染出来

# **DOM 够用就不用 OCR**。实测（百度百科）：
#   浏览器渲染后 DOM 有 11706 字、8.0s 拿到；同一页 OCR 只有 836 字、还要再花 37.2s。
# 因为反爬拦的是 HTTP 客户端，浏览器渲染出来的 DOM 是完整可读的 ——
# 所以 OCR 只是"DOM 也拿不到字"（纯 canvas/图片正文、或 DOM 被刻意清空）时的兜底。
_DOM_ENOUGH_CHARS = 200

# 明显是"被拦住"的页面特征：认出来就别费劲了，直接说读不到。
# 「安全验证」这条是实测来的：知乎对未登录+机房 IP 会渲染出一个标题为
# 「安全验证 - 知乎」、正文只有"请您登录后查看更多专业优质内容"的空壳页 ——
# 不认出来的话，模型会拿这 29 个字当正文，比读不到更糟。
_BLOCK_HINTS = (
    "请开启JavaScript", "请开启 JavaScript", "安全验证", "验证码",
    "访问过于频繁", "Forbidden", "Access Denied", "人机验证",
    "just a moment", "checking your browser",
    "请您登录后查看", "登录后查看更多", "请先登录",
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 反 headless 检测：把最常见的几个痕迹抹掉。不是万灵药，但成本极低。
_STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

# --------------------------------------------------------------------- 状态
_browser: Any = None            # 共享的 Chromium 实例
_playwright: Any = None
_lock: asyncio.Lock | None = None
_ocr: Any = None                # 共享的 OCR 引擎（第一次用才加载模型）
_last_error = ""
_started_at = 0.0


def _get_lock() -> asyncio.Lock:
    """锁要绑在当前事件循环上，不能在 import 时就建（那时可能还没有 loop）。"""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def deps_ready() -> tuple[bool, str]:
    """渲染依赖装了没。给控制台和 `available()` 用 —— 没装就别假装能渲染。"""
    try:
        import playwright  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "没装 playwright（镜像需要包含 requirements-render.txt 的依赖）"
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "没装 rapidocr-onnxruntime（OCR 引擎）"
    try:
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "没装 Pillow（截图缩放要用）"
    return True, ""


async def _ensure_browser() -> Any:
    """拿到共享浏览器；没有就起一个。失败抛异常，由调用方转成"读不到"。"""
    global _browser, _playwright, _last_error, _started_at
    if _browser is not None and getattr(_browser, "is_connected", lambda: False)():
        return _browser
    # 旧实例已断：清掉再重建
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:  # noqa: BLE001
            pass
        _browser = None
    from playwright.async_api import async_playwright
    if _playwright is None:
        _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",                     # 容器里没有特权，必须关
            "--disable-dev-shm-usage",          # /dev/shm 在容器里通常只有 64MB，不关容易崩
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            # 本机总内存只有 1.6GB（还要养 NapCat + SearXNG + bot 本身），
            # 所以把 Chromium 的内存往死里压。这些都是关缓存/关后台功能的开关，
            # 对"打开一页读文字"没有任何影响。
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-features=Translate,BackForwardCache,site-per-process",
            "--js-flags=--max-old-space-size=192",
            "--window-size=1280,900",
        ],
    )
    _started_at = time.time()
    logger.info("已启动共享 Chromium（渲染兜底就绪）")
    _last_error = ""
    return _browser


async def shutdown() -> None:
    """进程退出时收尾。没起过就什么都不做。"""
    global _browser, _playwright, _ocr
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:  # noqa: BLE001
            pass
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        _playwright = None
    _ocr = None


# --------------------------------------------------------------------- OCR
def _ocr_image(path: str, max_chars: int) -> str:
    """对截图做 OCR。**阻塞**（ONNX 推理），调用方放到线程里跑。

    第一次调用会加载模型（本地文件，约 15MB），所以引擎缓存在模块级。
    """
    global _ocr, _last_error
    try:
        if _ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr = RapidOCR()
        result, _elapse = _ocr(path)
    except Exception as exc:  # noqa: BLE001 - OCR 失败只该降级，不该炸
        _last_error = f"{type(exc).__name__}: {exc}"[:160]
        logger.warning("OCR 失败：%s", _last_error)
        return ""
    if not result:
        return ""
    # result 形如 [[box, text, score], ...]，按识别顺序拼即可（自上而下、自左而右）
    parts = [str(item[1]).strip() for item in result if len(item) > 1 and item[1]]
    return "\n".join(p for p in parts if p)[:max_chars]


def _shrink_screenshot(path: str) -> None:
    """把截图缩到 OCR 友好的尺寸，原地覆盖。

    为什么必须缩：1280 宽的全页长图直接喂 OCR 很慢，而字号缩到一定程度反而认得准
    （OCR 对 20–40px 的文字最稳）。同时顺手压掉体积，省内存和磁盘。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.width <= _SCALE_WIDTH:
                return
            ratio = _SCALE_WIDTH / float(im.width)
            resized = im.resize((_SCALE_WIDTH, max(1, int(im.height * ratio))), Image.LANCZOS)
            resized.save(path, format="PNG", optimize=True)
    except Exception:  # noqa: BLE001 - 缩放失败就用原图，不中断
        logger.debug("截图缩放失败（用原图）", exc_info=True)


# --------------------------------------------------------------------- 主流程
async def render_blocking_async(
    url: str,
    *,
    timeout: float = 45.0,
    max_chars: int = 1500,
    screenshot_dir: str = "/tmp/qqbot-shots",
) -> dict[str, Any]:
    """渲染 + 截图 + OCR，返回 `{url, title, text, error, source, elapsed}`。

    **任何失败都返回空正文 + 原因，不抛异常** —— 它只是兜底通道，
    失败之后调用方会退回摘要，用户感受不到"报错"。
    """
    global _last_error
    from . import fetch  # 局部导入：避免 fetch <-> render 循环导入

    import os

    started = time.time()
    url = str(url or "").strip()

    # ① 安全：浏览器能碰的东西比 HTTP 宽（file://、内网 localhost），这一步绝不能省
    ok, reason = fetch._check_url(url)  # noqa: SLF001 - 同包内的受控复用
    if not ok:
        return _fail(url, reason, started)

    ready, why = deps_ready()
    if not ready:
        return _fail(url, f"渲染兜底不可用：{why}", started)

    dom_title = ""
    dom_text = ""
    ocr_text = ""
    status = 0
    render_err = ""
    # ② 单飞：内存只够一个 Chromium + 一次 OCR，硬性串行
    async with _get_lock():
        page = None
        try:
            browser = await _ensure_browser()
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=_UA,
                locale="zh-CN",
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )
            await context.add_init_script(_STEALTH)
            page = await context.new_page()
            page.set_default_timeout(_NAV_TIMEOUT_MS)

            resp = await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            status = getattr(resp, "status", 0) if resp else 0
            await page.wait_for_timeout(_SETTLE_MS)

            try:
                dom_title = (await page.title() or "").strip()
                dom_text = await page.evaluate(
                    "() => { const b = document.body;"
                    " return b ? (b.innerText || b.textContent || '') : ''; }"
                )
            except Exception:  # noqa: BLE001
                logger.debug("取 DOM 文本失败（不影响后续截图）", exc_info=True)

            dom_clean = _clean(dom_text)
            # ③ 决定要不要 OCR —— **DOM 够用就不 OCR**（见 `_DOM_ENOUGH_CHARS` 的实测数据）。
            #    只有 DOM 拿不到字时才截图 + OCR，否则白等 30 多秒。
            if len(dom_clean) >= _DOM_ENOUGH_CHARS:
                logger.info("渲染取到 DOM 正文 %d 字，跳过 OCR（省时）", len(dom_clean))
            else:
                os.makedirs(screenshot_dir, exist_ok=True)
                shot = os.path.join(screenshot_dir, f"{int(time.time() * 1000)}.png")
                await page.screenshot(
                    path=shot,
                    clip={"x": 0, "y": 0, "width": 1280, "height": _MAX_HEIGHT},
                    animations="disabled",
                )
                try:
                    if os.path.getsize(shot) > _MAX_SCREENSHOT_BYTES:
                        _shrink_screenshot(shot)
                except OSError:
                    pass
                # OCR 放线程里（ONNX 推理阻塞，别卡事件循环）
                ocr_text = await asyncio.to_thread(_shrink_and_ocr, shot, max_chars)
        except Exception as exc:  # noqa: BLE001 - 浏览器起不来 / 导航失败 / 截图失败
            render_err = f"{type(exc).__name__}: {exc}"[:160]
            _last_error = render_err
            logger.info("渲染失败 %s（%s）", url[:110], render_err)
        finally:
            # 页面/上下文一定要关（**浏览器本身留着复用**），否则一页泄漏一个 context
            if page is not None:
                try:
                    await page.context.close()
                except Exception:  # noqa: BLE001
                    pass

    if render_err:
        return _fail(url, render_err, started)

    # ④ 选更好的那份：OCR 与 DOM 取字数多的
    #    （DOM 有字时通常更准；但纯图片/canvas 正文的页面只有 OCR 有货）
    #
    # 消毒放在**选定之后、进 prompt 之前**：`_clean` 只管排版（压空白、留段落），
    # 注入清洗是 `untrusted.sanitize` 的事，两者不能互相替代。
    dom_clean = _clean(dom_text)
    ocr_clean = _clean(ocr_text)
    if len(dom_clean) >= len(ocr_clean):
        text, source = dom_clean, "dom"
    else:
        text, source = ocr_clean, "ocr"
    text = untrusted.sanitize(text)
    safe_title = untrusted.sanitize(_clean(dom_title))[:200]

    elapsed = time.time() - started
    # 标题也要一起查：知乎那类是**标题**写着「安全验证」而正文只有一句登录提示，
    # 只看正文会漏判。
    haystack = f"{dom_title}\n{text}".lower()
    hit = [h for h in _BLOCK_HINTS if h.lower() in haystack]
    if hit:
        return {
            "url": url, "title": safe_title, "text": "",
            "error": f"页面看起来是验证/拦截页（命中「{hit[0]}」）",
            "source": "blocked", "elapsed": round(elapsed, 1),
        }
    if not text:
        return {
            "url": url, "title": safe_title, "text": "",
            "error": f"渲染成功但没读到文字（HTTP {status}）",
            "source": source or "none", "elapsed": round(elapsed, 1),
        }
    logger.info("渲染兜底成功 %s → %s %d 字，%.1fs", url[:90], source, len(text), elapsed)
    return {
        "url": url, "title": safe_title, "text": text,
        "error": "", "source": source, "elapsed": round(elapsed, 1),
    }


def _shrink_and_ocr(path: str, max_chars: int) -> str:
    _shrink_screenshot(path)
    return _ocr_image(path, max_chars)


def _clean(text: str) -> str:
    flat = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    flat = re.sub(r"[ \t\u00a0\u3000]+", " ", flat)
    flat = re.sub(r"\n{3,}", "\n\n", flat)
    return flat.strip()


def _fail(url: str, reason: str, started: float) -> dict[str, Any]:
    return {
        "url": url, "title": "", "text": "", "error": reason,
        "source": "none", "elapsed": round(time.time() - started, 1),
    }


def stats() -> dict[str, Any]:
    ready, why = deps_ready()
    return {
        "ready": ready,
        "reason": why,
        "browser_started": _browser is not None,
        "uptime": round(time.time() - _started_at, 1) if _started_at else 0,
        "last_error": _last_error,
        "max_height": _MAX_HEIGHT,
        "scale_width": _SCALE_WIDTH,
    }
