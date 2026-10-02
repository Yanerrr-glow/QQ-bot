"""实时指令：把「聊天里当场提的要求」变成真的会生效的动作。

## 为什么要这一层

改造前的机器人**只能被配置**：想改行为就得动 `persona.txt` / `settings.json` / Web 控制台，
然后重启。群里说「不要保存这张图片」，它只会当成一句闲聊回你一句「好呀」——
**嘴上答应，行为不变**。这就是"无法处理实时对话中的要求"的根因：
它没有把"话"翻译成"状态变更"的那一层。

本模块就是那一层。命中后**先改状态、再回话**，所以它答应的事立刻是真的。

## 主通道：斜杠指令（确定性优先）

```
/图 忽略 | /图 只看 | /图 开 | /图 状态 | /图 撤销 | /图 全局忽略
/风格 简短 | /风格 叫哥哥 | /风格 别叫我主人 | /风格 列表 | /风格 清
/记忆 存 <文本> | /记忆 列表 | /记忆 找 <词> | /记忆 忘 <id> | /记忆 清
/人设 看 | /人设 可调 | /人设 <键> <值> | /人设 清 <键> | /人设 贴 <PATCH 文本>
/机制 [图|记忆|记忆|风格|触发|全部]
/模型 [名字]
```

指令**由代码直接执行，不经过模型判断** —— 所以 100% 确定、零 token、零误判，
而且改完立即回执（回执本身也走一次模型，带人设口吻）。

## 副通道：自然语言兜底（保守）

用户明确选了"显式斜杠指令"，但 `/图 忽略` 这种写法对随口说话的场景不友好，
所以另有一小组**高置信度**正则，只在私聊主人、或被 @ / 引用机器人时生效，
且每个意图都必须同时命中"动作词 + 对象词"才动作：

* 「不要保存这张图片」「这张别存了」→ 图片入库存策略置为忽略
* 「以后别叫我主人」「叫我哥哥」→ 人设槽位「称呼」
* 「详细告诉我你的图片使用机制」→ 机制说明

自然语言兜底**永远不会**触发「清空记忆」「改模型」这类破坏性动作 ——
那种事只认斜杠指令。宁可少懂一点，也不要误解一句话就把记忆删了。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any


from . import (
    attention, clock, config, dsh_bridge, identity, memory, mode, persona, search, settings,
    signals, state, stickers,
)

logger = logging.getLogger("ai_chat.instructions")

# 中文全角斜杠也认（手机上很容易打成 ／）
_SLASH = "/／"
# 允许 /记忆 列表 这类无 @ 的写法被 @ 前缀挡住，所以前缀统一在这一层剥
_PREFIX_STRIP = re.compile(r"^[\s,，。：:]+")

# 图片策略的取值与别名。左值是真值，右值是所有接受的写法。
_IMG_ALIAS: dict[str, tuple[str, ...]] = {
    "normal": ("正常", "开", "恢复", "自动", "默认", "on"),
    "ignore": ("忽略", "别存", "不要存", "不存", "别保存", "不要保存", "off", "关", "停"),
    "only": ("只看", "只看不存", "只读", "不存只聊", "view"),
    "off": ("完全不看", "不看图", "免打扰", "静音"),
}


def _norm(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _parse_index(arg: str) -> int | None:
    """把 `/人设 采纳 <序号>` 的参数解成 1-based 序号；空则返回 None（= 最早那条）。

    解析失败也返回 None 而不是报错 —— 调用方会落到"取最早那条"，
    对"我只想采纳一条"的场景这是合理兜底，也比抛异常好。
    """
    text = _norm(arg)
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _resolve_img(word: str) -> str:
    word = _norm(word).lower()
    for key, aliases in _IMG_ALIAS.items():
        if word == key or word in aliases:
            return key
    return ""


def _is_command(text: str, prefix: str = "") -> str:
    """返回去掉斜杠后的指令正文；不是指令则返回空串。"""
    raw = _PREFIX_STRIP.sub("", str(text or ""))
    if prefix and raw.startswith(prefix):
        raw = raw[len(prefix) :].lstrip()
    if not raw or raw[0] not in _SLASH:
        return ""
    return raw[1:].strip()


# --------------------------------------------------------------------- 结果
@dataclass
class Action:
    """一条指令的执行结果。

    * `reply` 非空 → 直接把它发出去，**不再过模型**（指令回执要准，不要被文风带偏）；
    * `reply` 为空 → 把 `prompt_note` 交给模型，让它用人设口吻回话。
    """

    kind: str = ""
    ok: bool = True
    reply: str = ""
    prompt_note: str = ""
    stop: bool = False  # True = 到此为止，不进模型
    handled: bool = False  # 是否真的识别成指令
    clear_history: bool = False
    effect: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "handled": self.handled,
            "effect": self.effect,
            "stop": self.stop,
        }


def _say(kind: str, text: str, **effect: Any) -> Action:
    return Action(kind=kind, ok=True, reply=text, stop=True, handled=True, effect=effect)


def _deny(kind: str, text: str) -> Action:
    return Action(kind=kind, ok=False, reply=text, stop=True, handled=True, effect={"denied": True})


def _note(kind: str, text: str, **effect: Any) -> Action:
    """改完状态后，让模型用人设口吻把结果说出来（比一句死板的回执自然）。"""
    return Action(kind=kind, ok=True, prompt_note=text, handled=True, effect=effect)


def _reject(kind: str, text: str, **effect: Any) -> Action:
    """**没办成**，但要用人设口吻回话。

    `_say` 的语义是"办成了，这是回执"，所以拒绝类结果不该用它 ——
    虽然群里看着都是"她说了一句话"，但 `ok` 决定日志里能否把
    "她答了个没用的话"和"她拒绝了"分开。这是踩过两次的坑
    （先是 `/时间` 认不出参数时报成功，再是 `/风格` 的重复/超限）。
    """
    return Action(kind=kind, ok=False, prompt_note=text, handled=True, effect=effect)


def _reject_say(kind: str, text: str, **effect: Any) -> Action:
    """**没办成，而且必须原样直出**（不经模型改写）。

    为什么需要第三种拒绝出口：`_reject` 走 `prompt_note`（让模型用人设口吻转述），
    `_deny` 虽然直出但语义是"权限不足"。而像 `/dsh` 这种**定位就是不经模型**的指令，
    用法错误必须给出确定文本 —— 让模型改写一遍，"可用动作"就说不清了。
    """
    return Action(kind=kind, ok=False, reply=text, stop=True, handled=True,
                  effect={**effect, "rejected": True})


# --------------------------------------------------------------------- 主入口
async def parse(
    text: str,
    *,
    conv: str,
    is_master: bool,
    prefix: str = "",
    allow_natural: bool = False,
    bot: Any = None,
    image_segments: list[dict] | None = None,
) -> Action:
    """解析一条消息。

    先试斜杠指令（任何人可用，但破坏性动作限主人），再试自然语言兜底
    （仅在 `allow_natural` 为真时，即私聊主人或被明确叫到时）。

    **是 async 的**：「不要保存这张图片」需要把已经入库的那张真的删掉（磁盘 IO），
    指令回执必须等它落定才能说"好"，否则就成了改造前那种"嘴上答应、事没办"。

    `bot` / `image_segments` 是给 `/头像` 这类**要动 QQ 侧状态**的指令用的：
    只有它们需要真去调 OneBot 接口、或从消息里取图片字节。默认 None 表示
    "这个调用方没有这些上下文" —— 那种情况下这类指令会明确回一句"这里用不了"，
    **不会假装成功**（`/人设` 那次的教训：嘴上答应、事没办是最坏的结果）。
    """
    body = _is_command(text, prefix)
    if body:
        ctx = {"bot": bot, "images": list(image_segments or [])}
        action = await _dispatch_command(body, conv=conv, is_master=is_master, ctx=ctx)
        # 斜杠指令也是"他的显式要求"，一样记账 —— 第 2 步统计"同类要求出现过几次"时，
        # 「/风格 简短」和「你能不能短一点」应该算同一类。
        # `executed=True`：他敲指令的那一刻就已经按他说的改了（权限不够被拒的情况另算，
        # 那种会记成 `executed=False`，因为 `_dispatch_command` 返回的是拒绝动作）。
        _head, _, _rest = body.partition(" ")
        # **只扫参数、而且按词扫**：拿整串命令扫会误报 ——
        # 实测 `/人设 回复长度 短句` 里的槽位名「回复长度」被 `length_long` 的
        # `(?:详细|长)` 命中，于是账本里多出一条语义相反的 `length_long`。
        # 逐词扫还顺手解决了另一个问题：命令参数是用空格分隔的，逐词比整串更准。
        for _tok in _rest.split():
            _record_style_signal(
                _tok,
                conv=conv,
                executed=_style_action_applied(action),
                kind_hint=_head,
            )
        return action
    if allow_natural and settings.get("image_policy_enabled"):
        # 「不要保存这张图片」这类自然语言要求，本质是改图片策略 ——
        # 所以它跟 /图 共用同一个总开关，关掉就都不生效。
        # 注意：**记账独立于这个开关**，而且 `executed` 要按**动作结果**判 ——
        # 这里先执行、再记账（原来写成"先记后执行、executed 用默认 False"，
        # 于是「正经点」这类**确实改了人设**的要求被记成没执行过，实测踩到过）。
        action = await _match_natural(text, conv=conv, is_master=is_master)
        _record_style_signal(text, conv=conv, executed=_style_action_applied(action))
        return action
    # 走到这里说明要么不是在跟它说话（`allow_natural=False`），要么图片策略关着。
    # **仍然记账**：这是他"说过但没被执行"的偏好 —— 正是这一步要收集的东西。
    _record_style_signal(text, conv=conv)
    return Action()


# 哪些动作 kind 属于"改了说话方式"。用于判断信号是否**真的被执行**。
#
# **人格分层后它只剩空集的意义**：能改人设的 kind
# （`style_call` / `style_no_call` / `style_length` / `style_formal` / `style` / `persona`）
# 全都不存在了，所以 `_style_action_applied()` 恒为 False。
# 保留这个函数而不删，是为了让"记账时怎么判定 executed"只有一个定义处 ——
# 将来若真又有路径能改人设，改这里一处即可。
#
# 特别注意**不能**把 `"persona"` 放回来：`persona_change_request` 返回的
# `Action(kind="persona", ok=True)` 只是**一句"改人格要去编辑文件"的回答**，
# 没有任何写动作。早先它在表里，于是被记成 `executed=True`（实测踩到）。
_STYLE_KINDS: dict[str, str] = {}


def _style_action_applied(action: Action) -> bool:
    """这个动作有没有真的改掉一个说话方式槽位。"""
    return bool(action.ok) and action.kind in _STYLE_KINDS


# --------------------------------------------------------------------- 记账
def _record_style_signal(
    text: str, *, conv: str, executed: bool = False, kind_hint: str = ""
) -> int:
    """识别并记下「他在纠正我的说话方式」。**不改任何人设，只记账。**

    这是路线 A 第 1 步的全部内容（见 `分析路径/人设自我迭代的实现路线`）：
    以前 `command_natural` 关着时，他的偏好**完全丢掉**；现在至少进账本。
    成本：一次正则扫描，本地、0 token。**任何异常都不能影响回复。**
    """
    try:
        if not settings.get("persona_signal_enabled"):
            return 0
        raw = _norm(text)
        if not raw or len(raw) > 60:
            return 0
        # 与 `_match_natural` 同一道守卫：反问句不算要求
        if any(g in raw for g in _NL_GUARD):
            return 0
        found = signals.detect(raw)
        if not found:
            return 0
        return signals.record(
            found,
            conv=conv,
            uid=int(settings.get("master_qq") or 0),
            executed=executed,
            source=f"指令:{kind_hint}" if kind_hint else "自然语言",
        )
    except Exception:  # noqa: BLE001 - 记账失败绝不能影响回复或指令
        logger.exception("人设信号记账失败（不影响回复）")
        return 0


# --------------------------------------------------------------------- 斜杠指令
async def _dispatch_command(body: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    head, _, rest = body.partition(" ")
    head = _norm(head)
    rest = _norm(rest)

    table = {
        "图": _cmd_image,
        "图片": _cmd_image,
        "img": _cmd_image,
        "风格": _cmd_style,
        "人设": _cmd_persona,
        "记忆": _cmd_memory,
        "memory": _cmd_memory,
        "机制": _cmd_mechanism,
        "说明": _cmd_mechanism,
        "时间": _cmd_time,
        "几点": _cmd_time,
        "时间差": _cmd_time,
        "模式": _cmd_mode,
        "对话": _cmd_lease,
        "连线": _cmd_lease,
        "搜索": _cmd_search,
        "搜": _cmd_search,
        "查": _cmd_search,
        "模型": _cmd_model,
        "昵称": _cmd_nickname,
        "名字": _cmd_nickname,
        "头像": _cmd_avatar,
        "名片": _cmd_card,
        "dsh": _cmd_dsh,
        "帮助": _cmd_help,
        "help": _cmd_help,
        "?": _cmd_help,
    }
    handler = table.get(head.lower())
    if handler is None:
        return Action(
            kind="unknown",
            ok=False,
            reply=f"不认识「{head}」这个指令。可用的是：图 / 风格 / 人设 / 记忆 / 机制 / 时间 / 搜索 / 模式 / 对话 / 模型 / 昵称 / 头像 / dsh / 帮助。",
            stop=True,
            handled=True,
        )
    try:
        return await handler(rest, conv=conv, is_master=is_master, ctx=ctx)
    except Exception:  # noqa: BLE001 - 指令异常不能把整条回复打挂
        logger.exception("指令执行异常 head=%s rest=%s", head, rest)
        return Action(
            kind="error",
            ok=False,
            reply="这条指令执行时出错了，细节记在日志里了。",
            stop=True,
            handled=True,
        )


# ---- /图 ---------------------------------------------------------------
async def _cmd_image(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    parts = rest.split(" ")
    sub = parts[0] if parts else ""
    arg = " ".join(parts[1:]).strip()

    if sub in ("状态", "status", ""):
        return _say("image_status", state.describe_image_policy(conv), conv=conv)

    # 「全局」只在主人手里有意义；群友只能改自己所在的会话
    global_scope = False
    if sub in ("全局", "global"):
        if not is_master:
            return _deny("image", "这条只有主人能用。可以改成对自己这个会话生效，比如 /图 忽略。")
        global_scope = True
        sub = arg or "忽略"
        if sub == "撤销":
            n = state.clear_global_image_policy()
            return _say("image", f"全局图片策略撤掉了（影响 {n} 处）。" if n else "本来就没设全局策略。")
        mode = _resolve_img(sub)
        if not mode:
            return Action(
                kind="image", ok=False, handled=True, stop=True,
                reply="用法：/图 全局忽略 或 /图 全局恢复。",
            )
        state.set_global_image_policy(mode)
        label = {"normal": "恢复正常收图", "ignore": "不再保存图片", "only": "只看不存", "off": "完全不看图"}[mode]
        return _say("image", f"全局：{label}。所有会话都按这个来，直到 /图 全局撤销。", mode=mode)

    if sub == "撤销":
        ok = state.clear_image_policy(conv)
        return _say(
            "image",
            "这个会话的图片策略撤掉了，回到默认。" if ok else "本来就没设过，已经是默认。",
            cleared=ok,
        )

    if sub in ("删最近", "删掉这张", "取消这张"):
        digest, removed = await stickers.forget_latest(conv)
        if not digest:
            return _say("image", "我这儿没有最近发过的图，不知道你指哪张。")
        return _say(
            "image",
            "好，这张不收。" + ("顺便把已经存下来的那张删掉了。" if removed else "（它本来就没入库）"),
            hash=digest[:8],
            removed=removed,
        )

    mode = _resolve_img(sub)
    if not mode:
        return Action(
            kind="image", ok=False, handled=True, stop=True,
            reply=(
                "用法：\n"
                "· /图 忽略 —— 这个会话不再保存任何图片\n"
                "· /图 只看 —— 会看图、但不入库\n"
                "· /图 完全不看 —— 图不下载也不送模型\n"
                "· /图 正常 —— 恢复默认\n"
                "· /图 撤销 —— 撤掉本会话设置\n"
                "· /图 状态 —— 看当前是什么"
            ),
        )

    # 「这张图」的单张豁免是另一条路径（见 mark_current_image_ignored），
    # 这里只处理会话级策略。
    state.set_image_policy(conv, mode)
    label = {
        "normal": "恢复正常（会看图、符合条件就入库）",
        "ignore": "不再把图片收进表情包库了",
        "only": "会看图，但一张都不存",
        "off": "连看都不看了，图不会下载也不会送进模型",
    }[mode]
    return _note(
        "image",
        f"（系统已执行：本会话图片策略设为 {mode} —— {label}。"
        "请用你自己的语气简短确认一句，不要解释技术细节，也不要提「指令」这个词。）",
        mode=mode,
    )


def mark_current_image_ignored(conv: str) -> None:
    """把"最近那张图"加进忽略名单（同步版，只登记不删除）。

    异步的 `forget_latest` 才是给指令用的正路；这个留给"图还没入库"的早期拦截场景。
    """
    state.ignore_latest_image(conv)


# ---- /风格 与 /人设：**只读 + 撤回** --------------------------------------
# 人格分层之后，这两个指令**不再能改人设**。
#
# 为什么删掉改值的入口：人格分三层之后，"谁能改哪一层"必须由代码保证而不是约定 ——
# 底层与禁止事项只有用户能改（编辑文件），表层是自动迭代的地盘。
# 只要留一个"聊天里能直接改"的入口，那条边界就靠不住（任何能说话的人都能试着敲）。
#
# 保留的三种能力都是**只读或收尾**动作：
#   /风格           看三层现状
#   /风格 铁律      逐条看禁止事项（也就是自动迭代撞不过去的那几条）
#   /人设 日志      看自动迭代改了什么、以及**被拦下了什么**
#   /人设 撤回      撤销最近一次自动写入
#   /人设 重跑      立刻触发一次自我反思（后台也会定期跑）
def _persona_readonly_hint() -> str:
    return (
        "（改人格的指令已经删掉了 —— 现在只能直接编辑文件：\n"
        "  底层人设 / 禁止事项 / 表层人设 三个文件的路径见 /人设 状态。\n"
        "  生效时机：表层改完即生效；底层人设与禁止事项改完**要重启**）"
    )


async def _cmd_style(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """`/风格` 现在只是「看」的入口（原来它还能改人设）。"""
    sub, _, _arg = rest.partition(" ")
    sub = _norm(sub)

    if sub in ("", "看", "列表", "状态", "status"):
        st = persona.stats()
        head = (
            f"我的人格分三层（改人格直接编辑文件，改完不用重启）：\n"
            f"· 底层人设 {st['base_chars']} 字 —— 只有你能改\n"
            f"  {st['base_file']}\n"
            f"· 禁止事项 {st['forbidden_chars']} 字（{len(persona.forbidden_items())} 条）"
            f" —— 只有你能改\n"
            f"  {st['forbidden_file']}\n"
            f"· 表层人设 {st['surface_chars']} 字 —— **唯一会自动学的部分**，"
            f"改前过冲突闸门\n"
            f"  {st['surface_file']}"
        )
        if is_master:
            head += (
                f"\n\n自动迭代至今 {st['changes']} 条记录"
                "（写入 + 被丢弃都算）：/人设 日志 看明细，/人设 撤回 撤销最近一次。"
            )
        return _say("style", head)

    if sub in ("铁律", "禁止", "禁止事项"):
        if not is_master:
            return _deny("style", "禁止事项清单只有主人能看。")
        return _say(
            "style",
            "【禁止事项】（自动迭代碰到这些会**直接丢弃、不写入**）\n"
            + persona.forbidden_list_text(),
        )

    if sub in ("日志", "变更", "记录"):
        if not is_master:
            return _deny("style", "变更日志只有主人能看。")
        return _say("style", "【人格自动改动日志】\n" + persona.changelog_text())

    if sub in ("撤回", "撤销", "undo"):
        if not is_master:
            return _deny("style", "撤回只能主人做。")
        got = persona.undo_last()
        if not got.get("ok"):
            return _say("style", f"没撤回什么：{got.get('why')}")
        return _say("style", f"撤掉了上一条自动改动：\n· {got.get('text', '')[:70]}")

    return Action(
        kind="style", ok=False, handled=True, stop=True,
        reply=(
            "改人格的指令已经删掉了 —— 现在只能直接编辑那三个文件。\n"
            f"{_persona_readonly_hint()}\n\n"
            "还能用的：\n"
            "· /风格 —— 看三层现状\n"
            "· /风格 铁律 —— 逐条看禁止事项\n"
            "· /人设 日志 —— 看自动迭代改了什么、拦下了什么\n"
            "· /人设 撤回 —— 撤销最近一次自动改动\n"
            "· /人设 重跑 —— 立刻触发一次自我反思"
        ),
    )


async def _cmd_persona(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """`/人设` 现在只是「看 + 收尾」的入口（原来它还能按槽位改）。"""
    if not is_master:
        return _deny("persona", "人设相关的东西只有主人能看。")
    sub, _, arg = rest.partition(" ")
    sub, arg = _norm(sub), _norm(arg)

    if sub in ("", "看", "状态", "列表"):
        return _say("persona", persona.describe())

    if sub in ("铁律", "禁止", "禁止事项"):
        return _say(
            "persona",
            "【禁止事项】（自动迭代碰到这些会**直接丢弃、不写入**）\n"
            + persona.forbidden_list_text(),
        )

    if sub in ("日志", "变更", "记录"):
        return _say("persona", "【人格自动改动日志】\n" + persona.changelog_text())

    if sub in ("撤回", "撤销", "undo"):
        got = persona.undo_last()
        if not got.get("ok"):
            return _say("persona", f"没撤回什么：{got.get('why')}")
        return _say("persona", f"撤掉了上一条自动改动：\n· {got.get('text', '')[:70]}")

    if sub in ("候选", "待采纳", "池"):
        # 候选池：自动迭代默认不再直接生效
        return _say("persona", "【待采纳的人格候选】\n" + persona.candidates_text())

    if sub in ("采纳", "接受", "approve"):
        got = persona.approve_candidate(_parse_index(arg))
        if not got.get("ok"):
            return _say("persona", f"没采纳成：{got.get('why') or got.get('code')}")
        return _say("persona", f"采纳了，已经写进表层人设：\n· {got.get('text', '')[:70]}")

    if sub in ("否决", "拒绝", "reject"):
        got = persona.reject_candidate(_parse_index(arg))
        if not got.get("ok"):
            return _say("persona", f"没否决成：{got.get('why')}")
        return _say("persona", f"否决了，已从候选池丢掉：\n· {got.get('text', '')[:70]}")

    if sub in ("重跑", "反思", "迭代"):
        from . import persona_iter

        got = await persona_iter.reflect_once(notify=False)
        if not got.get("ok"):
            return _say("persona", f"这次没反思成功：{got.get('why')}")
        if got.get("auto_apply"):
            return _say(
                "persona",
                f"反思完成（自动生效模式）：写入 {got.get('written', 0)} 条、"
                f"被闸门拦下 {got.get('rejected', 0)} 条。\n用 /人设 日志 看明细。",
            )
        return _say(
            "persona",
            f"反思完成：**{got.get('proposed', 0)} 条进了候选池**、"
            f"被闸门拦下 {got.get('rejected', 0)} 条。\n"
            "候选还没生效 —— /人设 候选 看列表，/人设 采纳 <序号> 才写进表层。",
        )

    if sub in ("信号", "账本"):
        # 路线 A 第 1 步留下的信号账本，仍然可用（只观察，不改人设）
        if arg in ("清", "清空"):
            n = signals.clear()
            return _say("persona", f"信号账本清掉了 {n} 条。")
        st = signals.stats()
        return _say(
            "persona",
            f"人设信号账本：{st['items']} 条（{st['kinds']} 类，"
            f"其中 {st['executed']} 条当时就改过了）。\n"
            "**这些还没有自动改任何人设** —— 只记账。\n\n" + signals.report(),
        )

    # ---- 人设评估台（论文那套「对比素材 + 0-100 打分」）----
    # 为什么这里只给"看"和两个**显式**动作、不做成自动跑：评估要花 token，
    # 要不要花是主人的决定 —— 与 `/人设 重跑`（自我反思）同一个取舍。
    # 总闸在控制台「参数 → 人设评估」的 eval_enabled，关着时下面全都会直接说"没跑成"。
    if sub in ("评估", "评分", "评估台", "档案"):
        from . import persona_eval
        return _say("persona", persona_eval.render_status())

    if sub in ("评估跑", "跑评估"):
        from . import persona_eval
        got = await persona_eval.run_round()
        if not got.get("ok"):
            return _say("persona", f"没跑成：{got.get('why')}")
        return _say("persona",
                    f"跑完 {got['traits']} 个特质。\n\n" + persona_eval.render_status())

    if sub in ("评估素材", "生成素材"):
        from . import persona_eval
        got = await persona_eval.generate_artifacts()
        if not got.get("ok"):
            return _say("persona", f"没跑成：{got.get('why')}")
        return _say("persona",
                    f"素材：新生成 {len(got['generated'])} 个，"
                    f"跳过 {len(got['skipped'])} 个，失败 {len(got['failed'])} 个。\n\n"
                    + persona_eval.render_status())

    return Action(
        kind="persona", ok=False, handled=True, stop=True,
        reply=(
            "按槽位改人设的指令已经删掉了。\n"
            f"{_persona_readonly_hint()}\n\n"
            "还能用的：\n"
            "· /人设 —— 看三层状态与文件路径\n"
            "· /人设 铁律 —— 逐条看禁止事项\n"
            "· /人设 候选 / 采纳 <序号> / 否决 <序号> —— 看待采纳提议并决定要不要\n"
            "· /人设 日志 / /人设 撤回 —— 看与撤销已生效的改动\n"
            "· /人设 重跑 —— 立刻触发一次自我反思\n"
            "· /人设 信号 —— 看「他说过哪些说话方式的要求」\n"
            "· /人设 评估 —— 看评估台现状；/人设 评估跑、/人设 评估素材 是真跑（花 token）"
        ),
    )


# ---- /记忆 -------------------------------------------------------------
async def _cmd_memory(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    sub, _, arg = rest.partition(" ")
    sub, arg = _norm(sub), _norm(arg)

    if sub in ("列表", "看", "list", ""):
        text = memory.dump(limit=30)
        st = memory.stats()
        head = (
            f"记忆库：{st['facts']} 条事实、{st['events']} 条群事件、{st['people']} 个人物画像。"
            f"\n（排序证据：{st['used_once']} 条被想起过、{st['confirmed']} 条被反复提到过；"
            f"待落盘 {st['usage_pending']} 条）"
        )
        if not is_master and st["facts"] > 20:
            # 群里几十个人都在，谁都能把全部记忆翻出来并不合适
            return _say("memory", head + "具体内容只在主人私聊里给全。")
        return _say("memory", (head + "\n\n" + text) if text else head + "还是空的。")

    if sub in ("找", "搜", "search") and arg:
        rows = memory.search(arg)
        if not rows:
            return _say("memory", f"没找到跟「{arg}」有关的事。")
        return _say(
            "memory",
            f"找到 {len(rows)} 条：\n"
            + "\n".join(f"· #{r.get('id')} {r.get('text')}" for r in rows),
        )

    if sub in ("存", "记", "记住") and arg:
        if len(arg) < 2:
            return Action(kind="memory", ok=False, handled=True, stop=True, reply="要记的内容太短了。")
        item, _created = await memory.store_pending(
            arg, subject=("主人" if is_master else "群友"), conv=conv, importance=0.85
        )
        return Action(
            kind="memory_remember",
            ok=True,
            handled=True,
            prompt_note=(
                f"（系统已执行：下面这件事已经存进长期记忆了 —— 「{arg}」。"
                "请用你自己的语气确认一句，不要提「记忆库」这个词。）"
            ),
            effect={"text": arg, "id": item.get("id")},
        )

    if sub in ("忘", "删", "forget") and arg:
        m = re.search(r"\d+", arg)
        if not m:
            return Action(kind="memory", ok=False, handled=True, stop=True, reply="用法：/记忆 忘 <编号>")
        item_id = int(m.group(0))
        target = next((f for f in memory.all_facts() if int(f.get("id", -1)) == item_id), None)
        # 支持 /记忆 忘 <关键词>：不需要先查编号
        if target is None:
            rows = memory.search(arg)
            if len(rows) == 1:
                target = rows[0]
                item_id = int(target.get("id", 0))
        if target is None:
            return _say("memory", f"没找到 #{item_id} 这条。用 /记忆 找 <词> 定位一下。")
        await memory.forget(item_id)
        return Action(
            kind="memory_forget",
            ok=True,
            handled=True,
            prompt_note="（系统待办：刚刚那条记忆已删除。请简短应一声，不要提「数据库」或「编号」。）",
            effect={"id": item_id, "text": target.get("text", "")},
        )

    if sub in ("保护", "锁") and arg:
        m = re.search(r"\d+", arg)
        if not m:
            return Action(kind="memory", ok=False, handled=True, stop=True, reply="用法：/记忆 保护 <编号>")
        ok = await memory.set_protected(int(m.group(0)), True)
        return _say("memory", "锁上了，以后不会被自动淘汰。" if ok else "没找到这条。")

    if sub in ("清", "清空", "重置"):
        if not is_master:
            return _deny("memory", "清空记忆只有主人能做。")
        scope = "profile" if arg in ("画像", "人物") else ("facts" if arg in ("事", "事实") else "all")
        n = await memory.wipe(scope)
        return _say("memory", f"清掉了 {n} 项（{scope}）。")

    if sub in ("翻", "旧账", "原话", "查记录") and arg:
        # 翻旧账：查的是**聊天原文**（消息索引），不是提炼过的记忆条目。
        # 这条与 /记忆 找 的区别值得说清楚：一个是"原话怎么说的"，一个是"我记得什么"。
        if not is_master:
            return _deny("memory", "翻聊天原文只有主人能做。")
        from . import msgindex as _mi

        rows = _mi.search(arg, limit=8)
        if not rows:
            return _say("memory", f"记录了里没翻到跟「{arg}」有关的原话。"
                                  f"（也可以试 /记忆 找 {arg} 查提炼过的记忆）")
        lines = []
        for r in rows:
            when = time.strftime("%m-%d %H:%M", time.localtime(r["ts"])) if r["ts"] else "?"
            who = "我" if r["is_bot"] else (r["name"] or "某人")
            lines.append(f"· [{when} {who}] {r['text'][:70]}")
        return _say("memory", f"翻到 {len(rows)} 条原话：\n" + "\n".join(lines))

    if sub in ("索引", "重建索引"):
        if not is_master:
            return _deny("memory", "重建索引只有主人能做。")
        from . import msgindex as _mi

        st = _mi.stats()
        if arg in ("重建", "rebuild"):
            got = _mi.rebuild()
            return _say("memory", f"索引已重建：{sum(got.values())} 条消息。")
        return _say("memory", f"聊天记录索引：{st['messages']} 条消息 / {st['grams']} 个词条。"
                              f"用 /记忆 索 重建 可以重来一遍。")

    return Action(
        kind="memory", ok=False, handled=True, stop=True,
        reply=(
            "用法：\n"
            "· /记忆 列表 —— 看现在记得什么\n"
            "· /记忆 存 <一句话> —— 手动让它记住\n"
            "· /记忆 找 <词> —— 查提炼过的记忆\n"
            "· /记忆 翻 <词> —— 翻聊天原文（要限主人）\n"
            "· /记忆 忘 <编号或词> —— 让它忘掉\n"
            "· /记忆 保护 <编号> —— 永不被自动淘汰\n"
            "· /记忆 清 —— 全清（限主人）"
        ),
    )


# ---- /机制 -------------------------------------------------------------
async def _cmd_mechanism(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    from . import introspect  # 局部导入：introspect 要用到本模块的指令表

    topic = _norm(rest) or "全部"
    text = introspect.explain(topic, conv=conv, is_master=is_master)
    return _say("mechanism", text, topic=topic)


# ---- /时间 -------------------------------------------------------------
# 「现在几点」这类问题**代码直接答**，不过模型。理由是精确算术不该交给语言模型：
# 它可能把"再过 20 分钟"算错，而本地算一次是 0 token、也不会错。
# 模糊的时间理解（"刚吃过饭"）仍然交给模型 —— 那本来就不是算术问题。
_TIME_CLOCK = re.compile(r"^(\d{1,2})[:：](\d{2})(?::(\d{2}))?$")
_TIME_DATE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+(\d{1,2})[:：](\d{2}))?$")
_TIME_OFFSET = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*(秒|分钟|分|小时|天|周)$")


async def _cmd_time(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """当前时间 / 时间差计算。`/时间` 直接报点，`/时间 差 <时间>` 算间隔。"""
    arg = _norm(rest)
    now = clock.now()

    if not arg:
        return _say("time", _time_report(now))

    if arg in ("戳", "时间戳", "ts"):
        return _say(
            "time",
            f"时间戳 {int(now)}（{config.now_stamp()}）\n"
            "把它当 Unix 秒用就行；验证/离线验证_桩.py 里有时间计算的用例。",
        )

    # /时间 校准 —— 立刻跟 NTP 对一次，不用等后台周期
    if arg in ("校准", "校时", "对时", "同步", "ntp", "NTP"):
        st = await clock.sync()
        if st["status"] == "disabled":
            return _say(
                "time",
                "NTP 校准是关着的（控制台「时间校准」组里可以打开）。现在按系统时钟算。",
            )
        if not st["calibrated"] and st["status"] != "ok":
            return _say(
                "time",
                "跟 NTP 对时没成功，还在用系统时钟。\n"
                f"（{st['last_error'] or '服务器都没应答'}；试过 {len(st['servers'])} 台服务器）",
            )
        # 偏差细节只跟主人讲；群友问也给个准确结论，但不带服务器与毫秒
        body = clock.report() if is_master else "跟标准时间对过了，现在按校准后的时间算。"
        return _say("time", body, status=st["status"], offset=st["offset_seconds"])

    if arg in ("状态", "校准状态"):
        return _say("time", clock.report() if is_master else clock.report().splitlines()[0])

    # /时间 差 <时刻或日期>
    body = arg
    if body.startswith(("差", "距", "距离", "过了")):
        body = _norm(body.lstrip("差距过了")) or ""

    target = _parse_moment(body, now)
    if target is None:
        # 也接受"差 3 小时"这种相对量：直接报出那个时间点
        m = _TIME_OFFSET.match(body)
        if m:
            seconds = float(m.group(1)) * {
                "秒": 1, "分钟": 60, "分": 60, "小时": 3600, "天": 86400, "周": 604800,
            }[m.group(2)]
            if abs(seconds) < 60:
                # "45 秒之后是…" 读着别扭，秒级用口语说法
                rel = (
                    config.relative_time_after(now + seconds, now)
                    if seconds >= 0
                    else config.relative_time(now + seconds, now)
                )
                if rel:
                    return _say("time", f"{rel}是 {config.time_parts(now + seconds)[0]}。")
            direction = "之后" if seconds >= 0 else "之前"
            stamp, weekday, period = config.time_parts(now + seconds)
            return _say(
                "time",
                f"{config.human_duration(abs(seconds))}{direction}是 {stamp} {weekday}（{period}）。",
            )
        # 认不出就**明确报错**并给用法，不要默默按成功回执 ——
        # ok=False 才会在日志里留下痕迹，也让"它答了个没用的话"能被归因。
        return Action(
            kind="time",
            ok=False,
            handled=True,
            stop=True,
            reply=(
                "这个时间我读不出来。用法：\n"
                "· /时间                    现在几点、几号、星期几\n"
                "· /时间 差 14:30           距今天 14:30 还有多久\n"
                "· /时间 差 2026-09-20      距那天多久\n"
                "· /时间 差 3 小时          3 小时之后是几点\n"
                "· /时间 校准               立刻跟 NTP 对一次时\n"
                "· /时间 戳                 当前 Unix 时间戳"
            ),
        )

    stamp, weekday, period = config.time_parts(target)
    delta = now - target
    if target > now:
        return _say(
            "time",
            f"{stamp} {weekday}（{period}）还没到，距现在还有 {config.human_duration(target - now)}。",
        )
    rel = config.relative_time(target, now)
    lines = [
        f"{stamp} {weekday}（{period}）是 {config.human_duration(delta)}前。",
    ]
    if rel:
        lines.append(f"（口语说法：{rel}）")
    return _say("time", "\n".join(lines))


def _parse_moment(text: str, now: float) -> float | None:
    """把用户写的时间解析成时间戳。认这几种写法，认不出返回 None。

    * `14:30` / `14:30:05` —— **今天**的那个时刻（已过则指今天的过去，不自动跳明天，
      因为"距 14:30 多久"问的就是今天那个 14:30）；
    * `2026-09-20` / `2026/9/20` / `2026-09-20 14:30`；
    * `昨天` / `前天` / `明天`（当天 00:00）。
    """
    text = str(text or "").strip()
    if not text:
        return None

    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if text in ("昨天",):
        base = time.mktime(time.strptime(today, "%Y-%m-%d")) - 86400
        return base
    if text in ("前天",):
        base = time.mktime(time.strptime(today, "%Y-%m-%d")) - 2 * 86400
        return base
    if text in ("明天",):
        base = time.mktime(time.strptime(today, "%Y-%m-%d")) + 86400
        return base

    m = _TIME_DATE.match(text)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4)) if m.group(4) else 0
        minute = int(m.group(5)) if m.group(5) else 0
        try:
            return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))
        except (ValueError, OverflowError):
            return None

    m = _TIME_CLOCK.match(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        second = int(m.group(3)) if m.group(3) else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
        lt = time.localtime(now)
        try:
            return time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, second, 0, 0, -1)
            )
        except (ValueError, OverflowError):
            return None
    return None


def _time_report(now: float) -> str:
    stamp, weekday, period = config.time_parts(now)
    tz = time.strftime("%Z%z", time.localtime(now))
    return (
        f"现在是 {stamp} {weekday}（{period}），时区 {tz}。\n"
        f"（时间戳 {int(now)}；要是觉得不对，先看这个时区是不是 +0800）"
    )


# ---- /搜索 -------------------------------------------------------------
async def _cmd_search(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """手动搜一次。`/搜索 <词>` 是"这个词我看不懂"的显式出口。

    自动判定刻意保守（宁可漏判也不误搜），所以需要一个明确的手动通道 ——
    用户自己知道哪个词突兀，比任何启发式都准。
    """
    query = _norm(rest)
    if not query:
        st = search.stats()
        from . import search_memory

        mem = search_memory.stats()
        if not st["available"]:
            return _say(
                "search",
                f"联网搜索现在不可用：{st['reason']}。\n"
                "（在控制台「联网搜索」组里配置端点；SearXNG 实例需要开启 json 格式输出）",
            )
        return _say(
            "search",
            f"用法：/搜索 <关键词>\n"
            f"· /搜索 清 —— 清空释义库\n"
            f"当前后端：{st['backend']}，最多带回 {st['max_results']} 条，"
            f"每会话每分钟 {st['rate_limit']} 次。\n"
            f"释义库：{mem['count']} 条（低置信 {mem['low_confidence']}，过期 {mem['stale']}），"
            f"有效期 {mem['ttl_days']:.0f} 天。",
        )

    if query in ("清", "清空", "重置"):
        return await _cmd_search_reset()
    # `/搜索 重查 <词>`：绕过缓存强搜一次
    if query.startswith("重查"):
        query = _norm(query.lstrip("重查"))

    # ---- 先查释义库，再判断"能不能联网" ----
    #
    # 顺序很关键：**缓存里有答案时，联网能力根本不重要**。
    # 第一版把 `available()` 放在前面，于是在没配端点时，明明库里有释义却回一句
    # 「搜不了：没配搜索端点」—— 有答案却说搜不了，是最容易让人误判的一种回执。
    from . import search_memory

    cached, blocked = search_memory.usable(query)
    if cached is not None and not blocked:
        return _say(
            "search",
            search_memory.render(query) + "\n（这条是以前查的，要看最新的就 /搜索 重查 <词>）",
            cached=True,
        )

    if not search.available():
        # 库里没有、又连不上网 —— 这时才该说"搜不了"
        return _say("search", f"搜不了：{search.unavailable_reason()}。")
    if not search.rate_consume(conv):
        return _say("search", "这个会话这会儿搜得太频繁了，等一分钟再试。")

    payload = await search.search(query)
    results = payload["results"]
    if not results:
        return _say("search", f"「{query}」没搜到。\n（{payload['error'] or '结果为空'}）")

    # 只报标题与来源，不把摘录整段贴出来 —— 群里刷一大段网页内容很吵。
    # 要细节就让它自己（在正常对话里）带进回答。
    lines = [f"「{query}」查到 {len(results)} 条："]
    for idx, item in enumerate(results, start=1):
        snippet = item["snippet"][:60]
        lines.append(f"{idx}. {item['title']}\n   {snippet}…" if snippet else f"{idx}. {item['title']}")
    # 顺手沉淀释义，并把置信度回给主人看
    conf, _signals, reasons = search_memory.score_confidence(results)
    if search_memory.is_definition_query(query):
        definition = await search_memory.summarize(query, results)
        if definition:
            await search_memory.put(query, definition, results, by="/搜索")
            flag = "⚠️ 低置信度" if search_memory.is_low(conf) else "已存为释义"
            lines.append(f"\n（{flag} {conf:.2f}｜" + "；".join(reasons[:3]) + "）")
            lines.append(f"释义：{definition}")
    else:
        lines.append("（这条不像在问名词释义，没有存进释义库）")
    lines.append("（摘录只给到这儿；要我据此说点什么，直接问就行。）")
    return _say("search", "\n".join(lines), query=query, hits=len(results), confidence=conf)


async def _cmd_search_reset() -> Action:
    """`/搜索 清`：清空释义库。"""
    from . import search_memory

    n = await search_memory.wipe()
    return _say("search", f"释义库清掉了 {n} 条。" if n else "释义库本来就是空的。")


# ---- /模式 -------------------------------------------------------------
async def _cmd_mode(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """手动指定说话模式。`/模式 自动` 交回自判。

    改造前没有任何模式概念：同一套语气应对所有场合 —— 问报错和说"好累"得到的态度差不多。
    现在按场合切换（日常 / 专注 / 安慰），而这条指令是**人工兜底**：
    自动判定宁可不判（拿不准就交给模型），所以需要一个明确的开关。
    """
    name = _norm(rest)

    if not name or name in ("看", "状态", "当前"):
        cur = mode.current(conv)
        manual = mode.manual_of(conv)
        how = "你指定的" if manual else "我按场合自己判的"
        lines = [f"现在是「{cur}」（{how}）。", f"可选：{mode.names_text()}，或者 自动。"]
        if not manual:
            lines.append("（自动判定拿不准时不改语气，直接按人设来。想固定就用 /模式 专注 这样指定）")
        return _say("mode", "\n".join(lines), mode=cur, manual=manual)

    if not settings.get("mode_enabled"):
        return _deny("mode", "说话模式在控制台里是关着的，现在只有日常状态。")

    ok, note = mode.set_manual(conv, name)
    if not ok:
        return Action(kind="mode", ok=False, handled=True, stop=True, reply=note)
    target = mode.resolve(name)
    if target == "auto":
        return _say("mode", note, mode="auto")
    return _note(
        "mode",
        f"（系统已执行：说话模式切到「{target}」。{note}"
        "请用符合这个模式的一句话应一声，不要提「模式」这个词。）",
        mode=target,
    )


# ---- /对话（对话租约）-----------------------------------------------------
async def _cmd_lease(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """查看/结束「对话租约」—— 刚叫过它之后，接着聊不必每句都 @。

    为什么需要这条指令：租约是**隐式**的（用户看不见它什么时候到期），
    而"它怎么突然不理我了"最容易变成困惑。所以要有一个能问、能主动结束的入口。
    """
    name = _norm(rest)

    if name in ("结束", "停", "退出", "关", "取消"):
        attention.clear_lease(conv)
        return _say("lease", "好，这次连线就到这儿。还有事再 @ 我。", stopped=True)

    st = attention.lease_state(conv)
    if not st:
        return _say(
            "lease",
            "现在没有正在进行的一对一对话。@ 我一次就会开一段，之后你不必每句都 @ 我 —— "
            f"默认 {settings.get('attention_lease_seconds')} 秒内、"
            f"最多 {settings.get('attention_lease_max_turns')} 轮内接着聊我都接。",
            active=False,
        )
    return _say(
        "lease",
        "正在跟你连线对话中："
        f"还剩约 {st['left_seconds']:.0f} 秒（每说一轮会重新计时），"
        f"已用 {st['turns']}/{st['max_turns']} 轮。"
        "想现在就结束就用 /对话 结束。",
        active=True,
        turns=st["turns"],
    )


# ---- /模型 -------------------------------------------------------------
async def _cmd_model(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """`/模型`：说清"现在用哪家的哪个模型"，并且能当场切。

    三种用法（都是**热生效**，不用重启）：

    * `/模型`           —— 现状 + 有哪些接口档案；
    * `/模型 <档案id>`  —— 换接口档案（这一层才换 base_url 与密钥）；
    * `/模型 <模型名>`  —— 只覆盖模型名；`/模型 默认` 撤销覆盖。

    「档案 id」与「模型名」共用同一个位置，是因为**它们几乎不会重名**（id 是
    `deepseek` / `local` 这种短名，模型名带厂商前缀），而多开一个子命令
    （`/模型 切 xxx`）在手机上多打四个字。真撞上了以档案优先 —— 换档案影响更大，
    也只在这一个地方能换。
    """
    if not is_master:
        return _deny("model", "换模型只有主人能做。")
    name = _norm(rest)
    from . import llm, settings as st

    cur = llm.active()
    ids = [p["id"] for p in llm.profiles()]

    if not name:
        return _say("model", "\n".join([
            f"现在用「{cur['id']}」（{cur['label']}）：{llm.model_name()} @ {cur['base_url']}",
            "接口档案：" + " / ".join(ids),
            "换档案发 `/模型 <档案id>`；只改模型名直接发 `/模型 <模型名>`，撤销用 `/模型 默认`。",
        ]))

    # ① 命中档案 id → 换接口（base_url / 密钥一起换）
    if name in ids:
        if name == cur["id"]:
            return _say("model", f"现在用的就是「{name}」：{llm.model_name()} @ {cur['base_url']}。")
        # 覆盖是**针对某一家接口**设的，切档案时会被清掉（见 llm.set_active）——
        # 回执里必须说明，否则"我设的模型名怎么没了"就是下一句追问。
        had = str(st.get("model") or "").strip()
        ok, why = llm.set_active(name)
        if not ok:
            return _deny("model", why)
        new = llm.active()
        key_note = "" if llm.api_key(new) else "（**这个档案还没配密钥**，配好之前调不通）"
        over_note = f"，顺便清掉了模型名覆盖「{had}」" if had else ""
        return _say("model", f"接口档案从「{cur['id']}」换成「{name}」了{over_note}："
                             f"{new['base_url']} / {llm.model_name()}{key_note}，下一条消息就生效。")

    # ② `/模型 默认` → 清掉覆盖，回到档案自带的模型名
    if name in ("默认", "default", "auto", "-", "清空"):
        st.set_value("model", "")
        return _say("model", f"模型名覆盖清掉了，回到「{cur['id']}」档案自带的 {llm.model_name()}。")

    # ③ 其余当模型名覆盖。**不校验名字**：换一家接口之后本地根本无从知道对方有哪些模型，
    #    能验的只有"调得通"，所以回执里如实说清"名字对不对要看接口"。
    old = llm.model_name()
    st.set_value("model", name)
    return _say("model", f"模型名从 {old} 覆盖成 {name} 了（接口仍是「{cur['id']}」"
                         f"{cur['base_url']}）。名字对不对以接口为准 —— 调不通就在「模型」页里改回去。")


# ---- /昵称 · /头像 · /名片：改它自己的身份 ------------------------------
# 这是**唯一**一类会改 QQ 侧状态、而不是只改本地文件的指令。
#
# 设计取舍（连同 identity.py 一起看）：
#   * 全部限主人 —— 身份是全局可见的，且"分清自己说的话"靠的就是名字；
#   * **不做自然语言入口** —— 「你以后叫小鱼吧」只会被当成闲聊记下来，
#     不会有任何 QQ 侧动作。要它真改名就得敲指令。这跟图片策略正相反，是有意为之：
#     图片策略是可逆的本地状态，改名不是。
#   * **不进模型工具集** —— 见 identity.py 顶部说明。
async def _cmd_nickname(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """`/昵称` 看现状；`/昵称 <名字>` 改 QQ 昵称（全局）。"""
    name = _norm(rest)
    now = config.bot_name()
    if not name or name in ("看", "状态"):
        return _say(
            "identity",
            f"我现在叫「{now}」。改法：/昵称 <新名字>（限主人）。"
            "改了会同时写进设置，聊天记录里也认这个名字。",
        )
    if not is_master:
        return _deny("identity", "给我改名只有主人能做。")
    context = ctx if isinstance(ctx, dict) else {}
    bot = context.get("bot")
    if bot is None:
        return _reject_say(
            "identity", "这条路上拿不到发消息的接口，改不了。得在群聊/私聊里直接发指令。")
    try:
        text = await identity.apply_nickname(bot, name)
    except identity.IdentityError as exc:
        return _reject_say("identity", f"改名没成：{exc}")
    except Exception:  # noqa: BLE001 - 接口异常不该把回复打挂
        logger.exception("改昵称失败 name=%s", name)
        return _reject_say("identity", "改名的时候接口出错了，细节记在日志里了。")
    return _say("identity", f"从现在起我叫「{text}」了。QQ 上要过一会儿才刷新。")


async def _cmd_avatar(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """`/头像` 用本条消息（或它引用的消息）里的图当自己的头像。

    **回执一律直出**（不走模型改写）：这类动作的结果要么真发生了、要么真没有，
    让模型转述一遍只会把它说成"好像换了" —— 而"嘴上一句就算数"正是要根除的东西。
    """
    if not is_master:
        return _deny("identity", "换我自己的头像只有主人能做。")
    context = ctx if isinstance(ctx, dict) else {}
    bot = context.get("bot")
    if bot is None:
        return _reject_say(
            "identity", "这条路上拿不到发消息的接口，改不了。得在群聊/私聊里直接发指令。")
    segments = list(context.get("images") or [])
    if not segments:
        return _reject_say(
            "identity",
            "要用哪张图？把图和指令一起发，或者引用那张图再发 /头像。",
        )
    data = b""
    for seg in segments:
        data = await stickers.download_image(seg)
        if data:
            break
    if not data:
        return _reject_say("identity", "图没下下来（链接可能过期了），重新发一次试试。")
    try:
        note = await identity.apply_avatar(bot, data)
    except identity.IdentityError as exc:
        return _reject_say("identity", f"换头像没成：{exc}")
    except Exception:  # noqa: BLE001
        logger.exception("换头像失败 conv=%s", conv)
        return _reject_say("identity", "换头像的时候接口出错了，细节记在日志里了。")
    return _say("identity", note)


async def _cmd_card(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """`/名片 <名字>` 改**本群**的群名片；`/名片 清` 撤掉它、显示原昵称。

    群名片才是群里实际显示的名字：设了它，QQ 昵称就被它盖住。
    所以"在群里改个名字"其实要动的是这个 —— 只改昵称可能看不出变化。
    """
    if not is_master:
        return _deny("identity", "改群名片只有主人能做。")
    group_id = identity.group_id_of(conv)
    if group_id is None:
        return _reject_say(
            "identity", "群名片只在群里有效，这里（私聊）没得改。要改全局名字用 /昵称。")
    context = ctx if isinstance(ctx, dict) else {}
    bot = context.get("bot")
    if bot is None:
        return _reject_say("identity", "这条路上拿不到发消息的接口，改不了。得在群里直接发指令。")
    text = _norm(rest)
    if not text or text in ("看", "状态"):
        return _say("identity", f"本群（{group_id}）的名片改法：/名片 <新名字>，清掉用 /名片 清。")
    card = "" if text in ("清", "清空", "撤", "撤销", "取消") else text
    # 机器人自己的 QQ 号要从接口拿，不能从会话键推 —— 会话键里只有群号和说话人
    self_id = await identity.fetch_self_id(bot)
    if not self_id:
        return _reject_say("identity", "拿不到自己的 QQ 号，改不了群名片，细节记在日志里了。")
    try:
        new_card = await identity.set_group_card(bot, group_id, self_id, card)
    except identity.IdentityError as exc:
        return _reject_say("identity", f"改群名片没成：{exc}")
    except Exception:  # noqa: BLE001
        logger.exception("改群名片失败 conv=%s", conv)
        return _reject_say("identity", "改群名片的时候接口出错了，细节记在日志里了。")
    if new_card:
        return _say("identity", f"本群名片改成「{new_card}」了。")
    return _say("identity", "本群名片撤掉了，现在显示的是我的 QQ 昵称。")


# ---- /帮助 -------------------------------------------------------------
async def _cmd_help(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    lines = [
        "我能听懂的指令：",
        "· /图 忽略|只看|完全不看|正常|撤销|状态 —— 管图片怎么处理",
        "· /风格 —— 看我的人格三层现状（**改人格要直接编辑文件**）",
        "· /风格 铁律 —— 逐条看禁止事项（自动迭代撞不过去的那几条）",
        "· /人设 [候选|采纳 <序号>|否决 <序号>|日志|撤回|重跑|信号|评估] —— 管我的人格（限主人）",
        "· /记忆 列表|存|找|翻|忘|保护 —— 管我记住的东西",
        "· /机制 [图|记忆|风格|触发|安全|全部] —— 我把自己的机制讲给你听",
        "· /时间 [差 <时刻>|校准|戳] —— 现在几点、距某个时间多久、跟 NTP 对时（我直接算，不猜）",
        "· /搜索 <关键词> —— 手动联网查一次（也可直接问，我自己判断要不要查）",
        "· /模式 [日常|专注|安慰|自动] —— 换说话的场合态度（也可 /模式 看）",
        "· /对话 [结束] —— 看/结束当前的一对一对话（叫过我一次之后，接着聊不必每句都 @）",
        "· /模型 [档案id|模型名] —— 换接口档案或覆盖模型名（限主人）",
        "· /昵称 [新名字] —— 看/改我自己的 QQ 昵称（全局，限主人；聊天记录里也跟着换）",
        "· /头像 —— 把本条或引用的图当我的新头像（限主人）",
        "· /名片 [新名字|清] —— 改我在这一个群里的显示名（限主人，群里用）",
        "· /dsh run <一句话任务> —— 交给主人电脑上的 DSH 跑一次，结果原样带回（限主人）",
    ]
    if not is_master:
        lines = [ln for ln in lines if "限主人" not in ln]
    return _say("help", "\n".join(lines))


# --------------------------------------------------------------------- 自然语言兜底
# 每条规则形如 (意图, 必须同时命中的所有模式组)。用 all() 语义：
# 列表里每个元素是一组同义写法，每组至少要命中一个 —— 也就是"动作词 + 对象词"都要有。
#
# **人格分层之后这里只留两类规则**：
#   1. 图片策略（与人设无关，改的是 `state`）
#   2. 机制说明（只读地回答）
#
# 原来还有四条 `style_*` 规则，能直接改人设槽位 —— 已**全部删除**。
# 为什么必须删：人格分三层之后，"谁能改哪一层"要由代码保证。
# 只删斜杠指令是不够的：说一句「以后叫我哥哥」照样会命中 `style_call` 去改槽位，
# 那条边界就形同虚设。现在这类话会被 `persona_change_request` 认出来，
# **不改任何人设**，只回一句"改人格要直接编辑文件"，同时记进信号账本
# （账本仍有价值：它记录"他到底想要什么"，见 `persona_signal` 那套）。
_NL_RULES: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    (
        "image_ignore_this",
        (
            ("不要保存", "别保存", "不要存", "别存", "不用存", "删掉", "别收", "不要收"),
            ("这张", "这个图", "刚才那", "刚那张", "这图"),
        ),
    ),
    (
        "image_ignore_topic",
        (
            ("不要保存", "别保存", "不要存", "别存", "不用存", "别收", "不要收", "不保存"),
            ("图片", "照片", "图"),
        ),
    ),
    # 「想改人格」的识别：不改任何东西，只回答去哪里改。
    # 顺序上放在机制说明之前 —— 「改人设」比「讲机制」更需要一个明确回答。
    (
        "persona_change_request",
        (
            # 变化词：也可以**一个都没有**（「正经点」「简短」就是裸要求）。
            # 原来这一组写死了"以后/能不能/改/调/换"，于是「正经点」不命中 ——
            # 实测漏过。现在用一个几乎必中的词兜底，把判据压到**只靠对象词**。
            ("以后", "能不能", "可以", "帮我", "给我", "改", "调", "换", "点", "些", "一点"),
            ("叫我", "别叫", "称呼", "说话", "回复", "语气", "长一点", "短一点", "正经", "轻松", "啰嗦"),
        ),
    ),
    (
        "explain_mechanism",
        (
            ("机制", "原理", "怎么实现", "如何实现", "怎么工作", "工作机制", "流程"),
            ("图片", "图", "记忆", "记得", "人设", "你", "这个", "你的"),
        ),
    ),
)

# 这些词一旦出现，就绝不当成自然语言指令处理（避免误改状态）
_NL_GUARD = ("为什么", "是不是", "能不能", "可以吗", "行吗", "好吗", "？", "?")


# 「改人格」类意图的提示词片段 —— 变化词 + 对象词**都要命中**才算，
# 所以不会把「我以后叫你什么好呢」这种闲聊误判进来（它缺"改/换/调"这类变化词）。
# 注意：`persona_change_request` 与 `explain_mechanism` 可能同时命中
# （「你怎么改的人设」两个都撞）。`_match_natural` 按表顺序取**第一个**命中的，
# 而 `persona_change_request` 排在前面 —— 那种问句应该得到"去哪改"的回答。


async def _match_natural(text: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """保守的自然语言兜底。**只做两件事：图片策略、机制说明。**

    人格相关的意图会被 `persona_change_request` 认出来，但**不改任何人设** ——
    这是人格分层后的硬边界（见本段开头的说明）。
    """
    raw = _norm(text)
    if not raw or len(raw) > 60:
        return Action()
    # 「不要保存这张图片吗？」这种反问不能当指令执行
    if any(g in raw for g in _NL_GUARD):
        return Action()

    for intent, groups in _NL_RULES:
        if not all(any(p in raw for p in group) for group in groups):
            continue

        if intent == "image_ignore_this":
            # 「这张图」需要一个"最近图片"才能定位；没有就退化成会话级忽略
            if state.has_recent_image(conv):
                digest, removed = await stickers.forget_latest(conv)
                return _note(
                    "image_ignore_this",
                    "（系统已执行：刚说的那张图已经进了不保存名单，"
                    + ("我没入库；" if not removed else "并且把已经存下来的那张删掉了；")
                    + "以后也不会再收它。请用你自己的语气应一声，"
                    "不要提「名单」「哈希」这类词。）",
                    scope="single",
                    hash=digest[:8],
                    removed=removed,
                )
            state.set_image_policy(conv, "ignore")
            return _note(
                "image_ignore_this",
                "（系统已执行：这个会话不再保存图片。请用你自己的语气应一声。）",
                scope="conversation",
            )

        if intent == "image_ignore_topic":
            state.set_image_policy(conv, "ignore")
            return _note(
                "image_ignore_topic",
                "（系统已执行：这个会话以后不再保存图片入库。请简短应一声。）",
                scope="conversation",
            )

        if intent == "persona_change_request":
            # **不改任何人设**。给一条明确的路，别让它自己乱解释。
            return _say(
                "persona",
                "我的人格现在分三层，聊天里改不了了 —— 改要直接编辑文件：\n"
                f"· 底层人设（我是谁）：{persona.stats()['base_file']}\n"
                f"· 禁止事项（铁律）：{persona.stats()['forbidden_file']}\n"
                f"· 表层说话方式（**我会自己慢慢学的那一层**）："
                f"{persona.stats()['surface_file']}\n"
                "改完不用重启。你说的这句我记下来了（/人设 信号 能看）——"
                "同类要求攒多了我会自己往表层里学，前提是不碰上面两条。",
            )

        if intent == "explain_mechanism":
            from . import introspect

            topic = "图" if any(w in raw for w in ("图", "图片", "照片")) else "全部"
            return _say("mechanism", introspect.explain(topic, conv=conv, is_master=is_master))

    return Action()


async def _cmd_dsh(rest: str, *, conv: str, is_master: bool, ctx: Any = None) -> Action:
    """`/dsh run <任务>` —— **只对主人开放**，只做"命令与结果的转发站"。

    这条指令的意义是把本机的 DSH 接进群聊：主人发一句话，本机跑一次
    `dsh --profile headless "<任务>"`，把 stdout 原样带回群里。

    四个刻意的设计（都是安全相关，不是风格问题）：

    1. **限主人**：bot 读得到群里的任何消息，等于把"能驱动你电脑上的 DSH"
       接在不可信输入上。除主人外一律拒，连提示都不给细节。
    2. **服务器侧只写数据**：这里只落一个 JSON 任务文件，**绝不执行命令**。
       真正动手的是本机 agent，它按固定动作表（目前只有 `dsh.run`）执行。
    3. **不经 shell**：任务文本会作为**单个 argv 元素**交给
       `dsh --profile headless`，所以任务里带引号、分号、管道都只是普通文字。
    4. **超时不取消任务**：同步等 `WAIT_SECONDS` 秒是为了让短任务直接看到结果；
       超时就回一句"已下发"，任务仍在本机继续跑（结果留在 out/ 里）。
    """
    if not is_master:
        return _deny("dsh", "`/dsh` 只有主人能用。")

    sub, _, arg = rest.partition(" ")
    sub = _norm(sub).lower()
    arg = _norm(arg)

    if sub in ("", "帮助", "help", "?"):
        return _say(
            "dsh",
            "用法：\n"
            "· `/dsh run <一句话任务>` —— 让本机的 DSH 跑一次，结果原样带回来\n"
            "（只做转发：不解释、不改写、不润色）\n"
            f"（本机大约每 2 秒取一次任务；同步最多等 {int(dsh_bridge.WAIT_SECONDS)} 秒，"
            f"执行上限 {dsh_bridge.TIMEOUT_SECONDS} 秒）",
        )

    if sub != "run":
        # 用 `_reject_say`（不是 `_say` 也不是 `_reject`）：既要标记"没办成"，
        # 又要**不经模型**直出确定文本 —— 这条指令的定位就是只做转发。
        return _reject_say("dsh", f"`/dsh` 只认 `run`（收到的是「{sub}」）。用法：`/dsh run <任务>`。")

    if not arg:
        return _reject_say("dsh", "没给任务内容。用法：`/dsh run <一句话任务>`。")

    try:
        payload = dsh_bridge.enqueue(arg, from_user="master")
    except Exception as exc:  # noqa: BLE001 - 下发失败要说清楚，不能让主人干等
        logger.exception("DSH 任务下发失败")
        return _say("dsh", f"任务没能下发：{type(exc).__name__}。")

    task_id = str(payload["id"])
    # **不在指令层等结果**：这里同步等 60 秒会把整条消息处理堵住。
    # 改成"立刻回执 + 由主流程后台推送结果"（推送在 __init__ 里做，
    # 因为只有那里同时拿得到 bot 与 event）。
    logger.info("DSH 任务已下发 id=%s，结果将由后台推送", task_id)
    return _say(
        "dsh",
        f"已下发给本机（任务号 `{task_id}`）。\n跑完我把结果贴上来；本机 agent 不在线时任务会一直排队。",
        task_id=task_id,
    )
