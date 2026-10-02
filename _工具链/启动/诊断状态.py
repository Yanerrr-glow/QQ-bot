"""诊断：人格/记忆/图片策略现在到底是什么状态（不连 QQ、不调模型）。

## 为什么需要它

有三类问题在群里**看不出来原因**，因为表现都是"它好像没反应 / 还是老样子"：

1. 改了 `persona.txt` 但没生效 —— 因为 `.env` 里的 `AI_CHAT_SYSTEM_PROMPT` 把它盖掉了，
   或者 `AI_CHAT_PERSONA_FILE` 被写成了空串；
2. `/图 忽略` 之后还在收图 —— 全局策略把会话策略盖了，或者落盘文件没写好；
3. 明明记得的事它想不起来 —— 记忆条数超了上限被淘汰，或者 `memory_enabled` 是关的。

这个脚本把三层的**当前真实状态**一次印出来，省得去猜。

用法：
    .\\.venv\\Scripts\\python.exe '_工具链\\启动\\诊断状态.py'
退出码：恒为 0（这是只读诊断，不参与验收）。
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

# **项目根从脚本自身位置向上探测**，不写死层级。
# 原来写的是 `.parent.parent` —— 那假定脚本就在 `_工具链\` 下一层；2026-10-02
# 维护脚本按用途分组（启动 / 维护 / 发布）之后就多了一层，于是 ROOT 变成 `_工具链\`：
# `.env` 报"不存在"、`data/runtime/` 全落空、每一节都显示"还没这个文件"。
# 判据用"含 bot.py 且含 persona/"这一层（与工作区的自定位约定、与导出脚本一致）。
ROOT = pathlib.Path(__file__).resolve().parent
while ROOT.parent != ROOT and not (ROOT / "bot.py").is_file():
    ROOT = ROOT.parent
os.chdir(ROOT)

# 优先用 nonebot 的 Config 读 .env —— 跟机器人启动时走同一条路径，
# 免得"我明明改了 .env"和"程序读到的不是这个"各说各话。
# 没装 nonebot 时退回 dotenv 或自带的极简解析，保证在任何解释器下都能跑。
_env_file = ROOT / ".env"


class _Parsed(dict):
    """够用的 .env 容器：`getattr(cfg, 'ai_chat_xxx')` 取键，键名小写。"""

    def __getattr__(self, name):
        return self.get(name)


def _load_env_fallback() -> _Parsed:
    out = _Parsed()
    text = ""
    if _env_file.exists():
        text = _env_file.read_text(encoding="utf-8", errors="replace")
    try:  # 有 dotenv 就交给它（NoneBot2 的依赖，装了才有）
        from dotenv import dotenv_values

        for key, value in dotenv_values(_env_file).items():
            out[key.lower()] = value
        return out
    except ImportError:
        pass
    # 极简解析：KEY=VALUE，忽略注释与引号
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        out[key.strip().lower()] = value.strip().strip("'\"")
    return out


_parser = "自带解析"
try:
    from nonebot.config import Config

    cfg = Config(_env_file=str(_env_file) if _env_file.exists() else None)
    _parser = "nonebot.Config（与启动一致）"
except ImportError:
    cfg = _load_env_fallback()
    try:
        import dotenv  # noqa: F401

        _parser = "python-dotenv"
    except ImportError:
        pass

line = "─" * 68


def head(title: str) -> None:
    print(f"\n{line}\n{title}\n{line}")


def kv(key: str, value: object, note: str = "") -> None:
    tail = f"    {note}" if note else ""
    print(f"  {key:<28} {value}{tail}")


print(f"项目根：{ROOT}")
print(f".env ：{'已读取' if _env_file.exists() else '**不存在**'}（解析方式：{_parser}）")

# ------------------------------------------------------------------ 时间
head("① 时间：现在几点、准不准")


def _time_report_without_plugin() -> None:
    """不依赖 nonebot 的退化版本：直接读 .env 与 data/runtime/clock.json。

    为什么要有这一条：诊断脚本最常见的用法就是**在装依赖之前 / 装依赖失败之后**
    排查问题，而 `clock.py` 顶部会 import `settings` → `nonebot`。
    所以这里不 import 插件，改成自己读那两个文件里的关键字段。
    """
    import json as _json

    now = time.time()
    kv("系统时钟", time.strftime("%Y-%m-%d %H:%M:%S %Z%z", time.localtime(now)))

    _log_dir = pathlib.Path(str(getattr(cfg, "ai_chat_log_dir", "data/runtime") or "data/runtime"))
    if not _log_dir.is_absolute():
        _log_dir = ROOT / _log_dir
    if _log_dir.resolve() == (ROOT / "data").resolve():
        _log_dir = _log_dir / "runtime"
    _clock_file = _log_dir / "clock.json"

    _raw_flag = str(getattr(cfg, "ai_chat_ntp_enabled", "true") or "true").strip().lower()
    kv("NTP 开关", "**关**" if _raw_flag in ("false", "0", "no", "off") else "开（默认）")
    _servers = getattr(cfg, "ai_chat_ntp_servers", None)
    if _servers:
        kv("配置的服务器", str(_servers)[:70])

    if not _clock_file.exists():
        print("  还没有校准记录（data/runtime/clock.json 不存在）")
        print("  → 机器人启动后会自动同步一次；也可以发 /时间 校准 立刻对时")
    else:
        try:
            _saved = _json.loads(_clock_file.read_text(encoding="utf-8"))
            _off = float(_saved.get("offset") or 0.0)
            kv("上次校准的偏移", f"{_off:+.3f} 秒"
               + ("（系统时钟偏差，已按此校正）" if _off else "（与标准时间一致）"))
            if _saved.get("server"):
                kv("上次来源", f"{_saved['server']}（stratum {_saved.get('stratum')}，"
                              f"{_saved.get('updated_at', '')}）")
            if _off:
                kv("校准后的当前时间",
                   time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + _off)))
        except (OSError, ValueError) as _exc:
            kv("校准文件读不动", f"{type(_exc).__name__}: {_exc}")

    _tz = time.strftime("%Z%z", time.localtime(now))
    if "+0800" not in _tz:
        print(f"  **注意时区是 {_tz}，不是 +0800** —— 定时问候会在错误的钟点触发")
    print("  （本机没装 nonebot，以上是直接读文件的结果）")


try:
    # 复用插件里的 clock 模块，跟机器人走同一条逻辑（含 NTP 校准）
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "plugins"))
    from ai_chat import clock as _clock  # type: ignore

    _st = _clock.status()
    _now = _clock.now()
    _raw = time.time()
    kv("校准后的当前时间", time.strftime("%Y-%m-%d %H:%M:%S %Z%z", time.localtime(_now)))
    kv("系统时钟        ", time.strftime("%Y-%m-%d %H:%M:%S %Z%z", time.localtime(_raw)))
    kv("系统时钟偏差", f"{_st['offset_seconds']:+.3f} 秒"
       + ("（已按此校正）" if _st["offset_seconds"] else "（无需校正）"))
    kv("NTP 开关", "开" if _st["enabled"] else "**关**")
    kv("校准状态", _st["status"])
    if _st["server"]:
        kv("上次来源", f"{_st['server']}（stratum {_st['stratum']}，"
                      f"往返 {_st['delay_ms']:.0f} ms，{_st['synced_at']}）")
    if _st["stale"]:
        print("  **距上次同步较久**，可能已经过期")
    if _st["last_error"]:
        kv("最近一次失败", _st["last_error"][:70])
    _tz = time.strftime("%Z%z", time.localtime(_now))
    if "+0800" not in _tz:
        print(f"  **注意时区是 {_tz}，不是 +0800** —— 定时问候会在错误的钟点触发")
    print("  （想立刻对一次时：控制台「时间校准」组，或群里发 /时间 校准）")
except ImportError:
    _time_report_without_plugin()
except Exception as _exc:  # noqa: BLE001 - 诊断脚本不该因为一处失败就中断
    kv("时间状态读取失败", f"{type(_exc).__name__}: {_exc}")

# ------------------------------------------------------------------ 人设
head("② 人格三层：谁写的、写在哪、有没有变")
# 2026-09-25 人格分层：原来是「人设正文（一个文件）+ 运行时槽位」，
# 现在是三个文件、三层权限。这一节要把三层的**字数与文件**都打出来 ——
# 判断"它现在是什么性格"取决于三个文件，只说一个来源是不够的。
_logdir0 = pathlib.Path(str(getattr(cfg, "ai_chat_log_dir", "data/runtime") or "data/runtime"))
if not _logdir0.is_absolute():
    _logdir0 = ROOT / _logdir0
if _logdir0.resolve() == (ROOT / "data").resolve():
    _logdir0 = _logdir0 / "runtime"


def _layers_of_cfg() -> list[tuple[str, str, str]]:
    """(层名, 环境变量名, 默认文件名)。

    默认值是**包内文件名**：没有显式配置时，这三层来自当前人格包
    （`persona/packs/<id>/`）。人格包本身的判定见下一节的 `_pack_report()`。
    """
    return [
        ("底层人设（它是谁）", "ai_chat_persona_file", "base.txt"),
        ("禁止事项（铁律）", "ai_chat_forbidden_file", "forbidden.txt"),
        ("表层人设（怎么说话）", "ai_chat_surface_file", "surface.txt"),
    ]


def _active_pack(root: pathlib.Path) -> str:
    """当前激活的包：运行时标记 > 注册表 > 唯一一个启用的包（与 packs.py 同序）。"""
    packs_dir = root / "persona" / "packs"
    marker = _logdir0 / "persona" / "_active"
    for cand in (marker, root / "persona" / "_registry.json"):
        try:
            if cand.name == "_active":
                got = cand.read_text(encoding="utf-8").strip()
            else:
                import json as _json
                got = str((_json.loads(cand.read_text(encoding="utf-8")) or {}).get("active") or "").strip()
        except (OSError, ValueError):
            continue
        if got and (packs_dir / got).is_dir():
            return got
    try:
        names = sorted(n for n in os.listdir(packs_dir)
                       if not n.startswith(("_", ".")) and (packs_dir / n).is_dir())
    except OSError:
        return ""
    return names[0] if len(names) == 1 else ""


_PACK = _active_pack(ROOT)
_PACKS_DIR = ROOT / "persona" / "packs"
try:
    _PACK_LIST = sorted(n for n in os.listdir(_PACKS_DIR)
                        if not n.startswith(("_", ".")) and (_PACKS_DIR / n).is_dir())
except OSError:
    _PACK_LIST = []
kv("当前人格包", f"{_PACK or '（无）'}"
                 + (f"（共 {len(_PACK_LIST)} 个可用：{'、'.join(_PACK_LIST)}）" if _PACK_LIST else ""))
if _PACK:
    kv("包目录", _PACKS_DIR / _PACK)
    kv("运行数据目录", _logdir0 / "persona" / _PACK)
else:
    print("  ⚠ 没有可用的人格包 —— 只有 AI_CHAT_SYSTEM_PROMPT 兜底。"
          "照 persona/_TEMPLATE/README.md 建一个包")


for _label, _env, _default in _layers_of_cfg():
    _rawv = getattr(cfg, _env, None)
    if _rawv is None:
        # 没显式配 → 这一层来自当前人格包
        _name = str(_PACKS_DIR / _PACK / _default) if _PACK else ""
        _note = "（.env 没写 → 用当前人格包里的 %s）" % _default
    elif str(_rawv).strip() == "":
        _name, _note = "", "（**被写成空串 → 这一层不读文件**）"
    else:
        _name, _note = str(_rawv).strip(), "（.env 显式指定，绕过人格包）"
    print(f"  · {_label}")
    kv("    文件", _name or "（无）", _note)
    if not _name:
        continue
    _p = pathlib.Path(_name)
    if not _p.is_absolute():
        _p = ROOT / _p
    if not _p.exists():
        kv("    状态", "**文件不存在**")
        continue
    try:
        _t = _p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        kv("    状态", "**不是 UTF-8，会被跳过**")
        continue
    except OSError as exc:
        kv("    状态", f"**读不动**：{exc}")
        continue
    kv("    状态", f"{len(_t)} 字", " ".join(_t.split())[:34] + "…")

print("  → 三层的顺序就是权限：底色 → 铁律 → 表层，表层压不过上面两层")
print("  → **改人格直接编辑包里的三个文件**；进程不用重启（每轮现读），"
      "但**容器部署要重建镜像**才生效（persona/ 是 COPY 进镜像的，不是卷）")
print("  → 切人格：群里 /人设 切换 <id>，或控制台「人格」页；**立即生效**，不用重启")
print("  → 聊天里的 /人设、/风格 只能「看」和「撤回」，改不了人设内容（这是刻意的）")

_sysp = getattr(cfg, "ai_chat_system_prompt", None)
if _sysp not in (None, ""):
    print(f"  AI_CHAT_SYSTEM_PROMPT 也配了（长度 {len(str(_sysp))}）")
    print("    它只在**底层人设文件为空**时才会被用到")

# ------------------------------------------------------------------ 人格自动改动
head("③ 人格自我迭代（表层是唯一会自动变的一层）")
_iter = str(getattr(cfg, "ai_chat_persona_iter_enabled", "")).lower() not in ("false", "0", "no")
kv("自我迭代开关", "开" if _iter else "关（AI_CHAT_PERSONA_ITER_ENABLED=false）")
# **按包分开**：包化之后这些账本落在 `data/runtime/persona/<包>/` 下，
# 找一个固定路径是找不到的（那正是"包化后旧的排查脚本读不到东西"的原因）。
_pack_stage = (_logdir0 / "persona" / _PACK) if _PACK else (_logdir0 / "persona")
_log = _pack_stage / "changelog.json"
kv("变更日志", _log)
if _log.exists():
    import json

    try:
        data = json.loads(_log.read_text(encoding="utf-8"))
        items = data.get("items") or []
        added = [x for x in items if x.get("action") == "added"]
        rejected = [x for x in items if x.get("action") == "rejected"]
        undone = [x for x in items if x.get("action") == "undone"]
        kv("写入 / 拦下 / 撤回", f"{len(added)} / {len(rejected)} / {len(undone)}")
        if rejected:
            print("  最近被**闸门拦下**的（说明它想改什么、碰到了哪条铁律）：")
            for x in rejected[-3:]:
                print(f"    ⛔ {str(x.get('text', ''))[:40]}")
                print(f"       理由：{str(x.get('reason', ''))[:70]}")
        if added:
            print("  最近**写进表层**的：")
            for x in added[-3:]:
                print(f"    ✅ {str(x.get('text', ''))[:52]}   {str(x.get('at', ''))[:16]}")
    except (OSError, ValueError) as exc:
        print(f"  **变更日志损坏**：{exc}")
else:
    print("  （还没有这个文件 → 自我迭代还没改过任何东西）")

_signals = _pack_stage / "signals.json"
if _signals.exists():
    import json

    try:
        data = json.loads(_signals.read_text(encoding="utf-8"))
        kv("人设信号账本", f"{len(data.get('items') or [])} 条"
                            "（他提过的说话方式要求；只记账不改人设）")
    except (OSError, ValueError):
        kv("人设信号账本", "**读不动**")
else:
    print("  （还没有信号账本 → 他还没提过说话方式上的要求）")

# 旧文件提醒：分层之后它们不再被读，但留在原地容易让人以为还在生效
for _old, _why in (
    (_logdir0 / "persona_directives.json", "运行时槽位机制已删除"),
):
    if _old.exists():
        print(f"  ⚠ 发现旧文件 {_old.name}（{_why}，现在**不再被读取**）")
        print("    可以照着 分析记录/人设历史/原槽位映射.md 把要留的内容手工搬进三个新文件")

# ------------------------------------------------------------------ 记忆
head("④ 长期记忆")
_mem = _logdir0 / "memories.json"
_raw = getattr(cfg, "ai_chat_memory_enabled", None)
kv("AI_CHAT_MEMORY_ENABLED", "（未设）" if _raw in (None, "") else _raw,
   "未设时按默认 True（开着）")
if _mem.exists():
    import json

    try:
        data = json.loads(_mem.read_text(encoding="utf-8"))
        facts = data.get("facts") or []
        events = data.get("events") or []
        profile = data.get("profile") or {}
        kv("事实条数", len(facts))
        kv("群事件条数", len(events))
        kv("人物画像", len(profile))
        protected = sum(1 for f in facts if f.get("protected"))
        kv("受保护条数", protected)
        print("  最近 5 条：")
        for f in sorted(facts, key=lambda x: float(x.get("ts", 0)), reverse=True)[:5]:
            mark = "🔒" if f.get("protected") else "  "
            print(f"   {mark} #{f.get('id')} {str(f.get('text', ''))[:52]}")
    except (OSError, ValueError) as exc:
        print(f"  **文件损坏**：{exc}")
else:
    print("  （还没有这个文件 → 还没开始积累记忆）")

# 损坏留证：只要目录里有 .corrupt-*，说明曾经有一份记忆库/聊天记录被判为损坏。
# 这时机器人会进入**只读模式**（不写盘），现象是"它答应记住却总是忘"——
# 所以必须在这里显眼提示，否则排查时根本想不到是这件事。
_corrupts = sorted(list(_logdir0.glob("*.corrupt-*")))
if _corrupts:
    print("\n  ⚠ 发现损坏留证文件（说明曾被判为损坏，对应模块当时处于只读，不会写盘）：")
    for p in _corrupts[:8]:
        print(f"    · {p.name}（{p.stat().st_size} 字节；可手工修好后改名回去，或直接删掉＝放弃这份数据）")
    print("    → 修好或移走后重启机器人即可恢复写入")

# ------------------------------------------------------------------ 抽取水位线 / 会话摘要
head("⑤ 记忆抽取水位线 与会话摘要（2026-09-25 新增）")
_ext = _logdir0 / "memory_extract_state.json"
if _ext.exists():
    import json

    try:
        data = json.loads(_ext.read_text(encoding="utf-8"))
        convs = data.get("conv") or {}
        kv("已记录进度的会话数", len(convs))
        kv("今天已提炼（条消息）", data.get("day_messages", 0), f"（统计日：{data.get('day', '—')}）")
        kv("累计提炼（条消息）", data.get("total", 0))
        print("  各会话水位线（最近 6 个）：")
        items = sorted(convs.items(), key=lambda kv_: str((kv_[1] or {}).get("updated_at") or ""),
                       reverse=True)[:6]
        for name, info in items:
            print(f"    · {name}: 抽到 #{info.get('last_id')}（该会话累计 {info.get('total', 0)} 条）")
    except (OSError, ValueError) as exc:
        print(f"  **文件损坏**：{exc}")
        print("    （下次抽取会从「最近一批」重来 —— 可能产生少量重复条目，但不会漏记录）")
else:
    print("  （还没有这个文件 → 抽取还没跑过，或还没升级到带水位线的版本）")

_sum = _logdir0 / "session_summaries.json"
if _sum.exists():
    import json

    try:
        data = json.loads(_sum.read_text(encoding="utf-8"))
        convs = data.get("conv") or {}
        total = sum(len(v) for v in convs.values() if isinstance(v, list))
        kv("摘要轮次总数", total)
        kv("覆盖会话数", len(convs))
        for name, items in list(convs.items())[:3]:
            if not isinstance(items, list) or not items:
                continue
            last = items[-1]
            print(f"    · {name} 最新一轮（第 {last.get('session')} 轮，"
                  f"{last.get('messages')} 条）：{str(last.get('text', ''))[:56]}")
    except (OSError, ValueError) as exc:
        print(f"  **文件损坏**：{exc}")
else:
    print("  （还没有这个文件 → 还没有跨过会话边界，或会话摘要关着）")

# ------------------------------------------------------------------ 图片策略
head("⑥ 图片策略（会话级 / 全局）")
_st = _logdir0 / "conv_state.json"
if _st.exists():
    import json

    try:
        data = json.loads(_st.read_text(encoding="utf-8"))
        kv("全局策略", data.get("global_mode", "normal"))
        convs = data.get("convs") or {}
        if not convs:
            print("  （所有会话都是默认策略）")
        for conv, item in convs.items():
            ignored = len(item.get("ignore") or [])
            print(f"  · {conv}: mode={item.get('mode') or '（跟随全局）'}  点名不存 {ignored} 张")
    except (OSError, ValueError) as exc:
        print(f"  **文件损坏**：{exc}")
else:
    print("  （还没有这个文件 → 所有会话都是默认策略）")

# ------------------------------------------------------------------ 会话
head("⑦ 聊天记录概览")
_logdir = _logdir0
kv("目录", _logdir)
if _logdir.exists():
    logs = sorted(_logdir.glob("chatlog_*.json"))
    if not logs:
        print("  （还没有任何聊天记录）")
    for p in logs[:12]:
        try:
            import json

            raw = json.loads(p.read_text(encoding="utf-8"))
            msgs = raw.get("messages") or []
            unread = sum(1 for m in msgs if not m.get("read"))
            kv(p.stem, f"{len(msgs)} 条 / 未读 {unread} / 第 {raw.get('session')} 轮")
        except (OSError, ValueError):
            kv(p.stem, "**读不动**")
else:
    print("  （目录不存在）")

print(f"\n{line}\n")
