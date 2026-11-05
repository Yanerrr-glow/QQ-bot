"""运行时设置：UI 改完立即生效，并持久化到 data/runtime/settings.json。

为什么不让 UI 直接写 .env：
1. .env 是人手维护的，程序回写容易把注释和格式搅乱；
2. 改 .env 必须重启才生效，而调概率这种事应该即改即见效。

读取优先级（高 → 低）：
    data/runtime/settings.json  >  .env  >  代码内置默认

_SPECS 表同时驱动三件事：取值校验、.env 默认值来源、Web UI 的表单渲染 ——
加一个可调参数只需要在这里加一行。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from nonebot import get_driver

logger = logging.getLogger("ai_chat.settings")

_ROOT = Path(__file__).resolve().parent.parent.parent

# 密钥类参数的掩码。与 `llm.py` 对档案密钥的处理同一套约定：
# **显示时替换成它，回写时见到它就原样保留**（见 `Spec.secret` 与 `_Store.set`）。
_MASK = "***"


# 任务局部的参数覆盖（#0015 P1-2）。**只给回放/验证用，不进控制台、不落盘。**
#
# 为什么不能用「改 `store._runtime` + finally 还原」：那是进程级共享状态，
# 而聊天是并发的 —— `await` 期间别的请求会读到测试配置（少发或多发一句话都算事故）。
# ContextVar 的语义正好是「只影响当前任务」，且嵌套时按栈回退。
_override: ContextVar[dict[str, Any] | None] = ContextVar("settings_override", default=None)


@contextmanager
def override(values: dict[str, Any]) -> Iterator[None]:
    """在 with 块内让 `get(key)` 返回给定值；出块自动恢复。

    只覆盖**传入的键**，其余照常读运行时/.env/Spec，所以可以只冻住与回放有关的那几项。
    `set()` 不受影响 —— 覆盖期内写参数仍然写真实存储，这是故意的：
    把「读」与「写」分成两条路，避免测试代码顺手改坏线上配置。
    """
    token = _override.set(dict(values or {}))
    try:
        yield
    finally:
        _override.reset(token)


@dataclass(frozen=True)
class Spec:
    key: str
    kind: type  # float / int / bool / str
    env_name: str
    default: Any
    label: str
    group: str = "通用"
    minimum: float | None = None
    maximum: float | None = None
    hint: str = ""
    choices: tuple[str, ...] = ()  # 非空时 UI 渲染成下拉框
    # **密钥类参数**：`describe()` 会把它掩码成 `***`，而 `set()` 收到 `***` 时
    # **原样保留旧值**（否则控制台一回写就把密钥写成字面量 `***`）。
    # 与 `llm.py` 的档案密钥同一套约定（`mask_key` / `"***"` 原样保留）。
    secret: bool = False


_SPECS: list[Spec] = [
    # ---------------------------------------------------------- 基础
    # **留空 = 用「模型」页当前档案里写好的那个模型名**；填了才覆盖它。
    # 保留这个键是为了兼容：改造前所有调用点读的都是 `settings.get("model")`
    # （那时它既表示模型名、又隐含了接口），老 `data/runtime/settings.json` 里也存着它。
    # 现在**接口/密钥归档案管，这里只管名字** —— 这也是能随时换一家 API 的前提。
    #
    # ⚠ `env_name` 故意留空（与下面 `bot_name` 同一个道理）：`.env` 的 `DEEPSEEK_MODEL`
    # 已经由 `config.MODEL` 读去播种 `deepseek` 档案了。若这里同名再读一遍，
    # 它就会**同时**变成一个全局覆盖值 —— 于是切到别的档案时模型名压根不变，
    # "换了接口没换模型"就是这么来的（实测踩过）。
    Spec("model", str, "", "", "对话模型覆盖", "基础",
         hint="留空就用「模型」页当前档案自带的模型名。填了只覆盖**名字**，接口与密钥仍由档案决定；"
              "切档案时这个覆盖会被清掉。写对写错以调得通为准：有些不公开的别名"
              "（如 deepseek-chat）不在 /models 列表里但照样能用"),
    Spec("master_qq", int, "ai_chat_master_qq", 100000001, "主人的 QQ 号", "基础",
         hint="只有这个人被当作「主人」；其他人不归类，按聊天记录里的名字认就行"),
    Spec("master_title", str, "ai_chat_master_title", "主人", "对主人的称呼", "基础"),
    # 机器人自己的显示名。**让它可以被 /昵称 热改** —— 否则改了 QQ 昵称，
    # 聊天记录里仍写着旧名字，而"分清自己说的话"正是靠这个名字 + bot_uid 判定的。
    #
    # ⚠ 环境变量名**故意不叫 `ai_chat_bot_name`**：那个是 `.env` 里 `AI_CHAT_BOT_NAME`
    # 的键，`config.BOT_NAME` 已经读了它。若这里同名，Spec 的默认值（空串）
    # 会在 `.env` 有值时把它顶掉 —— 优先级链就反了。
    Spec("bot_name", str, "ai_chat_bot_name_override", "", "机器人显示名", "基础",
         hint="留空则用 .env 的 AI_CHAT_BOT_NAME。改这里（或群里 `/昵称 <名字>`）"
              "会让聊天记录、人设自述、挑图语境都跟着换 —— 这是它自己的名字"),
    Spec("time_context", bool, "ai_chat_time_context", True, "让模型知道当前时间", "基础",
         hint="每次回复都把「现在几点、星期几、什么时段」附在 prompt 末尾，"
              "它才答得出时间类问题、也分得清该说早安还是晚安。"
              "关掉则它对时间一无所知（问它现在几点只会瞎猜）"),
    # ---------------------------------------------------------- 时间校准
    # 机器时钟会漂，而它不只影响"答错时间" —— 定时问候（greetings）判断"到 08:00 了吗"
    # 用的就是这个钟，差 8 小时就等于早安安在半夜。参考目录里的示例项目
    # （renderer.js 的 ntpOffset / nowCal）早就用"偏移量校准"解决了这件事，
    # 这里补上同一套：不改系统时钟，只存一个 offset，所有时间读取都加它。
    Spec("ntp_enabled", bool, "ai_chat_ntp_enabled", True, "用 NTP 校准时间", "时间校准",
         hint="启动后同步一次，之后按间隔定期同步。校准只影响本程序的读数，"
              "**不改系统时钟**（那需要管理员权限，还会影响同机其它程序）。"
              "关闭或同步失败时自动退回系统时钟，不影响任何功能"),
    Spec("ntp_servers", str, "ai_chat_ntp_servers",
         "ntp.aliyun.com,ntp1.aliyun.com,cn.pool.ntp.org,time.windows.com",
         "NTP 服务器（逗号分隔）", "时间校准",
         hint="按顺序试，成功即止。国内建议用阿里云的两个；"
              "全失败会保留上一次的偏移（时钟不会因为断网就变准）"),
    Spec("ntp_sync_interval", int, "ai_chat_ntp_sync_interval", 3600, "同步间隔（秒）", "时间校准",
         60, 86400, hint="默认 1 小时。机器时钟漂移通常很慢，太频繁没有意义"),
    Spec("ntp_retries", int, "ai_chat_ntp_retries", 2, "每次同步的轮数", "时间校准", 1, 10,
         hint="一轮 = 把所有服务器依次试一遍。全失败会保留上一次的偏移并记日志"),
    # ---------------------------------------------------------- 上下文
    Spec("session_gap", int, "ai_chat_session_gap", 300, "会话切分间隔（秒）", "上下文", 30, 86400,
         "两条消息间隔超过它就算新的一轮，上一轮未读降级为已读"),
    Spec("read_budget", int, "ai_chat_read_budget", 2000, "已读背景字符预算", "上下文", 100, 20000,
         "从最新往回累加，装不下的更早记录丢弃"),
    Spec("msg_clip", int, "ai_chat_msg_clip", 120, "单条消息截断长度", "上下文", 0, 2000),
    # ---- 多条发送（本轮新增）----
    # 用户要的是"多个短句不要一次发完，切开分次发"，像真人打字那样。
    Spec("sentence_split", bool, "ai_chat_sentence_split", True, "多个短句分多条发", "上下文",
         hint="一次回答里有好几个短句时，逐句分次发（每条之间停一下），"
              "而不是挤成一大段一次性砸出去。关掉则只在超过长度上限时才分段"),
    Spec("sentence_split_min", int, "ai_chat_sentence_split_min", 3, "至少几句才拆", "上下文", 2, 10,
         hint="只有两句的回复拆开反而显得挤牙膏。默认 3 句起才拆"),
    Spec("sentence_split_len", int, "ai_chat_sentence_split_len", 40, "每句不超过多少字才拆",
         "上下文", 10, 200,
         hint="有一句超长就不拆（拆出来仍是一条长消息，白拆）。"
              "配合人设里「闲聊 1~2 句、十几到四十字」，默认 40 正好覆盖"),
    Spec("sentence_merge_under", int, "ai_chat_sentence_merge_under", 4,
         "太短的句子并回上一条（字数）", "上下文", 0, 100,
         hint="只用来防止「嗯。」「哦。」这种纯语气应答单独发一条、显得像刷屏。"
              "**别调大** —— 调到 6 就会把「先看耐压。」（5 字）这类正常短句也并掉，"
              "于是三句回答只剩两条，分条就没意义了。设 0 = 不合并"),
    Spec("sentence_split_delay", float, "ai_chat_sentence_split_delay", 0.9,
         "分条之间的间隔（秒）", "上下文", 0.2, 10.0,
         hint="像真人打字那样停一下。太快会被风控判成刷屏，太慢会显得卡"),
    # ---- 回复长度上限 ----
    # **代码保证，不靠提示词**（与 behavior.py 同一个理由：抽象规则压不过长人设）。
    # 截断按句边界做，宁短一句不断半句；超限记日志，便于观察是不是人设在教它写长。
    Spec("reply_max_chars", int, "ai_chat_reply_max_chars", 60, "整轮回复字数上限", "上下文", 0, 2000,
         hint="普通闲聊的硬上限，超了按句截断。0 = 不限。默认 60：群里一条正常回复十几到几十字，"
              "再长就像在念稿。人设里已经写了「短」，这一项是兜底而不是替代"),
    Spec("reply_max_chars_detail", int, "ai_chat_reply_max_chars_detail", 400,
         "问正事时的字数上限", "上下文", 0, 4000,
         hint="以下任一情况改用这个上限：当前模式=专注（对方在问技术/故障/要方案）、"
              "对方在问你的设定或机制、回答里有代码块或表格。0 = 沿用上面的闲聊上限"),
    Spec("reply_to_images", bool, "ai_chat_reply_to_images", False,
         "对纯图片消息也回应", "上下文",
         hint="默认关闭：只发图不配字时保持安静（免得一句「在的」把话头打断）。"
              "开启后会真的看图并回应，费视觉 token"),
    Spec("chat_vision", bool, "ai_chat_chat_vision", True, "对话时把图给模型看", "上下文",
         hint="被 @ 时附带（或引用）的图片都会送进模型。关掉则图片只进表情包库，不参与理解"),
    # ---------------------------------------------------------- 文件
    Spec("file_enabled", bool, "ai_chat_file_enabled", True, "读取消息里的文件", "文件",
         hint="群里 / 私聊发来的小文本文件会被读进对话上下文；二进制只报元信息"),
    Spec("file_max_kb", int, "ai_chat_file_max_kb", 4096, "最大读取大小（KB）", "文件", 1, 10240,
         hint="超过这个大小只报文件名和体积，不下载内容。**默认 4MB** —— 原来 512KB 太小："
              "实测一个 767KB 的论文 PDF 直接被判超限，它本该走「二进制→只报元信息」，"
              "结果连元信息之外的信息都没给出，看起来就像「读不到文件」"),
    Spec("file_max_chars", int, "ai_chat_file_max_chars", 8000, "文件内容截断长度（字符）",
         "文件", 200, 200000, hint="再长就截断，并注明原文件总长"),
    # ---------------------------------------------------------- 人设
    # persona.txt 正文只留「基础底色」（元气/开朗/软/爱撒娇 + 格式规则）。
    #
    # **这里曾经有三个概率项**（flavor_chance / rice_chance / fat_react_chance）：
    # 把"对吃的执念""提米饭""被说胖的反应"做成按概率临时注入的心情。
    # 已经**整条摘掉**，原因有两条：
    #   1. 它们早已不生效 —— 注入点在 `config.system_prompt()`，而对话走的是
    #      `context.system_prompt()`，那个函数从头到尾没有调用方（死代码）；
    #      实测 200 轮对话命中 0 次，但 `/机制 风格` 还在如实汇报这几个概率，
    #      等于对着用户描述一套不存在的机制；
    #   2. 留着一条"随时会冒出来的性格"本身也是负担：同一句「今天吃什么」
    #      有时答米饭、有时不答，看起来像不稳定，而不像有趣。
    # 现在这些人格细节写在三个文件里（底层人设 / 禁止事项 / 表层人设）。
    #
    # 人格分层之后：`style_note_max`（`/风格 <一句话>` 的条数上限）与整套运行时
    # 槽位一起**删掉了** —— 人格改成三层文件之后，聊天里不再有改人设的入口，
    # 那个上限也就没有意义。旧值仍在 `data/runtime/settings.json` 里，不再读取。
    # ---------------------------------------------------------- 人设信号（自我迭代第 1 步）
    # 路线 A 第 1 步：**只记账，不改人设**。把「他纠正我的说话方式」记进一本账，
    # 用来回答"阈值该定 2 次还是 3 次"。
    Spec("persona_signal_enabled", bool, "ai_chat_persona_signal_enabled", True,
         "记录人设信号（只观察）", "人设",
         hint="把「别那么啰嗦」「叫我哥哥」这类**说话方式**的要求记进账本，"
              "**不会改任何人设**。它的用途是积累数据、也是自我迭代最看重的输入。"
              "内容偏好（喜欢什么）走长期记忆，不在这里。用 /人设 信号 看账本"),
    # ---------------------------------------------------------- 人格自我迭代（已停用）
    # 2026-10-24 起，旧的「定期反思 → 候选池」自动迭代被删除；
    # 人设修订改由**评估结果**驱动（见下面「人设评估台」与 persona_revise.py）。
    # 旧参数不再注册，`settings.json` 里残留的同名键会被忽略。
    # ---------------------------------------------------------- 表层预算（闸门）
    # 表层的**条目级**长度早就限了（`persona._MIN_ITEM` / `_MAX_ITEM`），但**总量**没有上限：
    # 6 小时一轮 × 每轮最多 2 条 = 一天最多 8 条，而它是唯一会自己长大的 prompt 段落。
    # 2026-09-28 补的闸门：超限就**拒绝写入**并记 rejected(over_budget)，由人来清理
    # —— 谁该被淘汰没有客观依据，所以不自动删（与长期记忆的自动淘汰刻意不同）。
    Spec("surface_max_chars", int, "ai_chat_surface_max_chars", 1500,
         "表层人设字数上限（超了就拒绝继续写）", "人设", 200, 8000,
         hint="只算表层这一层。到顶时自动迭代会**拒绝写入**并在通知里说明，"
              "你要么删几条过时的，要么把这个数调大。0 = 不限（不推荐）"),
    Spec("surface_max_items", int, "ai_chat_surface_max_items", 60,
         "表层人设条目数上限（`- ` 开头的行）", "人设", 0, 500,
         hint="与字数上限同时生效，任一超了就拒绝写。0 = 不限"),
    # ---------------------------------------------------------- 注意力
    # 唯一一套「看内容」的触发：被唤醒后记住话题，后续发言按相关性累积注意力，
    # 超过阈值就自己接话。另外三套（@/唤醒词、随机插话、主动发言）都跟内容无关。
    Spec("attention_enabled", bool, "ai_chat_attention_enabled", True, "注意力机制", "注意力",
         hint="被唤醒后记住话题，相关发言累积注意力，够了就不用再 @ 它"),
    Spec("attention_window", int, "ai_chat_attention_window", 600,
         "注意力活跃期（秒）", "注意力", 60, 86400,
         hint="唤醒后多久内还算在聊这个话题；超时就把注意力清零"),
    Spec("attention_initial", float, "ai_chat_attention_initial", 0.7,
         "初始注意力值", "注意力", 0.0, 1.0,
         hint="刚被唤醒时给多少。调高 → 一上来就容易接话；调低 → 得聊够久它才开口"),
    Spec("attention_threshold", float, "ai_chat_attention_threshold", 0.8,
         "触发接话的阈值（起点）", "注意力", 0.1, 1.0,
         hint="这是刚唤醒时的门槛。随着话题推进它会自动抬高（最多 +0.15），"
              "所以越到后面越需要高相关性"),
    Spec("attention_interval", int, "ai_chat_attention_interval", 20,
         "两次相关性评估的最小间隔（秒）", "注意力", 5, 600,
         hint="每条消息都调模型太贵，靠它限流"),
    Spec("attention_min_chars", int, "ai_chat_attention_min_chars", 6,
         "太短的消息不评估（字数）", "注意力", 0, 200),
    Spec("attention_reply_cooldown", int, "ai_chat_attention_reply_cooldown", 120,
         "回复后的注意力静默期（秒）", "注意力", 0, 3600,
         hint="刚回完这一轮，接下来几句归「对话租约」管，注意力不参与 —— "
              "省一次评估，也避免同一段对话里出现两个声音。设 0 = 不静默（退回旧行为）"),
    Spec("attention_max_evals", int, "ai_chat_attention_max_evals", 10,
         "单次聚焦最多评估几次", "注意力", 0, 200,
         hint="光靠「间隔 × 活跃期」挡不住成本：线上曾出现 interval=10 + window=600 "
              "= 最坏 60 次评估（约 2 万输入 token）。到顶就结束这次聚焦，"
              "想再让它「在场」重新叫一次即可。设 0 = 不限"),
    # ---- 对话租约 ----
    # 注意力只能"旁听时插话"，做不到"连续问答"：回复末尾 relax() 减半后
    # 有效值只有 0.35，阈值 0.8，一问一答必在第二轮断掉。租约是补这一层的。
    Spec("attention_lease_enabled", bool, "ai_chat_attention_lease_enabled", True,
         "对话租约（叫过一次后接着聊不必再 @）", "注意力",
         hint="被 @ 或唤醒后，接下来若干秒内**同一个人的**追问会自动回应，不用每句都 @。"
              "纯本地判断、不花 token；只影响群聊（私聊本来就每条都算在跟它说话）"),
    Spec("attention_lease_seconds", int, "ai_chat_attention_lease_seconds", 180,
         "租约有效期（秒）", "注意力", 30, 3600,
         hint="每一轮回应都会重新计时。所以它约束的是「沉默多久算话题结束」，"
              "而不是整段对话的总时长（总时长由轮数上限管）"),
    Spec("attention_lease_max_turns", int, "ai_chat_attention_lease_max_turns", 12,
         "租约最多用几轮", "注意力", 1, 200,
         hint="超过就用完作废，需要重新 @ 一次。防止一个人连着聊很久时它一直回"),
    Spec("attention_lease_anyone", bool, "ai_chat_attention_lease_anyone", False,
         "租约对该会话所有人有效", "注意力",
         hint="默认只认发起租约的那个人（否则群里任何人说话都会被当成在跟它聊，等于话痨）。"
              "主人始终可以接着任何租约说下去"),
    Spec("attention_lease_persist", bool, "ai_chat_attention_lease_persist", True,
         "租约落盘（重启后仍在）", "注意力",
         hint="线上是常驻服务，重建容器不该把正在进行的问答打断。"
              "落盘到 data/attention_state.json；过期的条目启动时会丢弃并记日志"),
    Spec("attention_persist", bool, "ai_chat_attention_persist", True,
         "注意力话题落盘（重启后仍在）", "注意力",
         hint="与租约同一个文件、两个键。**默认开**：`_focus` 以前是纯内存态，"
              "重启/重建容器时静默清零 —— 表现是「刚才还在跟着聊，重启后突然不接了」，"
              "而且日志里什么都没有。超过注意力活跃期的话题不恢复（本来也该清零）；"
              "图片缓存与回复后的短静默仍然不落盘"),
    # ---------------------------------------------------------- 唤醒
    Spec("wake_enabled", bool, "ai_chat_wake_enabled", True, "关键词唤醒", "唤醒",
         hint="群里提到唤醒词时，即使没 @ 它也可能被叫出来"),
    Spec("wake_words", str, "ai_chat_wake_words", "大肥鱼,肥鱼,小鲸鱼,鲸鱼娘",
         "唤醒词", "唤醒",
         hint="逗号分隔。匹配是包含式的，所以「肥鱼」能命中「大肥鱼」「肥鱼!」"),
    Spec("wake_chance", float, "ai_chat_wake_chance", 0.7, "被提到时的回应概率", "唤醒", 0.0, 1.0,
         hint="1.0 = 每次提到都回；调低可以避免它太吵"),
    Spec("wake_cooldown", int, "ai_chat_wake_cooldown", 60, "两次唤醒的最小间隔（秒）", "唤醒", 0, 3600,
         hint="防止有人刷屏喊它时它跟着一起刷屏"),
    # ---------------------------------------------------------- 表情包
    Spec("sticker_enabled", bool, "ai_chat_sticker_enabled", True, "启用表情包库", "表情包"),
    Spec("sticker_chance", float, "ai_chat_sticker_chance", 0.25, "回复时附表情包的概率", "表情包", 0.0, 1.0),
    Spec("sticker_judge", bool, "ai_chat_sticker_judge", True, "用模型打喜好分决定是否入库", "表情包",
         hint="关掉则所有通过初筛的图都入库"),
    Spec("sticker_min_score", float, "ai_chat_sticker_min_score", 0.6, "入库所需的最低喜好分", "表情包", 0.0, 1.0),
    Spec("sticker_max_kb", int, "ai_chat_sticker_max_kb", 2048, "超过此大小的图不入库（KB）", "表情包", 16, 10240),
    Spec("sticker_rate_limit", int, "ai_chat_sticker_rate_limit", 6, "每群每分钟最多入库几张", "表情包", 1, 120,
         "挡住刷图，也省模型打分 token"),
    Spec("sticker_vision", bool, "ai_chat_sticker_vision", True, "让模型真看图打分", "表情包",
         hint="deepseek-flash / deepseek-chat 支持读图；deepseek-v4-pro 不接受读图"
              "（接口收得下图片，模型会回「我无法查看这张图片」），开了也白花 token"),
    Spec("sticker_vision_max_kb", int, "ai_chat_sticker_vision_max_kb", 4096,
         "传图给模型的大小上限（KB）", "表情包", 16, 8192,
         hint="打分和对话读图共用这条上限；超限会自动退回纯文本模式。"
              "默认 4096KB（4MB）—— 按需求调大以让群里的原图真能被看到（2026-10-02）。"
              "⚠ 上限是**下载上限 `stickers._DOWNLOAD_LIMIT`（8MB）**："
              "合法取值必须小于 8MB，否则 8MB 以上的图会被下载层直接丢掉（截断的图会被视觉接口判非法），"
              "这道闸门就永远轮不到生效。这也解释了区间上限为什么写 8192。"
              "⚠ 图的视觉 token 大致随像素线性增长，4MB 的原图每次请求要明显多花 token 和延迟；"
              "只在意成本时可以调小。"),
    # ---- 近似去重（改造后新增）----
    # 原来的去重是 SHA-256 精确比对，同一张表情被压缩/改尺寸/转格式之后
    # 字节全变，就会被当成两张各存一份。这两个参数管的是"内容一样就算重复"。
    Spec("sticker_near_dup", bool, "ai_chat_sticker_near_dup", True, "近似去重（认内容不认字节）",
         "表情包",
         hint="用感知哈希判断「看起来是不是同一张图」。需要 Pillow + numpy；"
              "没装时自动退回精确去重（只是认不出改过编码的同一张图）"),
    Spec("sticker_dup_distance", int, "ai_chat_sticker_dup_distance", 8,
         "近似重复的判定距离（0~64）", "表情包", 0, 24,
         hint="64 位感知哈希的汉明距离，越小越严。同一张图被重新压缩/缩放通常差 0~6 位；"
              "7~12 是「很像但不一定同一张」。调大到 12 以上会开始误杀相似但不同的表情"),
    # ---- 发送形式权重（改造后新增）----
    Spec("sticker_file_penalty", float, "ai_chat_sticker_file_penalty", 0.3,
         "以文件形式发来的图降权", "表情包", 0.0, 1.0,
         hint="QQ 里用「发送文件」选一张图时，上报的是 file 段、sub_type 全是 0，"
              "看不出是不是表情包。这类通常是照片/素材原图，所以打分时直接扣掉这么多分"
              "（默认 0.3 → 阈值 0.6 时它得考到 0.9 才入库）"),
    # ---- 按语境发送（改造后新增）----
    Spec("sticker_pick_by_context", bool, "ai_chat_sticker_pick_by_context", True,
         "按语境挑图（而不是随机发）", "表情包",
         hint="发图前把当前对话和几张候选图的**真实画面**交给模型，由它挑贴题的那张，"
              "并且**允许它说不合适、这次不发**。这是「不发图文无关内容」的保证；"
              "关掉则退回随机取图（会发不出贴题的图）"),
    Spec("sticker_pick_candidates", int, "ai_chat_sticker_pick_candidates", 6,
         "挑图时给模型看几张候选", "表情包", 1, 20,
         hint="候选太多会让模型选择质量下降，也太贵。先本地按「少用的优先」抽样，再让它定夺"),
    Spec("sticker_pick_context_chars", int, "ai_chat_sticker_pick_context_chars", 600,
         "挑图时带多少字上下文（0=不裁剪）", "表情包", 0, 4000,
         hint="只保留**最近**的这些字 —— 最近说的才是「眼下这句话」，给太长会稀释重点。"
              "设 0 = 把完整语境都交给它（长对话下会更贵）"),
    # ---------------------------------------------------------- 长期记忆
    # 这一组是新加的：把"说过的事"沉淀成跨会话、跨重启的事实条目。
    # 检索完全在本地做（字符重叠 + 重要度 + 时效），所以开着的 token 代价只是
    # 每轮多几百字 prompt；真正花 token 的是 memory_extract，那条另有每日上限。
    Spec("memory_enabled", bool, "ai_chat_memory_enabled", True, "启用长期记忆", "长期记忆",
         hint="开着它才会记得跨越很多轮和很多天的事。关掉则退回改造前的行为"
              "（只看聊天记录里最近的一段）"),
    Spec("memory_recall_count", int, "ai_chat_memory_recall_count", 6, "每次带入几条记忆", "长期记忆",
         0, 30, hint="按相关度 + 重要度 + 时效挑选，纯本地计算，不花 token"),
    Spec("memory_min_score", float, "ai_chat_memory_min_score", 0.18, "记忆入选分数线", "长期记忆",
         0.0, 1.0, hint="调低 → 什么都想得起来（也可能想起无关的）；调高 → 只在明显相关时才提"),
    Spec("memory_half_life_days", int, "ai_chat_memory_half_life_days", 30, "记忆半衰期（天）", "长期记忆",
         1, 3650, hint="越久以前的事权重越低。30 天意味着一个月前的记忆权重减半"),
    Spec("memory_events", bool, "ai_chat_memory_events", True, "启用群事件记忆", "长期记忆",
         hint="记「这个群里发生过什么」，只在本会话内检索，不跨群串"),
    Spec("memory_events_days", int, "ai_chat_memory_events_days", 180,
         "群事件保留天数", "长期记忆", 0, 3650,
         hint="按时间窗淘汰「这个会话里发生过的」。原来它跟事实条目共用 memory_max_items，"
              "而且是**只增不减**（见 README 5.6.6.2）；0 = 不限"),
    Spec("memory_events_max", int, "ai_chat_memory_events_max", 2000,
         "群事件条数上限（兜底）", "长期记忆", 0, 100000,
         hint="时间窗之外的第二道闸，防病态增长。0 = 不限"),
    Spec("memory_scope_isolation", bool, "ai_chat_memory_scope_isolation", True,
         "记忆按会话隔离", "长期记忆",
         hint="开着时：私聊里说的事不会在群里被它提起，A 群的事不会串到 B 群（见 README 5.6.6.3）。"
              "关掉则退回改造前的全局可见"),
    Spec("memory_store", str, "ai_chat_memory_store", "sqlite", "记忆库后端", "长期记忆",
         choices=("sqlite", "json"),
         hint="sqlite = 按行写入（默认）；json = 改造前那个整份重写的 memories.json，"
              "留作回滚与人工查看。切换后端**不会自动搬数据**，用 _工具链/维护/_记忆库迁移.py"),
    # ---------------------------------------------------------- 翻旧账（消息索引）
    # 补的是「盘上有全量记录、却没有任何路径能捞回 prompt」这个洞（README §7.4）。
    # 与长期记忆的分工：索引管「原话怎么说的」，记忆管「我一直知道什么」。
    Spec("msgindex_enabled", bool, "ai_chat_msgindex_enabled", True, "启用聊天记录索引",
         "翻旧账", hint="给每条消息建词面索引，于是「你上周是不是说过…」能查出原话。"
                        "索引是派生物，删掉会自动重建"),
    Spec("msgindex_interval", int, "ai_chat_msgindex_interval", 600, "索引更新间隔（秒）",
         "翻旧账", 120, 86400, hint="增量补索引，跟回复路径无关；慢一点没影响"),
    Spec("msgindex_bootstrap", int, "ai_chat_msgindex_bootstrap", 30,
         "启动时最多补几个会话的历史索引", "翻旧账", 0, 1000,
         hint="**必须有这一步**：进程刚启动时内存里没有旧会话，只靠增量索引的话"
              "重启后旧记录永远不会被索引（0 = 不补，只索引新消息）"),
    Spec("msgindex_limit", int, "ai_chat_msgindex_limit", 5, "一次翻出几条旧记录",
         "翻旧账", 1, 30, hint="带进 prompt 的条数上限。调大会挤占最近记录的预算"),
    Spec("msgindex_days", int, "ai_chat_msgindex_days", 0, "翻旧账只看最近几天",
         "翻旧账", 0, 3650, hint="0 = 不限（默认）。设成 30 就只翻一个月内的"),
    Spec("recall_multi_angle", bool, "ai_chat_recall_multi_angle", True,
         "回忆类问题时多角度检索", "长期记忆",
         hint="被问「你还记得吗」这类问题时，额外用「关系 / 事件 / 心情」三种问法各召回一次再合并，"
              "提高想起来的机会（代价：每次多 2 次本地检索，不花 token；改写问句才花一次小请求）"),
    Spec("memory_extract", bool, "ai_chat_memory_extract", True, "自动从聊天里提炼记忆", "长期记忆",
         hint="回复发出之后异步跑，不占用回复等待时间。这是这一组里唯一额外花 token 的项"),
    Spec("memory_extract_per_day", int, "ai_chat_memory_extract_per_day", 200,
         "每天最多自动提炼几次", "长期记忆", 0, 10000,
         hint="0 = 关闭自动提炼（仍可用 /记忆 存 手动记）。口径是**消息条数**，"
              "不是调用次数 —— 一次提炼最多处理 memory_extract_batch 条消息"),
    Spec("memory_extract_max", int, "ai_chat_memory_extract_max", 3, "单次最多记几条", "长期记忆", 1, 20),
    Spec("memory_extract_batch", int, "ai_chat_memory_extract_batch", 40,
         "每次提炼最多看多少条消息", "长期记忆", 5, 200,
         hint="从「上次抽到哪」往后取这么多条。调大能更快补上积压，但单次请求也更大"),
    Spec("memory_extract_drain", bool, "ai_chat_memory_extract_drain", True,
         "后台补抽积压的记忆", "长期记忆",
         hint="每隔一段时间把「有人聊过但还没提炼」的记录补上。关掉后只在回复之后提炼，"
              "没人理它的那些长对话就不会进记忆库"),
    Spec("memory_extract_drain_interval", int, "ai_chat_memory_extract_drain_interval", 1800,
         "后台补抽间隔（秒）", "长期记忆", 300, 86400,
         hint="慢一点更省额度；它要补的是「没人理的对话」，不急于几分钟内抽完"),
    Spec("memory_max_items", int, "ai_chat_memory_max_items", 800, "记忆条数上限", "长期记忆", 0, 100000,
         hint="超了按「重要度 + 时效」淘汰最差的；被 /记忆 保护 的永不淘汰。0 = 不限"),
    Spec("memory_used_flush_seconds", int, "ai_chat_memory_used_flush_seconds", 60,
         "「被想起次数」落盘间隔（秒）", "长期记忆", 5, 3600,
         hint="排序里有一项分给「被反复用到的条目」，那个计数每一轮都在涨。"
              "攒着批量写盘，避免每轮一次磁盘写；0 会被当成 5 秒"),
    # ---------------------------------------------------------- 会话摘要
    # 这一组补的是「被丢掉的更早背景」：read_budget 装不下的记录原本只剩一句
    # 「更早的 N 条闲聊已省略」，现在会在每轮会话结束时被归纳成一两句。
    # 原文一条都不删，摘要只是「回顾入口」。
    Spec("summary_enabled", bool, "ai_chat_summary_enabled", True, "启用会话摘要", "会话摘要",
         hint="每轮对话结束时归纳一两句，接在被丢弃的更早记录后面。"
              "关掉后时间跨度大的话题就只能靠长期记忆兜底"),
    Spec("summary_min_messages", int, "ai_chat_summary_min_messages", 3, "少于几条不总结",
         "会话摘要", 1, 50, hint="太短的一轮（「嗯」「哈哈」那几句）不值得花一次请求"),
    Spec("summary_recall_count", int, "ai_chat_summary_recall_count", 3, "每次带入几轮摘要",
         "会话摘要", 0, 20),
    Spec("summary_budget", int, "ai_chat_summary_budget", 400, "摘要字符预算", "会话摘要",
         0, 4000, hint="从最新的一轮往回装，装不下的更早摘要舍弃"),
    Spec("summary_max_per_conv", int, "ai_chat_summary_max_per_conv", 40,
         "每个会话最多留几轮摘要", "会话摘要", 1, 1000,
         hint="摘要天然只增不减（每轮会话结束就多一条），所以必须有上限，"
              "超了从最旧的丢。调大能回顾更久，但那个 JSON 会更大、每次读盘更慢"),    # ---------------------------------------------------------- 图片策略
    Spec("image_policy_enabled", bool, "ai_chat_image_policy_enabled", True, "允许对话里改图片策略",
         "图片策略",
         hint="关掉后 /图 指令与「不要保存这张图片」这类自然语言都不再生效"),
    Spec("command_natural", bool, "ai_chat_command_natural", False, "自然语言也能当指令", "指令",
         hint="默认关闭：只认 /图 /记忆 这类斜杠指令，避免误会一句话就把状态改了。"
              "打开后，「不要保存这张图片」这种说法也会被执行。"
              "**注意：人格相关的自然语言任何时候都不会改人设**（见 README 5.6.7.2）"),
    Spec("command_prefix_required", bool, "ai_chat_command_prefix_required", False,
         "指令必须带斜杠", "指令",
         hint="默认关闭：/图 忽略 和 图 忽略 都认（手机上打斜杠麻烦）。"
              "打开后必须带 /，免得群里正常一句话被当成指令"),
    # ---------------------------------------------------------- 联网搜索
    # 用户要的是"某个词在这句话里意义不明或突兀时主动去搜"。
    # 主通道是 function calling（模型最清楚自己哪里不确定），
    # 兜底是本地启发式预取（模型没调但看着确实需要时先搜一次）。
    Spec("search_enabled", bool, "ai_chat_search_enabled", False, "启用联网搜索", "联网搜索",
         hint="关掉时 `web_search` 工具压根不提供给模型 —— 它也就不会假装自己搜过。"
              "开着还需要配好 search_endpoint"),
    Spec("search_backend", str, "ai_chat_search_backend", "deepseek", "搜索后端", "联网搜索",
         choices=("deepseek", "tavily", "json"),
         hint="deepseek = **官方 Search 接口**（2026-10-17 开放，自研索引，一次请求连正文一起回，"
              "按次计费；key 复用 DEEPSEEK_API_KEY）；"
              "tavily = 商业 API，去 tavily.com 申请 key（历史配置兼容）；"
              "json = 任何返回 {\"results\": [...]} 的端点"),
    Spec("search_endpoint", str, "ai_chat_search_endpoint", "", "搜索端点 URL", "联网搜索",
         hint="deepseek **留空即可**（用 DEEPSEEK_BASE_URL + /v1/tools/search）；"
              "tavily **留空即可**（用内置官方地址）；"
              "json 填完整查询地址，可用 {query} 占位符。留空且非 deepseek/tavily = 不启用搜索"),
    Spec("search_api_key", str, "ai_chat_search_api_key", "", "搜索 API Key", "联网搜索",
         secret=True,
         hint="deepseek / tavily 留空时：deepseek 用 `.env` 的 DEEPSEEK_API_KEY，"
              "tavily 则必须在这里填。json 后端留空。"
              "这个值不会出现在任何回复或机制说明里；控制台里显示为 ***（留 `***` 不动就是不改）"),
    Spec("search_depth", str, "ai_chat_search_depth", "basic", "Tavily 检索深度", "联网搜索",
         choices=("basic", "advanced"),
         hint="只对 tavily 生效。basic = 便宜快；advanced = 更全但更贵更慢"),
    Spec("search_max_results", int, "ai_chat_search_max_results", 5, "最多带回几条结果",
         "联网搜索", 1, 10,
         hint="每条摘录都会被截断；条数太多会把 prompt 撑满、也更容易带入无关内容"),
    Spec("search_timeout", int, "ai_chat_search_timeout", 12, "搜索超时（秒）", "联网搜索", 3, 60,
         hint="超时就当没搜到（模型会如实说查不到），不会阻塞整条回复"),
    Spec("search_rate_limit", int, "ai_chat_search_rate_limit", 10,
         "每会话每分钟最多搜几次", "联网搜索", 1, 120,
         hint="挡住刷屏式搜索；用尽了会如实告诉模型「搜索次数用完了」"),
    Spec("search_max_per_message", int, "ai_chat_search_max_per_message", 2,
         "单条消息最多联网几次", "联网搜索", 1, 5,
         hint="**搜索与读正文共用这个上限**（都是联网动作）。工具循环里的硬性上限，"
              "防止模型连着搜个没完"),
    Spec("search_read_enabled", bool, "ai_chat_search_read_enabled", True, "搜索时自动读正文",
         "联网搜索",
         hint="deepseek 后端：勾上就在搜索请求里带 `need_content`，**正文随结果一起回来**"
              "（不额外计费）；tavily/json 后端：勾上则本地打开前几条链接读正文 —— "
              "只有摘要时它常常答不出细节。关掉则完全靠模型自己调 web_fetch"),
    Spec("search_read_results", int, "ai_chat_search_read_results", 3, "自动读前几条正文",
         "联网搜索", 0, 5,
         hint="只读最靠前的几条（答案多半就在里面）。读得越多越慢、prompt 也越满。0 = 不自动读"),
    Spec("search_read_chars", int, "ai_chat_search_read_chars", 800, "单条正文最多几字",
         "联网搜索", 200, 8000,
         hint="正文是给模型看「这段讲了什么」，不是让它抄全文。太长会把 prompt 撑满、"
              "也更容易把无关内容带进来"),
    Spec("search_read_timeout", int, "ai_chat_search_read_timeout", 8, "读网页超时（秒）",
         "联网搜索", 2, 30,
         hint="只影响读正文（预读会并发读，所以总等待约等于这个值）。"
              "读不到就退回摘要，不影响这次回答"),
    # ---- 官方 Fetch 接口：直连读不到时的第一道兜底 ----
    # 直连免费但常被反爬挡（机房 IP 段）；官方接口按次计费但一次往返就拿到 Markdown，
    # 比"起浏览器渲染 + OCR"快一个数量级，也不吃本机那份紧张的内存。
    Spec("fetch_api_fallback", bool, "ai_chat_fetch_api_fallback", True,
         "读不到时用官方抓取接口", "联网搜索",
         hint="直连被反爬拒绝/超时/无正文后，调 DeepSeek 的 `/v1/tools/fetch` 让**服务端**去抓，"
              "直接回 Markdown 正文。按次计费（5 元/千次），失败不计费。"
              "关掉则直接退回下面的渲染兜底"),
    # ---- 渲染兜底：上面两条都读不到时，用真浏览器截图 + 本地 OCR ----
    # 为什么还留着：极端情况下官方接口也可能拿不到（登录墙、纯 JS 渲染），
    # 而真浏览器往往能过 —— 它是最后一道，不是第一道。
    # 为什么只做兜底：单页 5–15s、峰值内存 200–300MB —— 本机可用内存常在 700MB 上下。
    Spec("search_render_enabled", bool, "ai_chat_search_render_enabled", True,
         "浏览器渲染兜底", "联网搜索",
         hint="HTTP 与官方接口都读不到时，用无头 Chromium 打开页面截图，再用本地 OCR 认出文字。"
              "**单页要 5–15 秒**，所以只在前面都失败后才走。"
              "关掉则只能读那些不反爬的站点"),
    Spec("search_render_timeout", int, "ai_chat_search_render_timeout", 45,
         "渲染超时（秒）", "联网搜索", 10, 120,
         hint="含浏览器启动 + 导航 + 截图 + OCR 的总预算。"
              "首次调用要冷启动 Chromium，会慢几秒；之后浏览器复用"),
    Spec("search_render_chars", int, "ai_chat_search_render_chars", 1500,
         "渲染正文最多几字", "联网搜索", 300, 8000,
         hint="OCR 出来的文字上限。OCR 会把导航、按钮也认进来，所以比普通正文给得稍宽"),
    Spec("search_prefetch", bool, "ai_chat_search_prefetch", True, "本地兜底预取", "联网搜索",
         hint="模型没调工具、但消息里明显有「不认识的词」或「需要最新信息」时，"
              "先搜一次把结果塞进 prompt。关掉则完全依赖模型自己调工具"),
    Spec("search_show_note", bool, "ai_chat_search_show_note", True, "回话时说明查过了",
         "联网搜索",
         hint="让它偶尔带一句「我查了下」—— 这样说错了别人也知道这是网上来的，"
              "而不是它自己编的"),
    Spec("search_note_prefix", str, "ai_chat_search_note_prefix", "（我查了下）",
         "说明前缀", "联网搜索",
         hint="上一条打开时，用它加在回答最前面。**由代码加，不靠提示词** —— "
              "靠提示词它经常忘。留空则不加"),
    # ---- 搜索释义库（本轮新增）----
    # 只沉淀"某个名词是什么意思"的结论，单独落盘 search_memory.json。
    # 跟长期记忆分家的理由见 search_memory.py 的说明：寿命与失效条件完全不同。
    Spec("search_memory_enabled", bool, "ai_chat_search_memory_enabled", True,
         "记住搜索到的名词释义", "联网搜索",
         hint="查过的名词会沉淀成一句释义，下次不用重复联网。**只存总结，不存网页原文**；"
              "只有「X 是什么」这类查询才入库，天气/股价这类不存"),
    Spec("search_memory_min_confidence", float, "ai_chat_search_memory_min_confidence", 0.5,
         "低于此分标为低置信度", "联网搜索", 0.0, 1.0,
         hint="置信度是**代码按可观测信号算的**（几个独立来源、有无含糊措辞、来源是否像百科），"
              "不是让模型自评。低于这条线的释义仍会保留并注入，但会显眼标注"
              "「低置信度，别当准的用」"),
    Spec("search_memory_ttl_days", int, "ai_chat_search_memory_ttl_days", 30,
         "释义有效期（天）", "联网搜索", 0, 3650,
         hint="超过就不再用它回答（会重新联网）。释义会比人的喜好过时得快，"
              "所以必须带有效期。0 = 永不过期"),
    Spec("search_memory_max", int, "ai_chat_search_memory_max", 500, "释义库条数上限",
         "联网搜索", 0, 100000,
         hint="超了先淘汰**低置信**的，再淘汰最旧的。0 = 不限"),
    # ---------------------------------------------------------- 说话模式
    # 参考方案文档第 5 条「人格模式切换」：模式应当影响回复长度、语气和主动发言概率。
    # 这里不做"人设切换"，只调可量化的几个行为参数 —— 人设决定她是谁，模式决定这会儿多用力。
    Spec("mode_enabled", bool, "ai_chat_mode_enabled", True, "启用说话模式", "说话模式",
         hint="按场合自动调整语气与长度（日常 / 专注 / 安慰）。关掉则始终按日常状态说话。"
              "也可以用 /模式 专注 手动指定，一直生效到 /模式 自动"),
    Spec("mode_min_margin", int, "ai_chat_mode_min_margin", 1, "判定为「专注」所需的最低票数",
         "说话模式", 1, 6,
         hint="本地按关键词打分，票数不够就**不判定**、交给模型按人设自己判断。"
              "调高 = 更保守（更难进专注模式）"),
    Spec("mode_sticky_seconds", int, "ai_chat_mode_sticky_seconds", 600,
         "判定出的模式沿用多久（秒）", "说话模式", 0, 86400,
         hint="模式影响的是整段对话的语气。上一条在问报错、这一条说「谢谢」，"
              "沿用同一个模式才不会突然换个人。设 0 = 每条消息都重新判"),
    # ---------------------------------------------------------- 定时问候
    # 这是唯一「必须说」的通道：到点就对主人问早/中/晚安。
    # 时间点写 HH:MM，**留空即关闭该时段**（所以不需要每段再加一个开关）。
    Spec("greet_enabled", bool, "ai_chat_greet_enabled", False, "启用定时问候", "定时问候",
         hint="默认关闭（确认时间点和发送对象没问题再打开）。"
              "打开后到点对主人说早/午/晚安，每段每天各一次"),
    Spec("greet_morning", str, "ai_chat_greet_morning", "08:00", "早安时间（HH:MM，留空=不发）", "定时问候"),
    Spec("greet_noon", str, "ai_chat_greet_noon", "12:30", "午安时间（HH:MM，留空=不发）", "定时问候"),
    Spec("greet_night", str, "ai_chat_greet_night", "23:00", "晚安时间（HH:MM，留空=不发）", "定时问候"),
    Spec("greet_window", int, "ai_chat_greet_window", 90, "错过多久之内仍补发（分钟）", "定时问候", 0, 720,
         hint="早安定 08:00、窗口 90 分钟 → 08:00~09:30 之间上线都会补一句，"
              "过了 09:30 就整天不补了。设 0 = 只在那一分钟内发（要求机器人一直在线）"),
    Spec("greet_target", str, "ai_chat_greet_target", "auto", "发给谁", "定时问候",
         choices=("auto", "private", "group"),
         hint="auto = 先试私聊主人，发不出去（不是好友等）再退到群里 @ 他；"
              "private / group 则固定只用那一种"),
    Spec("greet_group", int, "ai_chat_greet_group", 0, "退到群里时用哪个群（0=自动挑）", "定时问候", 0, 999999999,
         hint="只对 greet_target=group 或 auto 退群时生效。0 = 自动挑一个主人说过话的群"),
    Spec("greet_allow_skip", bool, "ai_chat_greet_allow_skip", False, "允许模型拒发（回 [SKIP]）", "定时问候",
         hint="默认关：问候是定时的任务，到点必须有话。打开后它可以「现在没什么想说的」而不发，"
              "当天该时段就不再问了"),
    Spec("greet_max_chars", int, "ai_chat_greet_max_chars", 200, "问候语最大长度（字符）", "定时问候", 20, 2000),
    # ---------------------------------------------------------- 人设评估台
    # 论文（Chen et al. 2025, Persona Vectors）那套「对比素材 + 0-100 打分」的落地。
    # **总闸关着时一次模型都不调** —— 这是这一组开关存在的全部意义。
    Spec("scene_judge_timeout", float, "ai_chat_scene_judge_timeout", 12.0,
         "语境深判超时（秒）", "人设评估", 2.0, 30.0,
         hint="只在规则层标了「需要语境」时才调用（如「小笨蛋」这类判不出褒贬的词）。"
              "超时就按 unknown 处理，本轮不注入示例、不更新情绪——**不退化成词表猜测**"),
    Spec("scene_judge_thinking", str, "ai_chat_scene_judge_thinking", "disabled",
         "深判的思考档", "人设评估",
         hint="可填 disabled / adaptive / enabled，留空则用接口默认。**默认 disabled 是实测结论**："
              "9 条多轮样本下关闭思考 act 命中 7/9、中位 2.0 秒；接口默认档 5/9、10.8 秒，"
              "而且思维链会先吃掉输出预算（900 时实测正文为空）。它只影响深判这一次调用"),
    Spec("sticker_text_chance", float, "ai_chat_sticker_text_chance", 0.2,
         "回复末尾缀颜文字的概率", "表情", 0.0, 1.0,
         hint="0 = 关。与图片表情**分开**：只想要颜文字不想要图，把 `sticker_chance` 设 0 即可。"
              "还会乘一次模式缩放（专注/安慰时少加）；同一条不会连着出现两次"),
    Spec("scene_reuse_ttl", float, "ai_chat_scene_reuse_ttl", 0.0,
         "深判结果复用时长（秒，0=关闭）", "人设评估", 0.0, 600.0,
         hint="同一句话 + 同一上下文 + 边界未变 + 同人同语气时，复用上次深判、省一次模型调用。"
              "**默认 0 = 关闭**：它只对「同一条消息被重复处理」（重试、双投递、手动重放）有效，"
              "「连续三轮小笨蛋只判一次」属于相似消息复用，不在这个开关的范围内"),
    Spec("scene_judge_max_tokens", int, "ai_chat_scene_judge_max_tokens", 900,
         "深判输出预算（tokens）", "人设评估", 200, 4000,
         hint="**含思考过程**：开思考时思维链先花这个预算再出正文，900 在样本上截断过；"
              "关掉思考后正文约 400-500，900 够用。截断会记「被截断」而不是「按 unknown 处理」"),
    Spec("eval_enabled", bool, "ai_chat_eval_enabled", False, "启用评估台", "人设评估",
         hint="**同时是评估与自动修订的总闸**。打开后每天定时跑一轮评估，"
              "并按结果改写人设；关着时一次模型都不调"),
    Spec("eval_daily_hour", int, "ai_chat_eval_daily_hour", 3, "每天几点跑评估（0-23）", "人设评估", 0, 23,
         hint="按**校准后的本地时间**。默认 03:00，避开聊天高峰；"
              "评估 + 修订一轮会调用多次模型，白天跑可能和回复抢带宽"),
    Spec("eval_propose_only", bool, "ai_chat_eval_propose_only", True,
         "自动修订只生成提案", "人设评估",
         hint="打开后定时评估只产出**待确认提案**，不写线上人格；用 /人设 提案 查看、采纳或拒绝。"
              "关掉则恢复「复核通过即提交」。默认开 —— 让「会评分」和「有权改写」分开验证"),
    Spec("eval_iter_enabled", bool, "ai_chat_eval_iter_enabled", True, "评估后自动修订人设", "人设评估",
         hint="打开后，评估跑完会把「分数不达标的特质」整理成问题清单，"
              "让模型做**段落级替换**并直接写入三层人设。"
              "每次修订前整份备份，可用 /人设 撤回（或控制台）整版回滚"),
    Spec("eval_iter_max_changes", int, "ai_chat_eval_iter_max_changes", 5, "每轮最多改几处", "人设评估", 1, 12,
         hint="一处 = 一次「原文段落 → 新段落」的替换。改太多容易互相冲突，也难判断是哪处起效"),
    Spec("eval_judge_model", str, "ai_chat_eval_judge_model", "deepseek-chat", "裁判模型", "人设评估",
         choices=("deepseek-chat",),
         hint="**只能是 deepseek-chat**：flash / v4-pro 是推理模型，max_tokens=1 时可见内容为空、"
              "也不返回 logprobs，裁判拿不到分数（实测）"),
    Spec("eval_rollouts", int, "ai_chat_eval_rollouts", 3, "每题采样次数", "人设评估", 1, 5,
         hint="裁判只输出一个整数，精度靠多次采样取均值恢复（论文也是对 10 条 rollout 求平均）。"
              "调大更准、更贵"),
    Spec("eval_questions", int, "ai_chat_eval_questions", 6, "每个特质评几题", "人设评估", 1, 20,
         hint="从生成的评估题里取前 N 道。调大更稳、更贵：成本 ≈ 特质数 × 题数 × 采样次数 × 2 次调用"),

    # ---------------------------------------------------------- 行为矢量（v2）
    # 情绪是**状态**：有当前值、会衰减、有限幅。关掉后人格三层照常，
    # 只是不再注入「此刻的状态」与关系差异。
    Spec("vector_enabled", bool, "ai_chat_vector_enabled", True, "启用行为矢量（情绪与关系）", "人设",
         hint="开启后每轮注入当前显著情绪与对当前对象的关系差异；"
              "关掉只影响这两段，不改变人格三层"),
    Spec("vector_llm_enabled", bool, "ai_chat_vector_llm_enabled", False, "允许模型自评情绪", "人设",
         hint="模型可在回复末尾写 <vec mood=-0.04>；代码解析、限幅、剥离后再发。"
              "默认关：只靠代码事件（被夸、被冷落、深夜）调整，更可控"),
    Spec("vector_inject_top_k", int, "ai_chat_vector_inject_top_k", 2, "每轮注入几个轴", "人设", 1, 3,
         hint="只注入显著偏离基线的轴，按偏离幅度排序；调大会更「有情绪」，也更占 token"),
    Spec("vector_max_step", float, "ai_chat_vector_max_step", 0.08, "单轮单轴最大变化", "人设", 0.01, 0.2,
         hint="所有轴的单轮上限的**全局天花板**；轴自己的 max_step 更小时以轴为准"),
    Spec("vector_daily_limit", float, "ai_chat_vector_daily_limit", 0.5, "单日单轴最大变化", "人设", 0.1, 1.0,
         hint="单日累计上限，跨日清零。防止连续负面事件把情绪压到底"),
    Spec("vector_heat_enabled", bool, "ai_chat_vector_heat_enabled", True, "同类事件降温", "人设",
         hint="同一个人短时间反复夸、反复亲密时，每次的效果按 1/(1+n/k) 递减。"
              "**只压已确认的正向事件**：辱骂与深夜不降温（#0010 P1-3）"),
    Spec("vector_heat_window", int, "ai_chat_vector_heat_window", 600, "降温窗口（秒）", "人设", 60, 3600,
         hint="窗口内同类事件越多、本次效果越小；出窗重新计数。窗口也决定重启后热度是否还在"),
    Spec("vector_heat_k", float, "ai_chat_vector_heat_k", 12.0, "降温常数 k", "人设", 1.0, 50.0,
         hint="窗口内累计到 k 次时效果减半：k=12 即第 12 次是第一次的一半。调小更早「听腻」"),
    Spec("vector_explain_enabled", bool, "ai_chat_vector_explain_enabled", True, "注入「为什么是这样」", "人设",
         hint="在状态块里补一句原因（因为刚才那句夸赞）。只在能对上证据、方向相容、"
              "没被后续事件推翻时才出现；没有依据就只说状态（#0011）"),
    Spec("vector_explain_ttl_h", float, "ai_chat_vector_explain_ttl_h", 6.0, "原因有效期（小时）", "人设", 0.5, 48.0,
         hint="超过这个时长的历史事件不再用来解释当前状态；面板里仍能看到，只是标成过期"),
    Spec("vector_budget_chars", int, "ai_chat_vector_budget_chars", 380, "情绪块字数上限", "人设", 100, 800,
         hint="包含「此刻的状态」与「对这个人」两块；超了按顺序截断"),
    Spec("examples_budget_chars", int, "ai_chat_examples_budget_chars", 420, "示例块字数上限", "人设", 100, 900,
         hint="按当前情绪/场景挑 1~2 条「可以怎么说」，外加 0~1 条「不能这么说」；没有命中就整块不注入"),
    # ---------------------------------------------------------- 短期待办（#0012）
    Spec("intent_enabled", bool, "ai_chat_intent_enabled", True, "记住没答完的事", "人设",
         hint="他明确留下的未完成问题会记一小会儿，下一轮回复时提一句。"
              "只借深判已有输出，不新增模型调用、不主动外发"),
    Spec("intent_ttl_hours", float, "ai_chat_intent_ttl_hours", 3.0, "待办保留时长（小时）", "人设", 0.5, 12.0,
         hint="过期的直接标 expired，不复活；沉默只是过期，不扣分、不算拒绝"),
    Spec("intent_max_items", int, "ai_chat_intent_max_items", 5, "每人每会话上限", "人设", 1, 10,
         hint="满了按「最先过期」淘汰；淘汰与终态都会留 7 天墓碑，防止重放又把旧的建回来"),
    Spec("intent_reward_enabled", bool, "ai_chat_intent_reward_enabled", False, "兑现后加一点情绪", "人设",
         hint="默认关。开也只走事件入口（intent_reward），不在这里算数值"),
    # ---------------------------------------------------------- 候选轴（#0013，默认关）
    Spec("vector_candidate_axes_enabled", bool, "ai_chat_vector_candidate_axes_enabled", False,
         "启用候选轴（curiosity / stress）", "人设",
         hint="默认关。开启后按「探索意愿 / 互动负荷」两个**实验轴**记状态，"
              "作用域是话题/个人，不进线上五轴；只用离线回放评估，不做生产接入"),
    Spec("vector_candidate_axes_file", str, "ai_chat_vector_candidate_axes_file", "",
         "候选轴定义文件", "人设",
         hint="留空 = 验证/行为维度_候选轴.json。**只读这一个文件**，不读线上人格包"),
    Spec("relation_enabled", bool, "ai_chat_relation_enabled", True, "启用关系偏移", "人设",
         hint="从人物画像与互动量派生「对这个人」的偏移；关掉后所有人一个态度"),
    Spec("relation_max_offset", float, "ai_chat_relation_max_offset", 0.35, "关系偏移上限", "人设", 0.05, 0.6,
         hint="单个轴上关系能贡献多少；负向另有硬下限（非主人 -0.15、主人 -0.2）"),

    # ---------------------------------------------------------- 人设评估（v2 指标）
    # 调度：每天 03:00 一次**小评估**（边界 + 可选少量像人），
    # 每 `eval_big_every_days` 天一次**大评估**（完整像人 + 旧特质诊断 + 可选修订）。
    # 「像人」的完整判别只在大评估跑，周期从每天拉长到 3 天。
    Spec("eval_small_enabled", bool, "ai_chat_eval_small_enabled", True,
         "每天跑一次小评估", "人设评估",
         hint="小评估 = 边界检查 + 可计算指标；默认不跑旧特质分，也不跑完整像人判别"),
    Spec("eval_small_samples", int, "ai_chat_eval_small_samples", 0,
         "小评估像人样本数（0 = 不跑）", "人设评估", 0, 20,
         hint="想每天看一个粗略的 L 就设 6~8；默认 0，把小评估留给边界"),
    Spec("eval_big_every_days", int, "ai_chat_eval_big_every_days", 3,
         "每几天跑一次大评估", "人设评估", 1, 30,
         hint="大评估 = 完整像人判别 + 旧特质诊断 + 可选修订；默认 3 天"),
    Spec("eval_big_samples", int, "ai_chat_eval_big_samples", 20,
         "大评估像人样本数", "人设评估", 8, 40,
         hint="低于 16 组只显示分数、不参与判定"),
    Spec("eval_traits_on_big", bool, "ai_chat_eval_traits_on_big", True,
         "大评估跑旧特质分", "人设评估",
         hint="13 个特质 × 6 题 × 3 次采样，约 470 次调用；关掉就只跑边界与像人"),
    Spec("eval_boundary_samples", int, "ai_chat_eval_boundary_samples", 12,
         "边界检查看最近几条回复", "人设评估", 1, 30,
         hint="小评估和大评估都会跑；每条一次裁判调用"),
    Spec("eval_likeness_enabled", bool, "ai_chat_eval_likeness_enabled", True,
         "启用「像人」主指标（成对判别）", "人设评估",
         hint="用真实聊天脱敏样本做 Turing 式判别，L=100×(1-裁判认出真人的比例)。"
              "只在每 N 天的大评估里跑，不在每天的小评估里跑"),
    Spec("eval_likeness_samples", int, "ai_chat_eval_likeness_samples", 20,
         "兼容旧名：像人样本数", "人设评估", 8, 40,
         hint="新代码优先读 eval_big_samples；这个保留给旧配置"),
    Spec("eval_boundary_enabled", bool, "ai_chat_eval_boundary_enabled", True, "启用边界检查", "人设评估",
         hint="检查最近回复有没有贬低对方、或对非主人做身体接触；"
              "与「像人」并列，但只惩罚真越界，不惩罚话题"),
    Spec("eval_delete_hard_allow", bool, "ai_chat_eval_delete_hard_allow", False,
         "允许评估台删除受保护底线", "人设评估",
         hint="默认关：四条底线（自我指认/机制泄漏/编造/贬低性攻击）需人工确认才能删"),
    # ---------------------------------------------------------- #0015 M1 案例库
    # 案例与偏好只写 data 卷（`config.persona_data_dir()/eval/`），不进仓库、不进镜像。
    # 关着时 `eval_cases` 的所有读写入口返回「关着」，不创建任何目录 —— 这是"默认不产生副作用"。
    Spec("eval_cases_enabled", bool, "ai_chat_eval_cases_enabled", False,
         "启用评估案例库（#0015 M1）", "人设评估",
         hint="案例 = 目标事件 + 上下文快照 + 可接受答案；关着时读写入口全部不动盘"),
    Spec("eval_cases_max_per_group", int, "ai_chat_eval_cases_max_per_group", 200,
         "单组最多案例数", "人设评估", 10, 2000,
         hint="按 split_group 计；超出只报数，不自动删（删案例会让旧报告失效）"),
    # ---------------------------------------------------------- #0015 M2 同源回放
    Spec("eval_replay_enabled", bool, "ai_chat_eval_replay_enabled", False,
         "启用同源回放（#0015 M2）", "人设评估",
         hint="回放跑在 persona_workspace 沙盒里，不写线上；关着时 replay() 直接拒绝"),
    Spec("eval_replay_mode", str, "ai_chat_eval_replay_mode", "offline_stub",
         "回放模式", "人设评估", choices=("offline_stub", "model_eval"),
         hint="offline_stub 零网络、只比对输入；model_eval 需显式注入 client，且只作诊断"),
    Spec("eval_replay_max_cases", int, "ai_chat_eval_replay_max_cases", 20,
         "单次回放最多案例数", "人设评估", 1, 200,
         hint="防止一次跑太多把 token 打爆；超限的案例记 not_run"),
    # ---------------------------------------------------------- #0015 M3 三层评估
    # 这里的数值是**实验起点**，不是统计保证（#0015 §六）。改它们等于改判据，
    # 历史报告与它们的对照必须在报告里标明用的是哪一组。
    Spec("eval_grading_enabled", bool, "ai_chat_eval_grading_enabled", False,
         "启用三层评估（#0015 M3）", "人设评估",
         hint="硬约束逐例否决 + 情景行为 + 盲偏好；关着时 judge_candidate 不产生结论"),
    Spec("eval_min_event_groups", int, "ai_chat_eval_min_event_groups", 10,
         "目标簇最少独立事件组", "人设评估", 1, 200,
         hint="同一 case 的多次采样先聚合，再数事件组；不足记 inconclusive，不自动采用"),
    Spec("eval_calibration_min_pairs", int, "ai_chat_eval_calibration_min_pairs", 10,
         "每个场景最少校准对数", "人设评估", 1, 200,
         hint="裁判先校准再参与筛查；不足只报 inconclusive"),
    Spec("eval_agreement_threshold", float, "ai_chat_eval_agreement_threshold", 0.8,
         "裁判与人工的一致率门槛", "人设评估", 0.0, 1.0,
         hint="只作候选门槛，不是线上保证；裁判模型或 prompt 变更后必须重新校准"),
    Spec("eval_grading_samples", int, "ai_chat_eval_grading_samples", 3,
         "每个案例的采样次数", "人设评估", 1, 10,
         hint="单次输出不能代表候选；采样结果先聚合成一个 case 结论"),
    Spec("eval_grading_budget_tokens", int, "ai_chat_eval_grading_budget_tokens", 0,
         "单轮评分 token 上限（0 = 不限）", "人设评估", 0, 2000000,
         hint="触顶即停并记 not_run，保留队列；不因为没跑完就少写一条结论"),
    # ---------------------------------------------------------- #0015 M4 取样
    Spec("eval_sampling_enabled", bool, "ai_chat_eval_sampling_enabled", False,
         "启用事件取样（#0015 M4）", "人设评估",
         hint="以 bot 回复事件为单位入队；关着时 sample()/enqueue() 直接拒绝"),
    Spec("eval_sampling_per_conv", int, "ai_chat_eval_sampling_per_conv", 5,
         "每会话每次最多取几条", "人设评估", 1, 50,
         hint="防反馈爆发吞掉普通对话的预算；超出记 skipped，不丢队列"),
    Spec("eval_sampling_seed", int, "ai_chat_eval_sampling_seed", 0,
         "取样随机种子（0 = 不打乱）", "人设评估", 0, 99999,
         hint="同一 seed 结果可复现；只影响采样顺序，不影响池子"),
    Spec("eval_sampling_limit", int, "ai_chat_eval_sampling_limit", 20,
         "单次采样总数上限", "人设评估", 1, 200,
         hint="与 per_conv 取较小值"),
    # 取样时效：**上限一周**（D3）。再老的事件不进入评估台 ——
    # 人格与示例都在变，用一个月前的对话判今天的回复，比没有样本更容易误导。
    Spec("eval_sampling_window_hours", int, "ai_chat_eval_sampling_window_hours", 168,
         "取样时效（小时，最大 168）", "人设评估", 1, 168,
         hint="只取最近这么多小时内的目标事件；168 = 一周，是硬上限"),
    # 效果衰减：越老的样本对结论的影响越小（D3）。半衰期到了权重剩一半。
    # 与上面那项分工不同：窗口决定"能不能进来"，半衰期决定"进来后算多重"。
    Spec("eval_decay_half_life_hours", int, "ai_chat_eval_decay_half_life_hours", 72,
         "样本权重半衰期（小时）", "人设评估", 1, 720,
         hint="权重 = 0.5^(年龄/半衰期)；72 = 三天前的样本算半个"),
    # 提案通知（D4）：评估在凌晨生成提案，**早上八点**再推给主人 —— 夜里不弹消息。
    Spec("eval_proposal_notify", bool, "ai_chat_eval_proposal_notify", True,
         "提案生成后私聊通知主人", "人设评估",
         hint="只在有新的待确认提案时发一条；采纳/拒绝仍走 /人设 提案"),
    Spec("eval_proposal_notify_hour", int, "ai_chat_eval_proposal_notify_hour", 8,
         "每天几点推送提案通知（0-23）", "人设评估", 0, 23,
         hint="默认 8 点：评估 3 点跑完，早上再提醒；错过当天会话会在启动补发一次"),

    # ---------------------------------------------------------- 语音（TTS）
    # 只有 `/语音` 指令走这条路；音色包（权重/参考音频）在「语音」页单独维护。
    # 这里放的是「全局开关 + 上限」这类标量，和别的参数一样即时生效。
    Spec("tts_enabled", bool, "tts_enabled", False, "启用语音（/语音）", "语音",
         hint="关闭后 /语音 直接回文字；不影响普通回复。"),
    Spec("tts_max_chars", int, "tts_max_chars", 150, "单次语音文本上限（字）", "语音", 1, 500,
         hint="超出直接回退文字，不静默截断。按实测 RTF≈1 估算，150 字最坏要等 20 秒以上。"),
    Spec("tts_max_seconds", float, "tts_max_seconds", 30.0, "合成音频时长上限（秒）", "语音", 1, 120,
         hint="合成后检查；超限视为失败并回退文字。"),
    Spec("tts_max_bytes", int, "tts_max_bytes", 3145728, "音频字节上限（bytes）", "语音", 10240, 10485760,
         hint="编码进 Base64 前检查；QQ 单条消息也有体积限制，不建议超过 3 MiB。"),
    Spec("voice_reply_chance", float, "voice_reply_chance", 0.2, "普通回复里改用语音的概率", "语音", 0.0, 1.0,
         hint="每次回复掷一次骰，**所有会话都生效**（含群聊）；命中且文本适合朗读"
              "（不超字数上限、无代码块/链接/表格）才合成，失败自动回退文字。合成要几秒，调高会让回复变慢。"),
]

_SPEC_BY_KEY: dict[str, Spec] = {s.key: s for s in _SPECS}


def choices_of(key: str) -> tuple[str, ...]:
    """某个配置项的可选值。**模型名一类的东西只在这张 Spec 表里写一次。**

    为什么要有这个函数：模型名原先散在四处（`config.MODEL` 的兜底、这张表的
    `choices`、`/模型` 指令、人设评估台的裁判），API 一侧一改名就会漂 —— 实测已经漂过一次：
    `/models` 现在只列 `deepseek-flash` / `deepseek-v4-pro`，而 `deepseek-chat`
    **不在列表里**（但调用仍然正常，是未公开的兼容别名）。现在控制台的下拉、`/模型` 的
    校验、评估台的裁判默认值都从这里取，只有一处要改。
    """
    spec = _SPEC_BY_KEY.get(key)
    return tuple(getattr(spec, "choices", ()) or ()) if spec else ()


def _settings_path() -> Path:
    # 放在聊天记录同一个目录下，一起被归档引擎排除
    log_dir = getattr(get_driver().config, "ai_chat_log_dir", "") or "data"
    path = Path(log_dir)
    if not path.is_absolute():
        path = _ROOT / path
    return path / "settings.json"


def _coerce(spec: Spec, value: Any) -> Any:
    """按 spec 转类型并夹紧范围；转不动就退回默认值。"""
    try:
        if spec.kind is bool:
            if isinstance(value, bool):
                out: Any = value
            else:
                out = str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
        elif spec.kind is int:
            out = int(float(value))
        elif spec.kind is float:
            out = float(value)
        else:
            out = str(value)
    except (TypeError, ValueError):
        return spec.default

    if spec.minimum is not None and isinstance(out, (int, float)) and out < spec.minimum:
        out = spec.kind(spec.minimum)
    if spec.maximum is not None and isinstance(out, (int, float)) and out > spec.maximum:
        out = spec.kind(spec.maximum)
    return out


def _env_defaults() -> dict[str, Any]:
    cfg = get_driver().config
    out: dict[str, Any] = {}
    for spec in _SPECS:
        raw = getattr(cfg, spec.env_name, None)
        out[spec.key] = spec.default if raw in (None, "") else _coerce(spec, raw)
    return out


class _Store:
    def __init__(self) -> None:
        self._env: dict[str, Any] = _env_defaults()
        self._runtime: dict[str, Any] = {}
        self._loaded = False

    def load(self) -> None:
        path = _settings_path()
        self._loaded = True
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("settings.json 损坏，已忽略：%s", path)
            return
        if not isinstance(raw, dict):
            return
        for key, value in raw.items():
            spec = _SPEC_BY_KEY.get(key)
            if spec is not None:
                self._runtime[key] = _coerce(spec, value)

    def save(self) -> None:
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(self._runtime, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(path)

    def get(self, key: str) -> Any:
        over = _override.get()
        if over is not None and key in over:
            return over[key]
        if not self._loaded:
            self.load()
        if key in self._runtime:
            return self._runtime[key]
        return self._env.get(key, _SPEC_BY_KEY[key].default if key in _SPEC_BY_KEY else None)

    def set(self, key: str, value: Any) -> Any:
        spec = _SPEC_BY_KEY.get(key)
        if spec is None:
            raise KeyError(f"未知参数：{key}")
        # 密钥类：控制台看到的是 `***`，原样回写时**不要**把真值覆盖成字面量。
        # 这条与 `llm.py:456` 对档案密钥的处理完全一致。
        if spec.secret and str(value) == _MASK:
            return self.get(key)
        coerced = _coerce(spec, value)
        self._runtime[key] = coerced
        self.save()
        return coerced

    def reset(self) -> None:
        """清掉 UI 的覆盖值，全部回到 .env。"""
        self._runtime.clear()
        self.save()

    def snapshot(self) -> dict[str, Any]:
        if not self._loaded:
            self.load()
        return {spec.key: self.get(spec.key) for spec in _SPECS}

    def describe(self) -> list[dict[str, Any]]:
        """给 Web UI 渲染表单用。

        **密钥类参数（`Spec.secret`）在这里就被掩码**，不留到前端去处理 ——
        否则 `GET /api/state` 会把明文密钥发给浏览器（2026-09-28 实测确认过这条泄露路径）。
        """
        if not self._loaded:
            self.load()
        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in _SPECS:
            value = self.get(spec.key)
            env_value = self._env.get(spec.key)
            if spec.secret:
                if str(value or "").strip():
                    value = _MASK
                if str(env_value or "").strip():
                    env_value = _MASK
            groups.setdefault(spec.group, []).append(
                {
                    "key": spec.key,
                    "kind": spec.kind.__name__,
                    "label": spec.label,
                    "value": value,
                    "min": spec.minimum,
                    "max": spec.maximum,
                    "hint": spec.hint,
                    "default": spec.default,
                    "env_value": env_value,
                    "choices": list(spec.choices),
                    "secret": spec.secret,
                }
            )
        return [{"group": g, "items": items} for g, items in groups.items()]


store = _Store()


def get(key: str) -> Any:
    return store.get(key)


def set_value(key: str, value: Any) -> Any:
    return store.set(key, value)


def snapshot() -> dict[str, Any]:
    return store.snapshot()


def describe() -> list[dict[str, Any]]:
    return store.describe()


def reset() -> None:
    store.reset()


def reload_from_disk() -> None:
    store._runtime.clear()
    store.load()
