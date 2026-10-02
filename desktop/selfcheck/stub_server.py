"""自检用的 HTTP 桩服务：在没有机器人、没有 GUI 的机器上验证桌面端核心。

为什么要桩而不是连真服务端：

- **失败分支测不到**：真服务端不会给你 401/410/非 JSON/503，而这些恰恰是最容易
  写错、写错了后果最重（把"未认证"当成"没有数据"）的分支。
- **不该动用户数据**：真服务端的写接口会改参数、发消息、花 token。自检必须是只读的。

桩的行为刻意照着 `plugins/ai_chat/webui.py` 复制：

    认证：X-Auth-Token / ?token= / cookie 三者任一（`webui.py:899-904`）
    前缀：/{prefix}/... 之外的路径**不校验认证**（`webui.py:861-867`）
    形状：`/api/state` 是聚合响应（`webui.py:927-978`），`/api/persona` 恒 410
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

COOKIE_NAME = "ai_chat_webui_token"


def sample_state() -> dict[str, Any]:
    """一份形状正确的 `/api/state` 样本（键名取自真实实现，值只求形状对）。"""
    return {
        "settings": [
            {
                "group": "基础",
                "items": [
                    {"key": "master_qq", "kind": "int", "label": "主人的 QQ 号", "value": 10001,
                     "min": None, "max": None, "hint": "", "default": 10001,
                     "env_value": None, "choices": [], "secret": False},
                    {"key": "search_api_key", "kind": "str", "label": "搜索 API Key", "value": "",
                     "min": None, "max": None, "hint": "", "default": "",
                     "env_value": None, "choices": [], "secret": True},
                ],
            },
            {
                "group": "主动发言",
                "items": [
                    {"key": "proactive_enabled", "kind": "bool", "label": "启用主动发言",
                     "value": False, "min": None, "max": None, "hint": "", "default": False,
                     "env_value": None, "choices": [], "secret": False},
                    {"key": "proactive_chance", "kind": "float", "label": "定时掷骰的发言概率",
                     "value": 0.1, "min": 0.0, "max": 1.0, "hint": "", "default": 0.1,
                     "env_value": None, "choices": [], "secret": False},
                ],
            },
        ],
        "models": {
            "items": [
                {"id": "deepseek", "label": "DeepSeek", "model": "deepseek-chat",
                 "base_url": "https://api.deepseek.com", "api_key": "sk-a***yz", "active": True},
            ],
            "active": "deepseek",
        },
        "models_editor": '{\n "deepseek": {"api_key": "***"}\n}',
        "persona": {
            "stats": {"base_chars": 1200, "surface_chars": 300, "forbidden": 7},
            "forbidden": ["不冒充人", "不主动要钱"],
            "layers": {"base": "底层人设……", "surface": "表层人设……", "forbidden": "禁止事项……"},
            "changelog": [{"ts": "2026-10-02 10:00:00", "action": "reflect", "text": "把口癖收了一点"}],
            "iter": {"runs": 3, "written": 5},
            "eval": {"enabled": False, "artifacts": 0, "traits": [{"key": "warm", "score": None}]},
        },
        "memory": {
            "facts": [
                {"id": 1, "text": "主人养了一只猫", "subject": "主人", "importance": 0.9,
                 "locked": True, "ts": 1780000000.0, "source": "manual", "used": 2},
                {"id": 2, "text": "主人在做网架项目", "subject": "主人", "importance": 0.7,
                 "locked": False, "ts": 1780001000.0, "source": "extract", "used": 0},
            ],
            "events": [{"id": 9, "text": "群里讨论过表情包", "conv": "g123", "importance": 0.5,
                        "ts": 1780002000.0}],
            "profile": [{"key": "主人", "uid": 10001, "summary": "喜欢安静的技术交流",
                         "facts": 12, "updated": "2026-10-01"}],
            "stats": {"facts": 2, "events": 1, "profile": 1, "protected": 1},
        },
        "image": {"global_mode": "normal", "convs": {"g123": "ignore"}, "valid_modes": ["normal", "ignore", "ask"]},
        "status": {
            "stickers": {"count": 12, "total_bytes": 3456789, "used": 30, "no_phash": 0,
                         "file_sent": 2, "duplicates": 1},
            "groups": ["g123", "g456"],
            "proactive_enabled": False,
            "proactive": {"day": "2026-10-02", "last_spoke": {"g123": "09:12:00"},
                          "day_count": {"g123": 1}, "msg_counter": {"g123": 40}},
            "greet": {"enabled": False, "times": {"早安": "08:00", "午安": "12:30", "晚安": "23:00"},
                      "target": "auto", "window_minutes": 90, "due_now": [], "today": "2026-10-02",
                      "sent": {}, "attempts": {}},
            "model": "deepseek-chat",
            "model_profile": "deepseek",
            "time": {"now": "2026-10-02 11:00:00", "raw": "2026-10-02 10:59:58", "offset": 2.0,
                     "source": "ntp", "synced": True},
            "search": {"enabled": False, "available": False, "backend": "searxng",
                       "reason": "未配置端点"},
        },
        "stickers_list": [
            {"hash": "abc123def456", "file": "abc123def456.png", "size": 20480, "score": 0.8,
             "uses": 3, "sub_type_label": "图片", "added_at": "2026-10-01 12:00:00",
             "from_conv": "g123", "from_name": "主人", "width": 200, "height": 200,
             "weight": 1.0, "file_sent": False},
        ],
    }


@dataclass
class StubConfig:
    prefix: str = "/ai"
    token: str = "s3cret-token-value"
    state: dict[str, Any] = field(default_factory=sample_state)
    # 打开后 `/api/state` 返回一段 HTML（模拟把端口连到了别的服务）。
    state_as_html: bool = False
    # 打开后写接口返回 503（测重试与"不重试写请求"）。
    write_unavailable: bool = False
    # 记录收到的请求：(method, path, headers 里的 auth, cookie 里的 token)
    requests: list[tuple[str, str, str, str]] = field(default_factory=list)
    settings_posts: list[dict[str, Any]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    speaks: int = 0
    greets: list[str] = field(default_factory=list)


class _Handler(BaseHTTPRequestHandler):
    server_version = "QQbotStub/1.0"
    protocol_version = "HTTP/1.1"

    # 关掉默认的 stderr 访问日志：自检输出要干净。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # ------------------------------------------------------------ 工具
    @property
    def cfg(self) -> StubConfig:
        return self.server.cfg  # type: ignore[attr-defined]

    def _auth_given(self) -> tuple[str, str]:
        """返回 (凭证值, 来源)。来源用于断言"插件有没有把 token 发错目标"。"""
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if query.get("token"):
            return query["token"][0], "query"
        header = self.headers.get("X-Auth-Token") or ""
        if header:
            return header, "header"
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value, "cookie"
        return "", "none"

    def _guard(self, path: str) -> bool:
        """认证判定，照抄 `webui.auth_decision` 的三条规则。"""
        prefix = self.cfg.prefix
        if not path.startswith(prefix):
            return True
        if not self.cfg.token:
            return True
        given, source = self._auth_given()
        self.cfg.requests.append((self.command, path, source, given))
        return bool(given) and given == self.cfg.token

    def _json(self, payload: Any, status: int = 200, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, body: bytes, status: int = 200, content_type: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口名
        path = urlsplit(self.path).path
        if not self._guard(path):
            self._json({"ok": False, "error": "未认证"}, 401)
            return
        prefix = self.cfg.prefix
        if path in (prefix, prefix + "/"):
            self._raw(b"<html><body>QQ_bot WebUI</body></html>", 200, "text/html; charset=utf-8")
            return
        if path == prefix + "/api/state":
            if self.cfg.state_as_html:
                self._raw(b"<html>not json</html>", 200, "text/html; charset=utf-8")
                return
            self._json(self.cfg.state)
            return
        if path == prefix + "/api/capabilities":
            # 桩**提供**能力表（真服务端暂时没有），用来验证"插件按 capability 降级"。
            self._json({"api_version": "1", "app": {"name": "QQ_bot", "version": "stub"},
                        "features": {"settings": True, "stickers": True}, "limits": {}})
            return
        if path == prefix + "/api/stickers":
            self._json({"items": self.cfg.state["stickers_list"],
                        "stats": self.cfg.state["status"]["stickers"]})
            return
        if path.startswith(prefix + "/api/stickers/"):
            digest = path.rsplit("/", 1)[-1]
            if digest != "abc123def456":
                self._raw(b"", 404)
                return
            self._raw(b"\x89PNG\r\n\x1a\nSTUB", 200, "image/png")
            return
        self._json({"error": f"没有这个路径：{path}"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not self._guard(path):
            self._json({"ok": False, "error": "未认证"}, 401)
            return
        if self.cfg.write_unavailable:
            self._json({"ok": False, "error": "服务暂时不可用"}, 503)
            return
        prefix = self.cfg.prefix
        body = self._read_body()
        if path == prefix + "/api/settings":
            if not body:
                self._json({"error": "请求体必须是 JSON 对象"}, 400)
                return
            self.cfg.settings_posts.append(body)
            self._json({"ok": True, "applied": body})
            return
        if path == prefix + "/api/settings/reset":
            self._json({"ok": True})
            return
        if path == prefix + "/api/model/active":
            return self._json({"ok": True, "detail": "已切换", "state": self.cfg.state["models"]})
        if path == prefix + "/api/model/test":
            return self._json({"ok": True, "detail": "探活成功", "models": [], "profile": "deepseek"})
        if path == prefix + "/api/model/save":
            return self._json({"ok": True, "detail": "当前用 deepseek", "state": self.cfg.state["models"]})
        if path == prefix + "/api/model/delete":
            return self._json({"ok": True, "detail": "已删除", "state": None})
        if path == prefix + "/api/persona":
            # 与 `webui.py:1051-1060` 一致：恒 410。
            return self._json(
                {"ok": False, "error": "改人设的接口已删除：现在只能直接编辑三个文件"}, 410
            )
        if path == prefix + "/api/persona/undo":
            return self._json({"ok": True, "note": "已撤回一条表层改动"})
        if path == prefix + "/api/persona/reflect":
            return self._json({"ok": True, "written": 1, "text": "反思完成"})
        if path == prefix + "/api/persona/eval":
            return self._json({"ok": True, "mode": body.get("mode") or "round", "score": 0.72})
        if path == prefix + "/api/memory":
            if len(str(body.get("text") or "")) < 2:
                return self._json({"ok": False, "error": "内容太短"}, 400)
            return self._json({"ok": True, "created": True, "id": 42})
        if path.startswith(prefix + "/api/memory/") and path.endswith("/protect"):
            return self._json({"ok": True})
        if path == prefix + "/api/image-policy":
            return self._json({"ok": True, "effective": body.get("mode") or body.get("global_mode")})
        if path == prefix + "/api/speak":
            self.cfg.speaks += 1
            return self._json({"said": True, "group_id": "g123"})
        if path == prefix + "/api/greet":
            self.cfg.greets.append(str(body.get("slot") or ""))
            return self._json({"said": True, "slot": body.get("slot") or "morning",
                               "label": "早安", "reason": ""})
        self._json({"error": f"没有这个路径：{path}"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if not self._guard(path):
            self._json({"ok": False, "error": "未认证"}, 401)
            return
        prefix = self.cfg.prefix
        if path.startswith(prefix + "/api/stickers/"):
            digest = path.rsplit("/", 1)[-1]
            self.cfg.deleted.append(digest)
            return self._json({"ok": True})
        if path.startswith(prefix + "/api/memory/"):
            self.cfg.deleted.append(path.rsplit("/", 1)[-1])
            return self._json({"ok": True})
        self._json({"error": f"没有这个路径：{path}"}, 404)


class StubServer:
    """线程化的桩服务，支持 `with` 用法。"""

    def __init__(self, cfg: StubConfig | None = None, *, host: str = "127.0.0.1") -> None:
        self.cfg = cfg or StubConfig()
        self._httpd = ThreadingHTTPServer((host, 0), _Handler)
        self._httpd.cfg = self.cfg  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "StubServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()

    def __enter__(self) -> "StubServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()
