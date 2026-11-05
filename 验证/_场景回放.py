"""场景回放：用固定样本集验证 `scene.judge()`，不连网、不装依赖。

用法：

    python 验证\\_场景回放.py           # 跑默认回放集
    python 验证\\_场景回放.py -v        # 连通过的样本也打印

退出码 0 = 全部命中；1 = 有未命中。

## 为什么要它

改判类规则时最容易犯的错，是**为了修一个样本把另一批弄坏**。
没有固定回放集，每次调整都只能靠感觉，而判类错误的代价是召回完全无关的语料
（「今天加班累得想睡觉」被当成亲密 → 回一句撒娇）。

## 标注原则

* `expect` 允许**多个可接受答案**：真实语言本来就有歧义；
* 判不准的样本标 `unknown`，用来验证"宁可不判"这条出口真的生效；
* `strict_target: true` 只给**冲突对**（「你这个废物」vs「我老板骂我废物」）——
  指向判定是线索级的，逐条比对没有意义，但这两条错了就是性质错误；
* 标注先于实现冻结：**规则改不动就改规则，不要为了让测试变绿去改标注**。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError):
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
SET = ROOT / "验证" / "场景回放集.jsonl"

_spec = importlib.util.spec_from_file_location(
    "ai_chat_scene", ROOT / "plugins" / "ai_chat" / "scene.py")
scene = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(scene)  # type: ignore[union-attr]


def load(path: pathlib.Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def main() -> int:
    verbose = "-v" in sys.argv
    rows = load(SET)
    failed: list[tuple[str, str, str, str]] = []
    act_ok = target_ok = conf_ok = strict_bad = 0

    for row in rows:
        # `play` 是这条样本假设的关系值（玩闹许可）；软辱骂的归类依赖它
        got = scene.judge(row["text"], play=float(row.get("play") or 0.0))
        act = str(got.get("act"))
        target = str(got.get("target"))
        conf = str(got.get("confidence"))
        hit_act = act in (row.get("expect") or [])
        hit_target = target == row.get("target")
        hit_conf = conf == row.get("confidence")
        act_ok += hit_act
        target_ok += hit_target
        conf_ok += hit_conf
        # 硬失败只有两类：act 不对；或者**冲突对**的指向不对
        if not hit_act:
            failed.append((row["id"], row["text"], act, "、".join(row.get("expect") or [])))
        elif row.get("strict_target") and not hit_target:
            strict_bad += 1
            failed.append((row["id"], row["text"], f"{act}/{target}",
                           f"{'、'.join(row.get('expect') or [])}/{row.get('target')}"))
        if verbose and hit_act:
            print(f"  [OK] {row['id']} {act}/{target}/{conf} <- {row['text'][:34]}")
        elif not hit_act:
            print(f"  [错] {row['id']} 判成 {act}，期望 {'、'.join(row.get('expect') or [])}"
                  f"｜{row['text'][:40]}")

    total = len(rows)
    print(f"\n回放集 {total} 条")
    print(f"  act 命中    {act_ok}/{total}  ({act_ok * 100 // max(1, total)}%)  ← 硬指标")
    print(f"  target 命中 {target_ok}/{total}  （参考；冲突对必须对，共 {strict_bad} 条失守）")
    print(f"  把握度命中  {conf_ok}/{total}  （参考）")
    if failed:
        print("\n未命中明细：")
        for rid, text, got, want in failed:
            print(f"  {rid}  判成「{got}」 期望「{want}」  {text[:44]}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
