"""容器内 PDF 端到端验证：真文件 + 造的中文扫描版 + 加密件。

用法（容器里跑）：
    docker exec ai-chat-bot python /app/_工具链/PDF端到端验证.py <真实PDF路径>

为什么必须在容器里跑这套：本机（Windows）没装 rapidocr，
"扫描版 → 渲染 → OCR" 那条路只有容器里能真正走通。
"""

from __future__ import annotations

import importlib.util
import sys
import types

# 为什么要绕这么一圈，而不是直接 `from plugins.ai_chat import pdf`：
#   真包 `plugins/ai_chat/__init__.py` 会去 init 配置、连数据库、读 persona.txt ——
#   为了验一个纯函数把整个机器人拉起来，没必要，还容易因为环境变量缺失而失败。
#   这里手工把那个目录挂成同名包，只加载 pdf.py 自己；render 用空壳替代
#   （pdf.py 只在 OCR 分支里碰它）。行为与线上一致的部分是 pdf.py 本体，这已经够了。
_PKG_DIR = "/app/plugins/ai_chat"

_p = types.ModuleType("plugins")
_p.__path__ = ["/app/plugins"]
sys.modules.setdefault("plugins", _p)

_ac = types.ModuleType("plugins.ai_chat")
_ac.__path__ = [_PKG_DIR]
sys.modules["plugins.ai_chat"] = _ac
_p.ai_chat = _ac

_rd = types.ModuleType("plugins.ai_chat.render")
sys.modules["plugins.ai_chat.render"] = _rd
_ac.render = _rd

_spec = importlib.util.spec_from_file_location("plugins.ai_chat.pdf", f"{_PKG_DIR}/pdf.py")
pdf = importlib.util.module_from_spec(_spec)
sys.modules["plugins.ai_chat.pdf"] = pdf
_ac.pdf = pdf
_spec.loader.exec_module(pdf)

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


print("=== 0. 依赖 ===")
ok, why = pdf.available()
check("pymupdf 可用", ok, why)
if not ok:
    sys.exit(1)
import pymupdf  # noqa: E402
print(f"  pymupdf 版本：{pymupdf.__doc__ or ''}".strip())

try:
    from rapidocr_onnxruntime import RapidOCR  # noqa: F401
    has_ocr = True
    print("  rapidocr：有")
except Exception as exc:  # noqa: BLE001
    has_ocr = False
    print(f"  rapidocr：无（{type(exc).__name__}）—— OCR 用例会失败")


def make_text_pdf_cjk() -> bytes:
    """电子版：有中文文本层，且**超过 `_OCR_MIN_CHARS`**。

    字数必须超过阈值，否则走的是"文本太少 → 回落 OCR"那条路，
    测不到文本层路径（第一版就踩了这个坑：17 字的样本被合理地判成了扫描版）。
    """
    doc = pymupdf.open()
    page = doc.new_page()
    line = "实习证明 ABC-123 测试文本，本行用于凑够文本层字数。"
    for i in range(6):
        page.insert_text((50, 70 + i * 24), f"{line} 第{i + 1}行",
                         fontname="china-ss", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


def make_short_text_pdf() -> bytes:
    """文本层**故意太少**：应被判定为扫描版，回落 OCR。"""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 100), "页眉", fontname="china-ss", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def make_scanned_pdf_cjk() -> bytes:
    """扫描版：页面里只有一张位图，**没有文本层**。"""
    src = pymupdf.open()
    page = src.new_page(width=520, height=200)
    page.insert_text((50, 110), "扫描件 SCAN-7788", fontname="china-ss", fontsize=30)
    png = page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    src.close()

    doc = pymupdf.open()
    page = doc.new_page(width=520, height=200)
    page.insert_image(pymupdf.Rect(0, 0, 520, 200), stream=png)
    data = doc.tobytes()
    doc.close()
    return data


def make_encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 100), "secret", fontsize=12)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                       owner_pw="owner", user_pw="user-pw-123")
    doc.close()
    return data


# ------------------------------------------------------------------ 1. 电子版
print("\n=== 1. 电子版（中文文本层，字数超过阈值）===")
got = pdf.extract(make_text_pdf_cjk(), 5000)
check("走文本层，不走 OCR", got["method"] == "text", got["method"])
check("认出中文", "实习证明" in got["text"], repr(got["text"][:40]))
check("认出 ASCII 编号", "ABC-123" in got["text"], repr(got["text"][:60]))
check("文本层结果无 OCR 标注", "（OCR）" not in got["text"])

# 阈值边界：文本层太少 → 一律翻成"没抽到正文"（**这是第一版用例踩出来的真缺陷**）
#   扫描件常带页眉/水印，`get_text()` 能抽到几个字；早先只判 `if text`，
#   于是"仅页眉"被当成正文返回且不报错 —— 用户以为读到了，正文其实全丢。
got = pdf.extract(make_short_text_pdf(), 5000, allow_ocr=False)
check("文本层过少时如实说明「少于 N 字」而不假装读到",
      "少于" in got["error"], f"{got['method']} / {got['error']}")

got = pdf.extract(b"", 1000)
check("空字节给出「空文件」", "空文件" in got["error"], got["error"])

# ------------------------------------------------------------------ 2. 扫描版
print("\n=== 2. 扫描版（无文本层 → 回落 OCR）===")
data = make_scanned_pdf_cjk()
check("确实没有文本层（前置条件）",
      len((pymupdf.open(stream=data, filetype="pdf")[0].get_text() or "").strip()) == 0)
if has_ocr:
    got = pdf.extract(data, 5000)
    check("回落 OCR 且 method=ocr", got["method"] == "ocr", got["method"])
    check("OCR 认出中文「扫描件」", "扫描" in got["text"], repr(got["text"][:60]))
    check("OCR 认出编号 SCAN-7788", "SCAN" in got["text"].upper(), repr(got["text"][:60]))
    check("标注了页码与 OCR 来源", "[第 1 页（OCR）]" in got["text"], repr(got["text"][:30]))
else:
    check("OCR 回落", False, "容器里没有 rapidocr")

# ------------------------------------------------------------------ 3. 加密
print("\n=== 3. 加密 / 损坏 ===")
got = pdf.extract(make_encrypted_pdf(), 1000)
check("加密件不抛异常且给出人话原因",
      got["text"] == "" and "密码" in got["error"], f"{got['method']} / {got['error']}")

got = pdf.extract(b"%PDF-1.4 garbage", 1000)
check("损坏件不抛异常", got["text"] == "" and bool(got["error"]), got["error"])

# ------------------------------------------------------------------ 4. 真实文件
if len(sys.argv) > 1:
    print(f"\n=== 4. 真实文件：{sys.argv[1]} ===")
    with open(sys.argv[1], "rb") as f:
        real = f.read()
    print(f"  体积：{len(real) / 1024:.1f} KB")
    got = pdf.extract(real, 8000)
    check("读出内容", len(got["text"]) > 50, f"{len(got['text'])} 字符")
    check("没报错", got["error"] == "", got["error"])
    print(f"  方式={got['method']} 页数={got['pages']} 截断={got['truncated']}")
    print("  正文前 200 字：")
    print("  " + got["text"][:200].replace("\n", "\n  "))
else:
    print("\n（没给真实 PDF 路径，跳过第 4 组）")

print(f"\n=== 结果：通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
for n in FAILED:
    print("  失败：", n)
sys.exit(1 if FAILED else 0)
