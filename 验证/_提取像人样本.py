"""从聊天记录提取「像人判别」候选样本（**产出需人工审核后才用**）。

用法：

    python 验证/_提取像人样本.py <聊天记录目录> <输出.jsonl> [每组上下文条数]

它做三件事：

1. 找真人说的、有一定信息量的一条（不是机器人、不是指令、不是纯表情）；
2. 取它前面 3 条真人发言作上下文；
3. 把昵称、QQ 号、@ 替换成占位符。

**不直接写 `likeness_samples.jsonl`**：候选里会有玩笑、隐私、没头没尾的片段，
需要人挑过才能当评估基准。挑好的行再放进
`data/runtime/persona/<包>/likeness_samples.jsonl`。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_QQ_RE = re.compile(r"\b\d{5,12}\b")
_AT_RE = re.compile(r"@\S+")


def _clean(text: str, names: dict[str, str]) -> str:
    out = _QQ_RE.sub("QQ", str(text or ""))
    out = _AT_RE.sub("@某人", out)
    for name, alias in names.items():
        if name and name in out:
            out = out.replace(name, alias)
    return out.strip()


def extract(log_dir: Path, out_path: Path, context_lines: int = 3) -> int:
    names: dict[str, str] = {}
    rows: list[dict] = []
    for path in sorted(log_dir.glob("chatlog_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            continue
        conv = str(data.get("conv") or path.stem)
        for index, msg in enumerate(messages):
            if msg.get("is_bot"):
                continue
            text = str(msg.get("text") or "").strip()
            if len(text) < 8 or text.startswith("/") or text.startswith("["):
                continue
            speaker = str(msg.get("name") or "")
            if speaker and speaker not in names:
                names[speaker] = f"群友{chr(65 + len(names) % 26)}"
            prior = [x for x in messages[:index] if not x.get("is_bot")][-context_lines:]
            ctx = [_clean(str(x.get("text") or ""), names) for x in prior]
            ctx = [x for x in ctx if x]
            if not ctx:
                continue
            rows.append({
                "id": f"ls-{conv}-{index}",
                "ctx": ctx,
                "human": _clean(text, names),
                "source": conv,
            })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + "\n",
        encoding="utf-8",
    )
    return len(rows)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    log_dir = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    context_lines = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    count = extract(log_dir, out_path, context_lines)
    print(f"候选 {count} 条 -> {out_path}")
    print("下一步：人工挑 20~40 条，改成 {\"id\",\"ctx\",\"human\"} 后放进 "
          "data/runtime/persona/<包>/likeness_samples.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
