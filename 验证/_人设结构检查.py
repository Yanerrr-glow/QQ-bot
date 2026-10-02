"""人设结构检查：把「关于同一个特质的知识散落在几处」变成可数的报告。

## 为什么需要它

改造前，一个特质的知识散落在人设文件、正则守卫、硬编码兜底文案、模块自带提示词里，
彼此不知道对方存在。后果是实测抓到的三处直接冲突：

| 位置 | 文案 | 违反 |
|---|---|---|
| `config.py` `MSG_NO_KEY` | 「我还没拿到 API Key，得先去 .env 里填一下。」 | 铁律「不主动谈论你自己」（参数） |
| `greetings.py` 晚安兜底 | 「…早点睡~」 | 铁律把「早点睡」逐字列为禁词 |
| `config.py` `MSG_EMPTY` | 「换个说法问？」 | 铁律「不作话头抛回者」（边界） |

而 `config.py` 里那句注释「注意别在这里写括号动作」说明项目早就知道这类问题 ——
只是用的是**注释提醒**，不是结构保证。

`persona/packs/<包>/traits.json` 把「所有表达该特质的通道」登记到特质名下，本脚本据此逐条核对。

## 三处冲突的结局（2026-09-26 已判定）

那三处"人设打自己脸"最初的处理是**两侧同时注释掉、待人工判**。现在都判完了，
**三条铁律全部恢复并生效**，冲突按各自的性质消解：

| 冲突 | 判定 | 做法 |
|---|---|---|
| `MSG_EMPTY` ↔ `不作话头抛回者` | 空回复是**故障**，不该由人设接管 | `MSG_EMPTY` 删除，改为记 error 且不发言 |
| `MSG_NO_KEY` ↔ `不主动谈论你自己` | 固定系统提示，**不属人格语域** | 登记进 `fixed_notice_channels` |
| `greetings.py` 晚安兜底 ↔ `不提时间，也不提睡眠` | 固定时间发的固定文本，**另一语域** | 登记进 `fixed_notice_channels` |

所以本脚本维护**三张清单**，它们互不判违规：

1. `persona/packs/<包>/traits.json` 的 `traits[].channels` —— 表达某个特质的通道（人格语域）；
2. `persona/packs/<包>/traits.json` 的 `fixed_notice_channels` —— 代码写死的固定文案（**非**人格语域，
   铁律管的是"模型自己怎么说"，管不到这里）；
3. `resolved_conflicts` —— 已经判完的历史冲突，只作考证。

`parked` 机制仍然保留（铁律被 `#` 注释时用它声明停用，闸门关键词会自动跟着停），
但**当前无人使用**。

## 判据

**失败（退出码 1）**：
* 通道的 `match` 在目标文件里找不到（引用的规则被删/改写了）；
* **孤儿铁律** —— `persona/packs/<包>/forbidden.txt` 里有条目没被任何特质认领
  （不可测量 = 不可验证，接不上「可诱发性准入」那条纪律）；
* **生效特质没有闸门关键词** —— 那样的特质对自动迭代是完全敞开的。

**警告（默认不影响退出码，加 `--strict` 才失败）**：
* 活跃的跨通道矛盾。

## 用法

```powershell
python '验证\\_人设结构检查.py'            # 报告 + 结构错误判定
python '验证\\_人设结构检查.py' --strict   # 警告也算失败（修完之后用来钉住）
python '验证\\_人设结构检查.py' --quiet    # 只打印警告/失败与汇总
```

纯逻辑、不联网、不调模型、不写盘。可用系统 python 直接跑（无第三方依赖）。
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
PKG = os.path.join(PROJ, "plugins", "ai_chat")
PACKS_DIR = os.path.join(PROJ, "persona", "packs")

# 自动迭代的**落点节**前缀：`apply_candidate` 是纯追加、不识别小节，
# 所以"文件末尾那个【…】小节"决定了新条目落在哪个语义下面。
LANDING_HEAD = "【自动学到的"

# 通道 kind → 包内文件名。**与 `plugins/ai_chat/packs.py` 的 `PACK_FILES` 一致**
# （本脚本刻意不 import 插件，所以这张表是两份；`离线验证_桩.py` 有比对断言防漂移）。
LAYER_FILES = {
    "base": "base.txt",
    "forbidden": "forbidden.txt",
    "surface": "surface.txt",
}

# 当前检查的包（`--pack` 指定，或按激活顺序推出来）。
PACK = ""
TRAITS_FILE = ""

FAILED: list[str] = []
WARNED: list[str] = []
STATS: dict[str, int] = {}


def active_pack_id() -> str:
    """当前激活的包：运行时标记 > 注册表 > 唯一一个启用的包。

    与 `plugins/ai_chat/packs.py` 的 `_resolve_active()` **同序**（这里复算一遍，
    理由同其它复算：本脚本要能脱依赖直跑）。
    """
    marker = os.path.join(PROJ, "data", "runtime", "persona", "_active")
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            got = fh.read().strip()
        if got and os.path.isdir(os.path.join(PACKS_DIR, got)):
            return got
    except OSError:
        pass
    try:
        with open(os.path.join(PROJ, "persona", "_registry.json"), "r", encoding="utf-8") as fh:
            got = str((json.load(fh) or {}).get("active") or "").strip()
        if got and os.path.isdir(os.path.join(PACKS_DIR, got)):
            return got
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        pass
    names = list_packs()
    return names[0] if len(names) == 1 else ""


def list_packs() -> list[str]:
    """可用的包 id（按名字排序，跳过 `_`/`.` 开头的脚手架与临时目录）。"""
    try:
        return sorted(
            name for name in os.listdir(PACKS_DIR)
            if not name.startswith(("_", ".")) and os.path.isdir(os.path.join(PACKS_DIR, name))
        )
    except OSError:
        return []


def pack_layer_path(kind: str) -> str:
    """当前包里某一层的路径。"""
    return os.path.join(PACKS_DIR, PACK, LAYER_FILES[kind])


def check(name: str, ok: bool, detail: str = "", *, warn_only: bool = False) -> bool:
    if ok:
        return True
    # 名字带上包名：一个仓库里可能有多个包，汇总里两条同名失败会看不出是哪个包。
    label = ("[%s] " % PACK) + name if PACK else name
    if warn_only:
        WARNED.append(label)
        print("  [WARN] " + name + ((" —— " + str(detail)) if detail else ""))
    else:
        FAILED.append(label)
        print("  [FAIL] " + name + ((" —— " + str(detail)) if detail else ""))
    return False


def read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _settings_int(key: str, fallback: int) -> int:
    """读一个整数参数的有效值：先看 `data/runtime/settings.json`（控制台改的），再回落到
    `settings.py` 里 `Spec(...)` 的默认值。

    为什么不用 import：本脚本刻意只依赖标准库（见文件头"不联网、不调模型、系统 python 直跑"），
    而 `settings.py` 顶部要 `from nonebot import get_driver` —— 一 import 就跑不起来了。
    按文本取默认值是糙一点，但 `_语法检查.py` 里已有同类先例（它也按文本核对 Dockerfile）。
    """
    try:
        with open(os.path.join(PROJ, "data", "runtime", "settings.json"), "r", encoding="utf-8") as fh:
            runtime = json.load(fh)
        if key in runtime:
            return int(runtime[key])
    except (OSError, ValueError, TypeError):
        pass
    src = read(os.path.join(PKG, "settings.py"))
    m = re.search(r'Spec\(\s*"%s"[^)]*?,\s*(\d+)\s*,' % re.escape(key), src)
    return int(m.group(1)) if m else fallback


def strip_comments(text: str) -> str:
    """去掉整行注释 —— 与 `config.strip_comments()` 同一口径。

    这里有意**复算一遍**而不是 import 插件：本脚本要能在不装依赖的机器上直跑
    （项目里 `_行为闸门验证.py` / `离线验证_桩.py` 也是这个路数）。
    """
    return "\n".join(
        ln for ln in str(text or "").splitlines() if not ln.lstrip().startswith("#")
    )


def forbidden_items(text: str) -> list[str]:
    """把禁止事项拆成条目。**与 `persona.forbidden_items()` 逐字同口径**。

    这里有意**复算一遍**而不是 import 插件（见 `strip_comments` 的说明），
    代价是两份实现会漂移 —— 2026-09-27 就漂了一次：本脚本要求 `- ` / `·` 在
    **strip 后**的行首，而运行时用 `[-*·•]` 判定，于是缩进的加粗续行
    （`  **这一条是硬禁止，不是"一次就够"。**`）在运行时变成**多出来的第 21 条**，
    本脚本却仍数出 20 条 —— "巡检 0 错误"把运行时的幻影条目盖了过去。

    现在两边都只看**原始行首**：无缩进才算新条目，带缩进的一律接回上一条。
    `离线验证_桩.py` 有逐条比对断言，改动任一侧都会当场失败。
    """
    out: list[str] = []
    in_section = True
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("【"):
            in_section = "禁止" in ln
            continue
        if not in_section:
            continue
        if raw[:1] in "-*·•":
            # 与 `persona.forbidden_items()` 一致：只吃一个标记字符 + 其后的空白。
            out.append(ln[1:].lstrip())
        elif raw.startswith((" ", "\t")) and out:
            out[-1] = f"{out[-1]} {ln}"
    return [x for x in out if len(x) >= 6]


def check_one_pack(pack_id: str, *, quiet: bool) -> None:
    """检查一个包。**每个包独立判定**：一个包坏了不该让别的包看不出结论。"""
    global PACK, TRAITS_FILE
    PACK = pack_id
    TRAITS_FILE = os.path.join(PACKS_DIR, pack_id, "traits.json")
    print("\n" + "=" * 62)
    print("== 人格包 %s ==" % pack_id)
    print("=" * 62)

    if not os.path.exists(TRAITS_FILE):
        # 注册表是**可选**的（`persona.py` / `behavior.py` 都有内置回退，
        # 公开副本按设计就不带它）。所以这里只报"查不了"，不当结构错误 ——
        # 真正"缺它就不能用"的判断在 `_人格包检查.py --strict` 里。
        print("  找不到 %s：%s" % (os.path.join("persona", "packs", pack_id, "traits.json"), TRAITS_FILE))
        print("  → 跳过本包的通道一致性 / 闸门派生检查（闸门会回退内置默认表）")
        return
    reg = json.loads(read(TRAITS_FILE))
    traits = reg.get("traits") or []

    raw_layer = {k: read(pack_layer_path(k)) for k in LAYER_FILES}
    active_layer = {k: strip_comments(v) for k, v in raw_layer.items()}
    code_raw: dict[str, str] = {}
    code_active: dict[str, str] = {}
    persona_py = read(os.path.join(PKG, "persona.py"))

    STATS["特质数"] = len(traits)
    STATS["通道数"] = sum(len(t.get("channels") or []) for t in traits)

    def code_text(fn: str) -> tuple[str, str]:
        if fn not in code_raw:
            txt = read(os.path.join(PKG, fn))
            code_raw[fn] = txt
            code_active[fn] = strip_comments(txt)
        return code_raw[fn], code_active[fn]

    # ---------------------------------------------------------------- 1 通道一致性
    if not quiet:
        print("-- 1. 通道一致性（每条通道的 match 能否在目标文件里找到）--")
    missing = 0
    for t in traits:
        slug = t.get("slug", "?")
        for ch in t.get("channels") or []:
            kind = ch.get("kind")
            match = ch.get("match") or ""
            if not match:
                check("通道缺 match：%s" % slug, False)
                missing += 1
                continue
            if kind in LAYER_FILES:
                hay, where = raw_layer.get(kind, ""), LAYER_FILES[kind]
            elif kind == "code":
                fn = ch.get("file") or ""
                hay, where = code_text(fn)[0], "plugins/ai_chat/" + fn
            else:
                check("未知通道 kind=%r（%s）" % (kind, slug), False)
                missing += 1
                continue
            if match not in hay:
                check("通道指不到：%s → %s 里的 %r" % (slug, where, match[:40]), False)
                missing += 1
    if not missing and not quiet:
        print("  [OK] %d 条通道全部指得到" % STATS["通道数"])

    # ---------------------------------------------------------------- 2 孤儿铁律
    if not quiet:
        print("-- 2. 孤儿铁律（禁止事项里没被任何特质认领的条目）--")
    items = forbidden_items(raw_layer.get("forbidden", ""))
    claimed = [
        ch.get("match") or ""
        for t in traits
        for ch in (t.get("channels") or [])
        if ch.get("kind") == "forbidden"
    ]
    orphans = [it for it in items if not any(m and m in it for m in claimed)]
    STATS["生效铁律条数"] = len(items)
    STATS["孤儿铁律"] = len(orphans)
    check(
        "禁止事项有 %d 条未被任何特质认领" % len(orphans),
        not orphans,
        "；".join(x[:28] for x in orphans[:4]),
    )
    if not orphans and not quiet:
        print("  [OK] %d 条生效铁律全部有归属" % len(items))

    # ---------------------------------------------------------------- 3 跨通道矛盾
    if not quiet:
        print("-- 3. 跨通道矛盾（活跃 / 已停用）--")
    active_hits, parked_hits = [], []
    for t in traits:
        slug = t.get("slug")
        parked = bool(t.get("parked"))
        for ch in t.get("channels") or []:
            if not ch.get("violates"):
                continue
            kind = ch.get("kind")
            match = ch.get("match") or ""
            fn = ch.get("file") or ""
            where = LAYER_FILES.get(kind, "plugins/ai_chat/" + fn)
            # **用去注释后的文本判"还活着吗"** —— 注释掉就等于停用。
            alive_txt = active_layer.get(kind, "") if kind in LAYER_FILES else code_text(fn)[1]
            (parked_hits if (parked and match not in alive_txt) else active_hits).append(
                (slug, where, match, ch.get("why") or "")
            )
    STATS["活跃矛盾"] = len(active_hits)
    STATS["已停用矛盾"] = len(parked_hits)
    for slug, where, match, why in parked_hits:
        print("  [已停用] %s ← %s：%r" % (slug, where, (match or "")[:36]))
    for slug, where, match, why in active_hits:
        WARNED.append("活跃跨通道矛盾：%s ← %s" % (slug, where))
        print("  [WARN] %s ← %s：%r" % (slug, where, (match or "")[:36]))
        if why:
            print("         %s" % why)
    if not active_hits and not parked_hits and not quiet:
        print("  [OK] 没有登记在案的矛盾")

    # ---------------------------------------------------------------- 3.5 固定系统文案
    # 它们**不属于人格语域**（模型没有即兴发挥的余地），所以不与铁律做冲突判定；
    # 但仍要核对"登记的那句话还在不在" —— 文案改了却忘了改登记，这份清单就会失真。
    if not quiet:
        print("-- 3.5 固定系统文案（不属人格语域，登记即受核对）--")
    fixed = (reg.get("fixed_notice_channels") or {}).get("items") or []
    bad_fixed = []
    for it in fixed:
        if not isinstance(it, dict):
            continue
        fn = str(it.get("file") or "")
        match = str(it.get("match") or "")
        hay = code_text(fn)[0] if fn else ""
        if not match or match not in hay:
            bad_fixed.append("%s：%r" % (fn or "?", match[:30]))
    STATS["固定文案"] = len(fixed)
    check("固定系统文案登记在册且都能在代码里找到",
          bool(fixed) and not bad_fixed, "；".join(bad_fixed))
    if fixed and not bad_fixed and not quiet:
        print("  [OK] %d 条固定文案核对通过（与铁律分属不同语域，不做冲突判定）" % len(fixed))

    # ---------------------------------------------------------------- 4 闸门派生
    # `persona.py` 的 _CONFLICTS / _NEGATION 现在由本表派生（_active_trait_gates()）。
    # 这里**独立复算同一套判据**，好把"哪条生效铁律的词进不了闸门"报出来。
    if not quiet:
        print("-- 4. 闸门派生（生效特质 → 关键词）--")
    active_forb = active_layer.get("forbidden", "")
    n_active, n_terms, no_terms = 0, 0, []
    for t in traits:
        if t.get("parked"):
            continue
        rules = [
            str(c.get("match") or "")
            for c in (t.get("channels") or [])
            if c.get("kind") == "forbidden"
        ]
        if rules and not any(r and r in active_forb for r in rules):
            continue  # 铁律被注释掉了 —— 闸门跟着停
        terms = [str(x) for x in (t.get("gate_terms") or []) if str(x).strip()]
        if not terms:
            no_terms.append(t.get("slug"))
            continue
        n_active += 1
        n_terms += len(terms)
    STATS["生效特质"] = n_active
    STATS["派生闸门词"] = n_terms
    print("  生效特质 %d 个 → 派生闸门关键词 %d 条" % (n_active, n_terms))
    check("生效特质都有闸门关键词", not no_terms, "；".join(str(x) for x in no_terms))
    parked = [t.get("slug") for t in traits if t.get("parked")]
    if parked:
        print("  已停用（parked）：%s" % "、".join(str(x) for x in parked))
    check(
        "persona.py 确实从注册表派生闸门",
        "load_traits" in persona_py and "_active_trait_gates" in persona_py,
    )

    # ---------------------------------------------------------------- 4.5 判据覆盖
    # 一个特质要能被"看见"，得先有判据。三类判据各自覆盖多少，这里摊开 ——
    # **没有判据的特质必须写明为什么**，否则报告读起来就成了"没报 = 没有"的假阴性。
    if not quiet:
        print("-- 4.5 判据覆盖（闸门词 / 输出特征 / 可数守卫）--")
    no_reason = [
        str(t.get("slug")) for t in traits
        if not (t.get("output_markers") or [])
        and not str(t.get("output_markers_note") or "").strip()
    ]
    with_out = [str(t.get("slug")) for t in traits if t.get("output_markers")]
    with_guard = [str(t.get("slug")) for t in traits if t.get("guards")]
    note_guard = [str(t.get("slug")) for t in traits if t.get("guards_note")]
    STATS["输出特征特质"] = len(with_out)
    STATS["可数守卫特质"] = len(with_guard)
    print("  输出特征（自示监控用）    : %d 个" % len(with_out))
    print("  可数守卫（behavior.py 用）: %s" % ("、".join(with_guard) or "无"))
    if note_guard:
        print("  有意不配守卫（已写原因）  : %s" % "、".join(note_guard))
    check("没有输出特征的特质都写了原因", not no_reason, "；".join(no_reason))

    # ---------------------------------------------------------------- 5 预算
    print("-- 5. 三层预算（含注释行）--")
    total = total_active = 0
    for kind, fn in LAYER_FILES.items():
        n, na = len(raw_layer.get(kind, "")), len(active_layer.get(kind, ""))
        total += n
        total_active += na
        STATS["%s_字符" % fn] = n
        print("  %-22s %5d 字符（去掉注释后 %d）" % (fn, n, na))
    STATS["三层合计"] = total
    STATS["三层合计_去注释"] = total_active
    print("  %-22s %5d 字符（去掉注释后 %d）" % ("合计", total, total_active))

    # 表层预算：运行时闸门在 `persona._budget_ok`（超限拒绝写入），这里是同一判据的巡检。
    # 为什么要有这一条：表层是唯一会**自己长大**的 prompt 段落，而这里以前只打印、不判失败，
    # 于是"长到爆"只能靠人去肉眼看数字（2026-09-28 补）。
    _surf_raw = len(raw_layer.get("surface", ""))
    _surf_items = sum(1 for ln in raw_layer.get("surface", "").splitlines()
                      if ln.strip().startswith("- "))
    _cap_chars = _settings_int("surface_max_chars", 1500)
    _cap_items = _settings_int("surface_max_items", 60)
    print("  表层条目数            %5d 条（上限 %s）" % (_surf_items, _cap_items or "不限"))
    if _cap_chars > 0:
        check("表层字数在上限内（%d ≤ %d）" % (_surf_raw, _cap_chars), _surf_raw <= _cap_chars,
              "超了自动迭代会拒绝写入（over_budget）—— 删几条过时的，或调大 surface_max_chars")
    if _cap_items > 0:
        check("表层条目数在上限内（%d ≤ %d）" % (_surf_items, _cap_items), _surf_items <= _cap_items,
              "超了自动迭代会拒绝写入（over_budget）")
    # 落点节：`apply_candidate` 是纯追加，末尾那个【…】小节决定条目落在哪
    _heads = [ln.strip() for ln in raw_layer.get("surface", "").splitlines()
              if ln.strip().startswith("【")]
    if _heads:
        check("表层末尾的小节是自动迭代的落点节", _heads[-1].startswith(LANDING_HEAD),
              "末尾是「%s」—— 条目会堆在语义不符的小节下面" % _heads[-1][:26])

    # ---------------------------------------------------------------- 汇总
    print()
    print("== 包 %s：结构错误 %d 项，警告 %d 项 ==" % (pack_id, len(FAILED), len(WARNED)))
    for f in FAILED:
        print("  失败: " + f)


def main() -> int:
    """`--pack <id>` 只查一个；否则查**所有可用的包**。

    **为什么要遍历所有包而不是只查当前那个**：包化之后"能切过去"的每个包
    都是一份会被加载的人格，只查当前激活的等于让别的包裸奔 ——
    而切换只需要一条指令，没有任何东西拦着你去用一个从没被检查过的包。
    """
    quiet = "--quiet" in sys.argv
    strict = "--strict" in sys.argv
    only = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--pack" and i + 1 < len(sys.argv):
            only = sys.argv[i + 1].strip()

    if not os.path.isdir(PACKS_DIR):
        print("没有 persona/packs/ 目录 —— 照 persona/_TEMPLATE/README.md 建一个包")
        return 1

    packs = [only] if only else list_packs()
    if not packs:
        print("persona/packs/ 下一个可用的包都没有")
        return 1
    if only and not os.path.isdir(os.path.join(PACKS_DIR, only)):
        print("找不到包目录：%s" % os.path.join(PACKS_DIR, only))
        return 1

    print("项目根：%s" % PROJ)
    print("当前激活：%s" % (active_pack_id() or "（无）"))
    print("要检查的包：%s" % "、".join(packs))

    for pack_id in packs:
        # 每个包一份独立的结论清单：`_pack_scope` 里把 check() 记到的名字都打上包名前缀，
        # 免得"两个包都失败"时汇总里出现两条一模一样的行，看不出是哪个包。
        global FAILED, WARNED, STATS
        check_one_pack(pack_id, quiet=quiet)
    return _summarize(strict=strict)


def _summarize(*, strict: bool) -> int:
    print()
    print("=" * 62)
    print("=== 全部包：结构错误 %d 项，警告 %d 项 ===" % (len(FAILED), len(WARNED)))
    for f in FAILED:
        print("  失败: " + f)
    for w in WARNED:
        print("  警告: " + w)
    if strict and WARNED:
        print("（--strict：警告按失败计）")
        return 1
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
