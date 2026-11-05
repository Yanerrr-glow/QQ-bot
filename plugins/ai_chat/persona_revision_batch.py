"""评估修订的隔离计划、完整备份与提交；网络等待期间不修改在线人格。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from . import config, persona, persona_examples, persona_relation, persona_vector
from .persona_workspace import use

FILES = ("surface.txt", "forbidden.txt", "examples.jsonl", "vector.json", "relations.json")


def _hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _json(path: Path, data: Any) -> None:
    _write(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


def snapshot(destination: Path) -> None:
    """冻结有效人格与动态状态，独立于是否存在运行时覆盖。"""
    destination.mkdir(parents=True, exist_ok=True)
    for role in ("base", "surface", "forbidden"):
        (destination / f"{role}.txt").write_text(persona.layer_text(role), encoding="utf-8")
    sources = {"vector.json": config.vector_file_path(), "examples.jsonl": config.examples_file_path()}
    for name in ("relations.json", "vector_state.json", "examples_usage.json"):
        sources[name] = config.persona_data_dir() / name
    for name, source in sources.items():
        if source.exists():
            (destination / name).write_bytes(source.read_bytes())
        else:
            (destination / name).write_text("" if name.endswith("jsonl") else "{}", encoding="utf-8")


def prepare(ops: list[dict[str, Any]]) -> dict[str, Any]:
    """任何一条非法操作使整批失败；只有隔离目录可能写入。"""
    if not isinstance(ops, list) or not 1 <= len(ops) <= 12:
        return {"ok": False, "why": "修订应包含 1..12 条操作"}
    root = config.persona_data_dir()
    rev = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    backup = root / "history" / rev
    before, candidate = backup / "before", backup / "candidate"
    paths = {name: root / name for name in FILES}
    paths["surface.txt"] = config.layer_file_path("surface")
    paths["forbidden.txt"] = config.layer_file_path("forbidden")
    expected = {name: _hash(path) for name, path in paths.items()}
    backup.mkdir(parents=True)
    for name, path in paths.items():
        if path.exists():
            _write(backup / "original" / name, path.read_bytes())
    snapshot(before)
    candidate.mkdir()
    for path in before.iterdir():
        (candidate / path.name).write_bytes(path.read_bytes())
    at = time.time()
    try:
        with use(candidate, at=at):
            text_changes = []
            for index, op in enumerate(ops):
                if not isinstance(op, dict) or op.get("target") == "base":
                    raise ValueError("非法操作或尝试修改底层人格")
                kind, target = op.get("op"), op.get("target")
                if kind in ("replace", "delete") and target in ("surface", "forbidden"):
                    text_changes.append({"layer": target, "find": op.get("find"),
                                         "replace": "" if kind == "delete" else op.get("replace"),
                                         "why": op.get("why", "")})
                    continue
                if kind == "add" and target == "examples":
                    item = {k: op[k] for k in ("id", "polarity", "tags", "ctx", "say", "why",
                                               "trait", "requires", "fit") if k in op}
                    item.setdefault("id", f"ex-{rev}-{index}")
                    # **模型产出的一律标 model**：不许凭模型自报就标成真实聊天原文（#0004）。
                    # 这是写入口，比"要求模型自觉"可靠 —— 提案 LLM 说什么都不影响这个字段。
                    item.setdefault("source", {"source_type": "model",
                                               "origin": "proposal", "rev": rev})
                    result = persona_examples.append(item)
                elif kind == "delete" and target == "examples":
                    result = persona_examples.delete(str(op.get("id") or ""))
                elif kind == "vector" and target in (None, "vector"):
                    result = persona_vector.set_param(str(op.get("axis") or ""), str(op.get("field") or ""), op.get("value"))
                elif kind == "relation":
                    # 人的许可不能由模型根据自然度评分代授；关系只走显式人工入口。
                    raise ValueError("自动修订不能修改个人关系或话题许可")
                else:
                    raise ValueError("不支持的操作")
                if not result.get("ok"):
                    raise ValueError(result.get("why", "操作失败"))
            if text_changes:
                result = persona.apply_revision(text_changes, allow_delete=True, dry_run=True)
                if not result.get("ok"):
                    raise ValueError(result.get("why", "文本校验失败"))
                for role, text in result["planned"].items():
                    if role != "base":
                        (candidate / f"{role}.txt").write_text(text, encoding="utf-8")
        changed = [name for name in FILES if _hash(before / name) != _hash(candidate / name)]
        if not changed:
            raise ValueError("没有实际变化")
        # `backup` / `before` / `candidate` 三个路径**必须写进 manifest**：
        # 提案机制要能从磁盘把这一批重新读出来再提交（`list_proposals()` 扫的就是这些文件），
        # 只靠 `prepare()` 的返回值，进程一重启提案就丢了。
        manifest = {"rev": rev, "pack": config.active_persona_id(), "at": at,
                    "expected": expected, "paths": {k: str(v.resolve()) for k, v in paths.items()},
                    "changed": changed, "ops": ops, "status": "prepared",
                    "backup": str(backup), "before": str(before), "candidate": str(candidate)}
        _json(backup / "batch.json", manifest)
        return {"ok": True, **manifest}
    except (ValueError, TypeError, OSError) as exc:
        _json(backup / "rejected.json", {"why": str(exc)})
        return {"ok": False, "why": str(exc), "backup": str(backup)}


# ----------------------------------------------------------------- 提案（待人工确认）
# 为什么单开一组函数：`revise_from_eval` 现在可以"只提案、不提交"（A+ 方案）。
# 提案必须能从**磁盘**重新读出来（进程重启、隔天再确认都常见），所以状态写在
# `history/<rev>/batch.json` 里，而不是留在内存返回值中。
#
# 状态机：prepared → awaiting_review → committed / rejected / expired / rolled_back

PROPOSAL_STATUS = "awaiting_review"
PROPOSAL_TTL_DAYS = 7
PROPOSAL_KEEP = 20


def _history_root() -> Path:
    return config.persona_data_dir() / "history"


def _read_batch(rev: str) -> dict[str, Any] | None:
    path = _history_root() / str(rev) / "batch.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _set_status(rev: str, status: str, **extra: Any) -> dict[str, Any] | None:
    meta = _read_batch(rev)
    if not meta:
        return None
    meta.update(extra)
    meta["status"] = status
    try:
        _json(_history_root() / str(rev) / "batch.json", meta)
    except OSError:
        return None
    return meta


def mark_awaiting_review(plan: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    """复核通过 → 记为待确认提案。**不写任何线上人格资产**。"""
    rev = str(plan.get("rev") or "")
    if not rev:
        return {"ok": False, "why": "提案缺 rev"}
    meta = _set_status(rev, PROPOSAL_STATUS, verification=verification,
                       proposed_at=time.time())
    if not meta:
        return {"ok": False, "why": "提案落盘失败"}
    return {"ok": True, "rev": rev, "backup": str(plan.get("backup") or ""),
            "status": PROPOSAL_STATUS}


def _summary(meta: dict[str, Any]) -> dict[str, Any]:
    """给列表/回执用的精简视图：**只放人能看懂的东西**，不塞整份 manifest。"""
    ops = meta.get("ops") or []
    items = []
    for op in ops[:6]:
        kind, target = str(op.get("op") or ""), str(op.get("target") or "")
        what = str(op.get("why") or op.get("find") or op.get("id") or "")[:60]
        items.append(f"{kind} {target}：{what}")
    return {
        "rev": meta.get("rev"),
        "at": meta.get("at"),
        "pack": meta.get("pack"),
        "changed": meta.get("changed") or [],
        "items": items,
        "ops": len(ops),
        "passed": bool((meta.get("verification") or {}).get("passed")),
    }


def pending_notice() -> list[dict[str, Any]]:
    """待通知的提案：`awaiting_review` 且还没推过。

    为什么单独一个函数而不是复用 `list_proposals()`：那个返回的是摘要（`_summary`），
    而通知要按 `proposed_at` 排序、还要看 `notified_at`，直接读 meta 更准。
    """
    root = _history_root()
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/batch.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict) or meta.get("status") != PROPOSAL_STATUS:
            continue
        if meta.get("notified_at"):
            continue
        out.append(meta)
    out.sort(key=lambda m: float(m.get("proposed_at") or m.get("at") or 0))
    return out


def mark_notified(revs: list[str], *, at: float | None = None) -> dict[str, Any]:
    """记「已通知过」。**不动 status** —— 通知是消息，不是状态转移。"""
    stamp = float(at if at is not None else time.time())
    done: list[str] = []
    for rev in revs or []:
        meta = _read_batch(str(rev))
        if not meta:
            continue
        meta["notified_at"] = stamp
        try:
            _json(_history_root() / str(rev) / "batch.json", meta)
        except OSError:
            continue
        done.append(str(rev))
    return {"ok": True, "notified": done, "at": stamp}


def list_proposals() -> list[dict[str, Any]]:
    """待确认提案，新的在前。扫 `history/*/batch.json`。"""
    root = _history_root()
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/batch.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(meta, dict) or meta.get("status") != PROPOSAL_STATUS:
            continue
        out.append(_summary(meta))
    out.sort(key=lambda x: float(x.get("at") or 0), reverse=True)
    return out


def commit_proposal(rev: str, *, source: str = "人工确认提案",
                    now: float | None = None) -> dict[str, Any]:
    """人工确认后提交。**提交前仍会重验哈希**（`commit()` 内），人格被改过就拒绝。

    ## 过期由**后端**判定（2026-11-03 按 #0006 审阅加）

    原来这里只查状态 + 哈希，**完全不看时间**：一条 8 天前的提案只要没被
    `expire_old()` 扫到，照样能提交。而"过期"只在前端按 `at + 7 天` 猜的话，
    不同入口（指令 / Web / 桌面端）行为会不一致 —— 所以规则放在这里，前端只展示结果。
    """
    meta = _read_batch(rev)
    if not meta:
        return {"ok": False, "why": f"没有这条提案：{rev}"}
    status = str(meta.get("status") or "")
    if status != PROPOSAL_STATUS:
        return {"ok": False, "why": f"提案状态是 {status}，不是待确认"}
    stamp = time.time() if now is None else float(now)
    age_days = (stamp - float(meta.get("at") or 0)) / 86400
    if age_days > PROPOSAL_TTL_DAYS:
        # 顺手把状态改成 expired：列表接口下次就能显示真实状态，不用前端再猜一遍。
        _set_status(rev, "expired", expired_at=stamp)
        return {"ok": False,
                "why": f"提案已超过 {PROPOSAL_TTL_DAYS} 天（{age_days:.1f} 天），已标记过期"}
    verification = meta.get("verification") or {}
    return commit(meta, source=source, verification=verification)


def reject_proposal(rev: str) -> dict[str, Any]:
    """拒绝一条提案。**只允许拒绝「待确认」的**：原来不校验状态，
    重复拒绝、拒绝已提交的都会返回 `ok`，日志里就再也看不清发生了什么（#0006 审阅）。"""
    current = _read_batch(rev)
    if not current:
        return {"ok": False, "why": f"没有这条提案：{rev}"}
    status = str(current.get("status") or "")
    if status != PROPOSAL_STATUS:
        return {"ok": False, "why": f"提案状态是 {status}，不是待确认"}
    meta = _set_status(rev, "rejected", rejected_at=time.time())
    if not meta:
        return {"ok": False, "why": f"没有这条提案：{rev}"}
    return {"ok": True, "rev": rev, "status": "rejected"}


def proposal_detail(rev: str, *, max_chars: int = 4000) -> dict[str, Any]:
    """一条提案的**完整**内容：ops 原文 + 逐文件 before/after。

    为什么需要它（#0006 审阅）：列表里每条最多 6 项、每项 60 字摘要，
    而提案能改 `forbidden` / `surface` / `vector` —— 只看摘要根本不足以"确认整批修改"。
    `before` / `after` 从提案目录里的 `original/` 与 `candidate/` 读（`snapshot()` 存的）。
    """
    meta = _read_batch(rev)
    if not meta:
        return {"ok": False, "why": f"没有这条提案：{rev}"}
    base = _history_root() / str(rev)
    files: list[dict[str, Any]] = []
    for name in (meta.get("changed") or []):
        row: dict[str, Any] = {"name": str(name)}
        for key, sub in (("before", "original"), ("after", "candidate")):
            path = base / sub / str(name)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                row[key] = ""
                continue
            if len(text) > max_chars:
                row[key] = text[:max_chars]
                row["truncated"] = True
            else:
                row[key] = text
        files.append(row)
    stamp = time.time()
    age_days = (stamp - float(meta.get("at") or 0)) / 86400
    return {
        "ok": True,
        "rev": meta.get("rev"),
        "status": meta.get("status"),
        "at": meta.get("at"),
        "pack": meta.get("pack"),
        "changed": meta.get("changed") or [],
        "ops": meta.get("ops") or [],
        "files": files,
        "verification": meta.get("verification") or {},
        # 过期与否由后端算，前端只展示 —— 同一套规则，不分入口
        "expired": age_days > PROPOSAL_TTL_DAYS,
        "age_days": round(age_days, 1),
    }


def expire_old(*, days: int = PROPOSAL_TTL_DAYS, keep: int = PROPOSAL_KEEP) -> list[str]:
    """过期待确认提案标 `expired`；只保留最近 `keep` 条可提交。返回被过期的 rev。"""
    rows = list_proposals()
    cutoff = time.time() - max(1, int(days)) * 86400
    done: list[str] = []
    for index, row in enumerate(rows):
        rev = str(row.get("rev") or "")
        too_old = float(row.get("at") or 0) < cutoff
        too_many = index >= max(1, int(keep))
        if (too_old or too_many) and rev:
            if _set_status(rev, "expired", expired_at=time.time()):
                done.append(rev)
    return done


def proposals_text() -> str:
    """给指令回执的纯文本。空列表也要说清楚"没有"，不要静默。"""
    rows = list_proposals()
    if not rows:
        return "现在没有待确认的修订提案。"
    lines = [f"待确认的修订提案 {len(rows)} 条（最新的在前）："]
    for row in rows[:5]:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(float(row.get("at") or 0)))
        lines.append(f"\n[{row.get('rev')}] {stamp}｜{row.get('pack')}｜"
                     f"改 {row.get('ops')} 处：{'、'.join(row.get('changed') or [])}")
        for item in (row.get("items") or [])[:4]:
            lines.append(f"  · {item}")
        lines.append(f"  复核：{'通过' if row.get('passed') else '未通过'}")
    lines.append("\n确认：/人设 提案 采纳 <rev>　丢弃：/人设 提案 拒绝 <rev>")
    return "\n".join(lines)


def restore(backup: Path, *, check_current: bool = True) -> bool:
    """恢复完整批次；默认拒绝覆盖提交后其他入口做出的修改。"""
    try:
        meta = json.loads((backup / "batch.json").read_text(encoding="utf-8"))
        targets = {name: Path(meta["paths"][name]) for name in meta["changed"]}
        if set(targets) - set(FILES):
            return False
        if check_current and any(_hash(path) != meta.get("after", {}).get(name) for name, path in targets.items()):
            return False
        for name, path in targets.items():
            original = backup / "original" / name
            if meta["expected"][name] is None:
                path.unlink(missing_ok=True)
            else:
                _write(path, original.read_bytes())
        meta["status"] = "rolled_back"
        _json(backup / "batch.json", meta)
        config.reset_persona_cache()
        return True
    except (OSError, ValueError, KeyError):
        return False


def commit(plan: dict[str, Any], *, source: str, verification: dict[str, Any]) -> dict[str, Any]:
    """无 await 的乐观提交；包或被编辑文件变化时拒绝覆盖。"""
    if not verification.get("passed"):
        return {"ok": False, "why": "复核未通过，未修改在线人格", "verification": verification}
    backup = Path(plan["backup"])
    if config.active_persona_id() != plan["pack"]:
        return {"ok": False, "why": "评估期间已切换人格包"}
    paths = {name: Path(path) for name, path in plan["paths"].items()}
    if any(_hash(paths[name]) != plan["expected"][name] for name in FILES):
        return {"ok": False, "why": "评估期间人格数据已变化，请重新生成提案"}
    data = persona._load_revisions()
    old_index = persona._revisions_path().read_bytes() if persona._revisions_path().exists() else None
    try:
        for name in plan["changed"]:
            _write(paths[name], (Path(plan["candidate"]) / name).read_bytes())
        after = {name: _hash(paths[name]) for name in plan["changed"]}
        _json(backup / "batch.json", {**plan, "after": after, "status": "committed", "verification": verification})
        data.setdefault("items", []).append({"rev": plan["rev"], "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source, "changes": plan["ops"], "written": plan["changed"],
            "backup": str(backup.relative_to(config.persona_data_dir())), "verification": verification})
        persona._save_revisions(data)
        config.reset_persona_cache()
        return {"ok": True, "rev": plan["rev"], "written": plan["changed"], "verification": verification}
    except OSError as exc:
        recovered = restore(backup, check_current=False)
        try:
            if old_index is None:
                persona._revisions_path().unlink(missing_ok=True)
            else:
                _write(persona._revisions_path(), old_index)
        except OSError:
            recovered = False
        return {"ok": False, "why": f"写入失败，回滚{'完成' if recovered else '未完成，保留备份'}：{exc}", "backup": str(backup)}
