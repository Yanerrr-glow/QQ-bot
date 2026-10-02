"""QQ_bot 服务端 API 客户端（唯一业务边界）。

只用标准库（`urllib` + `http.cookiejar`），理由见 `desktop/core/__init__.py`：
本机没装 PySide6 时也要能跑自检，把认证、错误分类、前缀拼接这些致命细节验完。

几条硬规矩，全部对应方案第 4 章的契约：

1. **认证只走 `X-Auth-Token` 请求头**，不拼 `?token=`。服务端三种方式都收
   （`webui.py:899-904`），但 URL 里的 token 会进日志/历史记录，与"不拼入持久 URL"矛盾。
2. **图片走同一个会话**：先发一次带认证头的请求拿到 `Set-Cookie`
   （`ai_chat_webui_token`，见 `webui.py:913`），`cookie_jar` 会收下来，
   之后 `GET …/stickers/{hash}` 自动带上 —— 这正是 WebUI 前端能显示缩略图的原因
   （它渲染的 `<img src>` 并不带 token，靠的就是这个 cookie）。
3. **错误分类必须区分"未认证"和"空数据"**：401/403 绝不能被上层渲染成"没有数据"。
4. **前缀是配置项**：所有路径由 `prefix` 派生，`/ai` 只是默认值（`config.py:566`）。
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .util import one_line, redact_text, truncate

logger = logging.getLogger("qqbot.desktop.api")

DEFAULT_PREFIX = "/ai"
DEFAULT_TIMEOUT = 15.0
# 只读请求在服务端忙/限流时值得重试；写请求**绝不**自动重试（可能重复发言、重复入库）。
RETRY_STATUS = (429, 502, 503, 504)
DEFAULT_MAX_RETRIES = 2

# 服务端返回体里常见的失败包络（`webui.py:1030/988/1053` 三种都有）。
ERROR_KEYS = ("error", "detail", "message", "reason")


class BotApiError(RuntimeError):
    """一次 API 调用的失败，带可操作建议。

    `hint` 是给用户看的中文建议（"检查机器人是否在跑"…），**不含令牌**。
    """

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        hint: str = "",
        payload: Any = None,
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = int(status or 0)
        self.hint = hint
        self.payload = payload
        self.url = redact_text(url)

    @property
    def unauthorized(self) -> bool:
        return self.status in (401, 403)

    @property
    def gone(self) -> bool:
        return self.status == 410

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "status": self.status,
            "message": self.message,
            "hint": self.hint,
            "url": self.url,
        }

    def __str__(self) -> str:  # pragma: no cover - 纯展示
        head = f"[{self.status}] " if self.status else ""
        return f"{head}{self.message}" + (f"（{self.hint}）" if self.hint else "")


@dataclass
class RawResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str = ""

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body.decode("utf-8", errors="replace"))
        except ValueError:
            return None

    @property
    def content_type(self) -> str:
        return str(self.headers.get("Content-Type") or "")


@dataclass
class ProbeResult:
    """连接探测结果（连接设置页"测试连接"用）。"""

    ok: bool
    status: int
    detail: str
    hint: str = ""
    elapsed_ms: int = 0
    server_prefix: str = ""
    state_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "elapsed_ms": self.elapsed_ms,
            "prefix": self.server_prefix,
            "state_keys": self.state_keys,
        }


def normalize_prefix(prefix: str) -> str:
    """把前缀归一化成 `/xxx`（无尾斜杠）。空值回默认。"""
    raw = str(prefix or "").strip()
    if not raw:
        return DEFAULT_PREFIX
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/") or DEFAULT_PREFIX


def normalize_base_url(base_url: str) -> str:
    """把 base URL 归一化成 `scheme://host[:port]`（无尾斜杠、无路径）。"""
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parts = urllib.parse.urlsplit(raw)
    if not parts.netloc:
        return ""
    scheme = parts.scheme or "http"
    return f"{scheme}://{parts.netloc}"


def join_url(base_url: str, prefix: str, path: str) -> str:
    """拼出完整 URL。`path` 只允许是相对 API 路径（不含 host）。"""
    base = normalize_base_url(base_url)
    pre = normalize_prefix(prefix)
    rel = str(path or "").strip()
    if rel.startswith("/"):
        # 允许调用方直接给 `/api/state`，但要**拒绝**绝对 URL（`http://…`）。
        if "://" in rel:
            raise BotApiError("只接受相对 API 路径，不接受绝对 URL", hint="这是插件越权的典型写法")
        rel = rel[1:]
    if "://" in rel:
        raise BotApiError("只接受相对 API 路径，不接受绝对 URL")
    return f"{base}{pre}/{rel}"


class ApiClient:
    """一个目标一个实例（独立的 cookie jar 与 token），切目标就换实例。"""

    def __init__(
        self,
        base_url: str,
        *,
        prefix: str = DEFAULT_PREFIX,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        verify_tls: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
        target_id: str = "",
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.prefix = normalize_prefix(prefix)
        self.token = str(token or "")
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.verify_tls = bool(verify_tls)
        self.max_retries = max(0, int(max_retries))
        self.target_id = target_id
        self.cookie_jar = http.cookiejar.CookieJar()
        self.last_status = 0
        self.last_error: BotApiError | None = None
        self._closed = False
        self._ctx = self._build_ssl_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar),
            urllib.request.HTTPSHandler(context=self._ctx),
        )

    # ------------------------------------------------------------ 内部
    def _build_ssl_context(self) -> ssl.SSLContext:
        if self.verify_tls:
            return ssl.create_default_context()
        logger.warning("目标 %s 关闭了 TLS 校验（仅建议用于自签证书的内网）", self.target_id or "-")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _describe(self) -> str:
        return f"{self.base_url}{self.prefix}"

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        out = {
            "Accept": "application/json, */*",
            "User-Agent": "QQbot-Desktop-Console",
        }
        if self.token:
            out["X-Auth-Token"] = self.token
        if extra:
            out.update({str(k): str(v) for k, v in extra.items()})
        return out

    def _hint_for_status(self, status: int, payload: Any) -> str:
        if status == 401 or status == 403:
            return (
                "认证失败：确认连接设置里的令牌与服务端 .env 的 AI_CHAT_WEBUI_AUTH_TOKEN 一致"
                "（服务端留空 = 不校验，此时应把令牌留空）"
            )
        if status == 404:
            return (
                f"路径不存在：确认 API 前缀是否应为 {self.prefix}"
                "（服务端默认 /ai，对应 AI_CHAT_WEBUI_PREFIX）"
            )
        if status == 410:
            note = ""
            if isinstance(payload, dict):
                note = one_line(payload.get("error") or payload.get("detail") or "", 200)
            return f"该接口已被服务端显式下线（410）{('：' + note) if note else ''}"
        if status == 400:
            note = ""
            if isinstance(payload, dict):
                note = one_line(payload.get("error") or payload.get("detail") or "", 200)
            return f"服务端拒绝了这个请求{('：' + note) if note else '：检查参数取值'}"
        if status == 422:
            return "参数形状不对（422）：确认 body 是 JSON 对象、字段名与方案 4.2 表一致"
        if status in (502, 503, 504):
            return "服务端暂时不可用：稍后重试；反复出现就看机器人日志"
        if status >= 500:
            return "服务端内部错误：看机器人日志里的 traceback"
        return ""

    def _request_once(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str] | None,
        timeout: float | None,
    ) -> RawResponse:
        request = urllib.request.Request(url, data=body, method=method.upper())
        for key, value in self._headers(headers).items():
            request.add_header(key, value)
        with self._opener.open(request, timeout=timeout or self.timeout) as resp:
            data = resp.read()
            return RawResponse(
                status=int(getattr(resp, "status", 0) or resp.getcode() or 0),
                headers={k: v for k, v in resp.headers.items()},
                body=data,
                url=url,
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> Any:
        if self._closed:
            raise BotApiError("该目标的连接已被关闭（切换目标时会取消未完成请求）")
        if not self.base_url:
            raise BotApiError("没有配置服务地址", hint="去「连接设置」里填 base URL")
        url = join_url(self.base_url, self.prefix, path)
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        method = method.upper()
        # **只重试 GET**：写请求重试可能造成重复发言、重复入库、重复计费。
        attempts = self.max_retries + 1 if method == "GET" else 1
        last: BotApiError | None = None
        for attempt in range(attempts):
            try:
                raw = self._request_once(
                    method, url, body=payload, headers=headers, timeout=timeout
                )
            except urllib.error.HTTPError as exc:
                # 4xx/5xx 走的是异常分支；这里要把 body 读掉再决定重试还是报错。
                raw = RawResponse(
                    status=int(exc.code or 0),
                    headers={k: v for k, v in (exc.headers or {}).items()},
                    body=exc.read() or b"",
                    url=url,
                )
                if raw.status in RETRY_STATUS and attempt + 1 < attempts:
                    delay = _retry_delay(raw, attempt)
                    logger.warning("服务端 %s，%0.1fs 后重试（%s %s）", raw.status, delay, method, url)
                    time.sleep(delay)
                    continue
                last = self._to_error(method, url, raw)
                break
            except socket.timeout as exc:
                last = BotApiError(
                    f"请求超时（>{timeout or self.timeout:g}s）",
                    hint="服务端可能在跑长任务；把超时调大，或稍后再试",
                    url=url,
                )
                logger.warning("%s %s 超时：%s", method, url, type(exc).__name__)
                break
            except urllib.error.URLError as exc:
                last = self._connect_error(method, url, exc)
                break
            except OSError as exc:
                last = BotApiError(
                    f"网络错误：{type(exc).__name__}: {one_line(exc, 200)}",
                    hint="检查本机网络/防火墙；服务器目标请确认 SSH 隧道是否在运行",
                    url=url,
                )
                break
            else:
                self.last_status = raw.status
                if 200 <= raw.status < 300:
                    if not expect_json:
                        return raw
                    data = raw.json()
                    if data is None and raw.body.strip():
                        # 200 但body不是 JSON：多半连到了别的服务（比如 NapCat 面板）。
                        raise BotApiError(
                            "响应不是 JSON：" + one_line(raw.body[:200].decode("utf-8", "replace"), 200),
                            status=raw.status,
                            hint="这个端口上可能不是机器人服务（NapCat 面板是另一个端口）",
                            url=url,
                        )
                    return data
                if raw.status in RETRY_STATUS and attempt + 1 < attempts:
                    delay = _retry_delay(raw, attempt)
                    logger.warning("服务端 %s，%0.1fs 后重试（%s %s）", raw.status, delay, method, url)
                    time.sleep(delay)
                    continue
                last = self._to_error(method, url, raw)
                break
        assert last is not None
        self.last_error = last
        logger.warning("API 失败：%s", last)
        raise last

    def _connect_error(self, method: str, url: str, exc: urllib.error.URLError) -> BotApiError:
        reason = getattr(exc, "reason", exc)
        text = one_line(reason, 200)
        refused = isinstance(reason, ConnectionRefusedError) or "refused" in text.lower()
        if refused:
            return BotApiError(
                f"连不上 {self._describe()}：连接被拒绝",
                hint=(
                    "依次确认：机器人进程在跑 / .env 的 DRIVER 含 ~fastapi / 端口与基数"
                    f"（当前 {self.base_url}）一致；服务器目标还要确认 SSH 隧道已启动"
                ),
                url=url,
            )
        return BotApiError(
            f"连不上 {self._describe()}：{type(reason).__name__}: {text}",
            hint="检查地址是否写错、目标机是否可达、SSH 隧道是否在运行",
            url=url,
        )

    def _to_error(self, method: str, url: str, raw: RawResponse) -> BotApiError:
        data = raw.json()
        message = f"{method} {self.prefix}{url.split(self.prefix, 1)[-1]} 失败（HTTP {raw.status}）"
        if isinstance(data, dict):
            for key in ERROR_KEYS:
                value = data.get(key)
                if value:
                    message = f"HTTP {raw.status}：{one_line(value, 300)}"
                    break
        elif data is None and raw.body and "json" not in raw.content_type.lower():
            message = f"HTTP {raw.status}：响应不是 JSON（{raw.content_type or '未知类型'}）"
        return BotApiError(
            message,
            status=raw.status,
            hint=self._hint_for_status(raw.status, data),
            payload=data if data is not None else truncate(raw.body.decode("utf-8", "replace"), 300),
            url=url,
        )

    # ------------------------------------------------------------ 生命周期
    def close(self) -> None:
        """标记关闭并清空 cookie —— 切目标时调用，避免把 A 的 cookie 带去 B。"""
        self._closed = True
        try:
            self.cookie_jar.clear()
        except Exception:  # noqa: BLE001 - cookiejar 的清理不该抛
            pass

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ 只读接口
    def state(self) -> dict[str, Any]:
        return self._request("GET", "api/state")

    def stickers(self) -> dict[str, Any]:
        return self._request("GET", "api/stickers")

    def capabilities(self) -> dict[str, Any] | None:
        """`GET api/capabilities`（**服务端当前没有这个端点**）。

        方案 5.4 建议补；这里做"有就用、没有就回 None"的软探测，
        这样插件可以按 capability 藏功能，而不用猜版本号。
        """
        try:
            return self._request("GET", "api/capabilities")
        except BotApiError as exc:
            if exc.status in (404, 410, 501):
                logger.info("服务端没有 capabilities 端点（HTTP %s），按旧版本处理", exc.status)
                return None
            raise

    def sticker_bytes(self, digest: str) -> bytes:
        """取缩略图/原图字节。

        走**同一个会话**（含 cookie），所以认证对图片同样生效 —— 这是方案第 4 章
        "图像请求也必须经过相同的认证会话"的落点。
        """
        raw = self._request(
            "GET",
            f"api/stickers/{urllib.parse.quote(str(digest), safe='')}",
            expect_json=False,
        )
        return bytes(raw.body)

    def webui_page(self) -> RawResponse:
        """取旧 WebUI 首页（回退入口探测用；不是桌面端的依赖）。"""
        return self._request("GET", "", expect_json=False)

    # ------------------------------------------------------------ 写接口
    def set_settings(self, **values: Any) -> dict[str, Any]:
        if not values:
            raise BotApiError("没有要保存的参数")
        return self._request("POST", "api/settings", body=dict(values))

    def reset_settings(self) -> dict[str, Any]:
        return self._request("POST", "api/settings/reset")

    def model_active(self, profile_id: str) -> dict[str, Any]:
        return self._request("POST", "api/model/active", body={"id": profile_id})

    def model_test(self, profile_id: str = "") -> dict[str, Any]:
        # 探活会真的调一次模型（花 token），所以超时给足。
        return self._request(
            "POST", "api/model/test", body={"id": profile_id}, timeout=max(self.timeout, 60.0)
        )

    def model_save(self, editor_text: str) -> dict[str, Any]:
        # 注意 body 形状：是把**整份 JSON 文本**当字符串塞进 `json` 字段（`webui.py:1008`），
        # 不是结构化对象。
        return self._request("POST", "api/model/save", body={"json": str(editor_text)})

    def model_delete(self, profile_id: str) -> dict[str, Any]:
        return self._request("POST", "api/model/delete", body={"id": profile_id})

    def persona_undo(self) -> dict[str, Any]:
        return self._request("POST", "api/persona/undo")

    def persona_reflect(self) -> dict[str, Any]:
        return self._request("POST", "api/persona/reflect", timeout=max(self.timeout, 180.0))

    def persona_eval(self, mode: str = "round") -> dict[str, Any]:
        if mode not in ("artifacts", "round"):
            raise BotApiError(f"未知的评估模式：{mode}", hint="只支持 artifacts / round")
        return self._request(
            "POST", "api/persona/eval", body={"mode": mode}, timeout=max(self.timeout, 600.0)
        )

    def memory_add(self, text: str, *, subject: str = "", importance: float = 0.85) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text}
        if subject:
            body["subject"] = subject
        if importance:
            body["importance"] = float(importance)
        return self._request("POST", "api/memory", body=body)

    def memory_delete(self, item_id: int) -> dict[str, Any]:
        return self._request("DELETE", f"api/memory/{int(item_id)}")

    def memory_protect(self, item_id: int, locked: bool) -> dict[str, Any]:
        return self._request("POST", f"api/memory/{int(item_id)}/protect", body={"locked": bool(locked)})

    def image_policy_global(self, mode: str) -> dict[str, Any]:
        return self._request("POST", "api/image-policy", body={"global_mode": mode})

    def image_policy_conv(self, conv: str, mode: str = "") -> dict[str, Any]:
        return self._request("POST", "api/image-policy", body={"conv": conv, "mode": mode})

    def sticker_delete(self, digest: str) -> dict[str, Any]:
        return self._request("DELETE", f"api/stickers/{urllib.parse.quote(str(digest), safe='')}")

    def speak(self) -> dict[str, Any]:
        # 主动发言会调模型且真的发消息，超时给足。
        return self._request("POST", "api/speak", timeout=max(self.timeout, 120.0))

    def greet(self, slot: str = "") -> dict[str, Any]:
        body = {"slot": slot} if slot else {}
        return self._request("POST", "api/greet", body=body, timeout=max(self.timeout, 120.0))

    # ------------------------------------------------------------ 探测
    def probe(self) -> ProbeResult:
        """连接测试：一次调用同时验证"地址通不通 / 前缀对不对 / 令牌对不对"。

        用 `GET api/state` 而不是 `GET /`：后者是 driver 的空路由，永远 200，
        测不出前缀写错或令牌写错（这正是方案原文写成 `GET /` 的坑）。
        """
        started = time.perf_counter()
        try:
            data = self.state()
        except BotApiError as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            if exc.unauthorized:
                detail = "服务可达，但认证被拒绝"
            elif exc.status == 404:
                detail = "服务可达，但 API 前缀不对"
            elif exc.status == 0:
                detail = "连接失败"
            else:
                detail = f"服务返回 HTTP {exc.status}"
            return ProbeResult(
                ok=False,
                status=exc.status,
                detail=detail,
                hint=exc.hint,
                elapsed_ms=elapsed,
                server_prefix=self.prefix,
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        keys = sorted(str(k) for k in data) if isinstance(data, dict) else []
        return ProbeResult(
            ok=True,
            status=self.last_status or 200,
            detail=f"连接正常，返回 {len(keys)} 个顶层字段",
            elapsed_ms=elapsed,
            server_prefix=self.prefix,
            state_keys=keys,
        )


def _retry_delay(raw: RawResponse, attempt: int) -> float:
    retry_after = raw.headers.get("Retry-After") or raw.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, min(10.0, float(str(retry_after).strip())))
        except ValueError:
            pass
    return min(4.0, 0.5 * (2 ** attempt))


def iter_error_keys(payload: Any) -> Iterable[str]:
    """给 UI 用：从失败包络里挑出能显示的一句话。"""
    if isinstance(payload, dict):
        for key in ERROR_KEYS:
            value = payload.get(key)
            if value:
                yield one_line(value, 300)
