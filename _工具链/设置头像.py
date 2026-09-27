"""把一张本地图片设为机器人 QQ 头像。

OneBot v11 标准里没有"改头像"这个动作，NapCat 把它做成了扩展接口
`set_qq_avatar`，参数 `file` 支持 `base64://` 与 `file://`。
这里统一转成 base64 发过去，免得路径里的中文和空格在 URL 编码上出岔子。

不需要机器人进程参与：直接以 WebSocket 客户端身份连 NapCat 的 OneBot 端口，
调完就断开。NapCat 的正向 WS 服务端支持多客户端并存，不会挤掉正在跑的机器人。

用法：
    .\\.venv\\Scripts\\python.exe '_工具链\\设置头像.py' '图片.jpg'
    .\\.venv\\Scripts\\python.exe '_工具链\\设置头像.py' '图片.jpg' --ws ws://127.0.0.1:6700

**与 `/头像` 指令的分工**（2026-09-26 起机器人自己也能换头像了）：
本脚本是**机器人在线时的旁路**（不需要机器人进程、直接从本机连 NapCat），
适合"在电脑前改一张图"；`/头像` 是在**聊天里**改（引用或附带一张图发指令即可），
由 `plugins/ai_chat/identity.py` 走 `bot.call_api()` 完成。
本脚本还有一个用处：它拿得到**完整信封**的返回体，所以能用来看接口到底回了什么。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import pathlib
import sys

import websockets

ECHO = "set-avatar"


async def set_avatar(image: pathlib.Path, ws_url: str, timeout: float) -> dict:
    data = image.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    payload = {
        "action": "set_qq_avatar",
        "params": {"file": f"base64://{encoded}"},
        "echo": ECHO,
    }

    # 图片 base64 后会膨胀约 1/3，心跳与事件也会从这条连接过来，所以
    # 不能只 recv 一次就当成响应 —— 要循环到 echo 对上的那一条。
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=15) as ws:
        await ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("echo") == ECHO:
                return message
            # 其余是事件/心跳，忽略


def main() -> int:
    parser = argparse.ArgumentParser(description="设置机器人 QQ 头像")
    parser.add_argument("image", help="本地图片路径（jpg/png/webp）")
    parser.add_argument("--ws", default="ws://127.0.0.1:6700", help="OneBot 正向 WS 地址")
    parser.add_argument("--timeout", type=float, default=60.0, help="等待接口返回的秒数")
    args = parser.parse_args()

    image = pathlib.Path(args.image)
    if not image.is_absolute():
        image = (pathlib.Path(__file__).resolve().parent.parent / image).resolve()
    if not image.exists():
        print(f"[阻塞] 图片不存在：{image}")
        return 1

    size_kb = image.stat().st_size / 1024
    print(f"图片：{image}")
    print(f"大小：{size_kb:.1f} KB（base64 后约 {size_kb * 4 / 3:.1f} KB）")
    print(f"目标：{args.ws}")

    try:
        result = asyncio.run(set_avatar(image, args.ws, args.timeout))
    except asyncio.TimeoutError:
        print(f"[阻塞] {int(args.timeout)} 秒内没等到接口返回 —— 检查 NapCat 是否在跑")
        return 1
    except OSError as exc:
        print(f"[阻塞] 连不上 {args.ws}：{exc}")
        print("       先确认 NapCat 已启动，且 6700 在监听。")
        return 1

    ok = result.get("retcode") == 0 or result.get("status") == "ok"
    print(f"返回：{json.dumps(result, ensure_ascii=False)[:400]}")
    if ok:
        print("\n[OK] 头像设置成功。QQ 客户端有缓存，可能过一会儿才刷新。")
        return 0
    print("\n[阻塞] 接口返回了失败，请看上面的 message 字段。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
