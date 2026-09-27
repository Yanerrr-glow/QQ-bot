"""验证 pdf.py：文本层提取 / 扫描版回落 OCR / 坏文件不抛异常。

跑法：python _工具链/_PDF读取验证.py   退出码 0 = 通过
需要 pymupdf；OCR 相关用例在没装 rapidocr 时自动跳过。
"""

from __future__ import annotations

import importlib.util
import sys
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


def skip(label: str, why: str) -> None:
    print(f"  [跳过] {label} —— {why}")


PKG = Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"
pkg = types.ModuleType("ai_chat")
pkg.__path__ = [str(PKG)]
sys.modules["ai_chat"] = pkg
# pdf.py 只在 OCR 分支里 import render，这里给个空壳避免 import 失败
render_stub = types.ModuleType("ai_chat.render")
sys.modules["ai_chat.render"] = render_stub
setattr(pkg, "render", render_stub)

spec = importlib.util.spec_from_file_location("ai_chat.pdf", PKG / "pdf.py")
pdf = importlib.util.module_from_spec(spec)
sys.modules["ai_chat.pdf"] = pdf
spec.loader.exec_module(pdf)

print(f"（已加载 pdf.py）")
ok, why = pdf.available()
print(f"pymupdf 可用：{ok} {why}\n")
if not ok:
    print("没装 pymupdf，无法继续")
    sys.exit(1)

has_ocr = importlib.util.find_spec("rapidocr_onnxruntime") is not None
import pymupdf  # noqa: E402

# --------------------------------------------------------------------- 造测试 PDF
CJK = "china-ss"


def make_text_pdf(pages: int = 2) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((60, 100), f"Page {i + 1}: The quick brown fox jumps over the lazy dog. " * 3,
                         fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def make_scanned_pdf() -> bytes:
    """扫描版：页面里**只有一张图片**、没有文本层。"""
    src = pymupdf.open()
    page = src.new_page(width=300, height=120)
    page.insert_text((40, 70), "SCANNED PAGE", fontsize=24)
    png = page.get_pixmap().tobytes("png")
    src.close()

    doc = pymupdf.open()
    page = doc.new_page(width=300, height=120)
    page.insert_image(pymupdf.Rect(0, 0, 300, 120), stream=png)
    data = doc.tobytes()
    doc.close()
    return data


# --------------------------------------------------------------------- 1. 文本层
print("=== 1. 文本版 PDF：抽文本层 ===")
got = pdf.extract(make_text_pdf(2), 5000)
check("识别为文本层（不走 OCR）", got["method"] == "text", got["method"])
check("页数正确", got["pages"] == 2, str(got["pages"]))
check("抽到正文内容", "quick brown fox" in got["text"], got["text"][:60])
check("带页码标记", "[第 1 页]" in got["text"] and "[第 2 页]" in got["text"])
check("无错误", got["error"] == "", got["error"])

got = pdf.extract(make_text_pdf(2), 100)
check("超长会截断并标记", got["truncated"] is True and len(got["text"]) <= 100,
      f"len={len(got['text'])} truncated={got['truncated']}")

# --------------------------------------------------------------------- 2. 扫描版
print("\n=== 2. 扫描版 PDF：没有文本层 → 回落 OCR ===")
got = pdf.extract(make_scanned_pdf(), 2000, allow_ocr=False)
check("关掉 OCR 时如实说没有文本层",
      got["method"] == "text" and "没有文本层" in got["error"], f"{got['method']} / {got['error']}")

if has_ocr:
    got = pdf.extract(make_scanned_pdf(), 2000, allow_ocr=True)
    check("回落 OCR 并认出文字", got["method"] == "ocr" and "SCANNED" in got["text"].upper(),
          f"{got['method']} / {got['text'][:60]!r}")
    check("OCR 结果带页码与标注", "[第 1 页（OCR）]" in got["text"], got["text"][:40])
else:
    skip("OCR 回落识别", "本机没装 rapidocr（容器里有，会在那边复核）")

# --------------------------------------------------------------------- 3. 坏文件
print("\n=== 3. 坏文件/异常输入：一律不抛 ===")
got = pdf.extract(b"not a pdf at all", 1000)
check("乱码字节返回错误而不抛", got["text"] == "" and bool(got["error"]), got["error"])

got = pdf.extract(b"", 1000)
check("空文件返回错误", got["text"] == "" and "空文件" in got["error"], got["error"])

# 截断成半份的 PDF
full = make_text_pdf(1)
got = pdf.extract(full[: len(full) // 2], 1000)
check("半截 PDF 不抛异常（要么读到、要么报错）", isinstance(got, dict) and "text" in got,
      f"method={got['method']} err={got['error'][:40]}")

print(f"\n=== 结果：通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
for n in FAILED:
    print("  失败：", n)
sys.exit(1 if FAILED else 0)
