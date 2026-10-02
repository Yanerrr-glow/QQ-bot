"""说话模式：让同一套人设在不同的对话场合下表现不同。

## 参考来源

参考目录里有两份材料都指向这件事：

1. `renderer.js`（桌面宠物）用的是 **`classifyTopic` + WORK/LIFE 关键词**，
   把「工作咨询」和「生活闲聊」严格分开，并明确写了「拿不准时选生活状态」；
2. `机器人体验优化与趣味功能方案.md` 第 5 条「人格模式切换」列了
   `/模式 技术` `/模式 安静` 这类指令，并指出**模式应当影响回复长度、语气和主动发言概率**。

两份都还没在本项目落地。这个模块补的就是这一层。

## 为什么不能照抄关键词分类

参考项目那套是 `text.includes(关键词)` 计数比大小。直接搬过来会出问题：

* **误判率高**。它自己的 LIFE 列表里有「今天」，而「今天这个报错怎么修」也含「今天」——
  一句技术提问会被判成闲聊。WORK 列表里有「项目」，而「我那个项目你还记得吗」是闲聊。
* **它不是"错一点"的问题**：判错了就该改语气和长度，用户会立刻感觉出来。

所以这里改成三段式：

| 来源 | 谁做 | 什么时候 |
|---|---|---|
| **人工指定** | `/模式 专注` | 优先级最高，一直生效到 `/模式 自动` |
| **本地打分** | `detect()` | 只在**票差够大**（≥ `mode_min_margin`）时才用它 |
| **模型自判** | 交给模型 | 票差不够时**不给提示**，让它按人设里的规则自己判断 |

「拿不准就不判」是这套的核心 —— 参考项目那句「拿不准时，选生活状态」也是同一个意思，
只不过它用"默认生活"实现，这里用"不给提示、交回模型"实现，后者更不容易误伤。

## 模式影响的不是"人设"，是"行为参数"

这一点很关键：模式**不换人设**，只调整几个可以量化的东西。

| 模式 | 回复长度 | 语气 | 发表情包 | 主动开口 |
|---|---|---|---|---|
| 日常 | 按运行时要求（默认短句） | 元气、软、爱撒娇 | 正常 | 正常 |
| 专注 | 先给结论，不闲聊 | 认真、直接，但仍然是她 | 压低 | 压低 |
| 安慰 | 短，一两句 | 先接情绪，不给方案 | 压低 | 压低 |

所以它跟人格三层（`persona/packs/<包>/base.txt` 底色 / `persona/packs/<包>/forbidden.txt` 铁律 /
`persona/packs/<包>/surface.txt` 表层）**不冲突**：
人设决定"她是谁"，模式决定"这会儿该多用力"。叠加顺序见 `context.system_prompt()`。

> 注：旧的运行时槽位文件 `persona_directives.json`（`/风格` 当场改的那些要求）
> 已随三层重构停用，别再往它上面挂东西 —— 对照表见 `分析记录/人设历史/原槽位映射.md`。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import settings

logger = logging.getLogger("ai_chat.mode")

# 模式定义。键是要显示给用户的中文名，也是 `/模式 <名>` 接受的写法。
MODES: dict[str, dict[str, Any]] = {
    "日常": {
        "key": "daily",
        "label": "日常",
        "hint": "",  # 日常是默认状态，不给提示 —— 人设正文本身就是按日常写的
        "sticker_scale": 1.0,
        "proactive_scale": 1.0,
    },
    "专注": {
        "key": "focus",
        "label": "专注",
        "hint": (
            "【此刻的场合：对方在问正事（技术、故障、代码、要方案、要判断）】\n"
            "- 先给结论或直接回答，再补必要的一句解释。别寒暄、别闲聊、别先铺垫。\n"
            "- 语气照旧是她的（可以软、可以短），但**不要开玩笑、不要卖萌、不要转移话题**。\n"
            "- 不要因为问得专业就变成客服腔或报告腔；她还是那个她，只是认真了。\n"
            "- 不确定就直说不确定，并说清缺什么，别硬编。"
        ),
        "sticker_scale": 0.15,
        "proactive_scale": 0.2,
    },
    "安慰": {
        "key": "comfort",
        "label": "安慰",
        "hint": (
            "【此刻的场合：对方情绪不好（累、烦、难过、被打击、在吐槽）】\n"
            "- **先接住情绪，不要给方案、不要讲道理、不要科普**。人家没问就先别建议。\n"
            "- 短，一两句就够。可以只说一句「辛苦了」「那确实挺烦的」。\n"
            "- 不要用「以后会好的」「你已经很棒了」这种空泛打气，也不要说教。\n"
            "- 可以顺着问一句怎么了，但别追问、别审问。\n"
            "- 除非对方明确要办法，否则整段里不要出现「建议你」。"
        ),
        "sticker_scale": 0.35,
        "proactive_scale": 0.3,
    },
}

_ALIAS: dict[str, str] = {
    # 用户可能怎么打字
    "日常": "日常", "生活": "日常", "普通": "日常", "闲聊": "日常", "daily": "日常",
    "自动": "auto", "auto": "auto", "默认": "auto",
    "专注": "专注", "技术": "专注", "工作": "专注", "认真": "专注", "focus": "专注",
    "安慰": "安慰", "情绪": "安慰", "安慰模式": "安慰", "comfort": "安慰",
}


def resolve(name: str) -> str:
    """把用户写的模式名归一化；认不出返回空串。"""
    return _ALIAS.get(str(name or "").strip().lower().replace("模式", ""), "") or _ALIAS.get(
        str(name or "").strip(), ""
    )


def names_text() -> str:
    return " / ".join(MODES)


# --------------------------------------------------------------------- 本地判定
# 打分用的词表。**故意比参考项目窄**：只收"几乎不会出现在闲聊里"的词，
# 宁可漏判（交给模型）也不要误判（改错语气）。
#
# 参考项目的表里收着「今天」「项目」「工作」「数据」这类词，那正是误判的来源：
# 「今天这个报错怎么修」会被它算成闲聊。这里不收。
_FOCUS_WORDS: tuple[str, ...] = (
    "报错", "报 exception", "栈", "堆栈", "traceback", "怎么修", "为什么报", "编译",
    "代码", "函数", "接口", "接口文档", "参数", "配置", "部署", "安装", "依赖",
    "正则", "sql", "脚本", "算法", "复杂度", "性能", "内存", "端口", "日志",
    "报错信息", "复现", "版本", "git", "docker", "服务器", "数据库", "语法",
    "帮我写", "帮我改", "帮我查", "怎么做", "如何实现", "原理", "区别",
)
_COMFORT_WORDS: tuple[str, ...] = (
    "好累", "累死", "烦死", "难受", "难过", "想哭", "哭了", "抑郁", "焦虑",
    "压力好大", "撑不住", "崩溃", "被骂", "被拒", "挂了", "失败了", "搞砸",
    "不想活", "没意思", "睡不着", "心疼", "委屈", "emo", "破防",
)
# 出现这些词会**取消**专注判定：「怎么修」是技术问题，但「好累啊怎么修」是吐槽
_COMFORT_OVERRIDE = ("心情", "情绪", "安慰", "陪我聊")

# 每个会话的人工指定与自动判定缓存
_lock = threading.Lock()
_manual: dict[str, tuple[str, float]] = {}   # conv -> (mode, 设置时刻)
_detected: dict[str, tuple[str, float]] = {}  # conv -> (mode, 判定时刻)


def set_manual(conv: str, name: str) -> tuple[bool, str]:
    """人工指定模式。`自动` 表示撤销指定、回到自判。"""
    target = resolve(name)
    if not target:
        return False, f"没有「{name}」这个模式。可选：{names_text()}，或者 自动。"
    with _lock:
        if target == "auto":
            _manual.pop(conv, None)
            _detected.pop(conv, None)
            return True, "好了，我按场合自己看着办。"
        _manual[conv] = (target, time.time())
    logger.info("模式由人工指定 conv=%s → %s", conv, target)
    return True, f"接下来按「{target}」来。"


def manual_of(conv: str) -> str:
    with _lock:
        item = _manual.get(conv)
    return item[0] if item else ""


def current(conv: str) -> str:
    """当前生效的模式名。人工指定优先；否则用本地判定；都没有就是「日常」。"""
    with _lock:
        item = _manual.get(conv)
        if item:
            return item[0]
        got = _detected.get(conv)
    return got[0] if got else "日常"


def clear(conv: str) -> None:
    with _lock:
        _manual.pop(conv, None)
        _detected.pop(conv, None)


# --------------------------------------------------------------------- 判定
def detect(text: str) -> str:
    """本地打分判定模式；**拿不准返回空串**（表示"交给模型"）。

    返回空串是这个函数最重要的行为：宁可让模型按人设自己判断，
    也不要用一个勉强的关键词命中把语气和长度改错。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""

    # 情绪优先：情绪词命中就判安慰，除非同时出现"取消词"（心情/情绪/安慰/陪我聊）
    comfort_hits = sum(1 for w in _COMFORT_WORDS if w in raw)
    if comfort_hits and not any(w in raw for w in _COMFORT_OVERRIDE):
        return "安慰"

    focus_hits = sum(1 for w in _FOCUS_WORDS if w in raw)
    margin = max(0, int(settings.get("mode_min_margin")))
    # 票差不够就不判 —— 这就是"拿不准时不给提示"
    if focus_hits >= max(1, margin):
        return "专注"
    return ""


def note(conv: str, text: str) -> str:
    """记下这次判定结果，供本会话后续几轮沿用（避免一条消息一个语气）。

    为什么要沿用：模式影响的是**整段对话的语气**，不是单条消息。
    上一条在问报错、这一条说"谢谢"，如果不沿用就会突然从专注跳回闲聊，
    读起来像换了个人。
    """
    got = detect(text)
    if got:
        with _lock:
            _detected[conv] = (got, time.time())
        return got

    # 没判定出来：上次的判定还够新就继续沿用，否则回落到日常。
    with _lock:
        prev = _detected.get(conv)
    # **注意 `<= 0` 必须走"不沿用"这条路**：早先这里写的是
    # `if prev and (now - t) <= sticky` —— sticky=0 时该条件在**同一毫秒内**仍成立，
    # 于是"设 0 = 每条消息都重新判"实际没生效，上一条的专注会被沿用下去。
    if prev and int(settings.get("mode_sticky_seconds")) > 0:
        if (time.time() - prev[1]) <= int(settings.get("mode_sticky_seconds")):
            return prev[0]
    with _lock:
        _detected.pop(conv, None)
    return "日常"


# --------------------------------------------------------------------- 注入
def hint(conv: str, text: str = "") -> tuple[str, str]:
    """返回 (模式名, 给 prompt 的模式提示)。`日常` 模式返回空提示。

    人工指定时不看内容 —— 用户说"接下来按专注来"就该一直专注，
    直到他自己改回来。
    """
    manual = manual_of(conv)
    if manual:
        name = manual
    elif text:
        name = note(conv, text)
    else:
        name = current(conv)
    spec = MODES.get(name) or MODES["日常"]
    return name, str(spec.get("hint") or "")


def scale_for(conv: str, kind: str) -> float:
    """按模式缩放某个行为概率。kind: `sticker` / `proactive`。

    模式不该被写成"不发表情包"这种布尔开关 —— 它是"少发一点"，
    所以做成乘数，仍然由原来的概率参数决定基准。
    """
    spec = MODES.get(current(conv)) or MODES["日常"]
    key = f"{kind}_scale"
    try:
        return float(spec.get(key, 1.0))
    except (TypeError, ValueError):
        return 1.0


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "modes": [dict(v, name=k) for k, v in MODES.items()],
            "manual": {k: v[0] for k, v in _manual.items()},
            "detected": {k: v[0] for k, v in _detected.items()},
        }
