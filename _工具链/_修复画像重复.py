"""修复记忆库里的**画像重复键**：合并同一个人，并删掉"机器人给自己建的画像"。

## 为什么需要它

控制台「人物画像」里出现好几个本该是同一个人的条目，有时还包括机器**自己**的昵称。
根因有三处，都在写入路径上（代码侧已修，见 `memory.py` 的 `_is_bot_self` /
`_link_key_to_uid`），但**已经写进去的存量数据不会自己变好**：

1. **机器人给自己建画像**。抽取输入里只有别人的发言，但模型照样可能输出一条
   `{"who": "鲸鱼娘（助手）", "habit": [...]}` —— 它不是人，不该进人物画像。
2. **名字变体解不出 uid**。`_resolve_uid` 只做精确匹配：模型这次写「小明」、
   下次写「小明 」、或某个批次没带上发送者名单，就解不出来，于是又建一个**独立人名键**。
   同一个人因此有 `uid:<QQ号>` 与若干个人名键。
3. **键被截到 24 字**（`_MAX_KEY`）。超长昵称的不同截法又分裂成多个键。

## 它怎么工作

判据是 `entities` 表（「见过这个名字 = 这个 uid」）+ 画像自己的 `display`。分三步：

| 步 | 动作 | 保守程度 |
|---|---|---|
| ① 删机器人画像 | uid 命中机器人、或名字是机器人名/角色名的变体 → 整条删掉 | 明确（配置里写着它是谁） |
| ② 并人名键 | 名字在 `entities` 里明确对应某个 uid → 并进 `uid:<QQ号>` | 明确（表里写着） |
| ③ 并同展示名 | 两个人名键的 `display` **规范化后完全相同**，且其一有 uid → 并进 uid 键 | 明确（同名同人） |

第 ③ 步刻意只认"display 规范化后完全相等"，**不做相似度猜测** ——
把两个人合并比留两条重复更糟。拿不准的条目会列在报告里让你人工判断。

## 用法

    python _工具链/_修复画像重复.py --dry-run        # 先看会改什么（推荐）
    python _工具链/_修复画像重复.py                  # 真的改（自动备份 memory.db）

    python _工具链/_修复画像重复.py --db data/memory.db
    python _工具链/_修复画像重复.py --keep-bot       # 只合并，不删机器人画像

也可以直接在容器里跑：

    docker exec -w /app ai-chat-bot python /app/_工具链/_修复画像重复.py --dry-run

退出码：0 = 跑完；1 = 库不存在/读不出来。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# 与 memory.py 保持一致（刻意复制：这个脚本要能脱离插件单独跑，也能在容器里直接跑）
_MAX_NAME = 40
_MAX_PROFILE_ITEM = 80
_MAX_PROFILE_FIELD = 12


def _clean(text: object, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit or limit <= 0:
        return flat
    return flat[:limit]


def _norm_name(name: str) -> str:
    """名字的规范化形式，用于"是否同一个展示名"的比较。

    只做**无歧义**的归一：去掉空白与各类括号后缀（`（助手）`、`(bot)`、`【AI】`）。
    不做同义词、不做相似度 —— 这一步只回答"这两个字符串是不是同一个名字"。
    """
    text = _clean(name, _MAX_NAME)
    for left, right in (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]")):
        while left in text and right in text:
            start = text.find(left)
            end = text.find(right, start)
            if end < 0:
                break
            text = text[:start] + text[end + 1 :]
    return text.strip().lower()


def _bigrams(text: str) -> set[str]:
    """中文按二元组切。

    一开始写的是"按单字取集合"，那会让「小明」与「小明明」的集合完全相同
    （都是 {小,明}）→ 相似度 1.0 → 可能把两个人合并。二元组就没这个问题。
    """
    norm = _norm_name(text)
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i : i + 2] for i in range(len(norm) - 1)}


def _overlap(a: str, b: str) -> float:
    """a 被 b 覆盖的比例（对称取大者，与 `memory._overlap` 同口径）。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return max(len(ga & gb) / len(ga), len(ga & gb) / len(gb))


def _merge_lists(old: list, new: list) -> list:
    """按相似度去重合并两个列表（与 `memory._merge_unique` 同口径的简化版）。"""
    out = [str(x) for x in (old or [])]
    for value in (new or []):
        text = str(value)
        if not text:
            continue
        if text in out:
            continue
        if any(_overlap(text, existing) >= 0.8 for existing in out):
            continue
        out.append(text)
    return out[:_MAX_PROFILE_FIELD]


class Repair:
    def __init__(self, db_path: Path, *, keep_bot: bool, bot_names: set[str], bot_uids: set[int]) -> None:
        self.db_path = db_path
        self.keep_bot = keep_bot
        self.bot_names = {n for n in bot_names if n}
        self.bot_uids = bot_uids
        self.removed: list[tuple[str, str]] = []       # (key, 原因)
        self.merged: list[tuple[str, str, str]] = []   # (旧键, 新键, 原因)
        self.kept_suspect: list[tuple[str, str]] = []  # (key, 为什么像重复但没动)

    # ---------------------------------------------------------------- 读取
    def load(self) -> tuple[dict[str, dict], dict[str, int]]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            profiles: dict[str, dict] = {}
            for row in conn.execute("SELECT * FROM profile"):
                profiles[str(row["key"])] = {
                    "display": str(row["display"] or ""),
                    "love": json.loads(row["love"] or "[]"),
                    "dislike": json.loads(row["dislike"] or "[]"),
                    "habit": json.loads(row["habit"] or "[]"),
                    "note": str(row["note"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                }
            entities: dict[str, int] = {}
            try:
                for row in conn.execute("SELECT name, uid FROM entities"):
                    if row["uid"] not in (None, ""):
                        entities[str(row["name"])] = int(row["uid"])
            except sqlite3.OperationalError:
                pass  # 老库没有 entities 表：退化成"只按 display 判"
            return profiles, entities
        finally:
            conn.close()

    # ---------------------------------------------------------------- 判定
    def _is_bot(self, key: str, info: dict) -> str:
        """这条画像是不是机器人自己。返回原因（空串 = 不是）。"""
        if key.startswith("uid:"):
            try:
                if int(key[4:]) in self.bot_uids:
                    return f"uid {key[4:]} 是机器人自己的号"
            except (TypeError, ValueError):
                pass
        for name in (key, info.get("display") or ""):
            norm = _norm_name(str(name))
            if not norm:
                continue
            for bot in self.bot_names:
                bot_norm = _norm_name(bot)
                if bot_norm and (bot_norm in norm or norm in bot_norm):
                    return f"名字命中机器人身份「{bot}」"
        return ""

    def plan(self, profiles: dict[str, dict], entities: dict[str, int]) -> None:
        # 先归一键：去掉首尾空白。库里的 `"小明 "` 与 `"小明"` 是两个键，
        # 但它们是同一个人 —— 不先 strip 的话这一步会把它当两个陌生人。
        normalized: dict[str, dict] = {}
        for key, info in profiles.items():
            clean_key = key.strip()
            if clean_key in normalized:
                # 撞键（`"uid:1001"` 与 `"uid:1001 "`）：合并内容，别丢字段
                kept = normalized[clean_key]
                if not kept.get("display"):
                    kept["display"] = info.get("display") or ""
                for field in ("love", "dislike", "habit"):
                    kept[field] = _merge_lists(kept.get(field) or [], info.get(field) or [])
                if not kept.get("note") and info.get("note"):
                    kept["note"] = info["note"]
                continue
            normalized[clean_key] = dict(info)
            if clean_key != key:
                # 键本身变了，得让 apply() 知道要删掉带空白的旧键
                self.merged.append((key, clean_key, "键首尾有空白（同一个人被写成两个键）"))
        profiles = normalized

        # 第 ① 步：机器人画像
        for key, info in list(profiles.items()):
            why = self._is_bot(key, info)
            if why and not self.keep_bot:
                self.removed.append((key, why))

        alive = {k: v for k, v in profiles.items() if k not in {x[0] for x in self.removed}}

        # 第 ② 步：名字在 entities 里明确对应某个 uid 的人名键
        for key, info in list(alive.items()):
            if key.startswith("uid:"):
                continue
            uid = entities.get(key)
            if uid is None:
                continue
            target = f"uid:{uid}"
            why = f"entities 表：名字「{key}」= uid {uid}"
            self.merged.append((key, target, why))

        # 第 ③ 步：display 规范化后完全相同的人名键，并到有 uid 的那个上
        taken = {x[0] for x in self.merged}          # 第 ② 步已经并掉的，别再算一遍
        by_norm: dict[str, list[str]] = {}
        for key, info in alive.items():
            if key in taken:
                continue
            norm = _norm_name(key if key.startswith("uid:") else (info.get("display") or key))
            if norm:
                by_norm.setdefault(norm, []).append(key)
        for norm, keys in by_norm.items():
            if len(keys) < 2:
                continue
            uid_keys = [k for k in keys if k.startswith("uid:")]
            name_keys = [k for k in keys if not k.startswith("uid:")]
            if not uid_keys:
                # 同名但没有 uid 可归：**不动**，列出来让人判断（可能是两个真同名的人）
                for k in name_keys[1:]:
                    self.kept_suspect.append(
                        (f"{name_keys[0]}  ×  {k}",
                         f"展示名同为「{norm}」但两边都没有 uid —— 可能是两个真同名的人")
                    )
                continue
            target = uid_keys[0]
            for k in name_keys:
                self.merged.append((k, target, f"展示名同为「{norm}」"))
                taken.add(k)

        # 剩下的"像重复"的：名字互相包含或高度重叠，但没法确定 —— 只报告
        done = taken | {x[0] for x in self.removed}
        rest = [k for k in alive if k not in done]
        for i, a in enumerate(rest):
            for b in rest[i + 1 :]:
                na = _norm_name(alive[a].get("display") or a)
                nb = _norm_name(alive[b].get("display") or b)
                if not na or not nb or na == nb:
                    continue
                if na in nb or nb in na or _overlap(na, nb) >= 0.7:
                    self.kept_suspect.append(
                        (f"{a}  ×  {b}",
                         f"名字很像（「{na}」/「{nb}」）但无法确定是不是同一个人")
                    )

    # ---------------------------------------------------------------- 写入
    def backup(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = self.db_path.with_name(f"{self.db_path.name}.bak-before-dedupe-{stamp}")
        shutil.copy2(self.db_path, bak)
        # WAL 模式下真正的数据可能还在 -wal 里，一并留证，否则备份可能是不完整的
        for suffix in ("-wal", "-shm"):
            side = self.db_path.with_name(self.db_path.name + suffix)
            if side.exists():
                shutil.copy2(side, bak.with_name(bak.name + suffix))
        return bak

    def apply(self, profiles: dict[str, dict]) -> tuple[int, int]:
        """把 self.removed / self.merged 落到库里。返回 (删了几条, 并了几条)。

        只动 `profile` 表 —— **facts / events 一律不碰**，所以合并错了最多是
        少两条画像，不会丢事实。
        """
        removed_n = 0
        merged_n = 0
        # 与 `plan()` 用同一套归一：先 strip 键、撞键就合内容。
        # 不这样做的话，`plan()` 算出来的"旧键"会在这里找不到（它已经被归一过了）。
        work: dict[str, dict] = {}
        for key, info in profiles.items():
            clean_key = key.strip()
            cur = work.get(clean_key)
            if cur is None:
                work[clean_key] = dict(info)
                continue
            if not cur.get("display"):
                cur["display"] = info.get("display") or ""
            for field in ("love", "dislike", "habit"):
                cur[field] = _merge_lists(cur.get(field) or [], info.get(field) or [])
            if not cur.get("note") and info.get("note"):
                cur["note"] = info["note"]

        for key, _why in self.removed:
            if work.pop(key.strip(), None) is not None:
                removed_n += 1

        # 按目标键**分组**再合并，而不是逐条 insert。
        # 原因：多个源键可能指向同一个目标（例如 `"小明"` 与 `"小明 "` 都并进
        # `uid:1001`），逐条合并会往同一行插两次 → UNIQUE 冲突（写第一版时就这么炸的）。
        grouped: dict[str, list[str]] = {}
        for old_key, new_key, _why in self.merged:
            grouped.setdefault(new_key.strip(), []).append(old_key.strip())

        for new_key, old_keys in grouped.items():
            if new_key in {k.strip() for k, _ in self.removed}:
                continue
            cur = work.setdefault(
                new_key,
                {"display": "", "love": [], "dislike": [], "habit": [], "note": "", "updated_at": ""},
            )
            for old_key in old_keys:
                if old_key == new_key:
                    continue
                old = work.pop(old_key, None)
                if old is None:
                    continue
                if not cur.get("display"):
                    cur["display"] = old.get("display") or old_key
                for field in ("love", "dislike", "habit"):
                    cur[field] = _merge_lists(cur.get(field) or [], old.get(field) or [])
                if not cur.get("note") and old.get("note"):
                    cur["note"] = old["note"]
                merged_n += 1
            cur["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cur_db = conn.cursor()
            cur_db.execute("DELETE FROM profile")
            for key, info in work.items():
                cur_db.execute(
                    "INSERT INTO profile (key, display, love, dislike, habit, note, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        _clean(key, 120),
                        _clean(info.get("display"), _MAX_NAME),
                        json.dumps([_clean(x, _MAX_PROFILE_ITEM) for x in (info.get("love") or [])][:_MAX_PROFILE_FIELD], ensure_ascii=False),
                        json.dumps([_clean(x, _MAX_PROFILE_ITEM) for x in (info.get("dislike") or [])][:_MAX_PROFILE_FIELD], ensure_ascii=False),
                        json.dumps([_clean(x, _MAX_PROFILE_ITEM) for x in (info.get("habit") or [])][:_MAX_PROFILE_FIELD], ensure_ascii=False),
                        _clean(info.get("note"), _MAX_PROFILE_ITEM),
                        _clean(info.get("updated_at"), 40),
                    ),
                )
            conn.commit()
            # 清空后重建：顺手把 WAL 收回去，免得文件一直胖着
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
        return removed_n, merged_n


def main() -> int:
    parser = argparse.ArgumentParser(description="修复记忆库里的画像重复键")
    parser.add_argument("--db", default="", help="memory.db 路径（默认 <项目>/data/memory.db）")
    parser.add_argument("--dry-run", action="store_true", help="只打印会改什么，不写库")
    parser.add_argument("--keep-bot", action="store_true", help="只合并重复，不删机器人画像")
    parser.add_argument("--bot-uid", default="", help="机器人自己的 QQ 号（逗号分隔）；默认读 .env 的 ACCOUNT")
    parser.add_argument("--bot-name", default="", help="机器人显示名（逗号分隔）；默认读 persona_base.txt 的角色名 + .env 的 AI_CHAT_BOT_NAME")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    db_path = Path(args.db) if args.db else (root / "data" / "memory.db")
    if not db_path.is_absolute():
        db_path = (root / db_path).resolve()
    if not db_path.exists():
        print(f"[阻塞] 找不到记忆库：{db_path}")
        print("        它默认在 data/memory.db。若后端是 json，这个脚本不适用 "
              "（那些人名键在 memories.json 里，用别的方式处理）。")
        return 1

    # 机器人身份：命令行 > 环境/配置
    bot_uids: set[int] = set()
    for chunk in (args.bot_uid or "").replace("，", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            bot_uids.add(int(chunk))
    if not bot_uids:
        for line in (root / ".env").read_text(encoding="utf-8", errors="replace").splitlines() if (root / ".env").exists() else []:
            if line.strip().startswith("ACCOUNT="):
                value = line.split("=", 1)[1].strip()
                if value.isdigit():
                    bot_uids.add(int(value))

    bot_names: set[str] = set()
    for chunk in (args.bot_name or "").replace("，", ",").split(","):
        if chunk.strip():
            bot_names.add(chunk.strip())
    if not bot_names:
        if (root / ".env").exists():
            for line in (root / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith("AI_CHAT_BOT_NAME="):
                    value = line.split("=", 1)[1].strip()
                    if value:
                        bot_names.add(value)
        base = root / "persona_base.txt"
        if base.exists():
            first = base.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
            if first:
                import re

                hit = re.search(r"[「『\"']([^」』\"']{1,20})[」』\"']", first[0])
                if hit:
                    bot_names.add(hit.group(1))
    if bot_names:
        # 人设角色名的常见变体：加后缀（（助手）之类）已被 `_norm_name` 处理，
        # 这里再补一个"去掉最后一个字"的短名（「鲸鱼娘」→「鲸鱼」），
        # 因为模型偶尔会只写前半截。
        for name in list(bot_names):
            if len(name) >= 3:
                bot_names.add(name[:2])
    else:
        print("[注意] 没能确定机器人自己的名字 —— 第 ① 步不会生效。"
              "用 --bot-name 显式指定（例如 --bot-name 鲸鱼娘,肥鱼）。")

    print(f"记忆库      : {db_path}")
    print(f"机器人 uid  : {sorted(bot_uids) or '（未确定）'}")
    print(f"机器人名字  : {sorted(bot_names) or '（未确定）'}")

    rep = Repair(db_path, keep_bot=args.keep_bot, bot_names=bot_names, bot_uids=bot_uids)
    profiles, entities = rep.load()
    print(f"当前画像    : {len(profiles)} 条，entities 表 {len(entities)} 个已知名字\n")
    rep.plan(profiles, entities)

    if not rep.removed and not rep.merged:
        print("没有需要处理的重复。")
        if rep.kept_suspect:
            print("\n但下面这些**看起来像重复、脚本没敢动**，需要你判断：")
            for what, why in rep.kept_suspect:
                print(f"  ? {what} —— {why}")
        return 0

    if rep.removed:
        print(f"要删掉的机器人画像（{len(rep.removed)} 条）：")
        for key, why in rep.removed:
            print(f"  ✗ {key} —— {why}")
    if rep.merged:
        print(f"\n要合并的重复键（{len(rep.merged)} 条）：")
        for old_key, new_key, why in rep.merged:
            print(f"  → {old_key}  ⇒  {new_key}   （{why}）")
    if rep.kept_suspect:
        print(f"\n没敢动的可疑项（{len(rep.kept_suspect)} 条）：")
        for what, why in rep.kept_suspect[:20]:
            print(f"  ? {what} —— {why}")

    if args.dry_run:
        print(f"\n（--dry-run：什么都没写。画像 {len(profiles)} 条 → 预计 "
              f"{len(profiles) - len(rep.removed) - len(rep.merged)} 条）")
        return 0

    bak = rep.backup()
    removed_n, merged_n = rep.apply(profiles)
    print(f"\n已备份   : {bak.name}")
    print(f"已删除   : {removed_n} 条机器人画像")
    print(f"已合并   : {merged_n} 条重复键")
    print(f"画像总数 : {len(profiles)} → {len(profiles) - removed_n - merged_n}")
    print("\n控制台刷新即可看到结果。机器人**不用重启** —— 但它内存里可能还留着旧画像，"
          "重启最干净。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
