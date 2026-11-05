"""示例检索的离线验证：**只验「选中了哪几条」**，不连网、不读真实示例库。

用法：

    python 验证\\_示例检索验证.py

退出码 0 = 通过。

## 为什么必须单独一个脚本

`验证/_语境回放.py` 跑的是「规则层 → 深判 → 解释器」，**根本不执行示例检索**。
所以它的 `--gate` 命中率再高，也证明不了 `pick()` 有没有按边界过滤（#0005 审阅原话）。

这里用**内存里造的示例库**（monkeypatch `persona_examples._load`）+ 固定的入参，
断言**选中的 id 集合**。好处是：示例内容可控、结果可复算，不受线上示例库增删影响。

## 验的是哪几条

1. `banned` 里有词时，含该词的 `good` 示例**一条都不出现** —— 且**不看场景标签**
   （归到 `scene:亲密` 的称呼示例一样要挡，这是 #0005 审阅点出的漏洞）；
2. `banned` 为空时，选中集合与改动前**逐条一致**（回归保护）；
3. `bad` 示例不受 `banned` 影响（它本身就是"别这么说"的反例，挡掉反而丢信息）；
4. `reception` **不**作为硬过滤条件（它来自模型，不能当确定性保护）；
5. 边界链路：原文「别再叫我小笨蛋了」→ `boundary_of()` 给出 `("称呼", "小笨蛋")`。
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "ai_chat"

passed = 0
failed: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global passed
    if cond:
        passed += 1
        print(f"  [OK] {label}" + (f" —— {detail}" if detail else ""))
    else:
        failed.append(label)
        print(f"  [失败] {label}" + (f" —— {detail}" if detail else ""))


def _load_pkg(name: str, filename: str, pkg: dict[str, object]):
    """按文件加载插件模块；相对导入用注入的假包兜住（本脚本不跑机器人）。"""
    spec = importlib.util.spec_from_file_location(name, PLUGIN / filename)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# 让 `persona_examples` 的相对导入能解析：它只 import 两个兄弟模块
# （`from . import config, persona_relation`），这里造一个最小包壳 + 两个桩。
# **不加载真实的 config**：那个模块会拉 nonebot，离线跑不起来。
import types

_pkg = types.ModuleType("ai_chat_examples_host")
_pkg.__path__ = []  # type: ignore[attr-defined]
sys.modules["ai_chat_examples_host"] = _pkg


def _stub(name: str, **attrs: object) -> None:
    mod = types.ModuleType(f"ai_chat_examples_host.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[f"ai_chat_examples_host.{name}"] = mod
    setattr(_pkg, name, mod)


# ⚠ 落点必须是**临时目录**：`append()` 会真的写 `examples.jsonl`。
# 第一次写这个脚本时桩给了 `.`，结果把文件写进了项目根（发现后已清理）。
_TMP = pathlib.Path(__file__).resolve().parent.parent / "temp" / "_examples_selftest"
_TMP.mkdir(parents=True, exist_ok=True)
_stub("config",
      examples_file_path=lambda: _TMP / "examples.jsonl",
      persona_data_dir=lambda: _TMP)
_stub("persona_relation",
      play=lambda uid, is_master=False: 0.0,
      topics=lambda uid, is_master=False: [])
# `check_fit()` 会用 `persona_vector.effective()` 的键做轴名校验；
# 这个假包不给它，轴名校验就会被静默跳过（生产里是"拿不到轴表就跳过"的兜底）。
_stub("persona_vector",
      effective=lambda uid="", is_master=False: {
          "mood": 0.0, "social": 0.0, "defiance": 0.0,
          "energy": 0.0, "vulnerability": 0.0})

_pe_spec = importlib.util.spec_from_file_location(
    "ai_chat_examples_host.persona_examples", PLUGIN / "persona_examples.py")
pe = importlib.util.module_from_spec(_pe_spec)  # type: ignore[arg-type]
sys.modules["ai_chat_examples_host.persona_examples"] = pe
try:
    _pe_spec.loader.exec_module(pe)  # type: ignore[union-attr]
except Exception as exc:  # noqa: BLE001
    print(f"加载 persona_examples 失败：{type(exc).__name__}: {exc}")
    raise SystemExit(1)

_ss = _load_pkg("ai_chat_scene_state", "scene_state.py", {})

# ---------------------------------------------------------------- 内存示例库
EXAMPLES = [
    {"id": "ex-call-01", "polarity": "good", "tags": ["scene:亲密"], "ctx": ["他忘带伞了"],
     "say": ["小笨蛋，伞又忘拿了吧，我给你送过去"], "why": "亲昵调侃"},
    {"id": "ex-call-02", "polarity": "good", "tags": ["scene:亲密", "mode:撒娇"],
     "ctx": ["钥匙丢了"], "say": ["傻瓜，在你兜里呢"], "why": "亲昵"},
    {"id": "ex-comfort-01", "polarity": "good", "tags": ["scene:吐槽"], "ctx": ["又被老板骂了"],
     "say": ["那确实挺烦的"], "why": "接住情绪"},
    {"id": "ex-call-bad", "polarity": "bad", "tags": ["scene:亲密"], "ctx": ["他忘带伞"],
     "say": ["小笨蛋，这都能忘"], "why": "反例：带贬低的称呼"},
]

_ORIGINAL_LOAD = pe._load
pe._load = lambda: [dict(x) for x in EXAMPLES]   # type: ignore[assignment]

WANT = ["scene:亲密", "scene:吐槽"]


def _ids(picked: dict) -> list[str]:
    return [str(x.get("id")) for x in (picked.get("good") or [])]


def run() -> int:
    print("=== 1. 没有边界时：选中集合是基准 ===")
    base = pe.pick(tags=WANT, scenes=[], uid="", query="")
    base_ids = _ids(base)
    check("基准选中 2 条 good", len(base_ids) == 2, base_ids)
    check("基准包含称呼类示例（没边界时本来就该选）", "ex-call-01" in base_ids, base_ids)

    print("\n=== 2. banned 命中：含该词的 good 一条都不能出现 ===")
    hit = pe.pick(tags=WANT, scenes=[], uid="", query="", banned=["小笨蛋"])
    hit_ids = _ids(hit)
    check("「小笨蛋」示例被挡掉", "ex-call-01" not in hit_ids, hit_ids)
    check("不含该词的示例保留", "ex-comfort-01" in hit_ids, hit_ids)
    check("**归到 scene:亲密 也要挡**（不看场景标签）", "ex-call-01" not in hit_ids, hit_ids)
    check("bad 示例不受 banned 影响",
          any(str(x.get("id")) == "ex-call-bad" for x in (hit.get("bad") or [])),
          [str(x.get("id")) for x in (hit.get("bad") or [])])

    print("\n=== 3. banned 为空：结果与基准逐条一致（回归保护） ===")
    same = pe.pick(tags=WANT, scenes=[], uid="", query="", banned=[])
    check("空 banned == 基准", _ids(same) == base_ids, f"{_ids(same)} vs {base_ids}")

    print("\n=== 4. 边界识别链路 ===")
    kind, word = _ss.boundary_of("别再叫我小笨蛋了")
    check("「别再叫我小笨蛋了」→ (称呼, 小笨蛋)", (kind, word) == ("称呼", "小笨蛋"), f"{kind}/{word}")
    check("话题类边界不掉进称呼分支", _ss.boundary_of("别聊这个了") == ("话题", ""),
          _ss.boundary_of("别聊这个了"))
    check("普通句子不是边界", _ss.boundary_of("今天好累啊") == ("", ""),
          _ss.boundary_of("今天好累啊"))

    print("\n=== 5. reception 不作为硬过滤（它来自模型，不能当确定性保护） ===")
    import inspect

    sig = inspect.signature(pe.pick)
    check("pick 的形参里没有 reception", "reception" not in sig.parameters,
          "、".join(sig.parameters))
    check("pick 有 banned 形参", "banned" in sig.parameters, "、".join(sig.parameters))

    print("")
    print("=== 6. fit：数值轴区间 + 标签条件（#0004） ===")
    with_fit = [dict(x) for x in EXAMPLES]
    with_fit.append({"id": "ex-fit-01", "polarity": "good", "tags": ["scene:吐槽"],
                     "ctx": ["又被老板骂了"], "say": ["换个角度想也不算坏事"],
                     "fit": {"axes": {"mood": [-0.2, 0.5]}, "tags": ["mode:认真"]}})
    pe._load = lambda: [dict(x) for x in with_fit]  # type: ignore[assignment]

    no_axes = pe.pick(tags=WANT, scenes=[], uid="", query="", limit_good=10)
    check("轴值缺失 → 带 fit 的条目跳过",
          "ex-fit-01" not in _ids(no_axes), _ids(no_axes))

    inside = pe.pick(tags=WANT, scenes=[], uid="", query="", limit_good=10,
                     axes={"mood": 0.0, "social": 0.0, "defiance": 0.0,
                           "energy": 0.0, "vulnerability": 0.0},
                     cur_tags={"mode:认真"})
    check("轴值在区间内且标签满足 → 选中", "ex-fit-01" in _ids(inside), _ids(inside))

    outside = pe.pick(tags=WANT, scenes=[], uid="", query="", limit_good=10,
                      axes={"mood": 0.9, "social": 0.0, "defiance": 0.0,
                            "energy": 0.0, "vulnerability": 0.0},
                      cur_tags={"mode:认真"})
    check("轴值出区间 → 不选", "ex-fit-01" not in _ids(outside), _ids(outside))

    tagmiss = pe.pick(tags=WANT, scenes=[], uid="", query="", limit_good=10,
                      axes={"mood": 0.0}, cur_tags={"mode:撒娇"})
    check("标签条件不满足 → 不选", "ex-fit-01" not in _ids(tagmiss), _ids(tagmiss))

    plain = pe.pick(tags=WANT, scenes=[], uid="", query="")
    check("无 fit 的条目在缺轴值时照常（兼容锚点）",
          "ex-call-02" in _ids(plain) or "ex-comfort-01" in _ids(plain), _ids(plain))

    print("")
    print("=== 7. source：模型产出不许标 chat（#0004） ===")
    ok_manual = pe.append({"id": "ex-src-01", "polarity": "good", "tags": ["scene:吐槽"],
                           "ctx": ["x"], "say": ["y"], "source": {"source_type": "manual"}})
    check("manual 可写入", ok_manual.get("ok") is True, ok_manual)
    bad_chat = pe.append({"id": "ex-src-02", "polarity": "good", "tags": ["scene:吐槽"],
                          "ctx": ["x"], "say": ["z"], "source": {"source_type": "chat"}})
    check("chat 缺 message_id → 拒绝", bad_chat.get("ok") is False, bad_chat)
    bad_type = pe.append({"id": "ex-src-03", "polarity": "good", "tags": ["scene:吐槽"],
                          "ctx": ["x"], "say": ["w"], "source": {"source_type": "听说"}})
    check("非法 source_type → 拒绝", bad_type.get("ok") is False, bad_type)

    # `fit` 写错不会报错，只会让这条示例**永远选不中** —— 所以必须在写入时挡住
    _bad_fit1 = pe.append({"id": "ex-fit-bad-1", "polarity": "good", "tags": ["scene:吐槽"],
                           "ctx": ["x"], "say": ["a"],
                           "fit": {"axes": {"mood": [0.8, 0.2]}}})
    check("fit 区间反了（lo>hi）→ 拒绝", _bad_fit1.get("ok") is False, _bad_fit1)
    _bad_fit2 = pe.append({"id": "ex-fit-bad-2", "polarity": "good", "tags": ["scene:吐槽"],
                           "ctx": ["x"], "say": ["b"],
                           "fit": {"axes": {"心情": [0.0, 1.0]}}})
    check("fit 的轴名不在项目里 → 拒绝", _bad_fit2.get("ok") is False, _bad_fit2)
    _good_fit = pe.append({"id": "ex-fit-ok", "polarity": "good", "tags": ["scene:吐槽"],
                           "ctx": ["x"], "say": ["c"],
                           "fit": {"axes": {"mood": [0.0, 1.0]}, "tags": ["mode:认真"]}})
    check("合法 fit 可以写入", _good_fit.get("ok") is True, _good_fit)

    print("")
    print("=== 8. 真实示例库：补的语料召得回吗（#0003） ===")
    # 前面几节把 `config.examples_file_path` 指到了临时目录（为了让 append 不写脏活项目），
    # 这一节要读**真的**示例库，所以把桩重新指回包内文件。
    _REAL_EXAMPLES = (pathlib.Path(__file__).resolve().parent.parent
                      / "persona" / "packs" / "whale_v2" / "examples" / "examples.jsonl")
    sys.modules["ai_chat_examples_host"].config.examples_file_path = (  # type: ignore[attr-defined]
        lambda: _REAL_EXAMPLES)
    print("")
    print("=== 9. 同一句话 / 不同情绪 / 不同回法（#0004 第 2 步） ===")
    # 这一节要读**真实**示例库：前面几节把 `examples_file_path` 指到了临时目录。
    # 自己把桩指回去，不依赖第 8 节的执行顺序（第一次插在这里就是因为顺序踩了坑）。
    _REAL10 = (pathlib.Path(__file__).resolve().parent.parent
               / "persona" / "packs" / "whale_v2" / "examples" / "examples.jsonl")
    sys.modules["ai_chat_examples_host"].config.examples_file_path = (  # type: ignore[attr-defined]
        lambda: _REAL10)
    pe._load = _ORIGINAL_LOAD  # type: ignore[assignment]
    # 这一节是本次改动的**核心验收**：同一条触发（「小笨蛋」），
    # 心情好、心情差、精力低必须各自选出**不同的**那一支。
    import importlib.util as _ilu
    import sys as _sys

    _axes_cases = [
        ("心情好", {"mood": 0.6, "energy": 0.5}, "ex-call-a-01"),
        ("心情差", {"mood": -0.6, "energy": 0.5}, "ex-call-a-02"),
        ("精力低", {"mood": 0.0, "energy": -0.6}, "ex-call-a-03"),
    ]
    _picked_ids = []
    for label, axes, want in _axes_cases:
        got = pe.pick(tags=["scene:亲密"], scenes=[], uid="", query="小笨蛋",
                      axes=axes, limit_good=10)
        ids = _ids(got)
        _picked_ids.append(tuple(ids))
        check(f"{label} → 选中 {want}", ids == [want], ids or "（空）")
    check("三种情绪选中的是**三支不同的**语料", len(set(_picked_ids)) == 3, _picked_ids)

    _neutral = pe.pick(tags=["scene:亲密"], scenes=[], uid="", query="小笨蛋",
                       axes={"mood": 0.05, "energy": 0.05}, limit_good=10)
    check("情绪都不显著 → 三支都不套（宁可回落到别的语料，也不硬套）",
          not [x for x in _ids(_neutral) if x.startswith("ex-call-a-")], _ids(_neutral))

    _no_axes = pe.pick(tags=["scene:亲密"], scenes=[], uid="", query="小笨蛋", limit_good=10)
    check("轴值缺失 → 带 fit 的三支全部跳过",
          not [x for x in _ids(_no_axes) if x.startswith("ex-call-a-")], _ids(_no_axes))

    check("窗口内择近：中心加分 > 边缘加分",
          pe._fit_bonus({"fit": {"axes": {"mood": [0.0, 1.0]}}}, {"mood": 0.5})
          > pe._fit_bonus({"fit": {"axes": {"mood": [0.0, 1.0]}}}, {"mood": 0.95}),
          (pe._fit_bonus({"fit": {"axes": {"mood": [0.0, 1.0]}}}, {"mood": 0.5}),
           pe._fit_bonus({"fit": {"axes": {"mood": [0.0, 1.0]}}}, {"mood": 0.95})))
    check("没有 fit 的条目不加分", pe._fit_bonus({"id": "x"}, {"mood": 0.5}) == 0.0)

    pe._load = _ORIGINAL_LOAD  # type: ignore[assignment]
    _real = {str(x.get("id")): x for x in pe._load()}
    check("真实库已加载（≥30 条）", len(_real) >= 30, len(_real))
    check("新补的 求助/寒暄/骂/冷场/夸赞/告别 样本都在库里",
          {"ex-ask-01", "ex-greet-01", "ex-scold-01", "ex-awkward-01",
           "ex-praise-01", "ex-bye-01"} <= set(_real),
          sorted(k for k in _real
                 if k.startswith(("ex-ask", "ex-greet", "ex-scold", "ex-awkward",
                                  "ex-praise", "ex-bye"))))

    # **这一条才是「补样生效」的判据**：按场景召回，必须能选中对应语料。
    # `scene:` 的名字走 `ACT_TO_SCENE`：求助 → 提问、诉苦 → 吐槽；冷场直接按 mode 传。
    for scene, want in (("提问", "ex-ask"), ("寒暄", "ex-greet"),
                        ("骂", "ex-scold"), ("冷场", "ex-awkward"),
                        ("夸赞", "ex-praise"), ("告别", "ex-bye")):
        picked = pe.pick(tags=[], scenes=[scene], uid="", query="", limit_good=10)
        ids = _ids(picked)
        check(f"给 scene={scene} → 选中 {want} 的语料",
              any(str(x).startswith(want) for x in ids), ids or "（空）")

    # 旧条目没有 source 是历史事实；2026-11-04 起要求"每条都有 source"，
    # 缺失的按 #0004 回填成 `migrated`（来源已不可考，不许编 message_id）。
    _missing_src = [str(x.get("id")) for x in _real.values() if not x.get("source")]
    check("每条示例都有 source", not _missing_src, _missing_src[:5] or "齐全")
    _bad_src = [str(x.get("id")) for x in _real.values()
                if str((x.get("source") or {}).get("source_type")) == "chat"
                and not (x.get("source") or {}).get("message_id")]
    check("标成 chat 的必须带 message_id", not _bad_src, _bad_src[:5] or "没有")

    print("")
    print("=== 10. 不知道：不该作为场景召回 ===")
    _unsure = [_real[k] for k in ("ex-unsure-01", "ex-unsure-02") if k in _real]
    check("「不知道」样本存在（人工参考）", len(_unsure) == 2, len(_unsure))
    check("但它们**没有** scene 标签（不参与场景召回）",
          all(not any(str(x).startswith("scene:") for x in (row.get("tags") or []))
              for row in _unsure),
          [row.get("tags") for row in _unsure])

    pe._load = _ORIGINAL_LOAD  # type: ignore[assignment]
    print(f"\n{'=' * 50}")
    print(f"通过 {passed} 项" + (f"，失败 {len(failed)} 项：{failed}" if failed else "，全部通过"))
    print("=" * 50)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
