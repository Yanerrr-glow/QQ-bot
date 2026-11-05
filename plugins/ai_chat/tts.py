"""GPT-SoVITS（或任意兼容 HTTP TTS）客户端：文本 -> 完整 WAV 字节。

只做「请求 + 校验 + 限流」，不碰 QQ、不决定发给谁 —— 发送在 `voice.py` 里。

为什么自己写而不是用现成 SDK：上游 `POST /tts` 是几十个字段的 JSON、响应是裸音频字节，
没有版本化契约；这里只固定首版用到的字段，其余全部由服务端配置决定，客户端不暴露本地路径。

**依赖**：生产容器里装的是 `httpx2`（没有 `httpx`），本地开发环境通常是 `httpx`，
两个都提供同一套 `AsyncClient`，所以这里做一次兼容导入，不额外往 requirements 里加包。
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import wave
from dataclasses import dataclass

try:  # 生产容器：httpx2；本地：httpx
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - 取决于部署环境
    import httpx2 as httpx  # type: ignore

logger = logging.getLogger("ai_chat.tts")


class TTSError(Exception):
    """合成失败。`kind` 用来决定回退文案与日志字段，不要拿 message 做判断。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass
class AudioResult:
    data: bytes
    media_type: str
    sample_rate: int
    duration: float
    elapsed: float


def _wav_info(data: bytes) -> tuple[int, float]:
    """解析 WAV 头，返回 (采样率, 秒数)；不是合法 WAV 就抛 TTSError。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            rate = reader.getframerate()
            frames = reader.getnframes()
            width = reader.getsampwidth()
            channels = reader.getnchannels()
            pcm = reader.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise TTSError("invalid-audio", str(exc)) from exc
    if not frames or rate <= 0:
        raise TTSError("empty-audio", "0 frames")
    if len(pcm) != frames * channels * width:
        raise TTSError("truncated-audio", f"{len(pcm)} != {frames * channels * width}")
    return rate, frames / float(rate)


async def synthesize(
    text: str,
    *,
    endpoint: str,
    token: str = "",
    ref_audio: str = "",
    prompt_text: str = "",
    gpt_weights: str = "",
    sovits_weights: str = "",
    prompt_lang: str = "zh",
    text_lang: str = "zh",
    connect_timeout: float = 3.0,
    total_timeout: float = 20.0,
    max_chars: int = 150,
    max_bytes: int = 3 * 1024 * 1024,
    max_seconds: float = 30.0,
) -> AudioResult:
    """合成一段短文本。

    失败一律抛 `TTSError`，**不返回空字节假装成功**：
    kind ∈ disabled / not-configured / too-long / timeout / network / http / bad-content-type /
           empty-audio / invalid-audio / truncated-audio / oversize / too-long-audio。
    """
    text = (text or "").strip()
    if not text:
        raise TTSError("empty-text")
    if len(text) > max_chars:
        raise TTSError("too-long", f"{len(text)} > {max_chars}")
    if not endpoint:
        raise TTSError("not-configured", "TTS_ENDPOINT 为空")

    payload = {
        "text": text,
        "text_lang": text_lang,
        "ref_audio_path": ref_audio,
        "prompt_text": prompt_text,
        "prompt_lang": prompt_lang,
        "media_type": "wav",
        "streaming_mode": False,
        "text_split_method": "cut5",
        "batch_size": 1,
        "parallel_infer": True,
        "speed_factor": 1.0,
    }
    # 自定义训练权重：由 relay 先调 /set_gpt_weights、/set_sovits_weights 再转发 /tts。
    # 留空表示沿用引擎当前权重。
    if gpt_weights:
        payload["gpt_weights"] = gpt_weights
    if sovits_weights:
        payload["sovits_weights"] = sovits_weights
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    started = time.perf_counter()
    timeout = httpx.Timeout(connect=connect_timeout, read=total_timeout,
                            write=total_timeout, pool=connect_timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream("POST", endpoint, json=payload, headers=headers) as response:
                ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if response.status_code != 200:
                    body = await response.aread()
                    raise TTSError("http", f"{response.status_code} {body[:200]!r}")
                if ctype not in {"audio/wav", "audio/x-wav", "audio/wave"}:
                    body = await response.aread()
                    raise TTSError("bad-content-type", f"{ctype} {body[:200]!r}")
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise TTSError("oversize", f">{max_bytes} bytes")
    except TTSError:
        raise
    except httpx.TimeoutException as exc:
        raise TTSError("timeout", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 网络层各种异常统一归类
        raise TTSError("network", f"{type(exc).__name__}: {exc}") from exc

    elapsed = time.perf_counter() - started
    data = bytes(buffer)
    rate, seconds = _wav_info(data)
    if seconds > max_seconds:
        raise TTSError("too-long-audio", f"{seconds:.1f}s > {max_seconds}s")
    logger.info(
        "TTS 合成完成 chars=%d bytes=%d rate=%d duration=%.2fs elapsed=%.2fs",
        len(text), len(data), rate, seconds, elapsed,
    )
    return AudioResult(data=data, media_type=ctype, sample_rate=rate,
                       duration=seconds, elapsed=elapsed)


async def healthz(endpoint: str, token: str = "", timeout: float = 3.0) -> bool:
    """探测网关存活；不触发合成。失败返回 False，不抛。"""
    base = endpoint.rsplit("/", 1)[0] if endpoint.endswith("/tts") else endpoint
    url = base.rstrip("/") + "/healthz"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url, headers=headers)
            return response.status_code == 200
    except Exception:  # noqa: BLE001 - 探活失败就是 False
        return False


async def synthesize_or_none(text: str, **kwargs) -> AudioResult | None:
    """给「失败就回退文字」的调用方用的薄封装。"""
    try:
        return await synthesize(text, **kwargs)
    except TTSError as exc:
        logger.warning("TTS 合成失败 kind=%s detail=%s", exc.kind, exc.detail[:300])
        return None
    except asyncio.CancelledError:
        raise
