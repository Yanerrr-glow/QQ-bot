"""评估案例与偏好（#0015 M1）：把「用户认可的情景行为」变成可追溯的数据。

## 为什么单独一层

旧评估台的证据是「一段对话 + 一个 0-100 分」，事后无法回答「这条为什么算问题、
当时的上下文是什么、什么答案才算对」。这一层只做数据：案例的写入、校验、分组与偏好，
**不调用模型、不碰人格文件**。

## 落点与形状

`config.persona_data_dir()/eval/cases/<case_id>.v<version>.json` —— **一案一文件**。
不用索引文件：目录扫描就能算出每个 case 的最高版本，少一份需要同步的状态。
旧提取器的产物不进这里，只进 `eval/incomplete/`（见 `import_jsonl`）。

## 两个维度不要混

* `source_type`：**从哪来** —— `chat` / `manual` / `migrated` / `model`（与 `persona_examples` 同词）；
* `provenance`：**谁确认过** —— `human` / `synthetic` / `legacy`。

`_提取像人样本.py` 的产物是 `source_type=chat` 但**没有**人工确认，所以 `provenance` 只能是
`legacy` 或空；人工修订之后才是 `human`。混成一个字段会让「机器生成的东西」被当成「真人认可」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import config, settings

logger = logging.getLogger("ai_chat.eval_cases")

# 失败类型：只有 expression 允许进人格表达修订，其余只出工程诊断（#0015 §四）
FAILURE_TYPES = ("context", "retrieval", "memory", "expression", "boundary", "unknown")
PROVENANCE = ("human", "synthetic", "legacy")
SOURCE_TYPES = ("chat", "manual", "migrated", "model")
SPLITS = ("train", "calibration", "validation", "sealed")
CHOICES = ("A", "B", "tie", "both_bad", "uncertain")
STATUSES = ("ok", "incomplete")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
_REQUIRED = ("case_id", "version", "source_refs", "persona_id", "persona_fingerprint",
             "pre_context", "original_reply", "failure_type", "provenance", "split_group")


# --------------------------------------------------------------------- 路径
def enabled() -> bool:
    """案例库总闸。关着时读写入口一律返回「关着」，不产生目录。"""
    try:
        return bool(settings.get("eval_cases_enabled"))
    except Exception:  # noqa: BLE001 - 参数表缺项时按关处理
        return False


def paths() -> dict[str, Path]:
    """全部落点。`cases` 是正式库，`incomplete` 是暂存区（可当语料，不入库）。"""
    root = config.persona_data_dir() / "eval"
    return {
        "root": root,
        "cases": root / "cases",
        "incomplete": root / "incomplete",
        "preferences": root / "preferences.jsonl",
    }


def _ensure(kind: str) -> Path:
    path = paths()[kind]
    if kind in ("root", "cases", "incomplete"):
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def case_path(case_id: str, version: int) -> Path:
    return paths()["cases"] / f"{_safe_id(case_id)}.v{int(version)}.json"


def _safe_id(case_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", str(case_id or "").strip())


# --------------------------------------------------------------------- 读
def versions(case_id: str) -> list[int]:
    """这个 case 在盘上有哪些版本（升序）。"""
    prefix = _safe_id(case_id) + ".v"
    out: list[int] = []
    directory = paths()["cases"]
    if not directory.exists():
        return out
    for item in directory.glob(prefix + "*.json"):
        tail = item.stem[len(prefix):]
        if tail.isdigit():
            out.append(int(tail))
    return sorted(out)


def load(case_id: str, version: int | None = None) -> dict[str, Any] | None:
    """读一条案例；`version=None` 取最高版本。读不动返回 None（不抛）。"""
    found = versions(case_id)
    if not found:
        return None
    target = int(version) if version is not None else found[-1]
    if target not in found:
        return None
    try:
        data = json.loads(case_path(case_id, target).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("案例读不动：%s v%s", case_id, target)
        return None
    return data if isinstance(data, dict) else None


def load_all() -> list[dict[str, Any]]:
    """扫描目录，每个 case_id 只取最高版本。"""
    directory = paths()["cases"]
    if not directory.exists():
        return []
    seen: dict[str, int] = {}
    for item in directory.glob("*.v*.json"):
        name, _, ver = item.stem.rpartition(".v")
        if not name or not ver.isdigit():
            continue
        seen[name] = max(seen.get(name, 0), int(ver))
    out: list[dict[str, Any]] = []
    for case_id, ver in sorted(seen.items()):
        data = load(case_id, ver)
        if data is not None:
            out.append(data)
    return out


# --------------------------------------------------------------------- 校验
def validate(case: Any) -> list[str]:
    """结构校验，返回问题清单（空 = 通过）。**不做语义判断**，只挡「写坏了」。"""
    problems: list[str] = []
    if not isinstance(case, dict):
        return ["案例必须是对象"]

    for key in _REQUIRED:
        if key not in case:
            problems.append(f"缺必填字段 {key}")

    case_id = case.get("case_id")
    if case_id is not None and not _ID_RE.match(str(case_id)):
        problems.append("case_id 只能由字母数字与 _.:- 组成，2~64 位")
    version = case.get("version")
    if version is not None and (not isinstance(version, int) or version < 1):
        problems.append("version 必须是 >= 1 的整数")

    refs = case.get("source_refs")
    if refs is not None:
        if not isinstance(refs, list) or not refs:
            problems.append("source_refs 必须是非空数组")
        else:
            for index, ref in enumerate(refs):
                if not isinstance(ref, dict):
                    problems.append(f"source_refs[{index}] 必须是对象")
                    continue
                if not ref.get("message_id"):
                    problems.append(f"source_refs[{index}] 缺 message_id")
                if "sha256" not in ref:
                    problems.append(f"source_refs[{index}] 缺 sha256")

    pre = case.get("pre_context")
    if pre is not None and (not isinstance(pre, list) or not pre):
        problems.append("pre_context 必须是非空数组")

    reply = case.get("original_reply")
    action = case.get("action")
    if reply in (None, "") and not action:
        # 旧提取器的产物没有目标回复，只能进 incomplete
        problems.append("original_reply 与 action 至少要有一个（缺则只能标 incomplete）")

    kind = case.get("failure_type")
    if kind is not None and kind not in FAILURE_TYPES:
        problems.append(f"failure_type 必须是 {'/'.join(FAILURE_TYPES)}")

    prov = case.get("provenance")
    if prov is not None and prov not in PROVENANCE:
        problems.append(f"provenance 必须是 {'/'.join(PROVENANCE)}")

    stype = case.get("source_type")
    if stype is not None and stype not in SOURCE_TYPES:
        problems.append(f"source_type 必须是 {'/'.join(SOURCE_TYPES)}")

    status = case.get("status")
    if status is not None and status not in STATUSES:
        problems.append(f"status 必须是 {'/'.join(STATUSES)}")

    if not str(case.get("split_group") or "").strip():
        problems.append("split_group 必须是非空字符串（分组切分靠它）")

    prefs = case.get("preferences")
    if prefs is not None:
        if not isinstance(prefs, list):
            problems.append("preferences 必须是数组")
        else:
            for index, item in enumerate(prefs):
                problems.extend(f"preferences[{index}]：{p}" for p in validate_preference(item))

    for index, item in enumerate(case.get("followups") or []):
        if not isinstance(item, dict) or not str(item.get("user") or "").strip():
            problems.append(f"followups[{index}] 必须有 user 文本")

    return problems


def validate_preference(item: Any) -> list[str]:
    """单条偏好。`answer_a`/`answer_b` 是「两个候选答案」，`choice` 是判定。"""
    if not isinstance(item, dict):
        return ["必须是对象"]
    problems: list[str] = []
    choice = str(item.get("choice") or "")
    if choice not in CHOICES:
        problems.append(f"choice 必须是 {'/'.join(CHOICES)}")
    for key in ("answer_a", "answer_b"):
        if not str(item.get(key) or "").strip():
            problems.append(f"{key} 不能为空")
    if not str(item.get("annotator") or "").strip():
        problems.append("缺 annotator（谁标的）")
    if item.get("swapped") is not None and not isinstance(item.get("swapped"), bool):
        problems.append("swapped 必须是布尔值")
    return problems


# --------------------------------------------------------------------- 写
def save(case: dict[str, Any], *, expected_version: int | None = None) -> dict[str, Any]:
    """写一条案例。**版本只增不改**：已存在的版本号不接受覆盖。

    `expected_version` 是调用方读到的当前最高版本；与磁盘不一致就拒绝（并发编辑保护）。
    """
    problems = validate(case)
    if problems:
        return {"ok": False, "why": "；".join(problems), "problems": problems}
    if not enabled():
        return {"ok": False, "why": "案例库关着（eval_cases_enabled）"}

    case_id = str(case["case_id"])
    version = int(case["version"])
    on_disk = versions(case_id)
    latest = on_disk[-1] if on_disk else 0
    if expected_version is not None and int(expected_version) != latest:
        return {"ok": False, "why": f"版本冲突：读到的 {expected_version}，当前 {latest}",
                "conflict": True, "current": latest}
    if version <= latest:
        return {"ok": False, "why": f"版本只增不改：已有 v{latest}，不接受 v{version}",
                "conflict": True, "current": latest}

    payload = dict(case)
    payload.setdefault("status", "ok")
    payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _ensure("cases")
    path = case_path(case_id, version)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("案例写盘失败：%s", exc)
        return {"ok": False, "why": f"写盘失败：{exc}"}
    return {"ok": True, "case_id": case_id, "version": version, "file": str(path)}


# --------------------------------------------------------------------- 导入
def import_jsonl(path: str | Path, *, annotator: str = "", dry_run: bool = False) -> dict[str, Any]:
    """导入两种格式：

    * **旧提取器产物**（`id / ctx / human / source`）：没有目标回复与稳定引用 →
      只进 `eval/incomplete/`，标 `status=incomplete`，**不入正式库**；
    * **案例格式**（含 `case_id`）：逐条 `validate` 后 `save`，按 `case_id+version` 幂等。

    重复执行结果一致：已存在的版本被判「不覆盖」，计入 `skipped`。
    """
    source = Path(path)
    if not source.exists():
        return {"ok": False, "why": f"文件不存在：{source}"}
    rows: list[dict[str, Any]] = []
    try:
        for line in source.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "why": f"读不动：{exc}"}

    imported, skipped, rejected, staged = 0, 0, 0, 0
    problems: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            rejected += 1
            problems.append(f"第 {index + 1} 行不是对象")
            continue
        if "case_id" not in row:
            if dry_run:
                staged += 1
                continue
            got = _stage_legacy(row, annotator=annotator)
            if not got.get("ok"):
                problems.append(got.get("why") or "暂存失败")
            elif got.get("skipped"):
                skipped += 1          # 幂等：同一条旧数据重复导入不再算新增
            else:
                staged += 1
            continue
        got = save(row, expected_version=None)
        if got.get("ok"):
            imported += 1
        elif got.get("conflict"):
            skipped += 1
        else:
            rejected += 1
            problems.append(f"{row.get('case_id')}: {got.get('why')}")
    return {"ok": not rejected, "rows": len(rows), "imported": imported, "skipped": skipped,
            "staged": staged, "rejected": rejected, "problems": problems[:10]}


def _stage_legacy(row: dict[str, Any], *, annotator: str) -> dict[str, Any]:
    """旧格式落进 incomplete/：保留原样 + 一句「缺什么」。"""
    raw_id = str(row.get("id") or "").strip() or f"legacy-{int(time.time() * 1000)}"
    payload = {
        "case_id": _safe_id(raw_id),
        "status": "incomplete",
        "source_type": "chat",
        "provenance": "legacy",
        "why_incomplete": "旧提取格式：缺 message_id / 时间 / 目标回复 / 当时状态",
        "raw": row,
        "annotator": annotator,
        "imported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _ensure("incomplete")
    path = paths()["incomplete"] / f"{_safe_id(raw_id)}.json"
    if path.exists():
        return {"ok": True, "skipped": True}
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "why": f"暂存写盘失败：{exc}"}
    return {"ok": True, "file": str(path)}


# --------------------------------------------------------------------- 偏好
def _pair_key(item: dict[str, Any]) -> str:
    """两个答案归一化后的配对键（与顺序无关）：交换位置仍是同一条偏好。"""
    a = str(item.get("answer_a") or "").strip()
    b = str(item.get("answer_b") or "").strip()
    left, right = sorted((a, b))
    raw = (left + "\x1f" + right).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def append_preference(case_id: str, preference: dict[str, Any]) -> dict[str, Any]:
    """追加一条偏好。

    `choice` 里的 A/B 是**当时界面上左右的位置**，所以判定胜者要看 `swapped`：
    同一配对两次结果指向不同答案 → 记 `uncertain`，不强行折中。
    """
    problems = validate_preference(preference)
    if problems:
        return {"ok": False, "why": "；".join(problems), "problems": problems}
    case = load(case_id)
    if case is None:
        return {"ok": False, "why": f"没有案例 {case_id}"}

    item = dict(preference)
    item.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
    item["pair"] = _pair_key(item)

    history = [x for x in (case.get("preferences") or []) if isinstance(x, dict)]
    winner = _winner(item)
    for old in history:
        if old.get("pair") == item["pair"] and _winner(old) != winner:
            item["conflict_with"] = old.get("at")
            item["choice"] = "uncertain"
            logger.info("偏好位置矛盾，记为 uncertain：%s", case_id)
            break

    case["preferences"] = history + [item]
    case["version"] = int(case["version"]) + 1
    got = save(case, expected_version=int(case["version"]) - 1)
    if got.get("ok"):
        got["preference"] = item
    return got


def _winner(item: dict[str, Any]) -> str:
    """把「界面左/右的选择」归一化成「哪个答案胜」。"""
    choice = str(item.get("choice") or "")
    swapped = bool(item.get("swapped"))
    if choice == "A":
        return "b" if swapped else "a"
    if choice == "B":
        return "a" if swapped else "b"
    return choice  # tie / both_bad / uncertain 原样


# --------------------------------------------------------------------- 分组
def split_of(case: dict[str, Any]) -> str:
    """这条案例属于哪个切分。未标时按 provenance 给默认：synthetic 只进 train。"""
    explicit = str(case.get("split") or "").strip()
    if explicit in SPLITS:
        return explicit
    if str(case.get("provenance") or "") == "synthetic":
        return "train"
    return "train"


def assert_no_leak(cases: list[dict[str, Any]] | None = None) -> list[str]:
    """同一 `split_group` 不得跨切分。返回违规描述（空 = 通过）。"""
    rows = load_all() if cases is None else cases
    seen: dict[str, str] = {}
    problems: list[str] = []
    for case in rows:
        group = str(case.get("split_group") or "").strip()
        split = split_of(case)
        if not group:
            problems.append(f"{case.get('case_id')}：缺 split_group")
            continue
        old = seen.get(group)
        if old and old != split:
            problems.append(f"split_group={group} 同时出现在 {old} 与 {split}")
        else:
            seen[group] = split
    return problems


# --------------------------------------------------------------------- 反馈契约
def feedback_contract() -> dict[str, Any]:
    """聊天反馈的字段契约（#0015 §七）。**只定义，不落库**；落库在 M4。"""
    return {
        "feedback_id": "稳定 ID，由调用方生成",
        "source_refs": "指向消息的引用（message_id + conv + sha256）",
        "target_reply_ref": "被评价的 bot 回复/动作（可为空 = 未定锚）",
        "sender": "身份取自事件，不取文本（缺则 unconfirmed）",
        "persona": "target 的 persona_id + fingerprint",
        "type": "expression / fact / context / memory / tool",
        "polarity": "positive / negative / neutral",
        "scope": "event / scene / global（默认 event）",
        "quote": "用户原话（脱敏后）",
        "confidence": "0..1，规则命中给初值，人工确认后置 1",
        "status": "unconfirmed / confirmed / retracted",
        "supersedes": "同范围内被本条替代的旧 feedback_id",
    }


def validate_feedback(obj: Any) -> list[str]:
    """反馈结构校验。身份缺失不算错，但会记 `unconfirmed`（fail-closed）。"""
    if not isinstance(obj, dict):
        return ["反馈必须是对象"]
    problems: list[str] = []
    if not str(obj.get("feedback_id") or "").strip():
        problems.append("缺 feedback_id")
    if str(obj.get("type") or "") not in ("expression", "fact", "context", "memory", "tool"):
        problems.append("type 必须是 expression/fact/context/memory/tool")
    if str(obj.get("polarity") or "") not in ("positive", "negative", "neutral"):
        problems.append("polarity 必须是 positive/negative/neutral")
    if str(obj.get("scope") or "event") not in ("event", "scene", "global"):
        problems.append("scope 必须是 event/scene/global")
    status = str(obj.get("status") or "")
    if status and status not in ("unconfirmed", "confirmed", "retracted"):
        problems.append("status 必须是 unconfirmed/confirmed/retracted")
    if status == "confirmed" and not str(obj.get("sender") or "").strip():
        problems.append("缺 sender 的反馈不能标 confirmed（fail-closed）")
    return problems


def feedback_path() -> Path:
    return paths()["root"] / "feedback.jsonl"


def save_feedback(obj: dict[str, Any]) -> dict[str, Any]:
    """落一条聊天反馈（#0015 M4）。**只入库，不等于已学会，也不改人格。**

    `status` 缺省是 `unconfirmed`：规则或人工都能写进来，但只有明确了身份与范围的
    才允许升成 `confirmed`（`validate_feedback` 会挡「缺 sender 却标 confirmed」）。
    """
    problems = validate_feedback(obj)
    if problems:
        return {"ok": False, "why": "；".join(problems), "problems": problems}
    if not enabled():
        return {"ok": False, "why": "案例库关着（eval_cases_enabled）"}
    payload = dict(obj)
    payload.setdefault("status", "unconfirmed")
    payload.setdefault("scope", "event")
    payload.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
    path = feedback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        return {"ok": False, "why": f"写盘失败：{exc}"}
    return {"ok": True, "feedback_id": payload.get("feedback_id"),
            "status": payload["status"], "file": str(path)}


def feedback_list(limit: int = 50) -> list[dict[str, Any]]:
    path = feedback_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        logger.warning("反馈读不动：%s", path)
    return rows[-max(1, int(limit)):]


# --------------------------------------------------------------------- 概况
def stats() -> dict[str, Any]:
    """给控制台/报告用的计数，不读大文件。"""
    cases = load_all()
    by_split: dict[str, int] = {}
    by_failure: dict[str, int] = {}
    for case in cases:
        by_split[split_of(case)] = by_split.get(split_of(case), 0) + 1
        kind = str(case.get("failure_type") or "unknown")
        by_failure[kind] = by_failure.get(kind, 0) + 1
    incomplete = len(list(paths()["incomplete"].glob("*.json"))) if paths()["incomplete"].exists() else 0
    return {
        "enabled": enabled(),
        "cases": len(cases),
        "incomplete": incomplete,
        "feedback": len(feedback_list(1000)),
        "by_split": by_split,
        "by_failure": by_failure,
        "leaks": assert_no_leak(cases),
        "files": {k: str(v) for k, v in paths().items()},
    }
