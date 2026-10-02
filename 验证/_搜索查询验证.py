r"""验证搜索词构造：4 类误搜必须拦掉，该搜的必须搜得出干净关键词。

用法：python 验证\_搜索查询验证.py     退出码 0 = 通过

## 为什么要有这个脚本

2026-09-24 线上查出：bot 的**自动预取搜索 4 次全部是误搜**，而且搜索词里混进了
会话统计行 —— 实际搜出去的是这种：

    「只看前面的摘要，这篇论文的主要目标 要目标是什么？ （这个会话记录里共 731 条，已读 725 条，未读 6 条）」
    「那为什么不回应你的主人 不回应你的主人 （这个会话记录里共 740 条，已读 738 条，未读 2 条）」

那 4 句话是从真实聊天记录里抄的。所以这里的用例**不是编的**，是回归护栏：
`离线验证_桩.py` 里也有一组同样的断言，但那个文件跑起来要几十秒，
这个脚本秒级出结果，改 `search.py` 时可以随手跑。

## 它不测什么

不联网、不调模型 —— 只验 `needs_search()` / `query_from()` 这两个纯函数。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

# --------------------------------------------------------------------- 加载被测模块
# 只加载 search.py 本体，不去 init 整个插件包（那会连数据库、读 persona.txt）。
# 与 `_PDF读取验证.py` 同一套桩加载手法。
_HERE = pathlib.Path(__file__).resolve().parent
_PLUGINS = _HERE.parent / "plugins"
_PKG_DIR = _PLUGINS / "ai_chat"

_p = types.ModuleType("plugins")
_p.__path__ = [str(_PLUGINS)]
sys.modules.setdefault("plugins", _p)
_ac = types.ModuleType("plugins.ai_chat")
_ac.__path__ = [str(_PKG_DIR)]
sys.modules["plugins.ai_chat"] = _ac
_p.ai_chat = _ac
for _name in ("config", "settings", "search_memory"):
    _m = types.ModuleType(f"plugins.ai_chat.{_name}")
    sys.modules[f"plugins.ai_chat.{_name}"] = _m
    setattr(_ac, _name, _m)
sys.modules["plugins.ai_chat.settings"].get = lambda k, *a: {
    "search_max_results": 5, "search_timeout": 10,
}.get(k, a[0] if a else None)

_spec = importlib.util.spec_from_file_location("plugins.ai_chat.search", _PKG_DIR / "search.py")
search = importlib.util.module_from_spec(_spec)
sys.modules["plugins.ai_chat.search"] = search
_ac.search = search
_spec.loader.exec_module(search)

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


# --------------------------------------------------------------------- 1. 误搜必须拦掉
# 这 4 句是线上真实触发过搜索的原话（chatlog_g100000003.json）
REAL_FALSE_POSITIVES = (
    "那为什么不回应你的主人",
    "为什么不喜欢椰蓉的",
    "你觉得什么是可爱",
    "只看前面的摘要，这篇论文的主要目标是什么？",
)
print("=== 1. 线上真实误搜的 4 句：绝不能再触发联网 ===")
for text in REAL_FALSE_POSITIVES:
    should, why = search.needs_search(text)
    q = search.query_from(text)
    # 判定为 False，或判 True 但拧不出词（query 为空 → 上游会放弃搜索），两者都算"不会搜"
    wont_search = (not should) or (not q)
    check(f"不会去搜「{text}」", wont_search,
          f"判定={should}（{why or '不搜'}） 查询={q!r}")

# --------------------------------------------------------------------- 2. 查询词必须干净
print("\n=== 2. 搜索词里不能出现 prompt 的结构性文字 ===")
META = "（这个会话记录里共 751 条，已读 750 条，未读 1 条）"
FULL_PROMPT = f"【现在需要你回应的发言】\n张三：你觉得什么是可爱\n\n{META}"
for label, q in (
    ("整段 prompt 当上下文（旧故障形态）", search.query_from("你觉得什么是可爱", FULL_PROMPT)),
    ("整段 prompt + 该搜的问题", search.query_from("「鲸落」是什么意思", FULL_PROMPT)),
    ("聊天记录当上下文", search.query_from("「鲸落」是什么意思", "[22:24 张三] 随便聊聊")),
):
    bad = [w for w in ("会话记录", "已读", "未读", "回应的发言", "[22:24") if w in q]
    check(f"不含元数据/位置标记（{label}）", not bad, f"{q!r} 含 {bad}")

# --------------------------------------------------------------------- 3. 该搜的仍要搜
print("\n=== 3. 收紧误判不能把正常查询一起收掉 ===")
SHOULD_SEARCH = (
    ("「鲸落」是什么意思", "鲸落"),
    ("你听说过「海龟汤」吗", None),        # 引号词 + 疑问语气
    ("GPT-5 是什么东西", None),
    ("H100 是什么卡", "H100 是什么卡"),
    ("鲸落 是什么", "鲸落"),
    ("今天天气怎么样", None),
    ("iPhone 17 多少钱", None),
    ("朱雀三号 发射了吗", None),
)
for text, want_q in SHOULD_SEARCH:
    should, why = search.needs_search(text)
    q = search.query_from(text)
    check(f"判定该搜「{text}」", should and bool(q), f"判定={should} 查询={q!r}")
    if want_q is not None:
        check(f"关键词正确「{text}」", q == want_q, repr(q))

# --------------------------------------------------------------------- 4. 不该搜的闲聊
print("\n=== 4. 闲聊/情绪一律不搜 ===")
for text in ("哈哈哈哈哈", "晚安啦", "在吗", "今天好累啊", "帮我看看这段代码"):
    should, _ = search.needs_search(text)
    check(f"不搜「{text}」", should is False)

# --------------------------------------------------------------------- 5. 超长查询裁剪
print("\n=== 5. 模型/手动传进来的超长查询要裁到关键词 ===")
long_q = "那为什么不回应你的主人 （这个会话记录里共 740 条，已读 738 条）"
clipped = search._clip_query(long_q)  # noqa: SLF001 - 纯函数，专为可测而提取
check("裁到 30 字以内", len(clipped) <= 30, f"{len(clipped)} 字：{clipped!r}")
check("短查询不被改动", search._clip_query("鲸落 是什么") == "鲸落 是什么")  # noqa: SLF001
check("空查询不炸", search._clip_query("") == "")  # noqa: SLF001

print(f"\n=== 结果：通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
for name in FAILED:
    print("  失败：", name)
sys.exit(1 if FAILED else 0)
