"""人格包结构检查：**把"这个包能不能用"变成可数的报告**。

## 为什么需要它

包化（2026-10）之后，人格从"一份文件"变成了"一个目录一套"：

```
persona/packs/<id>/
├─ _pack.json        身份元数据（名字 / 别名 / 唤醒词）
├─ base.txt          底层人设（它是谁）—— 必需
├─ forbidden.txt     禁止事项（铁律）—— 必需
├─ surface.txt       表层模板 —— 必需
└─ traits.json       特质 / 通道 / 闸门关键词 —— 可选（缺了闸门回退内置表）
```

"插上就能用"的前提是**缺东西时当场说得出来**。否则表现是切过去之后
人格少了一层、闸门没有任何关键词、或者聊天记录里认不出自己说的话 ——
三种都是**不报错的静默故障**（见 `验证/_人设结构检查.py` 的同一条理由）。

## 判据

**失败（退出码 1）**：

* 目录 / `_pack.json` 缺、`id` 不合法；
* `base.txt` / `forbidden.txt` / `surface.txt` 缺，或 `base.txt` 是空的
  （空底层 = 冲突闸门没有判定依据 = 自动迭代会被直接拒跑）；
* **表层模板里没有落点节**（`【自动学到的`）：自动迭代是"纯追加"的，
  没有落点节它一条都写不进去（`persona._landing_ok` 会整条拒绝）。

**警告（默认不影响退出码，`--strict` 才失败）**：

* 缺 `traits.json`（闸门回退内置默认表）；
* `_pack.json` 的 `bot_name` 没出现在 `base.txt` 里 —— `chatlog._speaker()`
  用「角色名 + QQ 号」判定哪句是机器人自己说的，对不上它就认不出自己刚说过的话；
* 人格正文里出现**全路径**（`persona/packs/<id>/...`）：换包/搬迁后会指错，
  要引用同包文件只写文件名。

## 用法

```powershell
python '验证\\_人格包检查.py'                 # 全部启用的包
python '验证\\_人格包检查.py' --pack whale    # 只查一个（切换前的预检）
python '验证\\_人格包检查.py' --all           # 含 enabled=false 的
python '验证\\_人格包检查.py' --strict        # 警告也算失败
```

纯逻辑、不联网、不调模型、不写盘。可用系统 python 直接跑（无第三方依赖）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)

# 包内文件名表。**与 `plugins/ai_chat/packs.py` 的 `PACK_FILES` 必须一致** ——
# 本脚本刻意不 import 插件（要能在不装依赖的机器上直跑，同 `_人设结构检查.py`），
# 所以这张表是**两份**。`离线验证_桩.py` 里有一条断言逐条比对两者，防漂移。
PACK_FILES = {
    "base": "base.txt",
    "forbidden": "forbidden.txt",
    "surface": "surface.txt",
    "traits": "traits.json",
}
REQUIRED = ("base", "forbidden", "surface")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# 自动迭代的落点节前缀（与 `persona._LANDING_HEAD` 一致）
LANDING_HEAD = "【自动学到的"
# 正文里出现这种全路径就是坑：换包/搬迁之后它指错了地方
FULL_PATH_RE = re.compile(r"persona/(?:packs|active)/")

PACKS_DIR = os.path.join(PROJ, "persona", "packs")
REGISTRY = os.path.join(PROJ, "persona", "_registry.json")

FAILED: list[str] = []
WARNED: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = "", *, warn_only: bool = False) -> bool:
    global CHECKS
    CHECKS += 1
    if ok:
        print("  [OK] " + name + ((" —— " + str(detail)) if detail else ""))
        return True
    if warn_only:
        WARNED.append(name)
        print("  [WARN] " + name + ((" —— " + str(detail)) if detail else ""))
    else:
        FAILED.append(name)
        print("  [FAIL] " + name + ((" —— " + str(detail)) if detail else ""))
    return False


def read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _gate_terms_of(traits_path: str) -> list[str]:
    """本包注册表里的闸门关键词（生效特质那些）。

    没有注册表时回退到**内置兜底表**——它与 `persona._LEGACY_CONFLICTS` 同一批词，
    刻意复算一遍（本脚本要能脱依赖直跑）。**只有 10 个词**：这正说明"想管住闸门
    必须自己把 `gate_terms` 写全"，规范 §4.5 里有说明。
    """
    reg = read_json(traits_path)
    terms: list[str] = []
    if isinstance(reg, dict) and isinstance(reg.get("traits"), list):
        for trait in reg["traits"]:
            if not isinstance(trait, dict) or trait.get("parked"):
                continue
            for word in trait.get("gate_terms") or []:
                word = str(word).strip()
                if word and word not in terms:
                    terms.append(word)
        if terms:
            return terms
    return ["客服腔", "敬语", "书面连接词", "自我指认", "作为一个AI", "作为一个 AI",
            "括号动作", "心理描写", "汇报图片", "图片处置"]


def _section_body(text: str, keyword: str) -> str:
    """取出 `【…keyword…】` 那一节的正文（到下一个 `【` 标题为止）。

    为什么要按节取：`_pack.json` 的角色名"出现在文件里任何地方"是不够的 ——
    模型是靠【它是谁】那一句确认自己是谁的；名字写在示例里、没写在自我介绍里，
    归属判定照样靠不住。取不到那一节时返回空串，由调用方决定算不算问题。
    """
    out: list[str] = []
    inside = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("【"):
            inside = keyword in stripped
            continue
        if inside:
            out.append(line)
    return "\n".join(out)


def _forbidden_items(text: str) -> list[str]:
    """把禁止事项拆成条目 —— **与 `persona.forbidden_items()` 逐字同口径**。

    刻意复算一遍而不是 import 插件：本脚本要能在不装依赖的机器上直跑
    （同 `验证/_人设结构检查.py`）。两份实现的一致性由桩回归的逐条比对钉住。

    口径要点：只认**行首无缩进**的 `- * · •`；带缩进的一律是上一条的续行；
    只会出现在「禁止事项」那一节里的条目才算（`【` 标题是分节锚点）。
    """
    items: list[str] = []
    in_section = True
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("【"):
            in_section = "禁止" in line
            continue
        if not in_section:
            continue
        if raw[:1] in "-*·•":
            items.append(line[1:].lstrip())
        elif raw.startswith((" ", "\t")) and items:
            items[-1] = f"{items[-1]} {line}"
    return [x for x in items if len(x) >= 6]


def active_pack_id() -> str:
    """当前激活的包：运行时标记 > 注册表 > 唯一一个启用的包（与 packs.py 同序）。"""
    marker = os.path.join(PROJ, "data", "runtime", "persona", "_active")
    try:
        with open(marker, "r", encoding="utf-8") as fh:
            got = fh.read().strip()
        if got and os.path.isdir(os.path.join(PACKS_DIR, got)):
            return got
    except OSError:
        pass
    reg = read_json(REGISTRY) or {}
    got = str(reg.get("active") or "").strip()
    if got and os.path.isdir(os.path.join(PACKS_DIR, got)):
        return got
    enabled = [name for name in list_packs(include_disabled=False)]
    return enabled[0] if len(enabled) == 1 else ""


def list_packs(*, include_disabled: bool) -> list[str]:
    """按名字排序列出包目录（跳过 `_` 与 `.` 开头 —— 脚手架与临时目录不算包）。"""
    if not os.path.isdir(PACKS_DIR):
        return []
    out: list[str] = []
    for name in sorted(os.listdir(PACKS_DIR)):
        if name.startswith("_") or name.startswith("."):
            continue
        if not os.path.isdir(os.path.join(PACKS_DIR, name)):
            continue
        if not include_disabled:
            raw = read_json(os.path.join(PACKS_DIR, name, "_pack.json")) or {}
            if raw.get("enabled") is False:
                continue
        out.append(name)
    return out


def check_pack(pack_id: str) -> None:
    """逐个包核对结构。**每个包都独立判**：一个包坏不该让别的包看不出结论。"""
    directory = os.path.join(PACKS_DIR, pack_id)
    print("\n== 包 %s ==" % pack_id)
    files = {role: os.path.join(directory, name) for role, name in PACK_FILES.items()}

    check("id 合法（ASCII 字母/数字/下划线/连字符，≤32 字符）", bool(_ID_RE.match(pack_id)), pack_id)
    manifest = read_json(files["traits"].replace("traits.json", "_pack.json"))
    if manifest is None:
        check("_pack.json 可读（合法 JSON 对象）", False, files["traits"].replace("traits.json", "_pack.json"))
        manifest = {}
    else:
        check("_pack.json 可读", True)
        mid = str(manifest.get("id") or "").strip()
        check("_pack.json 的 id（若写了）与目录名一致", (not mid) or mid == pack_id,
              "目录 %s / 声明 %s" % (pack_id, mid or "（未写）"), warn_only=True)
        check("_pack.json 写了 name", bool(str(manifest.get("name") or "").strip()),
              str(manifest.get("name") or ""), warn_only=True)

    for role in REQUIRED:
        check("有 %s" % PACK_FILES[role], os.path.isfile(files[role]), files[role])
    if not os.path.isfile(files["traits"]):
        check("有 %s（缺了闸门回退内置表，仍可用）" % PACK_FILES["traits"], False,
              files["traits"], warn_only=True)

    base = read(files["base"])
    check("base.txt 非空（空底层 = 闸门没有判定依据，自动迭代会拒跑）",
          bool(base.strip()), "%d 字符" % len(base))

    surface = read(files["surface"])
    check("surface.txt 里有自动迭代的落点节（%s）" % LANDING_HEAD,
          LANDING_HEAD in surface, "末尾 60 字：%r" % surface.strip()[-60:])
    check("落点节在**文件末尾**（追加就是落在那一节下面）",
          surface.strip().splitlines()[-1].strip().startswith("【") and LANDING_HEAD in surface.strip().splitlines()[-1],
          "最后一行：%r" % (surface.strip().splitlines()[-1] if surface.strip() else ""))

    # 身份一致性：角色名必须出现在底层人设里（chatlog 的归属判定靠它）
    bot_name = str(manifest.get("bot_name") or manifest.get("name") or "").strip()
    if bot_name:
        check("_pack.json 的角色名「%s」出现在 base.txt 里（聊天记录归属判定靠它）" % bot_name,
              bot_name in base, "", warn_only=True)
        # 更严的一层：**【它是谁】那一节里**必须出现它。散落在别处的名字救不了归属判定，
        # 而"哪一句自我介绍"正是模型用来确认"我是谁"的那句。
        who = _section_body(base, "它是谁")
        check("base.txt 的【它是谁】一节里写明了角色名「%s」" % bot_name,
              (not who) or bot_name in who,
              "【它是谁】一节：%r" % who.strip()[:60], warn_only=True)
    else:
        check("_pack.json 声明了 bot_name", False, "留空则切换人格时显示名不跟着换")

    # 机制型内容不该写进人设正文（见《人格包内容规范》§4）
    # 判据取窄：这些词出现在**职责说明**（该写进代码时机注入）里才报，不误伤正常聊天用词。
    mech_hits = [w for w in ("系统会","系统会附","代码里","由代码","prompt","提示词",
                             "聊天记录分","已读","未读","工具调用")
                 if w in base]
    check("base.txt 里没有机制说明（改由代码按时机注入，换角色不会丢）",
          not mech_hits, "命中：%s" % "、".join(mech_hits), warn_only=True)

    # ---- 禁止事项的格式（闸门**逐条**解析它们，格式错了就不生效）----
    items = _forbidden_items(_section_body(read(files["forbidden"]), "禁止事项"))
    check("forbidden.txt 的禁令能被逐条解析出来（闸门拿它做冲突判定）",
          bool(items), "%d 条" % len(items))
    # `forbidden_items()` 只看**行首无缩进**的 `- * · •`；**缩进的 `- ` 会被当"续行"
    # 接进上一条**（两个短句被拼成一条，闸门报错时指错地方）。
    # ⚠ 缩进的 `**加粗**` 是合法的续行写法（whale 里就有一条两行禁令），别误报。
    bad_bullet = [ln.strip() for ln in read(files["forbidden"]).splitlines()
                  if ln[:1] in (" ", "\t") and ln.lstrip()[:2] in ("- ", "*", "·", "•")]
    check("禁止事项里没有缩进的列表项（会被当成上一条的续行）",
          not bad_bullet, "；".join(x[:28] for x in bad_bullet[:3]), warn_only=True)
    # `##` / `###` 不是小节标题：它们会被当成正文接进上一条
    hash_heads = [ln.strip() for ln in read(files["forbidden"]).splitlines()
                  if ln.strip().startswith("##")]
    check("禁止事项里没有用 `##` 当标题（只有 `【】` 是标题）",
          not hash_heads, "；".join(x[:28] for x in hash_heads[:3]), warn_only=True)
    # 只有「禁止事项」那一节里的条目才被收：标题里必须真的带"禁止"两个字
    heads = [ln.strip() for ln in read(files["forbidden"]).splitlines()
             if ln.strip().startswith("【")]
    check("forbidden.txt 的每个小节标题都带「禁止」（否则那一节的条目不被收）",
          all("禁止" in h for h in heads) if heads else True,
          "；".join(h for h in heads if "禁止" not in h), warn_only=True)

    # ---- 表层的条目格式（预算与追加都按 `- ` 数）----
    surf_items = [ln.strip() for ln in surface.splitlines() if ln.strip().startswith("- ")]
    check("surface.txt 的条目都以 `- ` 开头（预算按它计数、迭代按纯追加落盘）",
          bool(surf_items), "%d 条" % len(surf_items), warn_only=True)
    # 预算与"条目"判定只认 `- ` 开头；`*`/`·`/`•` 写的条目不计数 → 静默漏算。
    # ⚠ 只认**它们后面跟空格**的写法：`**加粗**` 是正文强调，不是列表符号（别误报）。
    bad_surf = [ln.strip() for ln in surface.splitlines()
                if ln.strip()[:1] in "*·•" and ln.strip()[1:2] in (" ", "\t")]
    check("surface.txt 的条目都用 `- `（其它符号不被计数，预算会漏算）",
          not bad_surf, "；".join(x[:28] for x in bad_surf[:3]), warn_only=True)
    too_short_items = [x for x in surf_items if len(x) - 2 < 8]
    check("surface.txt 的条目都不短于 8 字（闸门会以 too_short 拒收）",
          not too_short_items, "；".join(x[:20] for x in too_short_items[:3]), warn_only=True)
    too_long_items = [x for x in surf_items if len(x) - 2 > 200]
    check("surface.txt 的条目都不长于 200 字（闸门会以 too_long 拒收）",
          not too_long_items, "；".join(x[:20] for x in too_long_items[:3]), warn_only=True)
    # 表面条目**不该**是"关于它自己是谁/怎么说话"的规定 —— 那些属于 base/forbidden。
    # 判据用现成的关键词表：命中就说明这条写错了层（闸门也会拒收）。
    dup_in_surf = [x for x in surf_items if any(w in x for w in ("客服腔", "自我指认", "括号动作"))]
    check("surface.txt 里没有把铁律换个说法再写一遍（层次混了，闸门也会拒收）",
          not dup_in_surf, "；".join(x[:28] for x in dup_in_surf[:2]), warn_only=True)

    # ---- 闸门词提示：只有**表层**怕踩它 ----
    # `validate_surface()` 命中闸门词就整条丢弃；而 base/forbidden 里出现这些词是**正常的**
    # （它们就是在规定这些），所以只对表层给提示，另外两层只报个数供核对。
    # 词表取自**本包**的 traits.json：没有注册表时用内置兜底表（只有 10 个词）。
    gate_terms = _gate_terms_of(files["traits"])
    if gate_terms:
        surf_hits = sorted({w for w in gate_terms for s in surf_items if w in s})
        check("surface.txt 没有踩到闸门词（踩了会被 validate_surface 整条拒收）",
              not surf_hits, "命中：%s" % "、".join(surf_hits), warn_only=True)
        base_hits = sorted({w for w in gate_terms if w in base})
        forb_body = read(files["forbidden"])
        forb_hits = sorted({w for w in gate_terms if w in forb_body})
        print("      （参考：闸门词在 base.txt 命中 %d 个、forbidden.txt 命中 %d 个 —— "
              "这两层出现这些词是正常的，它们就是在规定这些）"
              % (len(base_hits), len(forb_hits)))

    # 正文里不该出现全路径：换包/搬迁之后它指错地方，而那是写给模型看的优先级说明
    for role in ("base", "forbidden", "surface"):
        body = read(files[role])
        hit = FULL_PATH_RE.search(body)
        check("%s 里没有写成全路径的自我引用" % PACK_FILES[role], hit is None,
              ("命中 %r —— 引用同包文件只写文件名" % hit.group(0)) if hit else "",
              warn_only=True)

    # 注册表：结构与闸门关键词
    if os.path.isfile(files["traits"]):
        reg = read_json(files["traits"])
        if not isinstance(reg, dict) or not isinstance(reg.get("traits"), list):
            check("traits.json 结构正确（顶层对象 + traits 数组）", False, files["traits"])
        else:
            traits = [t for t in reg["traits"] if isinstance(t, dict)]
            check("traits.json 结构正确", True, "%d 个特质" % len(traits))
            active = [t for t in traits if str(t.get("status") or "active") == "active"]
            no_gate = [str(t.get("slug") or "?") for t in active
                       if not (t.get("gate_terms") or [])]
            check("生效特质都配了闸门关键词（否则自动迭代对它完全敞开）",
                  not no_gate, "、".join(no_gate))
            fixed = (reg.get("fixed_notice_channels") or {}).get("items") or []
            check("固定系统文案登记在册", len(fixed) >= 1, "%d 条" % len(fixed), warn_only=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="人格包结构检查")
    ap.add_argument("--pack", default="", help="只查这个包（切换前的预检）")
    ap.add_argument("--all", action="store_true", help="含 enabled=false 的包")
    ap.add_argument("--strict", action="store_true", help="警告也算失败")
    args = ap.parse_args()

    print("项目根：%s" % PROJ)
    print("包目录：%s" % PACKS_DIR)

    if not os.path.isdir(PACKS_DIR):
        print("\n[失败] 没有 persona/packs/ 目录 —— 照 persona/_TEMPLATE/README.md 建一个包")
        return 1

    packs = [args.pack] if args.pack else list_packs(include_disabled=args.all)
    if args.pack and not os.path.isdir(os.path.join(PACKS_DIR, args.pack)):
        print("\n[失败] 找不到包目录：%s" % os.path.join(PACKS_DIR, args.pack))
        return 1
    if not packs:
        print("\n[失败] persona/packs/ 下一个可用的包都没有")
        return 1

    reg = read_json(REGISTRY)
    active = active_pack_id()
    print("注册表：%s（active=%s）" % (REGISTRY, (reg or {}).get("active") or "（未写）"))
    print("当前激活：%s" % (active or "（无）"))
    print("要检查的包：%s" % "、".join(packs))
    check("当前激活的包在可用列表里（切换后没生效时先看这条）",
          (not active) or active in list_packs(include_disabled=True),
          "激活 %s / 可用 %s" % (active or "（无）", "、".join(list_packs(include_disabled=True))))

    for pack_id in packs:
        check_pack(pack_id)

    print("\n" + "=" * 60)
    print("共 %d 项检查：结构错误 %d 项，警告 %d 项" % (CHECKS, len(FAILED), len(WARNED)))
    for name in FAILED:
        print("  失败: " + name)
    for name in WARNED:
        print("  警告: " + name)
    if args.strict and WARNED:
        print("（--strict：警告按失败计）")
        return 1
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
