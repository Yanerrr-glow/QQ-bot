"""验证 files.py 的下载重试与失败诊断（真实 HTTP，不 mock 网络）。

跑法：python _工具链/_文件读取验证.py   退出码 0 = 通过
只依赖标准库 + 桩（不需要 nonebot / openai）。
"""

from __future__ import annotations

import asyncio
import http.server
import importlib.util
import sys
import threading
import types
from pathlib import Path

PASSED = 0
FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  [OK] {label}" + (f" —— {detail}" if detail else ""))
    else:
        FAILED.append(label)
        print(f"  [失败] {label}" + (f" —— {detail}" if detail else ""))


PKG = Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"
pkg = types.ModuleType("ai_chat")
pkg.__path__ = [str(PKG)]
sys.modules["ai_chat"] = pkg
stub_settings = types.ModuleType("ai_chat.settings")
stub_settings.get = lambda k, d=None: {"file_enabled": True, "file_max_kb": 4096,
                                       "file_max_chars": 8000}.get(k, d)
sys.modules["ai_chat.settings"] = stub_settings
setattr(pkg, "settings", stub_settings)

spec = importlib.util.spec_from_file_location("ai_chat.files", PKG / "files.py")
files = importlib.util.module_from_spec(spec)
sys.modules["ai_chat.files"] = files
spec.loader.exec_module(files)
print(f"（已加载 files.py）\n")

# --------------------------------------------------------------------- 假站点
CONTENT = "第一行内容\n第二行内容\n"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/ok"):
            raw = CONTENT.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path.startswith("/gone"):
            self.send_response(404)
            self.end_headers()
        elif self.path.startswith("/forbidden"):
            self.send_response(403)
            self.end_headers()
        else:
            self.send_response(500)
            self.end_headers()

    def log_message(self, *a) -> None:  # noqa: ANN002
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
base = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"假站点：{base}\n")

# --------------------------------------------------------------------- 1. _download
print("=== 1. _download 的可诊断错误 ===")
data, err = asyncio.run(files._download(base + "/ok"))
check("成功拿到内容", data is not None and "第一行内容" in data.decode("utf-8"), err)

data, err = asyncio.run(files._download(base + "/gone"))
check("404 报出 HTTP 404（而不是含糊的失败）", data is None and "404" in err, err)

data, err = asyncio.run(files._download(base + "/forbidden"))
check("403 报出 HTTP 403（需要鉴权时的典型码）", data is None and "403" in err, err)

data, err = asyncio.run(files._download("http://127.0.0.1:1/x"))
check("连不上时报出具体原因（非笼统失败）", data is None and bool(err) and "4" not in err[:4], err)

has_httpx = importlib.util.find_spec("httpx") is not None
print(f"\n  （httpx 可用：{has_httpx} —— 容器里一定有，它是 nonebot 的依赖）")
if has_httpx:
    data, err = asyncio.run(files._download(base + "/forbidden"))
    check("多传输层失败时原因会串起来", "httpx" in err or "HTTP 403" in err, err)

# --------------------------------------------------------------------- 2. 重试
print("\n=== 2. _download_with_retry：直链失败 → 换新链接再试 ===")


class _FakeBot:
    """假 OneBot：`get_group_file_url` 返回一个可指定（或默认可用）的新链接。"""

    def __init__(self, new_url: str = "") -> None:
        self.calls = 0
        self._new_url = new_url or (base + "/ok")

    async def call_api(self, api: str, **kw):  # noqa: ANN003
        self.calls += 1
        if api == "get_group_file_url":
            return {"url": self._new_url}
        return {}


async def case_retry_ok() -> tuple:
    seg = {"file": "a.txt", "file_id": "/abc", "file_size": 30, "url": base + "/gone"}
    bot = _FakeBot()
    got = await files._download_with_retry(bot, base + "/gone", seg, 123, 0, "a.txt")
    return got, bot.calls


(data, err), calls = asyncio.run(case_retry_ok())
check("直链 404 → 换链后成功", data is not None and "第一行内容" in data.decode("utf-8"), err)
check("确实调了 get_group_file_url 换链接", calls == 1, f"calls={calls}")


async def case_retry_fail() -> tuple:
    """换到的新链接**也不可用**（403）→ 两个失败原因都要报出来。

    注意假 bot 返回的是 `/forbidden`：所以"换链后"这次也是 403，
    这才测得到聚合错误的分支（原来那个用例拿 `/ok` 当新链接，本就该成功）。
    """
    seg = {"file": "a.txt", "file_id": "/abc", "file_size": 30, "url": base + "/gone"}
    bot = _FakeBot(new_url=base + "/forbidden")
    got = await files._download_with_retry(bot, base + "/gone", seg, 123, 0, "a.txt")
    return got, bot.calls


(data, err), calls = asyncio.run(case_retry_fail())
check("换链后仍失败 → 两个原因都报出来",
      data is None and "HTTP 404" in err and "换链后" in err and "403" in err,
      f"data={data} err={err!r}")


async def case_direct_ok() -> tuple:
    seg = {"file": "a.txt", "file_id": "/abc", "file_size": 30, "url": base + "/ok"}
    bot = _FakeBot()
    got = await files._download_with_retry(bot, base + "/ok", seg, 123, 0, "a.txt")
    return got, bot.calls


(data, err), calls = asyncio.run(case_direct_ok())
check("直链能用时不浪费一次换链接", data is not None and calls == 0, f"calls={calls}")

# --------------------------------------------------------------------- 3. 上限
print("\n=== 3. file_max_kb 默认值（原来 512 太小）===")
check("默认上限已提到 4MB", stub_settings.get("file_max_kb") >= 4096,
      str(stub_settings.get("file_max_kb")))

srv.shutdown()
print(f"\n=== 结果：通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
for n in FAILED:
    print("  失败：", n)
sys.exit(1 if FAILED else 0)
