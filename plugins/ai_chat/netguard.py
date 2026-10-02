"""网络出站安全：**公网校验**（SSRF 防线）的唯一实现。

## 为什么把它单独抽出来

改造前只有 `fetch.py` 有完整防护（`_check_url` + `_is_public_ip` + 逐跳重定向复检），
而这三处**都不过这道校验**：

* `files.download_bytes()` —— 群文件的 `url` 是外部可控输入；
* `files._download_via_ips()` —— 同一个入口，且它还会**临时替换全局 `socket.getaddrinfo`**；
* `stickers._download()` —— 图片段的 `url` / `file` 同样是外部可控输入。

于是群友发一条消息就能让容器去访问 `http://searxng:8080`（同 compose 网络内的服务）、
`http://169.254.169.254`（云元数据）这类内网目标。现在四处统一走这里。

## 判据（任一条不满足即拒绝）

1. scheme 必须是 `http` / `https`（`file://`、`gopher://` 一律拒）；
2. **DNS 解析出来的每一个地址**都必须是公网 —— 一个内网就拒，
   不给"同一域名既解析出公网又解析出内网"留碰运气的空间；
3. 直接写 IP 字面量时同样按上一条判（`getaddrinfo` 也能解析字面量）；
4. **重定向每一跳都要重查**（`SafeRedirect`）—— 首轮检查拦不住"公网域名 302 到内网"。

`is_public_ip` 另外把 **IPv4-mapped IPv6**（`::ffff:127.0.0.1`）还原成 IPv4 再判 ——
否则 `::ffff:169.254.169.254` 会被当成"普通的公网 v6"放行（Python 3.12 的
`IPv6Address.is_private` 不看映射进去的 v4）。

## 与 fetch 的分工

`fetch._check_url` 保留为**转发 + 网页策略**（它还要拒掉 `.zip`/`.pdf` 这类"不是网页"的链接）；
`files` / `stickers` 要的正是二进制，所以它们只调这里的 `check_url`，不走网页策略。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("ai_chat.netguard")

# 「按 IP 逐个试」那条路要临时替换 **全局** `socket.getaddrinfo`（原因见
# `files._download_via_ips` 的 docstring：只改解析、不改连接目标，SNI 与证书校验才正确）。
# 那是进程级改动，**同一时刻只允许一次**：并发下载时两边会互相把解析钉到对方的 IP 上。
# 改造前没有这把锁（进程级、非线程安全），这里补上。
PIN_LOCK = threading.Lock()


def is_public_ip(host: str) -> bool:
    """这个主机名解析出来的地址，是不是**全部**都是公网地址。

    任一解析结果是私网/环回/链路本地/保留段，就判为不安全。
    "全部"是关键：一个域名同时解析出公网 IP 和内网 IP 时，不能因为有个公网的放行。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    seen = 0
    for info in infos:
        addr = str(info[4][0]).split("%")[0]  # 去掉 v6 的 zone id
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            # ::ffff:127.0.0.1 这类：按它映射的 v4 判，别当"普通公网 v6"
            ip = mapped
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
        seen += 1
    return seen > 0


def check_url(url: str) -> tuple[bool, str]:
    """放行前的检查（**只管安全**，不管"是不是网页"）。返回 (能不能读, 不能读的原因)。"""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False, "URL 解析不了"
    if parsed.scheme not in ("http", "https"):
        return False, "只支持 http/https"
    host = parsed.hostname or ""
    if not host:
        return False, "没有主机名"
    # 直接写 IP 字面量时 getaddrinfo 也能解析，这个判断覆盖两种情况
    if not is_public_ip(host):
        return False, "目标是内网/本机地址，不允许访问"
    return True, ""


def public_ips(host: str, *, limit: int = 8) -> list[str]:
    """该主机的 IPv4 地址里**只保留公网的那些**（按 DNS 顺序去重）。

    「按 IP 逐个试」用它：即使 DNS 返回了内网地址，也永远不会被钉成连接目标。
    """
    try:
        raw = [i[4][0] for i in socket.getaddrinfo(host, None, socket.AF_INET)]
    except OSError:
        return []
    out: list[str] = []
    for addr in raw:
        if addr in out:
            continue
        try:
            ip = ipaddress.ip_address(str(addr).split("%")[0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
                or ip.is_multicast or ip.is_unspecified:
            continue
        out.append(addr)
        if len(out) >= limit:
            break
    return out


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """重定向时再查一次安全 —— 首轮检查拦不住"公网域名 302 到内网"。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        ok, reason = check_url(newurl)
        if not ok:
            raise urllib.error.URLError(f"重定向被拒：{reason}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener() -> urllib.request.OpenerDirector:
    """带逐跳复检的 opener。

    ⚠ `urllib.request.urlopen()` 用的是**默认 opener**，它**不查重定向** ——
    所以下载路径要用这个，别直接 `urlopen`。
    """
    return urllib.request.build_opener(SafeRedirect)


def open_checked(url_or_req: Any, *, timeout: float):
    """`urlopen` 的安全替代：重定向每一跳都过 `check_url`。

    调用方负责 `with ... as resp:`（返回值就是 response 对象）。
    """
    if isinstance(url_or_req, str):
        url_or_req = urllib.request.Request(  # noqa: S310 - scheme 由 check_url 兜住
            url_or_req, headers={"User-Agent": "nonebot-ai-chat/1.0"})
    return build_opener().open(url_or_req, timeout=timeout)
