"""长期记忆：把「说过的事」沉淀成可检索、会衰减、能改口的条目。

## 它跟 chatlog 的关系（这是最容易混淆的地方）

| | `chatlog` | `memory`（本模块） |
|---|---|---|
| 存什么 | 群里的**每一句话**，原样 | 从话里**提炼出来的事实**，一两句一条 |
| 生命周期 | 会话切分、已读后按字符预算丢弃 | 跨会话、跨重启，按重要度与时效衰减 |
| 谁读 | 模型（当作"刚才发生了什么"） | 模型（当作"我一直记得的事"） |
| 规模 | 受 `read_budget` 限制 | 受 `memory_max_items` 限制 |

改造前只有左边这一栏：机器人每次被 @ 都只看到「压缩背景 + 未读新发言」，
**回复一落地，那条消息对它就只剩字符预算里的一行**。所以它记不住上周说过的话，
也接不上跨越很多轮的逻辑 —— 这就是「没有长期记忆、对话没逻辑」的根因。

## 三层结构

```
profile   人物画像：称呼、喜好、忌讳、习惯   —— 每人一份，随时覆盖
facts     事实条目：谁在什么时候说过/做过什么 —— 带重要度与时间，会衰减
events    群事件  ：这个会话里发生过的节点   —— 只在本会话检索
```

## 检索：本地打分，不花 token

`build_context()` 纯粹靠字符 n-gram 重叠 + 重要度 + 时效 + 被用次数排序，
**不调模型**。所以「长期记忆」这一层的运行成本是 0 token，只多几十到几百字的 prompt。

代价是：它只能做词面匹配，跨语言的同义（"面试" ↔ "找工作"）匹配不到。
这是刻意的取舍 —— 用 `/记忆 找 <词>` 兜底，而不是每条消息都调一次模型打分。

（**曾有一句"`memory_rerank` 打开后会用一个极小请求做精排，默认关闭"
是错的** —— `settings.py` 里没有这个 Spec、代码里也没有任何调用点，
把它当成一个"已经存在只是默认关着"的开关会查很久。
语义召回这条路尚未实施。）

## 抽取：异步、合并、限量

新事实由 `extract_and_store()` 在**回复发出之后**异步抽取，不占用回复的等待时间。
落盘前去重（对已有条目做词面重叠判断），命中就**覆盖更新**而不是新增 ——
这样「我换工作了」能把「他在做网架设计」顶掉，而不是两条矛盾事实并存。

单次抽取最多落 `memory_extract_max` 条，每天总次数受 `memory_extract_per_day` 限制 ——
防止一个活跃群把 token 烧在抽取上（token 是次要考虑，但不等于不限量）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from . import clock, config, memstore, settings

logger = logging.getLogger("ai_chat.memory")

_client = AsyncOpenAI(api_key=config.API_KEY or "sk-not-configured", base_url=config.BASE_URL)

_FILE = "memories.json"
_MAX_TEXT = 300      # 事实一句话的上限。原来是 200 —— 实测最长才 63 字，200 是浪费
_MAX_KEY = 24        # 画像的键（人名）上限
_MAX_NAME = 40       # 名字做**显示**用，可以比键长：群里昵称经常又长又带符号
_MAX_PROFILE_ITEM = 80   # 画像单条上限。原来是 30 —— **实测 6 条恰好卡在 30 字被硬切**，
                         # 出现"……（如 21:44、凌晨"这种句子断在括号里的情况
_MAX_PROFILE_FIELD = 12  # 每个字段最多留几条
_TRUNCATE_ELLIPSIS = "…"

# 检索打分的权重。加起来为 1，便于心算。
#
# 新增 `_W_HITS`：「被想起来」与「被反复证实」拆成两个独立信号。
# 原来 `_W_USE=0.15` 全压在 `used` 上，而那个字段当时根本不落盘 ——
# 等于 15% 的权重长期是噪声（见 `mark_used` 的说明）。现在把它拆成 0.12 + 0.08。
#
# 再新增 `_W_MANUAL`。原因是 `importance` 由**模型自己填**，
# 于是"他可能喜欢吃辣"（抽取出来的猜测）与用户明说的"下周三要面试"在库里长得一样、
# 打分也一样 —— 只有 importance 一个数在区分它们，而那个数不可信。
# `source` 字段本来就在（`/记忆 存` 写 "manual"、自动抽取写 "extract"），
# 只是从来没参与打分。现在给它一点独立权重，让"人说的"结构性地压过"模型猜的"。
# 各权重按比例微调以保持总和为 1（有断言守着）。
_W_RELEVANCE = 0.45
_W_IMPORTANCE = 0.20
_W_RECENCY = 0.11
_W_USE = 0.12
_W_HITS = 0.06
_W_MANUAL = 0.06

# 有明确关键词时至少留这么多条位置给「词面相关但不算重要」的条目，
# 否则重要度权重会把它们全挤掉（"上次那个方案" 就找不回来了）。
_RELEVANCE_FLOOR = 0.18

# 高频停用词：它们在中文里到处都是，参与 n-gram 只会引入噪声
_STOP = frozenset(
    "的 了 是 在 我 你 他 她 它 们 这 那 有 和 与 就 都 也 还 不 没 很 太 吧 吗 呢 啊 呀 哦 嘛 嗯 "
    "什么 怎么 为什么 可以 一下 一个 这个 那个 现在 今天 然后 因为 所以 但是 如果 已经 自己".split()
)


# --------------------------------------------------------------------- 数据
def _path() -> Path:
    return config.LOG_DIR / _FILE


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", clock.localtime())


def _ts() -> float:
    return round(clock.now(), 3)


def _clean(text: Any, limit: int = _MAX_TEXT) -> str:
    """压空白 + 超长截断。

    **截断时加省略号，而不是硬切**。硬切会产出"……找助手聊天（如 21:44、凌晨"
    这种句子断在括号里的条目 —— 读起来像数据损坏，模型也可能把半句话当成完整事实
    （实测线上 6 条画像恰好卡在 30 字，全是这个形态）。

    另外会**尽量回退到最近的标点**再切：宁可少几个字，也别断在词中间。
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit or limit <= 0:
        return flat
    head = flat[: max(1, limit - 1)]
    # 回退到最近的标点（不跨太远，最多 12 字）
    cut = max(head.rfind(ch) for ch in "。！？；，、,.; ")
    if cut >= len(head) - 12 and cut > 0:
        head = head[:cut]
    return head.rstrip() + _TRUNCATE_ELLIPSIS


class _Db:
    """记忆数据的门面：把「存在哪儿」交给 `memstore`，本模块只管算法。

    **已换成后端可切换的 store**（默认 SQLite，可回滚到 JSON）。
    这里的属性名（`facts` / `events` / `profile` / `next_id` / `readonly_reason`）
    与改造前**完全一致**，所以下面 70 多处 `_db.facts.append(...)` 之类的算法代码
    一个字都不用改 —— 落盘由 store 的写入穿透容器负责，见 `memstore.py`。
    """

    def __init__(self) -> None:
        self.store: Any = None
        self.loaded = False

    # -------------------------------------------------------- 后端
    def _open(self) -> None:
        if self.store is not None:
            return
        self.store = memstore.open_store(
            config.LOG_DIR, prefer=str(settings.get("memory_store") or "sqlite")
        )

    # 属性代理：算法层读到的就是 store 里那份内存副本
    def __getattr__(self, name: str) -> Any:
        # 只在正常属性找不到时才走这里
        if name.startswith("__"):
            raise AttributeError(name)
        self._open()
        self.store.ensure()
        return getattr(self.store, name)

    # -------------------------------------------------------- 读写
    def ensure(self) -> None:
        self._open()
        if not self.loaded:
            self.load()
        self.store.ensure()

    def load(self) -> None:
        self.loaded = True
        self._open()
        self.store.load()
        # 载入后做一次清洗：把老数据的画像字段按当前上限规范化
        # （原来这段在 JSON 载入时做，现在挪到这里，两种后端共用）
        #
        # **注意**：这里原来写死 30 / 60 / [:12] / [:8]，而写侧
        # （`set_profile`）用的是 `_MAX_PROFILE_ITEM`(80) 与 `_MAX_PROFILE_FIELD`(12)。
        # 两边不一致的后果不是"截短一点"那么轻：**80 字的条目在写盘后重载时被
        # 降级回 30 字**，即"断在括号里"（`_MAX_PROFILE_ITEM` 的注释记的正是这个
        # bug）在重启后又复活一次 —— 而且是静默的，因为 `_MAX_PROFILE_FIELD` 恰好
        # 也是 12，条数没变，只有内容被割。
        # 修法是**消掉字面量**：载入规范化与写入约束共用同一组常量，以后再调只改一处。
        for key, item in list(self.store.profile.items()):
            if not isinstance(item, dict):
                continue
            self.store.profile[key] = {
                "display": _clean(item.get("display"), _MAX_NAME),
                "love": [_clean(x, _MAX_PROFILE_ITEM) for x in (item.get("love") or [])][:_MAX_PROFILE_FIELD],
                "dislike": [_clean(x, _MAX_PROFILE_ITEM) for x in (item.get("dislike") or [])][:_MAX_PROFILE_FIELD],
                "habit": [_clean(x, _MAX_PROFILE_ITEM) for x in (item.get("habit") or [])][:_MAX_PROFILE_FIELD],
                "note": _clean(item.get("note"), _MAX_PROFILE_ITEM),
                "updated_at": str(item.get("updated_at") or ""),
            }

    def save(self) -> None:
        self._open()
        self.store.ensure()
        self.store.save()

    def upsert_fact(self, fact: dict[str, Any]) -> None:
        """把**就地改过**的一条事实落盘。

        为什么需要显式调用：写入穿透列表只在 `append` / `remove` / 整体赋值时同步，
        而 `dup["text"] = x` 这种是改列表里那个 dict 对象 —— 列表本身没变，
        穿透钩子不会触发。改造前 `hits` / `importance` 的更新就是这么丢的。
        """
        self._open()
        self.store.ensure()
        if hasattr(self.store, "upsert_fact"):
            self.store.upsert_fact(fact)
        else:
            self.store.save()

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    def backend_name(self) -> str:
        """当前后端名（`sqlite` / `json`）。**会先把后端打开** —— 排查与自述都用它。

        为什么单独一个方法：`self.store` 是真实属性且初始为 None，
        直接读 `_db.store.name` 会拿到 None 而不是"打开后的后端"（`__getattr__` 不会被触发）。
        """
        self._open()
        return str(getattr(self.store, "name", "?"))


_db = _Db()
_lock = asyncio.Lock()


# ----------------------------------------------------------------- 抽取水位线
# 为什么需要它：原来只有 `_recent_lines(conv, n=14)` —— 抽取只看「最近 14 条」，
# 于是有两个**永不愈合**的漏口：
#   ① 一段没人 @ 机器人的长对话（它只被 @ 时才走抽取）根本不会被提炼；
#   ② 当天额度用完之后进来的消息，永久不入库，第二天也不会补。
# 水位线把「抽到哪了」记在盘上，配额不够只是**推迟**，不再是丢。
#
# 存储：`data/memory_extract_state.json`（**独立小文件**，不塞进 memories.json）。
# 理由有两个：一是 memories.json 已有损坏只读逻辑，水位线跟着它一起坏没有好处；
# 二是 M1 把记忆迁去 SQLite 时，这个文件可以原样搬过去，不需要改结构。
_EXTRACT_STATE = "memory_extract_state.json"
_extract_state: dict[str, Any] = {"version": 1, "conv": {}, "day": "", "day_messages": 0, "total": 0}
_extract_state_loaded = False


def _extract_state_path() -> Path:
    return config.LOG_DIR / _EXTRACT_STATE


def _load_extract_state() -> None:
    global _extract_state_loaded
    if _extract_state_loaded:
        return
    _extract_state_loaded = True
    path = _extract_state_path()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # 水位线坏了**不能当"已抽完"**：那样会永久跳过一大段记录。
        # 也不能当"从头抽"：那会把记忆库刷一遍重复内容。取中间：丢掉水位线，
        # 只保留每日额度计数，下一次抽取从「最近一批」重新开始。
        logger.warning("抽取水位线损坏，按「从最近一批重新开始」处理：%s", path.name)
        return
    if not isinstance(raw, dict):
        return
    conv = raw.get("conv")
    _extract_state["conv"] = dict(conv) if isinstance(conv, dict) else {}
    _extract_state["day"] = str(raw.get("day") or "")
    _extract_state["day_messages"] = int(raw.get("day_messages") or 0)
    _extract_state["total"] = int(raw.get("total") or 0)


def _save_extract_state() -> None:
    path = _extract_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(_extract_state)
        payload["updated_at"] = _now()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("抽取水位线写盘失败（下次会重抽这一段，不影响聊天）：%s", path.name)


def watermark(conv: str) -> int:
    """这个会话已经抽到哪条消息（返回消息 id；0 = 一条都没抽过）。"""
    _load_extract_state()
    try:
        return int((_extract_state["conv"].get(conv) or {}).get("last_id") or 0)
    except (TypeError, ValueError):
        return 0


def _roll_day() -> None:
    """跨天就重置当日用量。原来是「每天最多 200 次调用」，现在按**消息条数**记账 ——
    水位线会让单次带上几十条，按次数记账会严重高估额度消耗。"""
    today = time.strftime("%Y-%m-%d", clock.localtime())
    if _extract_state.get("day") != today:
        _extract_state["day"] = today
        _extract_state["day_messages"] = 0


def extract_budget_left() -> int:
    """今天还能提炼多少条消息。0 = 用完（或功能关闭）。"""
    _load_extract_state()
    _roll_day()
    cap = int(settings.get("memory_extract_per_day"))
    if cap <= 0:
        return 0
    return max(0, cap - int(_extract_state.get("day_messages") or 0))


def extract_state_stats() -> dict[str, Any]:
    _load_extract_state()
    _roll_day()
    conv = _extract_state.get("conv") or {}
    return {
        "conversations": len(conv),
        "pending_conversations": sum(1 for v in conv.values() if isinstance(v, dict)),
        "day_messages": int(_extract_state.get("day_messages") or 0),
        "budget_left": extract_budget_left(),
        "total_messages": int(_extract_state.get("total") or 0),
    }


def _advance_watermark(conv: str, last_id: int, n_messages: int) -> None:
    """把水位线推进到 `last_id`，并记账。**只在处理成功之后调用。**"""
    _load_extract_state()
    cur = _extract_state["conv"].get(conv)
    if not isinstance(cur, dict):
        cur = {}
    cur["last_id"] = int(last_id)
    cur["updated_at"] = _now()
    cur["total"] = int(cur.get("total") or 0) + int(n_messages)
    _extract_state["conv"][conv] = cur
    _extract_state["total"] = int(_extract_state.get("total") or 0) + int(n_messages)
    _extract_state["day_messages"] = int(_extract_state.get("day_messages") or 0) + int(n_messages)
    if len(_extract_state["conv"]) > 200:
        # 只留有水位的会话，且按更新时间留最近 200 个（群很多时才触发）
        items = sorted(
            _extract_state["conv"].items(),
            key=lambda kv: str((kv[1] or {}).get("updated_at") or ""),
            reverse=True,
        )
        _extract_state["conv"] = dict(items[:200])
    _save_extract_state()


def baseline_watermarks(*, apply: bool = False) -> dict[str, dict[str, int]]:
    """给**没有水位线的老会话**算一个起步位置：最后一次会话切分点。

    为什么需要它：水位线一旦建起来就精确了，但**第一次**跑时它是 0，
    而 0 的含义是「一条都没抽过」—— 后台补抽会从远古第一条开始，把
    已经抽过的历史重抽一遍（实测升级当场就有 758 条待抽），把当天额度整批浪费掉。
    （注：进程刚启动时 `chatlog` 还没 load 任何会话，所以正常启动路径下这个函数无事可做；
    它真正生效的场景是**离线脚本/一次性任务**，那里可以先把会话 load 进来再调它。）

    `apply=False` 时只算不写（dry-run），供审计与人工核对。
    """
    from . import chatlog

    _load_extract_state()
    out: dict[str, dict[str, int]] = {}
    for conv, log in chatlog.all_logs().items():
        if watermark(conv) > 0:
            continue
        boundary = log.last_session_boundary_id()
        if boundary <= 0:
            continue
        out[conv] = {"last_id": int(boundary), "max_id": int(log.max_id())}
        if apply:
            _advance_watermark(conv, boundary, 0)
    return out


# --------------------------------------------------------------------- 相似度
def _bigrams(text: str) -> set[str]:
    """中文按 2-gram 切，英文/数字按整词。用于词面相似度，不引入分词依赖。"""
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
    """a 被 b 覆盖的比例 0~1（不对称：以 a 为分母，适合"查询被条目覆盖多少"）。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga:
        return 0.0
    return len(ga & gb) / len(ga)


# 画像条目的去重阈值。实测（线上真实条目两两比对）：
#   同一件事的两种说法 → 2-gram 覆盖率 0.09~0.53
#   不同属性之间       → 全部 0.00
# 所以 0.25 是个安全的中间值。但「作息偏晚，常在深夜…」与「经常熬夜，凌晨零点过后…」
# 只有 0.09 —— 字面几乎不重叠，纯粹靠 n-gram 抓不到，得靠下面的主题词兜底。
_PROFILE_SIMILAR = 0.25

# 语义主题词：同一主题下的不同说法算同一件事。
# 为什么需要它：2-gram 是**字面**匹配，而模型描述同一个习惯时措辞可以差很远
# （"作息偏晚/经常熬夜/常在深夜活跃"讲的都是熬夜）。这类只能靠主题归并。
# 只收**具体到不会误伤**的主题；不写"喜欢"这种泛词，否则"喜欢月饼"会和"喜欢塔罗"合并。
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


def _topic(text: str) -> int:
    """这条文本命中哪个主题（返回主题序号，都没命中返回 -1）。"""
    s = str(text or "")
    for idx, words in enumerate(_TOPIC_WORDS):
        if any(w in s for w in words):
            return idx
    return -1


def _similar(a: str, b: str) -> bool:
    """判断两条画像条目是不是同一件事。

    两道判据，满足其一即可：
      1. 字面相似（2-gram 覆盖率达阈值）；
      2. **同一主题** —— 对付"作息偏晚"与"经常熬夜"这种字面几乎不重叠、但讲的是一件事的表述。
    """
    if a == b:
        return True
    if max(_overlap(a, b), _overlap(b, a)) >= _PROFILE_SIMILAR:
        return True
    ta, tb = _topic(a), _topic(b)
    return ta >= 0 and ta == tb


def _merge_unique(existing: list[str], incoming: list[str], cap: int) -> list[str]:
    """把新条目并进已有列表，**按相似度去重**。

    为什么不能用 `value not in merged`（原来的写法）：那是**精确字符串**比对，
    而模型每次换个说法（"作息偏晚" / "经常熬夜" / "常在深夜到凌晨活跃"）就被当成
    新条目追加。实测线上 小明 的 habit 里同一件事**占了 4 条**，把 12 条配额吃掉 1/3，
    后面更好的信息反而被挤掉。

    重复时保留**信息量更大的那条**（更长的 = 更具体），新条目更长就顶掉旧的。
    """
    out = [x for x in existing if x]
    for item in incoming:
        item = str(item or "").strip()
        if not item:
            continue
        # 在同一列表内部也要去重（一次抽取可能给出两条近义句）
        hit = next((i for i, old in enumerate(out) if _similar(old, item)), None)
        if hit is None:
            out.append(item)
        elif len(item) > len(out[hit]):
            out[hit] = item
    # 超上限时**先丢最短的**：短的往往是"常在群里发图整活"这类低信息量条目
    if cap > 0 and len(out) > cap:
        out.sort(key=len, reverse=True)
        out = out[:cap]
    return out


# --------------------------------------------------------------------- 写入
def _find_similar(text: str, limit: float = 0.34) -> dict[str, Any] | None:
    """找一条词面高度重叠的既有事实 —— 用来把「改口」变成覆盖更新而不是新增。

    阈值取 0.34 是实测调出来的：中文 2-gram 下，「主人在做网架参数化项目」与
    「主人最近在做一个叫网架参数化的项目」这类**同一件事的两种说法**覆盖率大约
    0.35~0.5；而「主人喜欢喝拿铁」与前者只有 0.1 上下。0.5 定得太高，同一件事
    改口就会留下两条矛盾事实 —— 那比记不住更糟。
    """
    best: dict[str, Any] | None = None
    best_score = limit
    for fact in _db.facts:
        score = _overlap(fact.get("text", ""), text)
        if score > best_score:
            best, best_score = fact, score
    return best


def add_fact(
    text: str,
    *,
    scope: str = "global",
    subject: str = "群友",
    uid: int | None = None,
    name: str = "",
    conv: str = "",
    importance: float = 0.5,
    source: str = "extract",
    protected: bool | None = None,
    ts: float | None = None,
) -> tuple[dict[str, Any], bool]:
    """加一条事实。词面高度重叠则**覆盖更新**。返回 (条目, 是否新建)。

    「覆盖」是刻意的一环：长期记忆里最伤的是两条互相矛盾的事实同时存在，
    模型只能随机挑一条，表现就是"记性时好时坏"。

    `ts` 是**原始发生时间**，只给补提取用（从历史 chatlog 里补记忆时要回到当时的时刻，
    否则所有补出来的事实都会挤在"今天"，时间线反而更乱）。

    `protected` 默认 `None` = **按来源决定**：`source="manual"`（`/记忆 存` 这类
    用户明示的入口）默认受保护，自动抽取默认不受保护。用户明说的事不该被
    后来的一批闲聊挤出库 —— 这是"模型猜的"与"人说的"在**淘汰**这一侧的分野，
    打分那一侧的分野见 `_W_MANUAL`。
    要覆盖这个默认（例如批量导入时不想全部保护）就显式传 `protected=False`。
    """
    _db.ensure()
    text = _clean(text)
    if not text:
        raise ValueError("空事实")
    importance = max(0.0, min(1.0, float(importance)))
    if protected is None:
        protected = source == "manual"
    protected = bool(protected)

    dup = _find_similar(text)
    if dup is not None:
        # **就地改字典不会触发落盘**：写入穿透列表只在 append/remove/整体赋值时同步，
        # `dup["x"] = y` 是改列表里的那个 dict 对象，列表本身没变。
        # 所以这里必须显式 upsert —— 否则「覆盖更新」只存在内存里，
        # 重启后又在库里看到旧文本（这正是 hits 长期不落盘的同一个坑）。
        dup["prev_text"] = dup.get("prev_text") or dup.get("text")
        dup["text"] = text
        dup["importance"] = max(float(dup.get("importance", 0.5)), importance)
        dup["updated_at"] = _now()
        # hits = "这件事被反复提到过几次"，是**被证实**的信号（见 score_fact 的说明）
        dup["hits"] = int(dup.get("hits", 0)) + 1
        dup["last_hit"] = _now()
        if protected:
            dup["protected"] = True
        if source == "manual":
            dup["source"] = "manual"
            # 一条自动抽取出来的事实，后来被用户用 /记忆 存 明确说了一遍 ——
            # 它从此升级为"人说的"，淘汰豁免与打分加权都要跟上。
            dup["protected"] = True
        _db.upsert_fact(dup)
        return dup, False

    item = {
        "id": _db.next_id,
        "ts": float(ts) if ts else _ts(),
        "time": _now(),
        "text": text,
        "scope": scope,
        # 可见范围（ACL）：**在打分之前过滤用**，见 `_visible_in`。
        # 老数据没有这个字段 → 视为 ["*"] 全局可见（保持改造前的行为，不静默改变召回）
        "vis": _vis_for(scope=scope, conv=conv),
        "subject": subject,
        "uid": uid,
        "name": name,
        "conv": conv,
        "importance": importance,
        "source": source,
        "protected": protected,
        "hits": 0,
        "last_hit": None,
        "used": 0,
        "last_used": None,
    }
    _db.next_id += 1
    _db.facts.append(item)
    return item, True


# --------------------------------------------------------------------- 可见性
def _vis_for(*, scope: str, conv: str) -> list[str]:
    """一条**新**事实的可见范围。

    规则刻意只有三条，能一句话说清：

    1. 主人私聊说的 → 全局可见（他是主人，跨会话记得他是本就该有的能力）；
    2. 群/别人私聊说的 → **只在这个会话里**可见（不串群、不漏私聊）；
    3. `scope="event"` 的照旧只在 `conv` 里（与原来的检索降权一致，现在变成硬隔离）。

    为什么默认给 `conv` 而不是给 `"*"`：`conv` 是我们本来就在每条消息上有的东西
    （`append_message` 已经这么存），不需要新建"场景/参与者"表就能得到
    Chronicler 那种「排序前过滤」的效果。见 README 5.6.6.3。
    """
    if scope == "event" and conv:
        return [conv]
    if conv.startswith("u"):
        return ["*"] if _is_master_conv(conv) else [conv]
    if conv.startswith("g"):
        return [conv]
    return ["*"]


def _is_master_conv(conv: str) -> bool:
    """这条会话是不是「主人私聊」。`u<QQ号>` 里 QQ 号等于 `master_qq` 即是。"""
    if not conv.startswith("u"):
        return False
    try:
        return int(conv[1:]) == int(settings.get("master_qq"))
    except (TypeError, ValueError):
        return False


def _visible_in(fact: dict[str, Any], conv: str, *, query: str = "") -> bool:
    """这条事实在当前会话里能不能被想起。**在打分之前调用。**

    为什么必须是"之前"：排序后再过滤会通过「排序器决定纳入/排除」泄漏 ——
    一条不该出现的记忆只要参与了打分，它有没有挤掉别人就已经影响了结果。
    Chronicler 用 `visible_to` 表达同一件事，这里用 `vis`。

    老数据没有 `vis`：按它自己的 `conv`/`scope` 推断，推断不出就当全局可见
    （= 改造前的行为）。这样升级不会突然让一批记忆"消失"，只对**新写入**的事实收紧。
    """
    if not settings.get("memory_scope_isolation"):
        return True  # 关掉隔离 = 退回改造前的全局可见
    vis = fact.get("vis")
    if not isinstance(vis, list) or not vis:
        # 老数据：没有 vis 字段
        scope = str(fact.get("scope") or "global")
        fconv = str(fact.get("conv") or "")
        if scope == "event" and fconv:
            return fconv == conv
        if fconv:
            return _conv_visible(fconv, conv)
        return True
    if "*" in vis:
        return True
    return any(_conv_visible(str(item), conv) for item in vis)


def _conv_visible(fact_conv: str, here: str) -> bool:
    """`fact_conv` 这条记录，在 `here` 这个会话里能不能被看到。

    只有两种情况算"能"：
      * 同一个会话（`g123` 对 `g123`、`u456` 对 `u456`）；
      * 事实来自**主人私聊** —— 主人跟它私下说过的事，它在哪里都可以记得。
    其余一律不通（A 群的记忆不会出现在 B 群，别人的私聊不会出现在群里）。
    """
    if not fact_conv or not here:
        return True
    if fact_conv == here:
        return True
    return _is_master_conv(fact_conv)



def add_event(text: str, *, conv: str, importance: float = 0.5,
              ts: float | None = None) -> dict[str, Any]:
    """记一条群事件（会话内可检索，跨群不串）。`ts` 同上，供补提取还原发生时间。"""
    _db.ensure()
    item = {
        "id": _db.next_id,
        "ts": float(ts) if ts else _ts(),
        "time": _now(),
        "text": _clean(text),
        "conv": conv,
        "importance": max(0.0, min(1.0, float(importance))),
        "used": 0,
    }
    _db.next_id += 1
    _db.events.append(item)
    return item


def remove(item_id: int) -> bool:
    _db.ensure()
    for bucket in (_db.facts, _db.events):
        for idx, item in enumerate(bucket):
            if int(item.get("id", 0)) == int(item_id):
                bucket.pop(idx)
                return True
    return False


def protect(item_id: int, locked: bool = True) -> bool:
    _db.ensure()
    for item in _db.facts:
        if int(item.get("id", 0)) == int(item_id):
            item["protected"] = bool(locked)
            return True
    return False


def set_profile(
    key: str,
    *,
    display: str = "",
    love: list[str] | None = None,
    dislike: list[str] | None = None,
    habit: list[str] | None = None,
    note: str = "",
) -> None:
    """覆盖式更新一个人的画像。传 None 的字段保持不变，传 [] 则清空该字段。

    **列表字段走相似度去重**（`_merge_unique`），不是精确字符串比对 ——
    否则模型的每次换个说法都会新增一条，同一件事越堆越多、把配额吃光。
    单条上限也从 30 提到 `_MAX_PROFILE_ITEM`(80)：30 字会把句子拦腰截断
    （实测 6 条恰好卡在 30 字，出现"……（如 21:44、凌晨"这种断在括号里的条目）。

    `key` 现在是 `entity.uid_key(uid)`（`uid:100000001`）或退回人名。
    合并旧键由 `entity.consolidate` 在抽取时按别名做，见 README 5.6.6.4。
    """
    _db.ensure()
    # **键先去掉首尾空白再截断**。
    # `_clean` 会把内部空白折叠、超长加省略号，但**不 strip**：模型偶尔会在名字后面
    # 多带一个空格，而 `"小明 "` 与 `"小明"` 是两个不同的键 —— 控制台里就多一条
    # 看起来一模一样的画像。实测库里真出现过带尾空格的键。
    key = _clean(str(key or "").strip(), _MAX_KEY)
    if not key:
        return
    cur = _db.profile.setdefault(
        key, {"display": "", "love": [], "dislike": [], "habit": [], "note": "", "updated_at": ""}
    )
    if display:
        # 名字是**显示**用，可以长一点：群里昵称经常又长又带符号
        cur["display"] = _clean(display, _MAX_NAME)
    for field_name, incoming in (("love", love), ("dislike", dislike), ("habit", habit)):
        if incoming is None:
            continue
        values = [_clean(v, _MAX_PROFILE_ITEM) for v in incoming]
        cur[field_name] = _merge_unique(
            list(cur.get(field_name) or []), values, _MAX_PROFILE_FIELD
        )
    if note:
        cur["note"] = _clean(note, _MAX_PROFILE_ITEM)
    cur["updated_at"] = _now()


# --------------------------------------------------------------------- 身份归一
def _record_name(name: str, uid: int | None, *, conv: str = "") -> None:
    """记一条「见过这个名字」到实体表。**只记不猜**：uid 为 None 也照记。"""
    name = _clean(name, _MAX_NAME)
    if not name:
        return
    _db.ensure()
    table = _db.entities
    now = _now()
    cur = table.get(name)
    if not isinstance(cur, dict):
        cur = {
            "uid": uid,
            "canonical": name,
            "conv": conv,
            "seen_count": 0,
            "first_seen": now,
            "last_seen": now,
        }
        table[name] = cur
    # **只在原来没有 uid 时补**：不覆盖已知的 uid，避免某个重名把身份改错
    if uid is not None and cur.get("uid") in (None, ""):
        cur["uid"] = int(uid)
    cur["seen_count"] = int(cur.get("seen_count") or 0) + 1
    cur["last_seen"] = now
    if conv and not cur.get("conv"):
        cur["conv"] = conv
    if isinstance(_db.store, memstore.SqliteStore):
        _db.store.upsert_entity(name, cur)


def _name_index() -> dict[str, int]:
    """`名字 → uid` 的快查表（只含已知 uid 的名字）。"""
    _db.ensure()
    out: dict[str, int] = {}
    for name, info in _db.entities.items():
        uid = (info or {}).get("uid")
        if uid not in (None, ""):
            try:
                out[str(name)] = int(uid)
            except (TypeError, ValueError):
                continue
    return out


def _uid_key(uid: int | None) -> str:
    """画像键：有 uid 就用 uid，没有就退回人名。

    为什么键要带前缀：`_MAX_KEY`(24) 的截断是按字符来的，纯数字 uid 很短不会撞；
    但人名既可能是 "123" 也可能真的是个 QQ 号，加 `uid:` / `name:` 前缀就没有歧义。
    """
    if uid in (None, ""):
        return ""
    try:
        return f"uid:{int(uid)}"
    except (TypeError, ValueError):
        return ""


def _bot_ids(messages: list[dict[str, Any]]) -> tuple[set[int], set[str]]:
    """机器人自己的 `(uid 集合, 显示名集合)`。

    `uid` 从消息里带的 `bot_uid` 取（`chatlog` 在机器人自己的发言上写过它），
    显示名从落盘的机器人发言取、再加上配置里的显示名与人设角色名。

    要它是因为**模型会给人设角色写画像** —— 输入里明明只有别人的发言，
    它照样可能输出一条 `{"who": "鲸鱼娘（助手）", "habit": [...]}`。
    实测线上就攒出了这种条目（含「鲸鱼娘」自己的昵称）。它不是人，不该进人物画像。
    """
    ids: set[int] = set()
    names: set[str] = {str(config.BOT_NAME or "").strip()}
    for msg in messages:
        if not msg.get("is_bot"):
            continue
        for candidate in (msg.get("bot_uid"), msg.get("uid")):
            try:
                if candidate not in (None, ""):
                    ids.add(int(candidate))
            except (TypeError, ValueError):
                continue
        name = _clean(msg.get("name"), _MAX_NAME)
        if name:
            names.add(name)
    return ids, {n for n in names if n}


def _is_bot_self(
    who: str, uid: int | None, *, bot_ids: set[int], bot_names: set[str]
) -> bool:
    """这条画像是不是在讲**机器人自己**。

    uid 能对上就按 uid 判（最稳）；否则退回名字判 ——
    模型写「鲸鱼娘（助手）」「小鲸鱼」这类变体时只能靠名字兜。
    名字判用**包含**而不是相等，因为变体通常是"原名 + 后缀"。
    """
    if uid is not None and uid in bot_ids:
        return True
    if not who:
        return False
    for name in bot_names:
        if name and (name in who or who in name):
            return True
    return False


def _link_key_to_uid(key: str) -> str:
    """人名键如果对应一个已知 uid，就并到 `uid:<QQ号>` 键下，返回归并后的键。

    **这是画像重复的主要成因**：`_resolve_uid` 只做精确匹配，模型这次写
    「小明」、下次写「小明 」或写在一个没带上发送者名单的批次里，就解不出 uid、
    于是又建一个独立的人名键。而这个键其实和某个 `uid:` 键是同一个人 ——
    实体表里就写着。

    做法是"懒归一"：不额外建索引，只在写入前查一次实体表（名字 → uid）。
    合并不动事实条目，最坏情况也只是少两条画像。
    """
    if not key or key.startswith("uid:"):
        return key
    try:
        hit = _name_index().get(key)
    except Exception:  # noqa: BLE001 - 归一失败绝不能挡住写记忆
        return key
    if hit in (None, ""):
        return key
    target = _uid_key(int(hit))
    if not target or target == key:
        return key
    consolidate_profile(key, target)
    return target


def _resolve_uid(name: str, senders: dict[str, int]) -> int | None:
    """把一个显示名解成 uid。

    两级：① 当前这批消息的发送者名单；② 实体表里见过（跨会话累积）。
    都没有就返回 None —— **宁可不归一，也不要猜错人**（把两个人合并比不合并更糟）。
    """
    if not name:
        return None
    if name in senders:
        return int(senders[name])
    hit = _name_index().get(name)
    return int(hit) if hit not in (None, "") else None


def _senders_of(
    messages: list[dict[str, Any]], *, bot_ids: set[int] | None = None
) -> dict[str, int]:
    """这一批消息里「显示名 → uid」。给身份归一用。

    `bot_ids` 里的 uid 会被排除 —— **这一条比"看 is_bot 标记"更硬**：
    老记录可能没写 `is_bot`，而机器人小号的 QQ 号是知道的。
    排除掉才不会有"机器人被当成群友"的画像。
    """
    out: dict[str, int] = {}
    skip = bot_ids or set()
    for msg in messages:
        if msg.get("is_bot"):
            continue
        name = _clean(msg.get("name"), _MAX_NAME)
        try:
            uid = int(msg.get("uid"))
        except (TypeError, ValueError):
            continue
        if uid in skip:
            continue
        if name:
            out.setdefault(name, uid)
    return out


def consolidate_profile(name: str, uid_key: str) -> None:
    """把「按人名建的旧画像」并进「按 uid 建的画像」。

    这是**就地归一**，不是一次性数据修复：当抽取第一次把某个人解出 uid 时，
    原来散落在人名键下的画像会被合并过来（字段走 `_merge_unique` 去重），旧键删掉。
    于是存量数据不需要单独跑脚本 —— 边聊边收敛，且**合并错了也只是少两条画像**，
    不会动事实条目。
    """
    if not uid_key or name == uid_key:
        return
    _db.ensure()
    old = _db.profile.get(name)
    if not isinstance(old, dict):
        return
    cur = _db.profile.setdefault(
        uid_key, {"display": "", "love": [], "dislike": [], "habit": [], "note": "", "updated_at": ""}
    )
    if not cur.get("display"):
        cur["display"] = old.get("display") or name
    for field_name in ("love", "dislike", "habit"):
        cur[field_name] = _merge_unique(
            list(cur.get(field_name) or []),
            [str(x) for x in (old.get(field_name) or [])],
            _MAX_PROFILE_FIELD,
        )
    if not cur.get("note") and old.get("note"):
        cur["note"] = old["note"]
    cur["updated_at"] = _now()
    _db.profile.pop(name, None)
    logger.info("画像已按 uid 归并：%s → %s", name, uid_key)



def clear(kind: str = "all") -> int:
    """清空记忆。kind: all / facts / events / profile。"""
    _db.ensure()
    n = 0
    if kind in ("all", "facts"):
        n += len(_db.facts)
        _db.facts.clear()
    if kind in ("all", "events"):
        n += len(_db.events)
        _db.events.clear()
    if kind in ("all", "profile"):
        n += len(_db.profile)
        _db.profile.clear()
    return n


# --------------------------------------------------------------------- 持久化封装
async def _persist() -> None:
    async with _lock:
        await asyncio.to_thread(_db.save)


async def remember(text: str, **kwargs: Any) -> tuple[dict[str, Any], bool]:
    """对外入口：加一条事实并落盘。

    手动入口默认按 `source="manual"` 处理，指令层不必自己拼这个参数。
    """
    _db.ensure()
    kwargs.setdefault("source", "manual")
    item, created = add_fact(text, **kwargs)
    compact()  # 也要受条数上限约束，不然 memory_max_items 只管自动抽取
    await _persist()
    return item, created


async def remember_event(text: str, *, conv: str, importance: float = 0.5) -> dict[str, Any]:
    _db.ensure()
    item = add_event(text, conv=conv, importance=importance)
    await _persist()
    return item


async def forget(item_id: int) -> bool:
    _db.ensure()
    ok = remove(item_id)
    if ok:
        await _persist()
    return ok


async def store_pending(text: str, **kwargs: Any) -> tuple[dict[str, Any], bool]:
    """`/记忆 存` 的执行体：手动记一条并落盘。

    跟 `remember` 的区别只是语义入口 —— 让指令层读起来是"存"而不是"记"。
    """
    return await remember(text, **kwargs)


async def set_protected(item_id: int, locked: bool = True) -> bool:
    _db.ensure()
    ok = protect(item_id, locked)
    if ok:
        await _persist()
    return ok


async def wipe(kind: str = "all") -> int:
    _db.ensure()
    n = clear(kind)
    if n:
        await _persist()
    return n


async def update_profile(key: str, **kwargs: Any) -> None:
    _db.ensure()
    set_profile(key, **kwargs)
    await _persist()


def _find_by_text(text: str, limit: float = 0.3) -> dict[str, Any] | None:
    """按文本找一条**已存在**的事实（给"更正"用）。

    与 `_find_similar` 的区别：那个是拿新文本去猜"是不是同一件事"，这个是模型
    明确告诉我们它要改哪一条（`prev_text`），所以阈值可以低一点、直接按文本比对。
    """
    target = _clean(text)
    if not target:
        return None
    best: dict[str, Any] | None = None
    best_score = limit
    for fact in _db.facts:
        score = _overlap(fact.get("text", ""), target)
        if score > best_score:
            best, best_score = fact, score
    return best


# --------------------------------------------------------------------- 检索
def _recency(ts: float) -> float:
    try:
        half_life = max(3600.0, float(settings.get("memory_half_life_days")) * 86400.0)
    except (TypeError, ValueError):
        half_life = 30 * 86400.0
    age = max(0.0, time.time() - float(ts or 0))
    return 0.5 ** (age / half_life)


def score_fact(fact: dict[str, Any], query: str = "") -> float:
    """一条事实对当前查询的得分 0~1。query 为空时退化成"重要度 + 时效"排序。

    两个"次数"信号，语义不同，现在都会真的落盘：

    | 字段 | 含义 | 谁会加 |
    |---|---|---|
    | `used` | **被想起来过几次** | 每次进 prompt（`mark_used`） |
    | `hits` | **被反复提到/证实过几次** | 抽取命中已有条目（`_find_similar` / `prev_text` 更正） |

    只用 `used` 会有一个退化成"越说越想起、越想起越说"的正反馈；
    `hits` 是外部证据（同一件事又被说了一遍），所以这里给它一点独立权重。
    两者都封顶（`/5`），避免老条目靠次数永久霸榜。

    第三个信号是**来源**（`_W_MANUAL`）：`source="manual"` 是用户用 `/记忆 存`
    明说的事，`"extract"` 是模型从聊天里猜的。两者本来只靠模型自填的
    `importance` 竞争，而那个数不可信；给 manual 一点独立权重之后，
    "人说的"会结构性地排在"模型猜的"前面（淘汰侧的对应保证见 `compact()`）。
    """
    relevance = _overlap(query, fact.get("text", "")) if query else 0.0
    importance = float(fact.get("importance", 0.5))
    recency = _recency(float(fact.get("ts", 0)))
    uses = int(fact.get("used", 0))
    hits = int(fact.get("hits", 0))
    use_score = min(1.0, uses / 5.0)   # 被反复用到的条目更可能是"真的重要"
    hit_score = min(1.0, hits / 3.0)   # 被反复提到过的更可能是"确有其事"
    manual_score = 1.0 if fact.get("source") == "manual" else 0.0
    score = (
        _W_RELEVANCE * relevance
        + _W_IMPORTANCE * importance
        + _W_RECENCY * recency
        + _W_USE * use_score
        + _W_HITS * hit_score
        + _W_MANUAL * manual_score
    )
    # 明确点名过的事实给一点点加权，让它不容易被后来的闲聊挤掉
    if fact.get("protected"):
        score += 0.05
    return round(min(1.0, score), 4)


def _people_in(text: str) -> list[str]:
    """从当前发言里挑出被提到的人（按画像里的 display 名匹配）。"""
    found: list[str] = []
    for key, info in _db.profile.items():
        for name in {key, info.get("display") or ""}:
            if name and len(name) >= 2 and name in text:
                found.append(key)
                break
    return found


def retrieve(
    query: str = "",
    *,
    conv: str = "",
    subjects: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """挑出当前该被想起的事实。

    排序完全在本地做，不调模型 —— 所以这一层的运行成本是 0 token。

    **先做可见性过滤再打分**（`_visible_in`）：原来私聊存的事实会参与
    任意群的检索，只对 `scope=="event"` 降权 0.25 —— 也就是"能漏，只是排得靠后"。
    现在是硬隔离，且过滤发生在**打分之前**（排序后再过滤会通过排序器泄漏）。
    """
    _db.ensure()
    if limit is None:
        limit = int(settings.get("memory_recall_count"))
    if limit <= 0 or not _db.facts:
        return []

    subjects = subjects or []
    # 可见性过滤：先算出一份"这个会话能看见的"候选池，后面所有排序都在这份池子里做
    pool = [f for f in _db.facts if _visible_in(f, conv, query=query)]
    if not pool:
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for fact in pool:
        score = score_fact(fact, query)
        # 同一个人相关的事实优先（聊天里提到谁，就更可能是在说谁）
        if subjects and fact.get("subject") in subjects:
            score += 0.08
        # 会话不匹配的群事件降权（避免把 A 群的事讲到 B 群去）
        # 注：可见性硬过滤之后这条基本不会再命中（异群 event 已经进不了 pool），
        # 保留它是为了 `memory_scope_isolation=false` 时仍有旧行为。
        if fact.get("scope") == "event" and fact.get("conv") and conv and fact["conv"] != conv:
            score -= 0.25
        scored.append((score, fact))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    # 有明确关键词时，保证词面最相关的几条一定进（见 _RELEVANCE_FLOOR）
    picked: list[dict[str, Any]] = []
    if query:
        lexical = sorted(
            pool, key=lambda f: _overlap(query, f.get("text", "")), reverse=True
        )
        for fact in lexical[: max(1, limit // 2)]:
            if _overlap(query, fact.get("text", "")) >= _RELEVANCE_FLOOR and fact not in picked:
                picked.append(fact)

    for score, fact in scored:
        if len(picked) >= limit:
            break
        if fact in picked:
            continue
        if score < float(settings.get("memory_min_score")):
            continue
        picked.append(fact)
    return picked[:limit]


# --------------------------------------------------------------------- 多角度检索
# 灵感来自 Somnia：它每轮让模型产出 3 个不同角度的 `search_query`
# （关系 / 事件 / 情感·动机），三路召回后去重。为什么有效：
# 中文里同一件事的说法差异很大（"面试" / "找工作" / "换工作"），
# 单条问句的 2-gram 只能覆盖其中一种说法，换三个角度就能多覆盖几种。
#
# 成本上的两个选择（都刻意做了）：
#   * **默认不调模型**：三个角度由本地规则从问句派生，所以这一步仍然是 0 token；
#   * 只有开了 `recall_multi_angle` 且判定为"回忆类"问题时才启用，
#     否则一次本地检索就够 —— 不能让每轮回复都多跑两遍打分。
_ANGLE_FRAMES: tuple[tuple[str, str], ...] = (
    # (角度名, 派生问句的模板；{q} 是原问句)
    ("事件", "{q}"),
    ("关系", "{q} 谁 和 他 她 关系 一起 说过"),
    ("心情", "{q} 感觉 心情 高兴 难受 喜欢 讨厌"),
)


async def rewrite_angles(query: str) -> list[str]:
    """让模型把问句改写成三个角度的检索问句。**失败就退回本地派生。**

    只有在 `recall_multi_angle` 开着、且这一轮确实被判定为"回忆类"时才会被调用。
    用极小请求（`max_tokens=120`），并且**任何异常都不影响回复**。
    """
    query = str(query or "").strip()
    if not query:
        return []
    prompt = (
        "把下面这句话改写成 3 个用于检索记忆的中文问句，分别从\n"
        "① 事件（发生了什么）② 关系（和谁有关）③ 心情（当时的感受）三个角度。\n"
        "每行一个，不要编号、不要解释、不要引号。\n\n"
        f"原句：{query}"
    )
    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=settings.get("model"),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=120,
            ),
            timeout=config.TIMEOUT,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    except Exception:  # noqa: BLE001 - 改写失败就退回本地派生
        logger.info("多角度问句改写失败（退回本地派生）")
        return []
    out = [ln.strip(" -·•\t") for ln in text.splitlines() if ln.strip()]
    return [ln for ln in out if 1 < len(ln) <= 60][:3]


def _local_angles(query: str) -> list[str]:
    """本地派生三个角度（0 token）。`{q}` 为空时只保留原问句那一路。"""
    query = str(query or "").strip()
    if not query:
        return [""]
    return [tpl.replace("{q}", query).strip() for _, tpl in _ANGLE_FRAMES]


def retrieve_multi(
    query: str,
    *,
    conv: str = "",
    subjects: list[str] | None = None,
    limit: int | None = None,
    angles: list[str] | None = None,
) -> list[dict[str, Any]]:
    """三路召回 + 合并。返回的仍然是事实条目列表，形状与 `retrieve()` 一致。

    合并规则：**按条目出现次数优先**（三个角度都召回到的，比只被一路召回的更相关），
    同票数时保留总分高的那个顺序。这样不需要再调模型就能把三路结果排好。
    """
    if limit is None:
        limit = int(settings.get("memory_recall_count"))
    if limit <= 0:
        return []
    queries = [q for q in (angles or _local_angles(query)) if q is not None]

    votes: dict[int, int] = {}
    items: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for q in queries:
        for fact in retrieve(q, conv=conv, subjects=subjects, limit=limit):
            fid = int(fact.get("id", 0) or 0)
            votes[fid] = votes.get(fid, 0) + 1
            if fid not in items:
                items[fid] = fact
                order.append(fid)

    # 先按票数、再按首次出现顺序（首次出现顺序已经隐含了单路打分的排序）
    ranked = sorted(order, key=lambda fid: (-votes.get(fid, 0), order.index(fid)))
    return [items[fid] for fid in ranked[:limit]]


def retrieve_events(query: str, conv: str, limit: int = 3) -> list[dict[str, Any]]:
    _db.ensure()
    pool = [e for e in _db.events if e.get("conv") == conv]
    if not pool:
        return []
    pool.sort(
        key=lambda e: (
            _overlap(query, e.get("text", "")) * 0.6
            + float(e.get("importance", 0.5)) * 0.4
        ),
        reverse=True,
    )
    return pool[:limit]


def mark_used(items: list[dict[str, Any]]) -> None:
    """记一次"被想起"。**会真的落盘**（原来是死字段，见下）。

    为什么落盘这件事值得单独做：`score_fact()` 里 `_W_USE = 0.15` ——
    排序有 15% 的权重压在"被用过几次"上，README 也写着"被反复用到的更容易被想起"。
    但改造前这两行只改内存、**从不触发写盘**，而 `_persist()` 只在抽取/记忆/忘记时调用 ——
    也就是说这个计数只在"恰好有别的原因写盘"时才被顺带带出去，绝大多数轮次都白改了。
    现象：新建条目 `used=0`，跟一条"用了 5 次"的老条目差 0.15 分，这个差距**基本靠运气**。
    实测线上 102 条事实里绝大部分 `used=0`。

    为什么要节流而不是每轮都写：`used` 是**每一轮回复都会变**的字段
    （`build_context` → `retrieve` → `mark_used`），逐轮提交就是每轮一次磁盘写。
    代价换来的精度没有意义（排序看的是量级，不是某一次的 +1），
    所以攒在内存里、由 `flush_usage()` 批量落盘。
    """
    if not items:
        return
    _db.ensure()
    for item in items:
        item["used"] = int(item.get("used", 0)) + 1
        item["last_used"] = _now()
        try:
            _used_dirty.add(int(item.get("id", 0) or 0))
        except (TypeError, ValueError):
            continue


# 攒着待落盘的使用计数（事实 id）。**只放内存**：进程被杀最多丢最近的几次 +1，
# 而那本来就是个统计量，不是数据。
_used_dirty: set[int] = set()
_used_last_flush = 0.0


def usage_pending() -> int:
    """还有几条使用计数没落盘（诊断用）。"""
    return len(_used_dirty)


def flush_usage(*, force: bool = False) -> int:
    """把攒着的使用计数落盘。返回写入条数。

    `force=False` 时受 `memory_used_flush_seconds` 节流 ——
    它由后台循环定期调用，情绪稳定的低频写就够了。
    `force=True` 用于关机/收到指令这种"该收尾了"的时刻。
    """
    global _used_last_flush
    if not _used_dirty:
        return 0
    now = time.time()
    interval = float(settings.get("memory_used_flush_seconds"))
    if not force and interval > 0 and (now - _used_last_flush) < interval:
        return 0
    _db.ensure()
    want = set(_used_dirty)
    _used_dirty.clear()
    _used_last_flush = now

    by_id = {int(f.get("id", 0) or 0): f for f in _db.facts}
    rows = [by_id[fid] for fid in want if fid in by_id]
    if not rows:
        return 0
    store = _db.store
    if store is not None and hasattr(store, "touch_facts"):
        # SQLite：按行 UPDATE，只写这四个可变字段
        store.touch_facts(rows)
    else:
        # JSON 后端：没有按行写的能力，只能整份重写（这就是它要被 SQLite 取代的原因）
        _db.save()
    logger.debug("使用计数已落盘：%d 条（另有 %d 条已不在库里）", len(rows), len(want) - len(rows))
    return len(rows)


# --------------------------------------------------------------------- 渲染
def has_anything() -> bool:
    _db.ensure()
    return bool(_db.facts or _db.profile or _db.events)


def stats() -> dict[str, Any]:
    _db.ensure()
    used_once = sum(1 for f in _db.facts if int(f.get("used", 0) or 0) > 0)
    confirmed = sum(1 for f in _db.facts if int(f.get("hits", 0) or 0) > 0)
    manual = sum(1 for f in _db.facts if f.get("source") == "manual")
    protected = sum(1 for f in _db.facts if f.get("protected"))
    return {
        "facts": len(_db.facts),
        "events": len(_db.events),
        "people": len(_db.profile),
        "protected": protected,
        "manual": manual,
        # 「人说的」与「模型猜的」各占多少 —— 前者永不淘汰，所以它一多，
        # memory_max_items 就名存实亡。这两个数直接决定上限还剩多少空间。
        "manual_protected": sum(
            1 for f in _db.facts
            if f.get("source") == "manual" and f.get("protected")
        ),
        "evictable": sum(
            1 for f in _db.facts
            if not f.get("protected") and f.get("source") != "manual"
        ),
        "limit": int(settings.get("memory_max_items")),
        # 下面两个是"排序里的两项权重到底有没有在动"的直接证据。
        # 改造前它们恒等于 0（字段不落盘），现在应该随聊天稳定增长。
        "used_once": used_once,
        "confirmed": confirmed,
        "usage_pending": len(_used_dirty),
    }


def render_profile(keys: list[str] | None = None, budget: int = 600) -> str:
    """把人物画像渲染成几行 —— 只在预算内、只挑相关的人。"""
    _db.ensure()
    keys = keys if keys is not None else list(_db.profile.keys())
    lines: list[str] = []
    used = 0
    for key in keys:
        info = _db.profile.get(key)
        if not info:
            continue
        bits: list[str] = []
        who = info.get("display") or key
        if info.get("love"):
            bits.append("喜欢" + "、".join(info["love"][:4]))
        if info.get("dislike"):
            bits.append("不喜欢" + "、".join(info["dislike"][:4]))
        if info.get("habit"):
            bits.append("习惯" + "、".join(info["habit"][:3]))
        if info.get("note"):
            bits.append(info["note"][:60])
        if not bits:
            continue
        line = f"- {who}：{'；'.join(bits)}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def render_facts(items: list[dict[str, Any]], *, with_usage: bool = False) -> str:
    """渲染事实条目。

    `with_usage=False`（注入 prompt 时）**不显示次数** ——
    "这条被想起过 7 次"对模型没有信息量，只会白占 token，还可能被它复述出来。
    `/记忆 列表` 与诊断用 `with_usage=True`，那时次数正是要给人看的证据。
    """
    lines: list[str] = []
    for item in items:
        when = str(item.get("time", ""))[:10]
        source = "（我记下的）" if item.get("source") == "manual" else ""
        tail = ""
        if with_usage:
            uses = int(item.get("used", 0) or 0)
            hits = int(item.get("hits", 0) or 0)
            bits = []
            if uses:
                bits.append(f"被想起 {uses} 次")
            if hits:
                bits.append(f"被提到 {hits} 次")
            if item.get("protected"):
                bits.append("已保护")
            if bits:
                tail = "（" + "，".join(bits) + "）"
        lines.append(f"- {when} {item.get('text', '')}{source}{tail}")
    return "\n".join(lines)


def dump(limit: int = 40) -> str:
    """给 /记忆 列表 用的完整清单（按时间倒序）。"""
    _db.ensure()
    rows: list[str] = []
    profile = render_profile()
    if profile:
        rows.append("【我记得的人】\n" + profile)
    if _db.facts:
        recent = sorted(_db.facts, key=lambda f: float(f.get("ts", 0)), reverse=True)[:limit]
        rows.append("【我记得的事】\n" + render_facts(recent, with_usage=True))
    if _db.events:
        recent = sorted(_db.events, key=lambda e: float(e.get("ts", 0)), reverse=True)[:10]
        rows.append("【群里发生过的】\n" + "\n".join(f"- {e.get('time','')[:16]} {e.get('text','')}" for e in recent))
    return "\n\n".join(rows) if rows else ""


def search(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    _db.ensure()
    keyword = keyword.strip()
    if not keyword:
        return []
    out = [f for f in _db.facts if keyword in str(f.get("text", "")) or keyword in str(f.get("name", ""))]
    out.sort(key=lambda f: (_overlap(keyword, f.get("text", "")), float(f.get("ts", 0))), reverse=True)
    return out[:limit]


def all_facts() -> list[dict[str, Any]]:
    _db.ensure()
    return sorted(_db.facts, key=lambda f: float(f.get("ts", 0)), reverse=True)


def all_events() -> list[dict[str, Any]]:
    _db.ensure()
    return sorted(_db.events, key=lambda e: float(e.get("ts", 0)), reverse=True)


# --------------------------------------------------------------------- 淘汰
def compact() -> int:
    """超出容量时按「分数最低优先」淘汰，protected 永不淘汰。返回删了几条。

    分数 = 重要度 * 0.6 + 时效 * 0.4。所以「很久以前、又不重要、还没被用过」的最先走 ——
    这跟人的记忆一样：细节会淡，重要的和最近的不淡。

    **这里修掉过一个真 bug**：原来 `events` 的淘汰写在这个函数**末尾**，
    而函数开头有一句 `if cap <= 0 or len(facts) <= cap: return 0` ——
    于是「facts 没超上限」时**提前返回，events 永远不被淘汰**。
    实测线上就是这个状态（102 条 facts / 800 上限 → 群事件只增不减）。
    更别扭的是：一旦哪天 facts 超了 800，这个分支又会突然生效，
    把 events 按**条数**砍到 800 —— 同一份数据两套语义，且切换点由另一个参数决定。
    现在两者彻底分开：facts 按分数淘汰，events 按**时间窗**（`memory_events_days`）
    + 一个独立的兜底条数上限。

    **另外**：`source="manual"` 的事实连同 `protected` 一起**豁免淘汰**。
    理由是同一件事的两侧：判分时它已经靠 `_W_MANUAL` 排在前面（`score_fact`），
    但"排在前面"只在入选名额里有效 —— 一旦上限压下来，用户明说的事仍可能被丢掉。
    淘汰是最该保护它的地方，因为丢在这里是**静默且不可恢复**的。
    """
    _db.ensure()
    cap = int(settings.get("memory_max_items"))
    removed = 0
    if cap > 0 and len(_db.facts) > cap:
        ranked = sorted(
            _db.facts,
            key=lambda f: (
                0 if f.get("protected") else 1,
                -(float(f.get("importance", 0.5)) * 0.6 + _recency(float(f.get("ts", 0))) * 0.4),
            ),
        )
        drop = len(_db.facts) - cap
        for fact in ranked:
            if removed >= drop:
                break
            # `protected` 与 `source == "manual"` 都永不淘汰。
            # 双条件而不是只看 protected：手动存的条目历史上可能没带 protected
            # （它这次的默认值刚改成"manual 即保护"），老数据不该因为版本差异被丢。
            if fact.get("protected") or fact.get("source") == "manual":
                continue
            _db.facts.remove(fact)
            removed += 1
        # 受保护的条目多到把可淘汰空间吃光时，上限会失效、库会持续增长。
        # 这类"看起来没爆但一直在涨"的状态最难查，所以留一条日志点名。
        if removed < drop:
            logger.warning(
                "记忆库超出上限 %d 条，但有 %d 条被保护/手动存过、不可淘汰 —— "
                "上限实际失效，库会继续增长。要么提高 memory_max_items，"
                "要么用 /记忆 删 清掉不再需要的受保护条目。",
                cap, len(_db.facts) - cap - removed,
            )

    # events：先按时间窗（语义正确的那把尺），再用独立条数上限兜底（防病态增长）
    ev_days = int(settings.get("memory_events_days"))
    if ev_days > 0:
        cutoff = time.time() - ev_days * 86400
        keep_events = []
        for event in _db.events:
            try:
                fresh = float(event.get("ts", 0)) >= cutoff
            except (TypeError, ValueError):
                fresh = True  # 时间戳读不出来时保留，宁可留旧也不要误删
            if fresh:
                keep_events.append(event)
        if len(keep_events) != len(_db.events):
            removed += len(_db.events) - len(keep_events)
            _db.events = keep_events

    ev_cap = int(settings.get("memory_events_max"))
    if ev_cap > 0 and len(_db.events) > ev_cap:
        _db.events.sort(key=lambda e: float(e.get("ts", 0) or 0), reverse=True)
        removed += len(_db.events) - ev_cap
        del _db.events[ev_cap:]
    return removed


# --------------------------------------------------------------------- 抽取
# 说明：这里原本有个 `_recent_lines(conv, n=14)`，只取「最近 14 条」——
# 正是「没人 @ 它的长对话 / 额度用完的那批消息永久不入库」的根因（见上面的水位线一节）。
# 抽取输入统一从**水位线之后**取（原来的 `_messages_to_lines()` 已删除）。


_EXTRACT_PROMPT = """你是记忆提取器。下面是一段群聊/私聊记录，请提取**值得长期记住**的信息。

只提取这几类：
1. 关于某个人的稳定信息：称呼、喜好、厌恶、习惯、职业、作息、正在长期做的事；
2. 明确发生过的、以后可能还会提到的事件或决定；
3. 谁纠正过你、或明确让你记住的事（这类重要度给 0.9 以上）。

不要提取：一时的情绪、玩笑、没有后续的闲话、你自己说过的话、重复已有记忆的内容。
不确定是否重要时，宁可不提 —— 垃圾记忆比没有记忆更糟。

**如果这条信息是在更正「已有记忆」里的某一条**（作息变了、换了工作、之前记错了），
就把那条旧记忆的原文照抄到 `prev_text` 里，我们会用它覆盖旧条目。
不写 `prev_text` 就会留下两条互相矛盾的事实并存 —— 那比不记更糟。

【对话记录】
{lines}

已有记忆（不要重复这些，除非是要更正它）：
{existing}

只输出 JSON，不要任何多余文字：
{{"profile": [{{"who": "显示名", "love": ["喜欢的东西"], "dislike": ["不喜欢的"], "habit": ["习惯或长期状态"]}}],
  "facts": [{{"text": "一句话事实，第三人称，含主体", "importance": 0.5, "subject": "谁", "prev_text": "（可选）被更正的旧记忆原文"}}],
  "events": [{{"text": "这个群里发生的事", "importance": 0.5}}]}}

没有可提取的就输出 {{"profile": [], "facts": [], "events": []}}。"""


async def _extract(lines: list[str]) -> dict[str, Any]:
    """调一次小模型做抽取。失败返回空结构，绝不抛。"""
    if not lines:
        return {}
    existing = "\n".join(
        f"- {f.get('text','')}" for f in sorted(_db.facts, key=lambda f: float(f.get("ts", 0)), reverse=True)[:20]
    )
    prompt = _EXTRACT_PROMPT.replace("{lines}", "\n".join(lines)).replace(
        "{existing}", existing or "（还没有）"
    )
    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=settings.get("model"),
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=800,
            ),
            timeout=config.TIMEOUT,
        )
        raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - 抽取失败绝不能影响聊天
        logger.info("记忆抽取失败（不影响聊天）")
        return {}


def _budget_ok(n_messages: int = 1) -> bool:
    """今天还允许提炼吗。**按消息条数记账**（见 `extract_budget_left` 的说明）。

    原来记的是「调用次数」并把计数放在内存里（`_extract_calls`），重启即清零 ——
    也就是说重启能绕过每日上限。现在计数落在水位线文件里，重启不会重置当天额度。
    """
    if not settings.get("memory_extract"):
        return False
    return extract_budget_left() >= max(1, int(n_messages))


def _messages_to_lines(messages: list[dict[str, Any]], clip: int | None = None) -> list[str]:
    """把消息对象渲染成抽取提示词用的行。与 `_recent_lines` 保持同一格式。"""
    from . import chatlog  # 局部导入：chatlog 不能反过来 import memory

    clip = int(settings.get("msg_clip")) if clip is None else clip
    lines: list[str] = []
    for msg in messages:
        who = "你" if msg.get("is_bot") else str(msg.get("name", ""))
        text = chatlog.ConversationLog.clip_text(msg.get("text", ""), clip)
        if text:
            lines.append(f"[{msg.get('time', '')} {who}] {text}")
    return lines


async def _apply_extraction(data: dict[str, Any], *, conv: str, ts_map: dict[int, float] | None = None,
                            source: str = "extract",
                            senders: dict[str, int] | None = None,
                            bot_ids: set[int] | None = None,
                            bot_names: set[str] | None = None) -> int:
    """把一次抽取的 JSON 结果落到库里，返回新增/更新条数。

    `ts_map`：**按行回填原始发生时间**，只给水位线/补提取用。
    没有它的话，补出来的历史事实会全部挤在「今天」，时间线反而比不补更乱。
    映射键是行序号（提示词里 `[时间 发言人]` 的行序），值是那一行的真实 `ts`。

    `senders`：这一批消息的「显示名 → uid」。**身份归一的第二级**（第一级是实体表）：
    模型给的 `who`/`subject` 是名字，用它落成 uid 才能让同一个人跨群只有一份画像与一份事实。
    传 None 时退化成纯人名（= 改造前的行为）。

    `bot_ids` / `bot_names`：机器人自己的 uid 与显示名。用来**拒掉"它给自己建的画像"** ——
    输入里明明只有别人的发言，模型照样可能输出一条人设角色的画像（实测线上就有）。
    """
    _db.ensure()
    added = 0
    max_items = int(settings.get("memory_extract_max"))
    senders = senders or {}
    bot_ids = bot_ids or set()
    bot_names = bot_names or set()

    for person in (data.get("profile") or [])[:4]:
        if not isinstance(person, dict):
            continue
        # **用 _MAX_NAME(40) 而不是写死 40/24**：原来键被 `_clean(key, _MAX_KEY=24)` 砍到
        # 24 字，导致群里长昵称（如「夜风の 旅人⭐」）被截断 ——
        # 同一个人因为每次截法不同，会分裂成好几个画像键，去重也就失效了。
        who = _clean(person.get("who"), _MAX_NAME)
        if not who:
            continue

        def _list(name: str) -> list[str]:
            raw = person.get(name)
            if isinstance(raw, str):
                raw = [raw]
            # 这里不再二次截断到 30 字 —— 长度统一由 set_profile 的 _MAX_PROFILE_ITEM 管，
            # 免得两处上限不一致、又出现断句。
            return [_clean(x, _MAX_PROFILE_ITEM) for x in (raw or []) if str(x or "").strip()]

        # ---- 身份归一：把"模型给的名字"解成 uid ----
        # 解出来就用 `uid:<QQ号>` 当画像键，并把散在人名键下的旧画像并过来；
        # 解不出来就照旧用人名当键（**不猜**，把两个人合并比不合并更糟）。
        uid = _resolve_uid(who, senders)

        # ---- 防线①：拒掉"机器人给自己建的画像" ----
        # 位置刻意在 set_profile 之前：不是"建完再删"，而是**根本不建**。
        # 建成再删会留下一条 updated_at 被刷新过的空条目，控制台照样看得见。
        if _is_bot_self(who, uid, bot_ids=bot_ids, bot_names=bot_names):
            logger.info("跳过机器人自己的画像（%s）", who[:40])
            continue

        key = _uid_key(uid) or who
        if uid is not None:
            _record_name(who, uid, conv=conv)
            consolidate_profile(who, key)
        else:
            # ---- 防线②：这个人名键可能早就对应一个已知 uid ----
            # 模型给出的名字变体（「小明」写成别的截法、或这一批没带上发送者名单）
            # 会让 `_resolve_uid` 解不出来。不处理的话同一个人就多一个画像键 ——
            # 那正是控制台里"好几个重复角色"的来源。实体表里如果写着这个名字属于谁，
            # 就并过去。
            key = _link_key_to_uid(key)

        set_profile(
            key,
            display=who,
            love=_list("love"),
            dislike=_list("dislike"),
            habit=_list("habit"),
        )
        added += 1

    for fact in (data.get("facts") or [])[:max_items]:
        if not isinstance(fact, dict):
            continue
        text = _clean(fact.get("text"))
        if len(text) < 4:
            continue
        try:
            importance = float(fact.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        subject = _clean(fact.get("subject"), _MAX_NAME) or "群友"
        # **更正路径**：模型可以带 prev_text 指明"我要推翻哪条旧记忆"。
        # 为什么需要它：`_find_similar` 只能猜（靠词面重叠），而"他换工作了"这种
        # 更正与旧条目"他在做网架设计"字面几乎不重叠 —— 猜不到就会留下两条矛盾事实，
        # 模型随机挑一条，表现就是"记性时好时坏"。
        prev = _clean(fact.get("prev_text"), _MAX_TEXT)
        if prev:
            target = _find_by_text(prev)
            if target is not None:
                # 同样是就地改字典 → 必须显式 upsert（见 add_fact 里的同类说明）
                target["prev_text"] = prev
                target["text"] = text
                target["importance"] = max(float(target.get("importance", 0.5)), importance)
                target["updated_at"] = _now()
                target["hits"] = int(target.get("hits", 0)) + 1
                target["last_hit"] = _now()
                _db.upsert_fact(target)
                logger.info("记忆更正：%r → %r", prev[:40], text[:40])
                continue
        fact_ts = _ts_map_pick(ts_map, fact)
        # 主体同样尽量落到 uid：按名字检索时"谁"只命中一半（README 里那个
        # 「小明 / 夜风の 旅人⭐」就是同一个 QQ 号的两个名字）。
        subj_uid = _resolve_uid(subject, senders)
        if subj_uid is not None:
            _record_name(subject, subj_uid, conv=conv)
        _, created = add_fact(
            text,
            scope="global",
            subject=subject,
            uid=subj_uid,
            name=subject,          # 名字单独留一份完整版，供以后按 uid 归并
            conv=conv,
            importance=importance,
            source=source,
            ts=fact_ts,
        )
        added += 1 if created else 0

    for event in (data.get("events") or [])[:max_items]:
        if not isinstance(event, dict):
            continue
        text = _clean(event.get("text"))
        if len(text) < 4:
            continue
        try:
            importance = float(event.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        add_event(text, conv=conv, importance=importance, ts=_ts_map_pick(ts_map, event))
        added += 1

    return added


def _ts_map_pick(ts_map: dict[int, float] | None, item: dict[str, Any]) -> float | None:
    """从 ts_map 里取这条事实/事件对应的原始时间。

    模型偶尔会带 `line`（第几行）字段；带了就用它，没带就退化成「这一批里最早的一条」——
    宁可时间早一点，也不要全部挤在「今天」。完全没有 ts_map（线上正常路径）返回 None，
    由 `add_fact` 用当前时刻，行为和改造前一致。
    """
    if not ts_map:
        return None
    try:
        line_no = item.get("line")
    except AttributeError:
        line_no = None
    if line_no is not None:
        try:
            hit = ts_map.get(int(line_no))
            if hit:
                return float(hit)
        except (TypeError, ValueError):
            pass
    return min(ts_map.values()) if ts_map else None


async def extract_and_store(conv: str) -> int:
    """回复发出之后触发：从「水位线之后」接着抽取。返回新增条数。

    与改造前的区别（这是「填补漏斗」的核心）：
      * 原来只看**最近 14 条**，且不记进度 → 没人 @ 机器人的长对话、以及当天额度用完
        的那批消息，**永久不入库**；
      * 现在从水位线往后取一批（`memory_extract_batch` 条），处理成功才推进水位线，
        **失败或额度不足都只是推迟**，下次接着抽。
    """
    return await drain_extraction(conv)


async def drain_extraction(conv: str, *, max_messages: int = 0, only_rolled: bool = True) -> int:
    """把一个会话「水位线之后」的消息抽进记忆库。返回新增/更新条数。

    `only_rolled=True` 时只处理**已经结束的会话轮次**（到 `last_session_boundary_id` 为止）——
    正在进行中的那一轮由回复路径负责，避免同一批消息被抽两遍。
    `max_messages` 覆盖单批条数（默认取 `memory_extract_batch`）。
    """
    from . import chatlog  # 局部导入，避免循环依赖

    _db.ensure()
    if not config.API_KEY or not settings.get("memory_enabled"):
        return 0
    if not _budget_ok(1):
        return 0

    log = chatlog.get_log_sync(conv)
    if log is None:
        return 0

    batch = int(max_messages or settings.get("memory_extract_batch"))
    if batch <= 0:
        return 0

    mark = watermark(conv)
    messages = log.since_id(mark, include_bot=False, limit=batch + 1)
    if not messages:
        return 0
    # 决定这次真正处理到哪条：
    #   有会话边界 → 只处理边界之前的（本轮定型部分）
    #   没有边界（同一轮里的长对话）→ 也允许处理，否则活跃群会一直积压
    boundary = log.last_session_boundary_id()
    if only_rolled and boundary > mark:
        messages = [m for m in messages if int(m.get("id", 0)) <= boundary]
        if not messages:
            return 0
    if len(messages) > batch:
        messages = messages[:batch]

    # 额度不够就只处理额度允许的部分 —— **不推进水位线到没处理的消息**，
    # 所以剩下的部分下一次还会被捞起来。
    left = extract_budget_left()
    if left < len(messages):
        messages = messages[:left]
        if not messages:
            return 0

    lines = _messages_to_lines(messages)
    if not lines:
        return 0
    ts_map = {i + 1: float(m.get("ts") or 0) for i, m in enumerate(messages)}
    ts_map = {k: v for k, v in ts_map.items() if v > 0}

    data = await _extract(lines)
    if not data:
        # 抽取失败：**不动水位线**，下一轮同一批会再试一次。
        return 0

    _bot_uid_set, _bot_name_set = _bot_ids(messages)
    added = await _apply_extraction(
        data, conv=conv, ts_map=ts_map, source="extract",
        senders=_senders_of(messages, bot_ids=_bot_uid_set),
        bot_ids=_bot_uid_set, bot_names=_bot_name_set,
    )
    last_id = int(messages[-1].get("id", 0))
    dropped = compact()
    _advance_watermark(conv, last_id, len(messages))
    await _persist()
    flush_usage(force=True)  # 抽取是低频动作，顺手把使用计数收尾，免得攒着
    logger.info(
        "记忆抽取 conv=%s 处理 %d 条（到 #%d），新增/更新 %d 条，淘汰 %d 条",
        conv, len(messages), last_id, added, dropped,
    )
    return added


async def drain_all(*, per_conv: int = 0, max_convs: int = 2) -> int:
    """后台滴取：把所有**有积压**的会话各抽一批。返回新增条数。

    给 `__init__` 的周期任务用。存在的理由：抽取原来只在「回复之后」触发，
    于是**没人 @ 机器人的那些长对话永远不会被提炼** —— 那是记忆库里最大的一块空白。
    这里每次只做 `max_convs` 个会话，避免一次把所有群都抽一遍烧额度。
    """
    from . import chatlog

    if not settings.get("memory_enabled") or not settings.get("memory_extract"):
        return 0
    if not settings.get("memory_extract_drain"):
        return 0
    if extract_budget_left() <= 0:
        return 0

    total = 0
    done = 0
    for conv, log in list(chatlog.all_logs().items()):
        if done >= max_convs:
            break
        mark = watermark(conv)
        if log.max_id() <= mark:
            continue  # 没有积压
        try:
            added = await drain_extraction(conv, max_messages=per_conv)
        except Exception:  # noqa: BLE001 - 滴取失败绝不能影响别的会话
            logger.exception("后台补抽失败 conv=%s", conv)
            continue
        total += added
        done += 1
    return total


async def usage_loop() -> None:
    """把「被想起次数」定期落盘。**与补抽完全独立** —— 它不调模型、不花 token。

    间隔取 `memory_used_flush_seconds`(60s)，比 `mark_used` 的调用频率低得多：
    排序看的是量级（`uses / 5` 封顶），攒 60 秒的精度损失对结果没有影响。
    """
    while True:
        try:
            flush_usage()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("使用计数落盘失败（下一轮继续）")
        try:
            await asyncio.sleep(max(5, int(settings.get("memory_used_flush_seconds"))))
        except asyncio.CancelledError:
            raise


async def drain_loop() -> None:
    """后台补抽的周期任务。**启动时不立刻跑** —— 先让机器人把冷启动忙完。

    间隔刻意长（默认 1800 秒）：这一层的目的是「补上没人理的那些对话」，
    不是「尽快把每句话都提炼掉」，慢一点反而更省额度、更不容易和回复抢带宽。
    """
    await asyncio.sleep(120)
    while True:
        try:
            if settings.get("memory_extract_drain") and extract_budget_left() > 0:
                added = await drain_all()
                if added:
                    logger.info("后台补抽完成：新增/更新 %d 条", added)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("后台补抽循环出错（下一轮继续）")
        try:
            await asyncio.sleep(max(300, int(settings.get("memory_extract_drain_interval"))))
        except asyncio.CancelledError:
            raise


# --------------------------------------------------------------------- 对外渲染
# 回忆类问法的特征词。命中它才会启用「多角度检索」与「翻旧账」——
# 这两件事都比一次本地打分贵，不能让每轮闲聊都付这个成本。
_RECALL_WORDS = (
    "还记得", "还记得吗", "记不记得", "想起来", "想不起来", "跟你说过", "跟你说",
    "我说过", "提过", "上次", "上回", "前几次", "前几天", "上周", "上个月", "以前",
    "之前说", "当初", "那时候", "当时", "是不是说过", "有没有说过", "聊过",
)


def wants_recall(text: str) -> bool:
    """对方是不是在"翻旧账"（问以前说过什么）。"""
    s = str(text or "")
    return any(w in s for w in _RECALL_WORDS)


def build_context(query: str, *, conv: str, people: list[str] | None = None,
                  multi_angle: bool = False) -> str:
    """组装「【我还记得的事】」文本块；没有可用的返回空串。

    这一段是长期记忆真正进入对话的地方 —— 它跟 chatlog 的"最近记录"是并列的两块：
    记录回答"刚才发生了什么"，这一块回答"我一直知道什么"。
    """
    if not settings.get("memory_enabled"):
        return ""
    _db.ensure()
    if not _db.facts and not _db.profile:
        return ""

    subjects = _people_in(query)
    if people:
        subjects = list(dict.fromkeys(subjects + people))

    if multi_angle and settings.get("recall_multi_angle"):
        # 多角度：三路召回再合并（默认用本地派生的三个角度，0 token）
        facts = retrieve_multi(query, conv=conv, subjects=subjects)
        if not facts:
            facts = retrieve(query, conv=conv, subjects=subjects)
    else:
        facts = retrieve(query, conv=conv, subjects=subjects)
    mark_used(facts)
    events = retrieve_events(query, conv) if settings.get("memory_events") else []

    blocks: list[str] = []
    profile_text = render_profile(subjects or None) if subjects else ""
    # 没点名谁的时候也带上画像 —— 不然"你记得我喜欢什么吗"就答不出来
    if not profile_text:
        profile_text = render_profile(budget=300)
    if profile_text:
        blocks.append("【你记得的人】\n" + profile_text)
    if facts:
        blocks.append("【你记得的事】\n" + render_facts(facts))
    if events:
        blocks.append(
            "【这个会话里发生过的】\n"
            + "\n".join(f"- {e.get('time','')[:16]} {e.get('text','')}" for e in events)
        )
    if not blocks:
        return ""

    return (
        "\n".join(blocks)
        + "\n【怎么用这些记忆】\n"
        "- 跟当下话题有关就自然带出来，不要念清单、不要逐条复述；\n"
        "- 对方问「你还记得吗」这类问题，就从这里找答案；\n"
        "- **只把上面写着的当真的**。这里没有的事，就是你不记得 —— 直接说记不清、"
        "或者按你的性子岔开、反问一句都行，**不要编一个「我们上次说好的」出来**；\n"
        "- 上面没写的细节（具体几点、具体金额、当时原话）不要补，宁可说「记不太清了」。\n"
        "- 如果 system 里另有「翻到的旧记录」，那是**聊天原文**，比这里的记忆条目更准 ——\n"
        "  回答「你上次具体怎么说的」时优先用它。\n"
        "（编造共同经历比单纯忘记更让人不信任 —— 忘记是人之常情，编是欺骗。）"
    )


def is_enabled() -> bool:
    return bool(settings.get("memory_enabled"))
