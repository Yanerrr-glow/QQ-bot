"""以 **bot 目标事件** 为单位提取评估案例候选（#0015 M1）。

和 `_提取像人样本.py` 的区别（这条是 codex P1-3 指出的）：

| | 旧提取器 | 本脚本 |
| --- | --- | --- |
| 单位 | 一条**真人**发言 | 一条 **bot 回复/动作** |
| 上下文 | 只取真人（`if not x.get("is_bot")`） | **双方**都取（含 bot 自己说过的话） |
| 产物 | `id / ctx / human / source` | 带 `message_id`、时间、`sha256`、双侧 `pre_context` |
| 能否进案例库 | 不能（没有目标回复） | 能（`failure_type=unknown`，`provenance=legacy` 待人工确认） |

用法：

    python 验证/_提取目标事件.py <聊天记录目录> <输出.jsonl> [--context 6] [--limit 200]

产出的行可以直接 `eval_cases.import_jsonl()` 导入；导入前 `eval_cases_enabled` 要打开。

**不包含**：坏样本的判定（`failure_type`）、可接受答案与反例 —— 那些必须人写，
脚本填进去就是编造。这也是 `provenance=legacy` 的含义：机器提取，未经确认。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_QQ_RE = re.compile(r"\b\d{5,12}\b")
_AT_RE = re.compile(r"@\S+")

# 与 `_提取像人样本.py` **同一套脱敏规则**：改一处要改两处。
def _clean(text: str, names: dict[str, str]) -> str:
    out = _QQ_RE.sub("QQ", str(text or ""))
    out = _AT_RE.sub("@某人", out)
    for real, alias in names.items():
        if real:
            out = out.replace(real, alias)
    return out.strip()


def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _active_pack() -> str:
    path = Path(ROOT) / "data" / "runtime" / "persona" / "_active"
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def extract(log_dir: Path, out_path: Path, *, context: int = 6, limit: int = 200) -> dict:
    """扫会话记录，产出案例候选。返回计数。"""
    names: dict[str, str] = {}
    rows: list[dict] = []
    seen: set[str] = set()
    pack = _active_pack()

    for path in sorted(Path(log_dir).glob("chatlog_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            continue
        conv = str(data.get("conv") or path.stem)
        for index, msg in enumerate(messages):
            if not msg.get("is_bot"):
                continue
            text = str(msg.get("text") or "").strip()
            if not text:
                continue
            prior = messages[max(0, index - context):index]
            if not prior:
                continue
            case_id = f"ev-{conv}-{msg.get('id')}"
            if case_id in seen:
                continue
            seen.add(case_id)
            speaker = str(msg.get("name") or "")
            if speaker and speaker not in names:
                names[speaker] = f"群友{chr(65 + len(names) % 26)}"
            ctx: list[dict] = []
            for item in prior:
                who = str(item.get("name") or "")
                if who and who not in names:
                    names[who] = f"群友{chr(65 + len(names) % 26)}"
                ctx.append({
                    "uid": "bot" if item.get("is_bot") else str(item.get("uid") or ""),
                    "name": "机器人" if item.get("is_bot") else _clean(who, names),
                    "text": _clean(str(item.get("text") or ""), names),
                    "ts": item.get("ts"),
                    "is_bot": bool(item.get("is_bot")),
                })
            rows.append({
                "case_id": case_id,
                "version": 1,
                "status": "ok",
                "source_type": "chat",
                "provenance": "legacy",
                "persona_id": pack,
                "persona_fingerprint": "unknown",
                "split_group": f"conv:{conv}",
                "scope": "group" if str(conv).startswith("g") else "private",
                "source_refs": [{
                    "conv": conv,
                    "message_id": msg.get("id"),
                    "ts": msg.get("ts"),
                    "sha256": _sha(text),
                }],
                "pre_context": ctx,
                "original_reply": _clean(text, names),
                "action": "",
                "failure_type": "unknown",
                "preferences": [],
                "followups": [],
            })
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break

    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(x, ensure_ascii=False) for x in rows)
    out_path.write_text(body + ("\n" if body else ""), encoding="utf-8")
    return {"rows": len(rows), "conversations": len(list(Path(log_dir).glob("chatlog_*.json")))}


def main(argv: list[str]) -> int:
    args = [x for x in argv[1:] if not x.startswith("--")]
    flags = [x for x in argv[1:] if x.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    context, limit = 6, 200
    for flag in flags:
        if flag.startswith("--context="):
            context = max(1, int(flag.split("=", 1)[1]))
        elif flag.startswith("--limit="):
            limit = max(1, int(flag.split("=", 1)[1]))
    log_dir = Path(args[0])
    if not log_dir.exists():
        print(f"找不到聊天记录目录：{log_dir}")
        return 2
    got = extract(log_dir, Path(args[1]), context=context, limit=limit)
    print(f"会话 {got['conversations']} 个，提取目标事件 {got['rows']} 条 -> {args[1]}")
    if not got["rows"]:
        print("（没有 bot 回复事件；这批记录里可能只有用户消息）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
