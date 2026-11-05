"""TTS 客户端与 /语音 指令的离线验证：不连真实引擎、不连 QQ。

运行：python 验证/_TTS语音指令验证.py

* 本机没有 NoneBot 时只跑「客户端」部分（tts.py 无包内依赖，直接按路径加载）；
* 容器内（装了 NoneBot）额外跑指令匹配、白名单与 `voice.handle` 的成功/回退路径。

真实 GPT-SoVITS / NapCat / QQ 播放不在本脚本内验证。
"""
from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

STUB = {"enabled": True}   # 桩环境里的语音开关；用例会临时改写
PLUGIN_READY = True
try:  # 容器里有一整套依赖；本机通常没有 nonebot
    from plugins.ai_chat import config, tts, voice  # noqa: E402
except Exception:  # noqa: BLE001
    PLUGIN_READY = False
    import importlib.util
    import types

    _spec = importlib.util.spec_from_file_location(
        "tts_client", ROOT / "plugins" / "ai_chat" / "tts.py")
    tts = importlib.util.module_from_spec(_spec)
    sys.modules["tts_client"] = tts  # dataclass 需要能按模块名回查
    _spec.loader.exec_module(tts)  # type: ignore[union-attr]

    try:  # 容器里能 import nonebot，但直接 import 插件包会初始化 NoneBot（这里没有）
        import nonebot  # noqa: F401
        _HAVE_NONEBOT = True
    except ImportError:
        _HAVE_NONEBOT = False

    if _HAVE_NONEBOT:  # 用桩包把 voice.py 单独装起来，只测指令逻辑
        _pkg = types.ModuleType("plugins.ai_chat")
        _pkg.__path__ = [str(ROOT / "plugins" / "ai_chat")]
        config = SimpleNamespace(
            TTS_ENABLED=False, TTS_ENDPOINT="", TTS_TOKEN="",
            TTS_REF_AUDIO="", TTS_PROMPT_TEXT="", TTS_PROMPT_LANG="zh",
            TTS_TEXT_LANG="zh", TTS_MAX_CHARS=150, TTS_MAX_SECONDS=30.0,
            TTS_MAX_BYTES=3 * 1024 * 1024, TTS_CONNECT_TIMEOUT=3.0,
            TTS_TOTAL_TIMEOUT=20.0, TTS_ALLOWED_USERS=[], TTS_ALLOWED_GROUPS=[],
            bot_name=lambda: "测试",
        )
        _pkg.config = config
        _pkg.tts = tts
        sys.modules["plugins.ai_chat.tts"] = tts  # 相对导入按模块名查找，必须登记
        _pkg.settings = SimpleNamespace(get=lambda k: {
            "tts_enabled": STUB["enabled"], "tts_max_chars": 150,
            "tts_max_bytes": 3 * 1024 * 1024, "tts_max_seconds": 30.0}.get(k))
        _pkg.voices = SimpleNamespace(resolve=lambda *_: {
            "id": "test-voice", "name": "测试音色",
            "gpt_weights": "", "sovits_weights": "",
            "ref_audio": "", "prompt_text": "",
            "prompt_lang": "zh", "text_lang": "zh"})
        _pkg.chatlog = SimpleNamespace(conversation_id=lambda g, u: "test",
                                       append_message=None, mark_read_until=None)
        sys.modules["plugins.ai_chat"] = _pkg
        _vspec = importlib.util.spec_from_file_location(
            "plugins.ai_chat.voice", ROOT / "plugins" / "ai_chat" / "voice.py")
        voice = importlib.util.module_from_spec(_vspec)
        sys.modules["plugins.ai_chat.voice"] = voice
        _vspec.loader.exec_module(voice)  # type: ignore[union-attr]
        PLUGIN_READY = True
    else:
        config = SimpleNamespace(TTS_MAX_CHARS=150, TTS_MAX_SECONDS=30.0,
                                 TTS_MAX_BYTES=3 * 1024 * 1024, TTS_ALLOWED_USERS=[],
                                 TTS_ALLOWED_GROUPS=[])
        voice = None

TOKEN = "test-token"
PASSED: list[str] = []


def wav_bytes(seconds: float = 0.3, rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(rate * seconds))
    return out.getvalue()


GOOD = wav_bytes()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.headers.get("Authorization", "") != f"Bearer {TOKEN}":
            self._send(401, "application/json", b'{"error":"unauthorized"}')
            return
        if "/slow" in self.path:
            time.sleep(1.5)
            self._send(200, "audio/wav", GOOD)
        elif "/json" in self.path:
            self._send(200, "application/json", b'{"error":"engine failed"}')
        elif "/truncated" in self.path:
            self._send(200, "audio/wav", GOOD[:20])
        elif "/long" in self.path:
            self._send(200, "audio/wav", wav_bytes(5.0))
        else:
            assert body["text"], "empty text"
            self._send(200, "audio/wav", GOOD)

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


async def fetch(base, **kwargs):
    return await tts.synthesize("你好", endpoint=base + "/tts", token=TOKEN, **kwargs)


class FakeBot:
    self_id = 10000

    def __init__(self):
        self.sent = []

    async def send_private_msg(self, **kw):
        self.sent.append(kw)
        return {"message_id": 777}   # 与 OneBot 适配器实际返回一致

    async def send_group_msg(self, **kw):
        self.sent.append(kw)
        return {"message_id": 778}


async def plugin_cases(base):
    """只在容器里跑：指令匹配、白名单、voice.handle。"""
    ev = SimpleNamespace(get_plaintext=lambda: "/语音 你好呀")
    assert voice.match(ev) == "你好呀"
    assert voice.match(SimpleNamespace(get_plaintext=lambda: "/语音：带冒号")) == "带冒号"
    assert voice.match(SimpleNamespace(get_plaintext=lambda: "/语音")) == ""
    assert voice.match(SimpleNamespace(get_plaintext=lambda: "普通消息")) is None
    PASSED.append("指令匹配：/语音、/语音：、空参数、非指令")

    assert voice.allowed(SimpleNamespace(user_id=1, group_id=None), is_master=True)
    config.TTS_ALLOWED_USERS = [222]
    config.TTS_ALLOWED_GROUPS = [333]
    assert voice.allowed(SimpleNamespace(user_id=222, group_id=None), is_master=False)
    assert voice.allowed(SimpleNamespace(user_id=9, group_id=333), is_master=False)
    assert not voice.allowed(SimpleNamespace(user_id=9, group_id=444), is_master=False)
    PASSED.append("白名单：主人 / 指定用户 / 指定群 / 拒绝陌生会话")

    import plugins.ai_chat.voice as voice_mod

    def _async(value):
        async def _run():
            return value
        return _run()

    voice_mod.chatlog.conversation_id = lambda gid, uid: "test-conv"
    written = []

    async def fake_append(*a, **k):
        written.append((a, k))
        return ({"id": 1}, False)

    voice_mod.chatlog.append_message = fake_append
    voice_mod.chatlog.mark_read_until = lambda *a, **k: _async(0)

    config.TTS_ENABLED = True
    config.TTS_ENDPOINT = base + "/tts"
    config.TTS_TOKEN = TOKEN
    config.TTS_MAX_CHARS = 150
    config.TTS_MAX_BYTES = 3 * 1024 * 1024
    config.TTS_MAX_SECONDS = 30

    event = SimpleNamespace(user_id=1, group_id=None, message_id=5,
                            get_plaintext=lambda: "/语音 你好")
    bot = FakeBot()
    mid = await voice.handle(bot, event, _send, is_master=True)
    assert mid == 777, mid   # dict 回执也要能取到 message_id
    assert bot.sent and bot.sent[0]["message"][0].type == "record"
    assert bot.sent[0]["message"][0].data["file"].startswith("base64://")
    # 语音文本必须作为机器人消息写进聊天记录，否则下一轮模型不知道自己说过什么
    assert written and written[0][0][3] == "你好", written
    assert written[0][1].get("is_bot") is True, written
    assert written[0][1].get("bot_uid") == FakeBot.self_id, written
    PASSED.append("成功路径：发出单条 record（Base64），语音文本写入聊天记录")

    bot = FakeBot()
    STUB["enabled"] = False
    await voice.handle(bot, event, _send, is_master=True)
    assert bot.sent and bot.sent[0]["message"][0].type == "text"
    STUB["enabled"] = True
    PASSED.append("关闭时回退文字")

    bot = FakeBot()
    STUB["enabled"] = True
    config.TTS_ENDPOINT = "http://127.0.0.1:1/tts"
    await voice.handle(bot, event, _send, is_master=True)
    assert bot.sent and bot.sent[0]["message"][0].type == "text"
    PASSED.append("合成失败回退文字，且不重试")

    # ---- 普通回复的随机语音：可朗读判定 + 返回 record / 失败返回 None
    assert voice.speakable("你好呀", 10)
    assert not voice.speakable("```py print(1)```", 100)
    assert not voice.speakable("看这个 https://example.com", 100)
    assert not voice.speakable("一二三四五六七八九十", 5)
    PASSED.append("可朗读判定：代码块/链接/超长被排除")

    config.TTS_ENDPOINT = base + "/tts"
    msg = await voice.as_reply("你好", event)
    assert msg is not None and msg[0].type == "record", msg
    PASSED.append("随机语音回复返回 record 消息")

    config.TTS_ENDPOINT = "http://127.0.0.1:1/tts"
    assert await voice.as_reply("你好", event) is None
    PASSED.append("合成失败时随机语音返回 None（调用方回退文字）")


async def _send(bot, event, message):
    if getattr(event, "group_id", None) is not None:
        return await bot.send_group_msg(group_id=int(event.group_id), message=message)
    return await bot.send_private_msg(user_id=int(event.user_id), message=message)


async def client_cases(base):
    result = await fetch(base)
    assert result.sample_rate == 16000 and abs(result.duration - 0.3) < 0.01
    assert result.data == GOOD
    PASSED.append("正常合成：16 kHz / 0.3 s / 字节一致")

    for path, kind in (("/json", "bad-content-type"), ("/truncated", "invalid-audio")):
        try:
            await fetch(base + path)
        except tts.TTSError as exc:
            assert exc.kind == kind, (path, exc.kind)
        else:
            raise AssertionError("accepted bad response: " + path)
    PASSED.append("拒绝 JSON 错误响应与截断 WAV")

    try:
        await tts.synthesize("你好", endpoint=base + "/tts", token="wrong")
    except tts.TTSError as exc:
        assert exc.kind == "http"
    else:
        raise AssertionError("accepted 401")
    PASSED.append("401 正确归类为 http")

    try:
        await fetch(base, max_bytes=1024)
    except tts.TTSError as exc:
        assert exc.kind == "oversize"
    else:
        raise AssertionError("accepted oversize")
    PASSED.append("超过 max_bytes 被拒")

    try:
        await fetch(base, max_chars=1)
    except tts.TTSError as exc:
        assert exc.kind == "too-long"
    else:
        raise AssertionError("accepted too-long text")
    PASSED.append("超长文本在发请求前被拒")

    try:
        await fetch(base + "/slow", total_timeout=0.4)
    except tts.TTSError as exc:
        assert exc.kind == "timeout"
    else:
        raise AssertionError("timeout swallowed")
    PASSED.append("超时归类为 timeout")

    try:
        await fetch(base + "/long", max_seconds=2)
    except tts.TTSError as exc:
        assert exc.kind == "too-long-audio"
    else:
        raise AssertionError("accepted too-long audio")
    PASSED.append("超过 max_seconds 被拒")


async def main(base):
    await client_cases(base)
    if PLUGIN_READY:
        await plugin_cases(base)
    for label in PASSED:
        print("PASS:", label)
    mode = "容器（含 NoneBot）" if PLUGIN_READY else "本机（仅客户端）"
    print(f"RESULT: {len(PASSED)}/{len(PASSED)} passed in {mode}; "
          "real GPT-SoVITS / NapCat / QQ playback NOT tested")


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
