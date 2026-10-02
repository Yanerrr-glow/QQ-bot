"""`netguard`（公网校验 / SSRF 防线）的离线验证：**纯逻辑，不联网、不调模型**。

为什么单独一个脚本：`netguard.py` 只依赖标准库，所以这里**连桩都不用打** ——
按文件直接加载即可（不需要 nonebot，也不需要 config/settings）。

它守的是 2026-09-28 补上的那道口子：改造前只有 `fetch.py` 有完整防护，
而 `files.download_bytes()` / `files._download_via_ips()` / `stickers._download()`
**都不过校验** —— 群文件 url、图片段 url/file 是外部可控输入，
于是可以让容器去访问 `http://searxng:8080`、`http://169.254.169.254`（云元数据）。
"""
from __future__ import annotations

import importlib.util
import os
import socket
import sys
import types
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PKG = os.path.join(PROJ, "plugins", "ai_chat")

FAILED = []
PASSED = 0


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  [OK] " + name)
    else:
        FAILED.append(name)
        print("  [FAIL] " + name + ((" —— " + str(detail)) if detail else ""))


# ---- 按文件加载 netguard（只设 __path__，不执行包的 __init__.py）------------
_pkg = types.ModuleType("ai_chat")
_pkg.__path__ = [PKG]
sys.modules["ai_chat"] = _pkg

spec = importlib.util.spec_from_file_location("ai_chat.netguard", os.path.join(PKG, "netguard.py"))
ng = importlib.util.module_from_spec(spec)
sys.modules["ai_chat.netguard"] = ng
spec.loader.exec_module(ng)

REAL_GETADDRINFO = socket.getaddrinfo


def fake_dns(mapping):
    """把 host → [ip...] 的假解析装上去（返回原来的 getaddrinfo 以便还原）。"""
    def _gi(host, port, *args, **kw):
        if host in mapping:
            out = []
            for ip in mapping[host]:
                fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
                out.append((fam, socket.SOCK_STREAM, 6, "", (ip, port or 0)))
            return out
        return REAL_GETADDRINFO(host, port, *args, **kw)
    ng.socket.getaddrinfo = _gi
    return _gi


def restore_dns():
    ng.socket.getaddrinfo = REAL_GETADDRINFO


print("-- 1. 内网 / 特殊地址一律拒绝 --")
BAD = [
    "http://127.0.0.1/",
    "http://127.0.0.1:8080/onebot/v11/ws",
    "http://localhost/",
    "http://10.0.0.5/x",
    "http://192.168.1.1/x",
    "http://172.16.0.9/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/",
    "http://224.0.0.1/",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:169.254.169.254]/",
]
for u in BAD:
    ok, why = ng.check_url(u)
    check("拒绝 %s" % u, not ok, why)

print("\n-- 2. scheme 白名单 --")
for u in ["file:///etc/passwd", "gopher://x/", "ftp://example.com/a", "javascript:alert(1)"]:
    ok, why = ng.check_url(u)
    check("拒绝 scheme：%s" % u, not ok, why)
ok, why = ng.check_url("https://example.com/")
check("放行 https", ok, why)
ok, why = ng.check_url("http://")
check("没有主机名要拒绝", not ok, why)

print("\n-- 3. 公网地址放行 --")
for u in ["http://1.1.1.1/", "https://8.8.8.8/x"]:
    ok, why = ng.check_url(u)
    check("放行 %s" % u, ok, why)

print("\n-- 4. DNS 多地址：有一个内网就拒（不给碰运气的空间）--")
fake_dns({"mixed.example": ["93.184.216.34", "10.1.2.3"]})
ok, why = ng.check_url("http://mixed.example/")
check("公网+内网混合解析 → 拒绝", not ok, why)
fake_dns({"pure.example": ["93.184.216.34", "93.184.216.35"]})
ok, why = ng.check_url("http://pure.example/")
check("全公网解析 → 放行", ok, why)
fake_dns({"mapped.example": ["::ffff:10.0.0.7"]})
ok, why = ng.check_url("http://mapped.example/")
check("IPv4-mapped 内网 → 拒绝", not ok, why)
restore_dns()
ok, why = ng.check_url("http://no-such-host.invalid/")
check("解析不出来 → 拒绝（不是「默认放行」）", not ok, why)

print("\n-- 5. public_ips 只留公网地址（按 IP 重试那条路用）--")
fake_dns({"cdn.example": ["10.0.0.1", "93.184.216.34", "127.0.0.1", "93.184.216.34"]})
ips = ng.public_ips("cdn.example")
check("内网被过滤掉", ips == ["93.184.216.34"], str(ips))
restore_dns()

print("\n-- 6. 重定向：每一跳都复检 --")
h = ng.SafeRedirect()
try:
    h.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/")
    check("302 到内网必须被拒", False, "没抛异常")
except urllib.error.URLError as exc:
    check("302 到内网被拒（URLError）", "重定向被拒" in str(exc), str(exc))

print("\n-- 7. PIN_LOCK 存在（并发下载不能互相钉 IP）--")
check("netguard.PIN_LOCK 是个可用的锁", hasattr(ng.PIN_LOCK, "__enter__") and hasattr(ng.PIN_LOCK, "__exit__"))

print("\n=== 结果：通过 %d 项，失败 %d 项 ===" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  失败：", f)
sys.exit(1 if FAILED else 0)
