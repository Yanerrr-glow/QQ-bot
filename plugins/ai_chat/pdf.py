"""PDF 读取：先抽文本层，抽不出来就渲染成图再用 OCR 认。

## 两条路，缺一不可

| 类型 | 特征 | 走哪条 |
|---|---|---|
| **电子版**（Word 导出、LaTeX 编译的论文）| 有文本层 | `PyMuPDF` 直接抽字，快且准 |
| **扫描版 / 图片型**（扫描件、截图打印件）| 没有文本层 | 渲染成 PNG → `rapidocr` 认字 |

只做第一条会漏掉扫描件；只做第二条则把"本来就有精确文字"的 PDF 白白 OCR 一遍
（慢十倍、还可能认错字）。所以先抽文本，**抽出来的字太少**才回落 OCR。

## 为什么选 PyMuPDF

容器里已经有 `rapidocr-onnxruntime`（当初为网页截图兜底装的），所以 OCR 那半不用新增。
PDF 这半选 PyMuPDF 而不是 pdfplumber/pdfminer：它是**单个自带的 wheel**（24.6MB，
`manylinux_2_28_x86_64`，不依赖 poppler 之类的系统库），而且**同一个库既能抽文本又能渲染图片** ——
不用再装 pdf2image + poppler。

## 三条硬上限（跟 files.py 的其它路径同一套思路）

* 页数：只读前 `_MAX_PAGES` 页（一份 300 页的论文不该把 prompt 和内存一起撑爆）；
* 字数：正文截到调用方给的 `max_chars`；
* 渲染页数：OCR 只认前 `_MAX_OCR_PAGES` 页 —— OCR 是**秒级/页**的，几十页会让人以为机器人卡死。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ai_chat.pdf")

# 硬上限（不受配置放大，配置只管"最终给模型多少字"）
_MAX_PAGES = 30          # 最多看 30 页
_MAX_OCR_PAGES = 3       # OCR 最多认 3 页（每页 ~10s，再多会拖死回复）
_OCR_MIN_CHARS = 120     # 文本层少于这个字数 → 认为是扫描版，回落 OCR
_OCR_DPI_SCALE = 2.0     # 渲染缩放：150 DPI 左右，OCR 对 20~40px 字最稳


def available() -> tuple[bool, str]:
    """PDF 依赖装好了没。没装就让调用方退回"只报元信息"，而不是报错。"""
    try:
        import pymupdf  # noqa: F401
    except Exception:  # noqa: BLE001
        return False, "没装 pymupdf"
    return True, ""


def _ocr_image_bytes(png: bytes) -> str:
    """对一张 PNG 做 OCR（阻塞，调用方放线程里）。"""
    import io

    from rapidocr_onnxruntime import RapidOCR

    from . import render  # 复用 render 里缓存的 OCR 引擎实例
    global_engine = getattr(render, "_ocr", None)
    engine = global_engine
    if engine is None:
        engine = RapidOCR()
        render._ocr = engine  # noqa: SLF001 - 同包内共享，省一次模型加载

    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        arr = np.array(im.convert("RGB"))
    result, _ = engine(arr)
    if not result:
        return ""
    return "\n".join(str(item[1]).strip() for item in result if len(item) > 1 and item[1])


def extract(data: bytes, max_chars: int, *, allow_ocr: bool = True) -> dict[str, Any]:
    """读一份 PDF。返回 `{text, pages, method, error, truncated}`。

    **不抛异常**：PDF 损坏、加密、字体乱码都只体现为 `error`，
    由调用方决定怎么跟用户说 —— 读文件失败不该让整条回复挂掉。
    """
    ok, why = available()
    if not ok:
        return {"text": "", "pages": 0, "method": "none", "error": why, "truncated": False}
    if not data:
        return {"text": "", "pages": 0, "method": "none", "error": "空文件", "truncated": False}

    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        logger.info("PDF 打不开：%s", type(exc).__name__)
        return {"text": "", "pages": 0, "method": "none",
                "error": f"打不开（{type(exc).__name__}）", "truncated": False}

    try:
        if doc.is_encrypted and not doc.authenticate(""):
            return {"text": "", "pages": doc.page_count, "method": "none",
                    "error": "PDF 有密码，读不了", "truncated": False}

        total = doc.page_count
        # ---- ① 文本层 ----
        parts: list[str] = []
        for i in range(min(total, _MAX_PAGES)):
            try:
                got = doc[i].get_text() or ""
            except Exception:  # noqa: BLE001 - 单页坏了就跳过，不毁整份
                got = ""
            if got.strip():
                parts.append(f"[第 {i + 1} 页]\n{got.strip()}")
        text = "\n\n".join(parts).strip()

        if len(text) >= _OCR_MIN_CHARS:
            truncated = len(text) > max_chars
            logger.info("PDF 文本层抽到 %d 字（%d 页）", len(text), total)
            return {"text": text[:max_chars], "pages": total, "method": "text",
                    "error": "", "truncated": truncated}

        # ---- ② 文本太少 → 多半是扫描版，渲染 + OCR ----
        if not allow_ocr:
            # 少于 _OCR_MIN_CHARS 就是"没抽到正文"，哪怕抽到了几个字也不能装作读到了：
            # 扫描件常带页眉/水印（如「附件1-1」），那点字会被 get_text() 抽出来，
            # 若只判 `if text` 就会把"仅页眉"当成正文返回 —— 用户以为读到了，其实正文全丢。
            # 所以只要有 OCR 就可能被回落，这里如实说明"只有 N 字"。
            return {"text": text[:max_chars], "pages": total, "method": "text",
                    "error": (f"文本层只有 {len(text)} 字，少于 {_OCR_MIN_CHARS} 字，"
                              "多半是扫描版、正文没抽出来") if text else "没有文本层（扫描版？）",
                    "truncated": False}

        logger.info("PDF 文本层只有 %d 字，回落到渲染 + OCR", len(text))
        ocr_parts: list[str] = []
        for i in range(min(total, _MAX_OCR_PAGES)):
            try:
                pix = doc[i].get_pixmap(
                    matrix=pymupdf.Matrix(_OCR_DPI_SCALE, _OCR_DPI_SCALE))
                page_text = _ocr_image_bytes(pix.tobytes("png"))
            except Exception as exc:  # noqa: BLE001
                logger.info("第 %d 页 OCR 失败：%s", i + 1, type(exc).__name__)
                continue
            if page_text.strip():
                ocr_parts.append(f"[第 {i + 1} 页（OCR）]\n{page_text.strip()}")

        ocr_text = "\n\n".join(ocr_parts).strip()
        if not ocr_text:
            return {"text": text[:max_chars], "pages": total, "method": "none",
                    "error": "扫描版且 OCR 也没认出字", "truncated": False}

        note = ""
        if total > _MAX_OCR_PAGES:
            note = f"（只 OCR 了前 {_MAX_OCR_PAGES} 页，共 {total} 页）"
        truncated = len(ocr_text) > max_chars
        logger.info("PDF OCR 得到 %d 字（前 %d 页）", len(ocr_text), min(total, _MAX_OCR_PAGES))
        return {"text": ocr_text[:max_chars] + ("\n" + note if note else ""),
                "pages": total, "method": "ocr", "error": "", "truncated": truncated}
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
