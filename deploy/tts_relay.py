#!/usr/bin/env python3
"""TTS 中继：把 bot 容器里的请求转给 Windows 上的 GPT-SoVITS（经 SSH 反向隧道）。

拓扑（设计文档与实测产物只在开发工作区，不随仓库分发）：

    bot 容器 --(host.docker.internal:19881)--> 本进程 --(127.0.0.1:19880)--> SSH 隧道 --> Windows 9880

为什么绑在 docker0 网桥地址而不是 0.0.0.0：
容器访问宿主机的标准入口是网桥网关（本机 172.17.0.1）；只绑这个地址，公网与
其他网段都碰不到。**不要**为了省事绑 0.0.0.0 —— 那等于把 GPU 合成接口暴露到整台机器。

只放行两个路径：`POST /tts`（Bearer 鉴权）与 `GET /healthz`（探活）。
不转发 `/control`、权重切换或任何其他路径 —— 上游 API 有进程控制接口，不能整体代理。

只依赖标准库（服务器上不必额外装 pip 包）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("tts-relay")

TOKEN = os.environ.get("TTS_RELAY_TOKEN", "").strip()
BIND = os.environ.get("TTS_RELAY_BIND", "172.17.0.1").strip()
PORT = int(os.environ.get("TTS_RELAY_PORT", "19881"))
UPSTREAM = os.environ.get("TTS_UPSTREAM", "http://127.0.0.1:19880").rstrip("/")
MAX_BODY = int(os.environ.get("TTS_RELAY_MAX_BODY", str(16 * 1024)))
MAX_RESPONSE = int(os.environ.get("TTS_RELAY_MAX_RESPONSE", str(8 * 1024 * 1024)))
TIMEOUT = float(os.environ.get("TTS_RELAY_TIMEOUT", "60"))

# 当前引擎里加载的权重 + 串行锁。
# 单 GPU 同时跑两次合成没有收益，反而会让"切换权重"和"合成"交错；
# 所以整条 /tts 请求（切换 + 合成）一把锁串起来。
_SWITCH_LOCK = threading.Lock()
_LOADED = {"gpt": "", "sovits": ""}


def _check_weight_path(path: str, suffix: str) -> str:
    """只接受服务端绝对路径 + 指定后缀；拒绝换行与超长串。

    这里**无法校验文件是否存在**（relay 在 Linux，文件在 Windows），
    存在性由引擎的 /set_*_weights 返回错误来暴露。
    """
    if not path:
        return ""
    if len(path) > 400 or any(ch in path for ch in "\r\n\x00"):
        raise ValueError("权重路径非法")
    if not path.lower().endswith(suffix):
        raise ValueError("权重后缀必须是 " + suffix)
    return path


def _load_weights(path: str, kind: str) -> None:
    """调用上游 set 接口。kind ∈ {gpt, sovits}。"""
    key = "gpt" if kind == "gpt" else "sovits"
    if not path or _LOADED[key] == path:
        return
    endpoint = f"{UPSTREAM}/set_{kind}_weights?" + urllib.parse.urlencode({"weights_path": path})
    with urllib.request.urlopen(endpoint, timeout=TIMEOUT) as resp:
        resp.read(4096)
        if resp.status != 200:
            raise IOError(f"set_{kind}_weights HTTP {resp.status}")
    _LOADED[key] = path



class Handler(BaseHTTPRequestHandler):
    server_version = "tts-relay/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 - 统一走 logging
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _json(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        if not TOKEN:
            return False
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {TOKEN}"

    def do_GET(self):
        if self.path != "/healthz":
            self._json(404, {"error": "not found"})
            return
        upstream_ok = False
        try:
            with urllib.request.urlopen(UPSTREAM + "/docs", timeout=2) as resp:
                upstream_ok = resp.status == 200
        except Exception:  # noqa: BLE001 - 探活失败就是 False
            upstream_ok = False
        self._json(200 if upstream_ok else 503,
                   {"ok": upstream_ok, "upstream": UPSTREAM})

    def do_POST(self):
        if self.path != "/tts":
            self._json(404, {"error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "bad content-length"})
            return
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"error": "body too large", "limit": MAX_BODY})
            return
        body = self.rfile.read(length)
        try:
            payload_json = json.loads(body)
        except json.JSONDecodeError:
            self._json(400, {"error": "body is not JSON"})
            return

        # 自定义训练权重：从请求里摘掉再转发（引擎不认这两个字段）。
        try:
            gpt = _check_weight_path(str(payload_json.pop("gpt_weights", "") or "").strip(), ".ckpt")
            sovits = _check_weight_path(str(payload_json.pop("sovits_weights", "") or "").strip(), ".pth")
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        body = json.dumps(payload_json, ensure_ascii=False).encode("utf-8")

        with _SWITCH_LOCK:
            try:
                _load_weights(gpt, "gpt")
                _load_weights(sovits, "sovits")
            except Exception as exc:  # noqa: BLE001 - 切权重失败要明确报，不要带着旧权重合成
                self._json(502, {"error": "switch weights failed",
                                 "detail": "%s: %s" % (type(exc).__name__, exc)})
                return
            request = urllib.request.Request(
                UPSTREAM + "/tts", data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
                    status = resp.status
                    ctype = resp.headers.get("Content-Type", "application/octet-stream")
                    payload = resp.read(MAX_RESPONSE + 1)
            except urllib.error.HTTPError as exc:
                payload = exc.read(4096)
                self._json(502, {"error": "upstream http %s" % exc.code,
                                 "detail": payload.decode("utf-8", "replace")[:500]})
                return
            except Exception as exc:  # noqa: BLE001 - 上游不可达统一 502
                self._json(502, {"error": "upstream unreachable",
                                 "detail": "%s: %s" % (type(exc).__name__, exc)})
                return

        if len(payload) > MAX_RESPONSE:
            self._json(502, {"error": "upstream response too large"})
            return
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not TOKEN:
        LOG.error("TTS_RELAY_TOKEN 未设置，拒绝启动（不允许无鉴权代理）")
        return 2
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    LOG.info("tts-relay 监听 %s:%s -> %s", BIND, PORT, UPSTREAM)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
