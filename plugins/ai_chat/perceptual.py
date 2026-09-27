"""感知哈希：认出「内容一样但文件不一样」的图。

## 为什么需要它

原来的去重是**内容哈希**（SHA-256）—— 只要字节差一个就判为不同：

```
同一张表情，A 转发一次 → hash a1b2...
被压缩过、或被别人重新保存转发 → hash 9f8e...   ← 当成两张，库里存两份
```

群里最典型的重复就是这么来的：同一张图经过微信 / QQ 的二次压缩、改尺寸、
重新编码（png↔jpg），字节全变，但**人眼看上去是同一张**。
所以它会一遍遍往库里塞同一张图，看着"库在长大"，其实全是重复。

## 用什么办法

**pHash（DCT 感知哈希）**，64 位：缩放 → 灰度 → DCT → 取左上低频 8×8 → 与均值比较取 0/1。

低频系数描述的是"大块的明暗分布"，正好是压缩、缩放、轻微改色都不太动的部分。
两张图相似度就用 64 位里的**汉明距离**（不同位的个数）衡量：

| 汉明距离 | 通常意味着 |
|---|---|
| 0~6 | 同一张图的再压缩 / 缩放 / 转格式 |
| 7~12 | 很像的图（同一套表情的不同张、加了字的同一张） |
| 20+ | 基本是两张不同的图 |

阈值默认 8，可在控制台「表情包」组里调（`sticker_dup_distance`）。

## 为什么不用 aHash / 纯平均色

`aHash`（灰度均值哈希）对亮度整体偏移太敏感，被调过亮度的同一张图会判成两张。
`dHash`（相邻像素差分）对**缩放**比 pHash 敏感。三者的抗性实测比较见
`_工具链/离线验证_桩.py` 的第 20 节。

## 依赖是可选的

需要 Pillow（解码图片）+ numpy（DCT）。**两个都没装时本模块优雅退化**：
`available()` 返回 False，入库流程退回原来的 SHA-256 精确去重，
只是认不出"改过编码的同一张图"。不会因为缺依赖就崩。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ai_chat.perceptual")

# 64 位哈希的十六进制长度
HEX_LEN = 16
_BITS = 64

try:  # Pillow：解码任何常见格式（jpg/png/gif/webp/bmp）并缩放
    from PIL import Image  # type: ignore

    _HAVE_PIL = True
except Exception:  # noqa: BLE001 - 缺依赖不是错误，退化成"只有精确去重"
    Image = None  # type: ignore
    _HAVE_PIL = False

try:
    import numpy as np  # type: ignore

    _HAVE_NUMPY = True
except Exception:  # noqa: BLE001
    np = None  # type: ignore
    _HAVE_NUMPY = False

# DCT 需要 numpy；没有 numpy 时可以退回纯 Python 的 DCT，但没必要 ——
# 64 点的 2D DCT 在纯 Python 里每张图要几十毫秒，而入库本来就异步，不值得为它写两套。
_READY = _HAVE_PIL and _HAVE_NUMPY


def available() -> bool:
    """感知哈希能不能用（Pillow + numpy 都在）。"""
    return _READY


def unavailable_reason() -> str:
    if _READY:
        return ""
    missing = []
    if not _HAVE_PIL:
        missing.append("Pillow")
    if not _HAVE_NUMPY:
        missing.append("numpy")
    return "缺少 " + " 和 ".join(missing)


# --------------------------------------------------------------------- 计算
# pHash 的固定参数：先缩到 32×32，DCT 后取左上 8×8 低频块
_RESIZE = 32
_LOW = 8
_HASH_SIZE = 8


def _dct_matrix(n: int):
    """n×n 的 DCT-II 变换矩阵（正交归一化）。"""
    k = np.arange(n).reshape(-1, 1)
    x = np.arange(n).reshape(1, -1)
    matrix = np.cos(np.pi * (2 * x + 1) * k / (2 * n))
    matrix[0, :] *= np.sqrt(1.0 / n)
    matrix[1:, :] *= np.sqrt(2.0 / n)
    return matrix


_DCT = _dct_matrix(_RESIZE) if _READY else None


def phash(data: bytes) -> str:
    """算 64 位感知哈希，返回 16 位十六进制（失败返回空串）。

    步骤就是教科书 pHash：
    1. 解码 → 转灰度 → 缩到 32×32（缩放本身顺便抹掉了压缩噪声）；
    2. 做 2D DCT，取左上 8×8 的**低频**块（丢掉高频细节 = 丢掉压缩痕迹）；
    3. 与这块自身的均值比较，大于记 1、否则记 0，得到 64 bit。

    第 3 步用"与均值比"而不是"取中位数"，是为了让**整体亮度偏移**不改变结果
    （同一张图调亮一点，低频块整体抬升，但相对均值的大小关系基本不变）。
    """
    if not _READY:
        return ""
    try:
        import io

        with Image.open(io.BytesIO(data)) as img:
            # GIF 动图取第一帧 —— 库里只需要一个代表
            if getattr(img, "is_animated", False):
                img.seek(0)
            gray = img.convert("L").resize((_RESIZE, _RESIZE), Image.LANCZOS)
            pixels = np.asarray(gray, dtype=np.float64)
    except Exception:  # noqa: BLE001 - 解码失败（截断文件、不支持的格式）
        logger.debug("感知哈希：图片解码失败")
        return ""

    try:
        # 2D DCT = D · A · Dᵀ
        coeffs = _DCT @ pixels @ _DCT.T
        block = coeffs[:_LOW, :_LOW]
        # 去掉直流分量再取均值：DC 项代表整体亮度，参与比较会让"整体调亮"改变结果
        flat = block.flatten()
        avg = (flat.sum() - flat[0]) / (flat.size - 1)
        bits = (flat > avg).astype(np.uint8)
        value = 0
        for bit in bits:
            value = (value << 1) | int(bit)
        return f"{value:0{HEX_LEN}x}"
    except Exception:  # noqa: BLE001
        logger.debug("感知哈希：DCT 计算失败")
        return ""


def distance(a: str, b: str) -> int:
    """两个 16 位十六进制哈希的汉明距离；任一非法返回 64（= 完全不像）。"""
    if not a or not b or len(a) != HEX_LEN or len(b) != HEX_LEN:
        return _BITS
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return _BITS


def size_of(data: bytes) -> tuple[int, int]:
    """(宽, 高)；读不出来返回 (0, 0)。只用于记录元信息与诊断，不参与判定。"""
    if not _HAVE_PIL:
        return (0, 0)
    try:
        import io

        with Image.open(io.BytesIO(data)) as img:
            return (int(img.width), int(img.height))
    except Exception:  # noqa: BLE001
        return (0, 0)


# --------------------------------------------------------------------- 近似去重
def find_near(
    items: list[dict[str, Any]],
    *,
    digest: str = "",
    phash_value: str = "",
    limit: int,
) -> tuple[dict[str, Any] | None, int]:
    """在已有条目里找与新图**内容近似**的那一条。返回 (命中的条目, 最小距离)。

    两级判定，先便宜后贵：

    1. **精确哈希**先比一次 —— 字节完全相同是最常见的情况（同一个人重复转发），
       这时连感知哈希都不用算；
    2. 再按感知哈希的汉明距离找最近的。距离 0 也算命中（不同编码方式算出同一个感知哈希）。

    老条目可能没有 `phash`（改造前入库的），这时只有精确哈希能命中 ——
    不会误判，只是认不出它们的历史重复。
    """
    if not items:
        return None, _BITS

    if digest:
        for item in items:
            if item.get("hash") == digest:
                return item, 0

    if not phash_value or limit is None or limit < 0:
        return None, _BITS

    best: dict[str, Any] | None = None
    best_distance = _BITS
    for item in items:
        other = str(item.get("phash") or "")
        if not other:
            continue
        value = distance(phash_value, other)
        if value < best_distance:
            best, best_distance = item, value
    if best is not None and best_distance <= limit:
        return best, best_distance
    return None, best_distance
