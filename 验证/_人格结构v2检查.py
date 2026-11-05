"""检查人格包 v2 结构：矢量、示例、schema 与 base 约束。

用法：

    python 验证/_人格结构v2检查.py --pack whale_v2
    python 验证/_人格结构v2检查.py --all

退出码 0 = 通过；1 = 有错误。v1 包（`_pack.json` 没有 `schema: 2`）只提示"跳过"。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKS = os.path.join(ROOT, "persona", "packs")
TAG_PREFIX = ("axis:", "mood:", "social:", "defiance:", "energy:", "vulnerability:",
              "mode:", "scene:", "topic:", "audience:")
TOPICS = {"appearance", "romance", "sex", "skill", "age"}
# `scene:` 的合法值是**语料场景名**，不是 `scene.ACTS`（行为枚举）。
# 两套词汇表分开的理由（2026-11-03，#0003）：
#   * 现有库里 提问 / 吐槽 / 撒娇 / 自闭 / 追问 都是**风格**，不是 act；
#   * 而 act 要能召回到语料，必须经 `context._LEGACY_SCENE`（现名 `ACT_TO_SCENE`）映射过来。
# 上一版方案要求 `scene:` ∈ ACTS，会让现有库直接验收失败 —— 这条例行检查专门防这个。
SCENE_TAGS = {"提问", "吐槽", "撒娇", "自闭", "追问",          # 现有（风格）
              "分享", "玩笑", "亲密", "指令", "纠正",          # 与 act 同名
              "骂", "求助", "寒暄", "告别", "夸赞",            # 与 act 同名 / 本轮补齐
              "冷场"}                                          # 靠 scene_state.mode 召回，不是 act
# 注：`骂` 与 `夸赞` 目前在库里**一条语料都没有** —— 那是 #0003 的数据任务，
# 本检查只管「名字合法」，管不了「有没有内容」。别把两件事混成一条红灯。
# **必须与 `plugins/ai_chat/context.py` 的 `_LEGACY_SCENE` 逐项一致**
ACT_TO_SCENE = {"诉苦": "吐槽", "求助": "提问"}
STATS = {"errors": 0, "warnings": 0, "packs": 0}


def check(name: str, ok: bool, detail: str = "", *, warn: bool = False) -> bool:
    if ok:
        print("  [OK] " + name + (" —— " + detail if detail else ""))
        return True
    level = "WARN" if warn else "FAIL"
    if warn:
        STATS["warnings"] += 1
    else:
        STATS["errors"] += 1
    print(f"  [{level}] {name}" + (" —— " + detail if detail else ""))
    return False


def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return raw if isinstance(raw, dict) else {}


def check_pack(pack: str) -> None:
    directory = os.path.join(PACKS, pack)
    print(f"\n== {pack} ==")
    if not os.path.isdir(directory):
        check("包目录存在", False, directory)
        return
    manifest = read_json(os.path.join(directory, "_pack.json"))
    schema = int(manifest.get("schema") or 0)
    if schema < 2:
        print("  [SKIP] v1 包（_pack.json 没有 schema: 2）")
        return
    STATS["packs"] += 1

    base = open(os.path.join(directory, "base.txt"), encoding="utf-8").read()
    check("base.txt 不超过 700 字", len(base) <= 700, f"{len(base)} 字")
    for bad in ("【示例", "对方：", "你：", "回复长度", "语气词清单"):
        check(f"base.txt 不包含细节小节：{bad}", bad not in base)

    vec_path = os.path.join(directory, "vector.json")
    check("vector.json 存在", os.path.isfile(vec_path))
    if os.path.isfile(vec_path):
        vec = read_json(vec_path)
        axes = vec.get("axes") or []
        check("轴数量在 1~12 之间", 1 <= len(axes) <= 12, f"{len(axes)} 个")
        ids = [str(a.get("id") or "") for a in axes if isinstance(a, dict)]
        check("轴 id 唯一", len(ids) == len(set(ids)), "、".join(ids))
        for axis in axes:
            if not isinstance(axis, dict):
                check("轴是对象", False)
                continue
            aid = str(axis.get("id") or "?")
            for field in ("name", "low", "high", "baseline", "half_life_h", "max_step", "max_daily"):
                check(f"{aid} 有 {field}", field in axis, str(axis.get(field)))
            check(f"{aid} 的 scope 合法", str(axis.get("scope") or "global") in ("global", "both", "relation"),
                  str(axis.get("scope")))
            if str(axis.get("scope")) in ("both", "relation"):
                weight = float(axis.get("relation_weight", 0) or 0)
                check(f"{aid} 的 relation_weight 在 0~1", 0 <= weight <= 1, str(weight))
        relation = vec.get("relation") or {}
        check("relation.max_offset 在 0~1", 0 < float(relation.get("max_offset", 0.35)) <= 1)
        intimacy = vec.get("intimacy") or {}
        check("intimacy 声明了 physical_master_only", "physical_master_only" in intimacy)
        check("intimacy 声明了 physical_private_only", "physical_private_only" in intimacy)
        check("intimacy 的 other_ceiling 不超过 0.3",
              0 <= float(intimacy.get("other_ceiling", 0.2)) <= 0.3)
        mode_ids = [str(m.get("id") or "") for m in (vec.get("modes") or []) if isinstance(m, dict)]
        examples = _read_examples(directory)
        ex_ids = {str(x.get("id")) for x in examples}
        for mode in vec.get("modes") or []:
            if not isinstance(mode, dict):
                continue
            for ex in mode.get("examples") or []:
                check(f"模式 {mode.get('id')} 引用的示例存在：{ex}", str(ex) in ex_ids)

    examples = _read_examples(directory)
    check("examples.jsonl 至少 6 条", len(examples) >= 6, f"{len(examples)} 条")
    check("good 示例至少 6 条", sum(1 for x in examples if x.get("polarity") == "good") >= 6)
    check("bad 示例至少 3 条", sum(1 for x in examples if x.get("polarity") == "bad") >= 3)
    ids = [str(x.get("id") or "") for x in examples]
    check("示例 id 唯一", len(ids) == len(set(ids)))
    for item in examples:
        eid = str(item.get("id") or "?")
        check(f"{eid} 的 polarity 合法", item.get("polarity") in ("good", "bad"))
        tags = [str(t) for t in (item.get("tags") or [])]
        check(f"{eid} 的 tags 前缀合法", all(t.split(":", 1)[0] + ":" in TAG_PREFIX for t in tags),
              "、".join(tags))
        # `fit`：写错不会报错，只会让这条示例永远选不中（`_fit_ok` 直接判假）
        fit = item.get("fit")
        if isinstance(fit, dict):
            _axes = fit.get("axes")
            if isinstance(_axes, dict):
                for _name, _span in _axes.items():
                    ok = (isinstance(_span, (list, tuple)) and len(_span) == 2
                          and all(isinstance(x, (int, float)) for x in _span)
                          and float(_span[0]) <= float(_span[1])
                          and -1.0 <= float(_span[0]) and float(_span[1]) <= 1.0)
                    check(f"{eid} 的 fit.axes[{_name}] 是合法区间 [lo,hi]（都在 -1..1 之内）", ok, str(_span))
            _tags = fit.get("tags")
            if isinstance(_tags, list):
                check(f"{eid} 的 fit.tags 前缀合法",
                      all(str(x).split(":", 1)[0] + ":" in TAG_PREFIX for x in _tags),
                      "、".join(str(x) for x in _tags))
        scenes = [x.split(":", 1)[1] for x in tags if x.startswith("scene:")]
        check(f"{eid} 的 scene 值在 SCENE_TAGS 里（不是 ACTS）",
              all(x in SCENE_TAGS for x in scenes), "、".join(scenes) or "（无 scene 标签）")
        for tag in tags:
            if tag.startswith("topic:"):
                check(f"{eid} 的话题在内置集合里", tag.split(":", 1)[1] in TOPICS, tag)
        body = "".join(str(x) for x in (item.get("ctx") or []) + (item.get("say") or []))
        check(f"{eid} 不超过 160 字", len(body) <= 160, f"{len(body)} 字")
        if "topic:sex" in tags and item.get("polarity") == "good":
            check(f"{eid} 的性话题示例标了受众", any(t.startswith("audience:") for t in tags))


def _read_examples(directory: str) -> list[dict]:
    """读示例库。**一行一条与多行对象都认**（与 `persona_examples.parse_examples` 同口径：
    2026-11-04 起写盘用多行缩进，人可读；旧文件一行一条照样能读）。"""
    path = os.path.join(directory, "examples", "examples.jsonl")
    if not os.path.isfile(path):
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        text = "".join(line for line in fh if not line.lstrip().startswith("#"))
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        while idx < len(text) and text[idx] in " \t\r\n":
            idx += 1
        if idx >= len(text):
            break
        try:
            item, idx = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            nxt = text.find("\n", idx)
            if nxt < 0:
                break
            idx = nxt + 1
            continue
        if isinstance(item, dict) and item.get("id"):
                out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack", default="")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.all or not args.pack:
        packs = sorted(
            name for name in os.listdir(PACKS)
            if not name.startswith(("_", ".")) and os.path.isdir(os.path.join(PACKS, name))
        )
    else:
        packs = [args.pack]
    for pack in packs:
        check_pack(pack)
    print("\n" + "=" * 60)
    print(f"检查 {STATS['packs']} 个 v2 包：错误 {STATS['errors']} 项，警告 {STATS['warnings']} 项")
    return 0 if STATS["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
