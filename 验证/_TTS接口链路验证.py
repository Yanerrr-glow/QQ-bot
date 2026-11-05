"""通用 HTTP TTS 链路验证，不调用真实引擎、不连接或发消息到 QQ。

运行：python 验证/_TTS接口链路验证.py
HTTP 服务返回合成的测试音调 WAV（不是人声）；执行真实 _send 函数，Bot 为桩。
"""
from __future__ import annotations

import ast
import asyncio
import base64
import io
import json
import math
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1024 * 1024


def fixture_wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(4000)))
    return out.getvalue()


WAV = fixture_wav()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["text"] == "这是一条接口链路测试。"
        code, mime, data = 200, "audio/wav", WAV
        if self.path == "/unauthorized":
            code, mime, data = 401, "application/json", b'{"error":"unauthorized"}'
        elif self.path == "/json-error":
            mime, data = "application/json", b'{"error":"engine failed"}'
        elif self.path == "/empty":
            data = b""
        elif self.path == "/invalid":
            data = b"not a wav"
        elif self.path == "/large":
            data = b"x" * (MAX_BYTES + 1)
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


async def fetch_wav(client, url):
    # 这是仅支持二进制 WAV 响应的验证适配器；不同厂商的字段须单独映射。
    async with client.stream("POST", url, json={"text": "这是一条接口链路测试。"}, timeout=5) as response:
        response.raise_for_status()
        if response.headers.get("content-type", "").split(";")[0] not in {"audio/wav", "audio/x-wav"}:
            raise ValueError("not WAV response")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("audio exceeds limit")
    if not data:
        raise ValueError("empty audio")
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getnframes() <= 0 or w.getframerate() <= 0:
            raise ValueError("invalid WAV")
        expected = w.getnframes() * w.getnchannels() * w.getsampwidth()
        if len(w.readframes(w.getnframes())) != expected:
            raise ValueError("truncated WAV")
    return bytes(data)


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_group_msg(self, **kwargs):
        self.calls.append(("send_group_msg", kwargs))

    async def send_private_msg(self, **kwargs):
        self.calls.append(("send_private_msg", kwargs))


def load_actual_send():
    tree = ast.parse((ROOT / "plugins/ai_chat/__init__.py").read_text(encoding="utf-8-sig"))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send")
    # 仅载入真实函数，避免导入插件触发驱动初始化、配置读取和运行数据迁移。
    module = ast.Module(body=[fn], type_ignores=[])
    namespace = {"Bot": FakeBot, "MessageEvent": SimpleNamespace}
    exec(compile(ast.fix_missing_locations(module), "actual_bot_send", "exec"), namespace)
    return namespace["_send"]


async def main(base):
    passed = []
    async with httpx.AsyncClient(trust_env=False) as client:
        audio = await fetch_wav(client, base + "/tts")
        assert audio == WAV
        passed.append("HTTP POST -> valid PCM WAV (16000 Hz, mono, 0.25s)")
        message = [{"type": "record", "data": {"file": "base64://" + base64.b64encode(audio).decode("ascii")}}]
        wire = json.loads(json.dumps(message))
        assert base64.b64decode(wire[0]["data"]["file"][9:], validate=True) == WAV
        passed.append("record JSON serialization and Base64 lossless roundtrip")
        send = load_actual_send()
        bot = FakeBot()
        await send(bot, SimpleNamespace(group_id=101, user_id=202), message)
        await send(bot, SimpleNamespace(user_id=202), message)
        assert bot.calls == [("send_group_msg", {"group_id": 101, "message": message}), ("send_private_msg", {"user_id": 202, "message": message})]
        passed.append("actual _send group/private routing with fake Bot; no QQ sends")
        for path in ("unauthorized", "json-error", "empty", "invalid", "large"):
            try:
                await fetch_wav(client, base + "/" + path)
            except (httpx.HTTPStatusError, ValueError, wave.Error, EOFError):
                passed.append("reject " + path)
            else:
                raise AssertionError("accepted invalid response: " + path)
    async def timeout_handler(request):
        raise httpx.ReadTimeout("simulated timeout", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        try:
            await fetch_wav(client, "http://test.invalid/tts")
        except httpx.ReadTimeout:
            passed.append("timeout propagates (transport fault injection)")
        else:
            raise AssertionError("timeout swallowed")
    for label in passed:
        print("PASS:", label)
    print(f"RESULT: {len(passed)}/{len(passed)} passed; real TTS/NapCat/QQ playback NOT tested")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        asyncio.run(main(f"http://127.0.0.1:{server.server_port}"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
