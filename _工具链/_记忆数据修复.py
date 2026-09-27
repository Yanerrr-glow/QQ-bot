"""记忆数据修复：把线上已有的画像重复项按新规则合并、并把 facts 的主体迁到 uid。

**为什么需要它**：`set_profile` 原来用精确字符串比对去重（换个说法就新增一条），
线上已经攒出"同一件事 4 条"这种画像；`subject` 也被截到 24 字，同一个人分裂成
`小明` 与 `夜风の 旅人⭐` 两个身份。改代码只能防止**今后**变坏，
存量数据得单独过一遍。

纯标准库，不依赖 nonebot / openai。用法：

    python _记忆数据修复.py <memories.json> [输出.json] [chatlog目录或文件 ...]

不给输出路径就只打印报告（dry-run）。原始文件一律不动（会自动存 .bak-before-repair）。
给了 chatlog 就能把 facts 的 subject 从**名字**归一到**uid 对应的规范名** ——
实测 uid 100000001 在两个群分别叫「小明」和「夜风の 旅人⭐」，
不归一的话同一个人在记忆里是两个身份。
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

# 与 memory.py 保持一致的规则（刻意复制一份：这个脚本要能脱离插件单独跑）
_MAX_TEXT = 300
_MAX_NAME = 40
_MAX_PROFILE_ITEM = 80
_MAX_PROFILE_FIELD = 12
_SIMILAR = 0.25
_STOP = frozenset(
    "的 了 是 在 我 你 他 她 它 们 这 那 有 和 与 就 都 也 还 不 没 很 太 吧 吗 呢 啊 呀 哦 嘛 嗯 "
    "什么 怎么 为什么 可以 一下 一个 这个 那个 现在 今天 然后 因为 所以 但是 如果 已经 自己".split()
)
_TOPIC_WORDS: tuple[frozenset[str], ...] = (
    frozenset({"熬夜", "作息", "深夜", "凌晨", "晚睡", "通宵", "失眠", "几点睡", "睡觉", "零点半"}),
    frozenset({"宿舍", "住处", "房间", "租房", "搬家", "住校"}),
    frozenset({"工程师", "程序", "写代码", "开发", "职业", "岗位", "工作"}),
    frozenset({"整活", "搞怪", "发图", "表情包", "擦边", "逗"}),
    frozenset({"复读", "引用", "转发"}),
    frozenset({"指令", "/风格", "/人设", "/人格", "调参", "控制台"}),
    frozenset({"搜索", "联网", "查询", "检索"}),
    frozenset({"游戏", "明日方舟", "艾尔登", "steam", "手游"}),
)


def _clean(text: object, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit or limit <= 0:
        return flat
    head = flat[: max(1, limit - 1)]
    cut = max(head.rfind(ch) for ch in "。！？；，、,.; ")
    if cut >= len(head) - 12 and cut > 0:
        head = head[:cut]
    return head.rstrip() + "…"


def _bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", str(text or "").lower()):
        if re.fullmatch(r"[A-Za-z0-9_]+", token):
            if token not in _STOP:
                out.add(token)
            continue
        if token in _STOP:
            continue
        if len(token) == 1:
            out.add(token)
            continue
        for i in range(len(token) - 1):
            gram = token[i : i + 2]
            if gram not in _STOP:
                out.add(gram)
    return out


def _overlap(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    return len(ga & gb) / len(ga) if ga else 0.0


def _topic(text: str) -> int:
    for idx, words in enumerate(_TOPIC_WORDS):
        if any(w in str(text or "") for w in words):
            return idx
    return -1


def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    if max(_overlap(a, b), _overlap(b, a)) >= _SIMILAR:
        return True
    ta, tb = _topic(a), _topic(b)
    return ta >= 0 and ta == tb


def _same_identity(x: str, y: str) -> bool:
    """两个名字是不是同一个人。

    用**对称相似度**（任一侧被另一侧覆盖 ≥0.5）：
    * 「鲸鱼娘（助手）」与「肥鱼（助手）」重合度高 → 同一身份
    * 「小明」与「张三」重合度 0 → 不同人
    """
    if not x or not y:
        return False
    if x == y:
        return True
    return max(_overlap(x, y), _overlap(y, x)) >= 0.5


def _merge_similar_keys(profile: dict, facts: list, name_alias: dict[str, str]) -> list[str]:
    """把画像里"其实是一个人"的键并成一个。返回报告行。

    为什么需要：补提取时模型会把同一个身份写成好几种名字 ——
    实测出现了「鲸鱼娘（助手）」「肥鱼（助手）」「夜风の旅人⭐」
    三个键，画像字段各存一部分，检索时按名字匹配只能命中一半。

    规范名怎么选：**先看 facts 里哪个名字出现得多**（那才是"大家平时怎么叫"），
    没有 facts 记录时取最长的（信息最全）。用 facts 频率而不是"最短"，
    是因为「鲸鱼娘（助手）」出现 4 次、「肥鱼（助手）」0 次 —— 该保留前者。
    """
    names: list[str] = []
    for key, info in profile.items():
        names.append(str(key))
        if isinstance(info, dict) and info.get("display"):
            names.append(str(info["display"]))
    for fact in facts:
        if isinstance(fact, dict) and fact.get("subject"):
            names.append(str(fact["subject"]))
    # 去重保序，并先把 name_alias 认得的名字换成规范名
    uniq: list[str] = []
    for n in names:
        n = name_alias.get(n, n)
        if n and n not in uniq:
            uniq.append(n)

    # 并查集：把所有"同一个人"的名字连起来
    parent = {n: n for n in uniq}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(uniq):
        for b in uniq[i + 1 :]:
            if _same_identity(a, b):
                union(a, b)

    groups: dict[str, list[str]] = {}
    for n in uniq:
        groups.setdefault(find(n), []).append(n)

    freq: dict[str, int] = {}
    for fact in facts:
        if isinstance(fact, dict):
            sub = name_alias.get(str(fact.get("subject") or ""), str(fact.get("subject") or ""))
            freq[sub] = freq.get(sub, 0) + 1

    canon_of: dict[str, str] = {}
    report: list[str] = []
    for members in groups.values():
        if len(members) < 2:
            canon_of[members[0]] = members[0]
            continue
        canon = max(members, key=lambda n: (freq.get(n, 0), len(n)))
        report.append(f"  身份合并：{members} → {canon!r}")
        for m in members:
            canon_of[m] = canon
    for n in uniq:
        canon_of.setdefault(n, n)
    _merge_similar_keys.canon_of = canon_of  # type: ignore[attr-defined]
    return report


def merge_list(items: list[str], cap: int = _MAX_PROFILE_FIELD) -> list[str]:
    """相似度合并 + 超限丢最短。"""
    out: list[str] = []
    for raw in items:
        item = _clean(raw, _MAX_PROFILE_ITEM)
        if not item:
            continue
        hit = next((i for i, old in enumerate(out) if _similar(old, item)), None)
        if hit is None:
            out.append(item)
        elif len(item) > len(out[hit]):
            out[hit] = item
    if cap > 0 and len(out) > cap:
        out.sort(key=len, reverse=True)
        out = out[:cap]
    return out


def load_uid_names(paths: list[str]) -> dict[str, dict]:
    """从 chatlog 文件（或目录）里统计 uid → {名字: 次数}。"""
    uid2name: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.glob("chatlog_*.json")))
        elif p.is_file():
            files.append(p)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for msg in data.get("messages") or []:
            uid, name = msg.get("uid"), str(msg.get("name") or "").strip()
            if uid and name:
                uid2name[str(uid)][name] += 1
    return {uid: dict(c) for uid, c in uid2name.items()}


def build_identity_map(uid_names: dict[str, dict], profile: dict) -> tuple[dict[str, str], dict[str, str]]:
    """返回 (名字→规范名, uid→规范名)。

    规范名取"该 uid 出现最多的那个名字"（实测 小明 297 次 vs 长昵称 27 次 → 选 小明）。
    再把画像里已有的键并进来：画像用的是哪种写法，就认它是同一个身份。
    """
    uid_canon: dict[str, str] = {}
    name_alias: dict[str, str] = {}
    for uid, names in uid_names.items():
        if not names:
            continue
        canon = max(names.items(), key=lambda kv: kv[1])[0]
        uid_canon[uid] = canon
        for nm in names:
            name_alias[nm] = canon
            # **去掉空格的版本也要映射**：旧代码按 24 字截断过 subject，
            # 实测 facts 里存的是「夜风の旅人⭐」（无空格），
            # 而 chatlog 里是「夜风の 旅人⭐」（有空格）——
            # 不对齐就永远归并不上。
            compact = nm.replace(" ", "")
            if compact and compact != nm:
                name_alias.setdefault(compact, canon)
    # 画像键也参与：画像里写过「小明」就把它当别名指向自身
    for key in list(profile or {}):
        key = str(key or "").strip()
        if key and key not in name_alias:
            name_alias[key] = key
    return name_alias, uid_canon


def repair(data: dict, name_alias: dict[str, str] | None = None,
           uid_canon: dict[str, str] | None = None) -> tuple[dict, list[str]]:
    """就地修复并返回 (数据, 报告行)。"""
    report: list[str] = []
    name_alias = name_alias or {}
    uid_canon = uid_canon or {}

    # ---------------------------------------------------------- 0. 身份归一
    def _base(name: str) -> str:
        """去掉名字后面括号里的别名：「助手（大肥鱼）」→「助手」。"""
        return re.split(r"[（(]", str(name or "").strip())[0].strip()

    report.append("【身份归一】")
    profile: dict = data.setdefault("profile", {})

    # 先按 name_alias 把画像键规整一遍，再让聚类去看"这些名字里有没有同一个人的多种写法"
    pre: dict[str, dict] = {}
    for key, info in list(profile.items()):
        if not isinstance(info, dict):
            continue
        norm = name_alias.get(str(key), str(key))
        dst = pre.setdefault(
            norm, {"display": "", "love": [], "dislike": [], "habit": [], "note": "", "updated_at": ""}
        )
        if not dst["display"]:
            dst["display"] = info.get("display") or norm
        for field in ("love", "dislike", "habit"):
            dst[field] = list(dst[field]) + [str(x) for x in (info.get(field) or [])]
        if not dst["note"] and info.get("note"):
            dst["note"] = info["note"]
        dst["updated_at"] = max(str(dst["updated_at"]), str(info.get("updated_at") or ""))

    # 聚类：把"其实是一个人"的键并起来（规范名取 facts 里出现最多的那个）
    report += _merge_similar_keys(pre, data.get("facts") or [], name_alias)
    canon_of: dict[str, str] = getattr(_merge_similar_keys, "canon_of", {})

    # facts 里可能还有画像没覆盖到的名字，一并补进映射表，让 subject 也用同一套规范名
    for fact in data.get("facts") or []:
        if isinstance(fact, dict) and fact.get("subject"):
            s = str(fact["subject"])
            canon_of.setdefault(name_alias.get(s, s), name_alias.get(s, s))

    def _resolve(subject: str) -> str:
        """把一个名字解析成规范名。认不出就原样返回（去括号别名）。"""
        s = str(subject or "").strip()
        if not s:
            return ""
        if s in canon_of:
            return canon_of[s]
        alias = name_alias.get(s)
        if alias and alias in canon_of:
            return canon_of[alias]
        if s in uid_canon:                      # 旧数据可能存的是 uid
            return canon_of.get(uid_canon[s], uid_canon[s])
        base = _base(s)
        if base and base != s:
            return canon_of.get(name_alias.get(base, base), _base(name_alias.get(base, base)))
        for name, canon in canon_of.items():     # 昵称被截断过时用前缀兜底
            if len(name) >= 6 and (name.startswith(s) or s in name):
                return canon
        return s

    merged: dict[str, dict] = {}
    for key, info in pre.items():
        canon = _resolve(key)
        if canon != key:
            report.append(f"  画像键 {key!r} → {canon!r}")
        dst = merged.setdefault(
            canon, {"display": "", "love": [], "dislike": [], "habit": [], "note": "", "updated_at": ""}
        )
        if not dst["display"]:
            dst["display"] = info.get("display") or canon
        for field in ("love", "dislike", "habit"):
            dst[field] = list(dst[field]) + [str(x) for x in (info.get(field) or [])]
        if not dst["note"] and info.get("note"):
            dst["note"] = info["note"]
        dst["updated_at"] = max(str(dst["updated_at"]), str(info.get("updated_at") or ""))
    profile.clear()
    profile.update(merged)

    # ---------------------------------------------------------- 1. 画像去重
    report.append("【画像去重】")
    for key, info in profile.items():
        if not isinstance(info, dict):
            continue
        for field in ("love", "dislike", "habit"):
            before = [str(x) for x in (info.get(field) or [])]
            if not before:
                continue
            after = merge_list(before)
            info[field] = after
            if len(after) != len(before):
                report.append(f"  {key}.{field}: {len(before)} → {len(after)} 条")
                for x in before:
                    if not any(_similar(x, y) for y in after):
                        report.append(f"      - 丢弃：{x[:50]}")
        if info.get("display"):
            info["display"] = _clean(info["display"], _MAX_NAME)

    # ---------------------------------------------------------- 2. subject 迁移
    report.append("【facts 主体】")
    changed = 0
    for fact in data.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        old = str(fact.get("subject") or "")
        new = _resolve(old)
        if new and new != old:
            fact["subject"] = new
            fact["name"] = fact.get("name") or old
            changed += 1
            report.append(f"  id={fact.get('id')} {old!r} → {new!r}")
        # 顺带把事实文本也按新上限规整（原来 200，现在 300）
        if fact.get("text"):
            fact["text"] = _clean(fact["text"], _MAX_TEXT)
    if not changed:
        report.append("  （无需迁移）")

    # ---------------------------------------------------------- 3. 统计
    report.append("【汇总】")
    report.append(f"  画像 {len(data.get('profile') or {})} 人")
    report.append(f"  facts {len(data.get('facts') or [])} 条、events {len(data.get('events') or [])} 条")
    return data, report


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="QQ bot 记忆数据修复（画像去重 + subject 归一）")
    ap.add_argument("memories", help="memories.json 路径")
    ap.add_argument("--out", default="", help="修复后的输出路径；不给则只打印报告（dry-run）")
    ap.add_argument("--chatlog", nargs="*", default=[], help="chatlog 文件或目录（用于 subject 归一）")
    args = ap.parse_args()

    src = Path(args.memories)
    data = json.loads(src.read_text(encoding="utf-8"))
    out_path = Path(args.out) if args.out else None

    name_alias: dict[str, str] = {}
    uid_canon: dict[str, str] = {}
    if args.chatlog:
        uid_names = load_uid_names(args.chatlog)
        name_alias, uid_canon = build_identity_map(uid_names, data.get("profile") or {})
        print(f"从 chatlog 读到 {len(uid_names)} 个 uid 的身份映射")
        for uid, canon in uid_canon.items():
            variants = list(uid_names[uid].keys())
            if len(variants) > 1:
                print(f"  uid {uid} 有多个名字 {variants} → 归一到 {canon!r}")
    else:
        print("（未给 --chatlog：跳过 subject 归一，只做画像去重）")

    backup = src.with_suffix(src.suffix + ".bak-before-repair")
    if not backup.exists():
        backup.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已备份原始数据：{backup}")

    fixed, report = repair(json.loads(json.dumps(data, ensure_ascii=False)),
                           name_alias=name_alias, uid_canon=uid_canon)
    print("\n".join(report))

    if out_path is not None:
        out_path.write_text(json.dumps(fixed, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写出：{out_path}")
    else:
        print("\n（dry-run：未写出文件。要落盘加 --out <路径>）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
