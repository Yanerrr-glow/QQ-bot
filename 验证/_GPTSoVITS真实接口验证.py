"""请求已运行的 GPT-SoVITS api_v2；只保存音频，不发送 QQ 消息。"""
import argparse
import base64
import io
import json
import time
import wave
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:9880/tts')
    parser.add_argument('--ref', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--text', default='你好，我已经可以通过接口生成语音了。')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    payload = dict(text=args.text, text_lang='zh', ref_audio_path=args.ref,
                   prompt_text=args.prompt, prompt_lang='zh', media_type='wav',
                   streaming_mode=False, text_split_method='cut5', batch_size=1,
                   seed=42, parallel_infer=True)
    started = time.perf_counter()
    with httpx.Client(timeout=180, trust_env=False) as client:
        with client.stream('POST', args.url, json=payload) as response:
            response.raise_for_status()
            content_type = response.headers.get('content-type', '')
            if not content_type.startswith('audio/'):
                raise ValueError('Expected audio response: ' + response.read()[:1500].decode('utf-8', errors='replace'))
            audio = bytearray()
            for chunk in response.iter_bytes():
                audio.extend(chunk)
                if len(audio) > 20 * 1024 * 1024:
                    raise ValueError('Audio exceeds 20 MiB test limit')
    elapsed = time.perf_counter() - started
    with wave.open(io.BytesIO(audio), 'rb') as reader:
        rate, channels, width, frames = reader.getframerate(), reader.getnchannels(), reader.getsampwidth(), reader.getnframes()
        pcm = reader.readframes(frames)
        assert frames > 0 and len(pcm) == frames * channels * width
    encoded = base64.b64encode(audio).decode('ascii')
    assert base64.b64decode(encoded, validate=True) == audio
    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(audio)
    result = dict(text=args.text, http_status=200, content_type=content_type,
                  bytes=len(audio), sample_rate=rate, channels=channels, sample_width=width,
                  duration_seconds=round(frames / rate, 3), request_seconds=round(elapsed, 3),
                  base64_roundtrip=True, qq_sent=False, output=str(output))
    output.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
