"""表情包库：下载群里的图、由人格打喜好分、按概率随机取用。

## 模型的视觉能力（**全项目只在这里写一份，别处一律引用这段**）

2026-09 实测（见 README 5.7「视觉能力的实测边界」）：

| 模型 | 读图 |
|---|---|
| `deepseek-flash` | ✅ 能真正读图，能点评画面本身 |
| `deepseek-chat` | ✅ 能真正读图 |
| `deepseek-v4-pro` | ⚠️ 接口**接受**图片输入，但模型回「我无法查看这张图片」 |

结论有两条，别混：

1. `deepseek-flash` / `deepseek-chat` **不是纯文本模型** —— 图像输入可用。
   凡是"模型看不到像素"的说法都是**过时的**，曾经的依据是 `deepseek-chat`，
   实测已否。
2. 默认配置的两个模型（`deepseek-flash`、`deepseek-v4-pro`）里，
   **v4-pro 是那个不看图的**。所以 `sticker_vision` / `chat_vision`
   这类开关不是"永远开着就行"，换模型时必须跟着复查。

## 打分的两条路径是**降级关系**，不是二选一

`judge_image()` 的做法：`sticker_vision` 开着、且图不超过
`sticker_vision_max_kb` 时，**把图片本体一起送过去**，模型直接点评画面；
图太大 / 开关关了 / 调用失败时，退回"元信息 + 上下文"模式 —— OneBot 的
`sub_type`（1=表情 / 7=贴纸…）、文件大小、谁发的、是不是主人发的、前后文聊什么。
两条路径下分数都是模型给的，区别只在**它有没有看到像素**。

存储：data/stickers/ 下按内容哈希命名，索引写 data/stickers/index.json。
同一个文件在群里被转发多次只会存一份（哈希去重）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from openai import AsyncOpenAI

from . import config, perceptual, settings, state
from .perceptual import distance as phash_distance

logger = logging.getLogger("ai_chat.stickers")

_client = AsyncOpenAI(api_key=config.API_KEY or "sk-not-configured", base_url=config.BASE_URL)

# OneBot v11 image 段的 sub_type
SUB_TYPE_LABEL = {
    0: "普通图片",
    1: "表情",
    2: "热图",
    3: "斗图",
    4: "智能图",
    7: "贴纸",
}

# 能直接判定为"表情类"的 sub_type，优先送模型评分（其余的先按大小粗筛）
STICKER_LIKE_SUB_TYPES = {1, 2, 3, 4, 7}

_DOWNLOAD_LIMIT = 8 * 1024 * 1024  # 单张图最大下载 8MB，防止拉爆内存


def _guess_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return "bin"


def _download(url: str, timeout: float = 15.0) -> bytes:
    """同步下载，调用方用 to_thread 包起来。

    用 urllib 而不是引入 aiohttp/httpx：这个项目只在这一处需要拉图，
    不值得为一个功能多背一个 HTTP 依赖。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "nonebot-ai-chat/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 来源是 OneBot 给的图片地址
        return resp.read(_DOWNLOAD_LIMIT)


class StickerLibrary:
    def __init__(self) -> None:
        self.dir: Path = config.STICKER_DIR
        self.index_path: Path = self.dir / "index.json"
        self.items: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._loaded = False
        self._recent: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=64))

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        self._loaded = True
        if not self.index_path.exists():
            return
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("表情包索引损坏，按空库继续：%s", self.index_path)
            return
        if isinstance(raw, dict):
            self.items = [it for it in (raw.get("items") or []) if isinstance(it, dict)]

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(self.items),
            "items": self.items,
        }
        tmp = self.index_path.with_name(self.index_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_path)

    # ------------------------------------------------------------ 限流
    def _rate_ok(self, conv: str) -> bool:
        """每群每分钟最多入库 N 张 —— 既挡刷图，也省模型打分 token。"""
        limit = int(settings.get("sticker_rate_limit"))
        now = time.time()
        bucket = self._recent[conv]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    # ------------------------------------------------------------ 写入
    def find(self, digest: str) -> dict[str, Any] | None:
        for it in self.items:
            if it.get("hash") == digest:
                return it
        return None

    def add(
        self,
        data: bytes,
        *,
        conv: str,
        uid: int,
        name: str,
        is_master: bool,
        sub_type: int,
        score: float,
        reason: str,
        phash_value: str = "",
        size_wh: tuple[int, int] = (0, 0),
        weight: float = 1.0,
        file_sent: bool = False,
        near_of: str = "",
        near_distance: int = 0,
    ) -> dict[str, Any] | None:
        digest = hashlib.sha256(data).hexdigest()[:16]
        if self.find(digest) is not None:
            return None  # 字节完全相同，重复转发

        ext = _guess_ext(data)
        filename = f"{digest}.{ext}"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / filename).write_bytes(data)

        item = {
            "hash": digest,
            "file": filename,
            # 感知哈希与尺寸：前者用于近似去重，后者只用于人工核对（见 webui 的记忆库标签页）
            "phash": phash_value,
            "width": int(size_wh[0]),
            "height": int(size_wh[1]),
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": round(time.time(), 3),
            "from_conv": conv,
            "from_uid": uid,
            "from_name": name,
            "is_master": bool(is_master),
            "sub_type": sub_type,
            "sub_type_label": SUB_TYPE_LABEL.get(sub_type, f"未知({sub_type})"),
            "size": len(data),
            "score": round(float(score), 3),
            # 这条图"作为表情包"的权重：以文件形式发来的图会被压低，见 store_from_message
            "weight": round(float(weight), 3),
            "file_sent": bool(file_sent),
            "reason": reason[:200],
            "uses": 0,
            "last_used": None,
            # 如果是"内容近似"判重时用户坚持收录，记下跟谁像、差多少，便于以后复核
            "near_of": near_of,
            "near_distance": int(near_distance),
        }
        self.items.append(item)
        self.save()
        return item

    def remove(self, digest: str) -> bool:
        for idx, it in enumerate(self.items):
            if it.get("hash") == digest:
                try:
                    (self.dir / str(it.get("file"))).unlink(missing_ok=True)
                except OSError:
                    logger.warning("删除表情包文件失败：%s", it.get("file"))
                self.items.pop(idx)
                self.save()
                return True
        return False

    def find_by_source(self, conv: str, uid: int) -> list[dict[str, Any]]:
        """按「哪个会话里谁发的」查已入库的图，供「这张别存了」回溯删除。"""
        return [
            it
            for it in self.items
            if str(it.get("from_conv")) == conv and int(it.get("from_uid", -1)) == int(uid)
        ]

    def clear(self) -> int:
        n = len(self.items)
        for it in self.items:
            try:
                (self.dir / str(it.get("file"))).unlink(missing_ok=True)
            except OSError:
                pass
        self.items.clear()
        self.save()
        return n

    # ------------------------------------------------------------ 读取
    def pick(self, *, candidates: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """按"用得越少越容易被选中"抽一张；也可以只在一个候选子集里抽。

        `candidates` 是给"按语境挑图"用的：先用本函数从全库随机取一小撮候选
        （保持"少用的更容易被选中"这个性质），再交给模型从中挑或否决。
        多出来的那一步见 `pick_for_context()`。
        """
        pool = [it for it in (candidates if candidates is not None else self.items) if it]
        if not pool:
            return None
        # 用得越少越容易被选中，避免老是同一张
        weights = [1.0 / (1.0 + int(it.get("uses", 0))) for it in pool]
        chosen = random.choices(pool, weights=weights, k=1)[0]
        chosen["uses"] = int(chosen.get("uses", 0)) + 1
        chosen["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()
        return chosen

    def sample(self, count: int) -> list[dict[str, Any]]:
        """取一小撮候选（按"少用的优先"，但不是纯随机 —— 见得少的图更该被考虑）。

        为什么不让模型看全库：库里几十上百张，每张都附给模型既贵又慢，
        而且候选一多它的选择质量反而下降。所以先本地粗选，再让模型定夺。
        """
        if count <= 0 or not self.items:
            return []
        pool = list(self.items)
        if len(pool) <= count:
            random.shuffle(pool)
            return pool
        # 权重 = 用得越少越可能进候选；再加一点随机，避免候选集合固定不变
        weights = [1.0 / (1.0 + int(it.get("uses", 0))) for it in pool]
        picked: list[dict[str, Any]] = []
        remaining = list(pool)
        remaining_weights = list(weights)
        for _ in range(count):
            if not remaining:
                break
            choice = random.choices(range(len(remaining)), weights=remaining_weights, k=1)[0]
            picked.append(remaining.pop(choice))
            remaining_weights.pop(choice)
        return picked

    def path_of(self, item: dict[str, Any]) -> Path:
        return self.dir / str(item.get("file"))

    def stats(self) -> dict[str, Any]:
        return {
            "count": len(self.items),
            "total_bytes": sum(int(it.get("size", 0)) for it in self.items),
            "used": sum(int(it.get("uses", 0)) for it in self.items),
            "no_phash": sum(1 for it in self.items if not it.get("phash")),
            "file_sent": sum(1 for it in self.items if it.get("file_sent")),
            "duplicates": self.duplicate_groups(),
        }

    def duplicate_groups(self, limit: int | None = None) -> int:
        """按感知哈希互相靠近的条目粗暴分几组，返回"疑似重复的组数"。

        只用于给控制台一个体检数字 —— 入库时已经拦过近似重复，
        还能查出组来，说明是**改造前**入库的历史重复（那时没有 phash 判定），
        或者阈值调松之后新进来的。要清理就按组删。
        """
        if limit is None:
            limit = int(settings.get("sticker_dup_distance"))
        hashed = [it for it in self.items if it.get("phash")]
        if not hashed or limit < 0:
            return 0
        parent = list(range(len(hashed)))

        def root(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(hashed)):
            for j in range(i + 1, len(hashed)):
                if phash_distance(str(hashed[i]["phash"]), str(hashed[j]["phash"])) <= limit:
                    ri, rj = root(i), root(j)
                    if ri != rj:
                        parent[rj] = ri
        groups: dict[int, int] = {}
        for i in range(len(hashed)):
            groups[root(i)] = groups.get(root(i), 0) + 1
        return sum(1 for size in groups.values() if size > 1)


_library: StickerLibrary | None = None


async def get_library() -> StickerLibrary:
    global _library
    if _library is None:
        lib = StickerLibrary()
        await asyncio.to_thread(lib.load)
        _library = lib
    return _library


_MIME = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


def content_with_images(prompt: str, images: list[bytes]) -> Any:
    """构造 user content：有图就逐张附上 image_url，没有就纯文本。

    之所以支持多张：当前发言的图和「被引用消息里的图」需要一起交给模型，
    它才能把两者对上号。
    """
    if not images:
        return prompt
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for data in images:
        mime = _MIME.get(_guess_ext(data), "image/jpeg")
        encoded = base64.b64encode(data).decode("ascii")
        blocks.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
        )
    return blocks


async def judge_image(
    *,
    conv_label: str,
    sender: str,
    is_master: bool,
    sub_type: int,
    size_kb: float,
    context: str,
    persona: str,
    image_data: bytes | None = None,
    file_sent: bool = False,
) -> tuple[float, str]:
    """让模型以人设身份给这张图打 0~1 的喜好分。

    实测 deepseek-flash / deepseek-chat **能真正读图**（deepseek-v4-pro 接口接受
    图片输入、但会回"我无法查看这张图片"）—— 完整结论表见文件头，**别在这里再写一份**。
    默认把图片本体一起送过去，它就能点评画面本身，而不是复述元信息。
    图太大、开关关了、或换成了不看图的模型时，退回"元信息 + 上下文"模式。
    失败一律退回规则分，不让收图流程中断。

    `file_sent=True` 时会在提示里明说"这张是以文件形式发来的" —— 打分本身也降一档
    （见下面 `file_penalty`，与 `store_from_message` 里的扣分是**两处独立**的：
    这里扣在模型的判断上，那里扣在最终分数上）。
    """
    fallback = 0.75 if sub_type in STICKER_LIKE_SUB_TYPES else 0.35
    if file_sent:
        fallback = min(fallback, 0.3)
    if not config.API_KEY:
        return fallback, "未配置 Key，按规则分"

    who = "主人" if is_master else "别人"
    label = "以文件形式发送的图片" if file_sent else SUB_TYPE_LABEL.get(sub_type, f"未知({sub_type})")
    sight = (
        "图片就附在下面，你能直接看到它。"
        if image_data is not None
        else "图片没有附上（图超过上限，或这个模型不看图——如 deepseek-v4-pro），只能依据元信息和上下文判断。"
    )
    file_note = (
        "\n注意：这张图是对方**以「发送文件」的方式**发来的，不是作为表情包发的。"
        "这种通常是照片、素材、截图的原图，一般不适合当表情包 —— 除非它本身明显就是梗图。\n"
        if file_sent
        else ""
    )
    prompt = (
        f"{persona}\n\n"
        "----\n"
        f"群里有人发了一张图，{sight}请判断它值不值得收藏进你的表情包库。\n\n"
        f"【图片元信息】\n"
        f"- QQ 上报类型：{label}\n"
        f"- 文件大小：{size_kb:.1f} KB\n"
        f"- 发送者：{sender}（{who}）\n"
        f"- 来自：{conv_label}\n"
        f"{file_note}\n"
        f"【前后文】\n{context or '（没有更多上下文）'}\n\n"
        # 措辞刻意**不提"打分/分数"**：分数只是内部用来排序与过阈值的量，
        # 从不对人显示（见 README「不要输出对图片打分的相关内容」）。这里只讲"留不留、为什么留"。
        "以你的性格和喜好决定留不留：像表情包、斗图素材、跟群里正在玩的梗相关的就留下；"
        "普通照片、截图、文件图、表情包原图之外的大图不要；主人发的可以适当放宽。"
        '只输出 JSON，不要任何多余文字：{"score": 0.0, "reason": "一句话理由"}'
    )

    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=settings.get("model"),
                messages=[
                    {
                        "role": "user",
                        "content": content_with_images(
                            prompt, [image_data] if image_data else []
                        ),
                    }
                ],
                response_format={"type": "json_object"},
                # 推理模型（deepseek-flash / v4-pro）会先花掉一大截 token 思考，
                # 给太小的话 content 直接是空的，JSON 就解析不出来。留够余量。
                max_tokens=800,
            ),
            timeout=config.TIMEOUT,
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        payload = json.loads(text)
        score = float(payload.get("score", fallback))
        reason = str(payload.get("reason", ""))[:200]
        return max(0.0, min(1.0, score)), reason
    except Exception:  # noqa: BLE001 - 打分失败不该影响收图
        logger.exception("表情包打分失败，回退规则分")
        return fallback, "打分失败，按规则分"


def extract_images(event: Any) -> list[dict[str, Any]]:
    """取出消息里**以图片形式**发的图片段（OneBot v11）。"""
    out: list[dict[str, Any]] = []
    message = getattr(event, "message", None)
    if message is None:
        return out
    for seg in message:
        if getattr(seg, "type", None) == "image":
            data = getattr(seg, "data", None) or {}
            out.append(dict(data))
    return out


# 以「文件」形式发来的图能认出来的扩展名。QQ 里"发送文件"选一张图很常见，
# 尤其是 PC 端拖拽 —— 这时 OneBot 上报的是 file 段，sub_type 那套（1=表情 7=贴纸）
# 一个都不会有，等于丢掉了"这是不是表情包"这个最重要的信号。
_IMAGE_FILE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".apng", ".tif", ".tiff", ".avif", ".heic",
}


def is_image_file(name: str) -> bool:
    return Path(name).suffix.lower() in _IMAGE_FILE_EXTS


def extract_image_files(event: Any) -> list[dict[str, Any]]:
    """取出消息里**以文件形式**发来的图片段。

    单独一条路是有意的：这类图要被打低权重（用户要的"给较低的权重"），
    所以要跟真·表情包分开走，而不是混进 `extract_images()` 里分不出来。
    """
    out: list[dict[str, Any]] = []
    for seg in getattr(event, "message", None) or []:
        if getattr(seg, "type", None) != "file":
            continue
        data = dict(getattr(seg, "data", None) or {})
        name = str(data.get("file") or data.get("name") or "")
        if is_image_file(name):
            out.append(data)
    return out


async def forget_latest(conv: str) -> tuple[str, bool]:
    """「不要保存这张图片」的执行体：把刚发的那张加进忽略名单，**已经入库的就删掉**。

    两步都要做，因为时序上有竞争：
    * 图是先到的，`_ingest_image` 是**异步**跑的（下载 + 打分 + 落库可能要几秒），
      所以「别存这张」完全可能赶在落库之前 —— 这时靠忽略名单在入库前拦下来；
    * 也可能它已经落库了 —— 这时光拦没用，得真的删。

    只认"最近一张"，不搞"把这个人发过的都删了"：后者太危险（一句手滑清空一批收藏）。
    """
    digest = state.ignore_latest_image(conv)
    if not digest:
        return "", False
    lib = await get_library()
    removed = await asyncio.to_thread(lib.remove, digest)
    if removed:
        logger.info("按指令删掉了已入库的图 hash=%s conv=%s", digest[:8], conv)
    return digest, removed


async def forget_by_hash(conv: str, digest: str) -> bool:
    """按哈希点名不要（供 Web 控制台 / 扩展指令用）。"""
    state.ignore_image(conv, digest)
    lib = await get_library()
    return await asyncio.to_thread(lib.remove, digest)


async def download_image(seg: dict[str, Any]) -> bytes:
    """下载一个图片段的字节。**公开入口**，给"要用这张图做别的事"的调用方用。

    以前只有私有的 `_download(url)`，于是别的模块要用就得 `stickers._download`
    （`__init__._load_first_image` 就是这么写的，还带了个 `# noqa: SLF001`）。
    这里把"挑地址 + 下载 + 丢线程"三步收进一个函数：链接过期、接口不给地址
    都是常态，失败返回 `b""`，调用方自己决定要不要试下一张。
    """
    source = image_source(seg)
    if not source:
        return b""
    try:
        data = await asyncio.to_thread(_download, source)
    except Exception:  # noqa: BLE001 - 图片链接过期是常态
        logger.info("下载图片失败（链接可能已过期）")
        return b""
    return data or b""


def image_source(seg: dict[str, Any]) -> str:
    """挑一个可下载的地址：优先 http(s)，退回 file://。"""
    url = str(seg.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        return url
    raw = str(seg.get("file") or "").strip()
    if raw.startswith("file://"):
        return raw
    if raw.startswith(("http://", "https://")):
        return raw
    return ""


async def store_from_message(
    *,
    conv: str,
    conv_label: str,
    uid: int,
    name: str,
    is_master: bool,
    seg: dict[str, Any],
    context: str,
    persona: str,
    preloaded: bytes | None = None,
    file_sent: bool = False,
) -> dict[str, Any] | None:
    """完整入库流程：策略 → 限流 → 下载 → 精确判重 → 近似判重 → 打分 → 落库。

    `preloaded` 让调用方把已经下载好的字节递进来 —— 调用方为了「记住最近一张图」
    本来就要下载一次，没必要再拉一遍。

    `file_sent=True` 表示这张图是**以文件形式**发来的（不是 QQ 表情/图片），
    会被打一个权重折扣，见下面 `sticker_file_penalty`。
    """
    if not settings.get("sticker_enabled"):
        return None

    # 会话图片策略：命中「不存」时**在下载之前**就退出，连图都不拉。
    # 这是「不要保存这张图片」真正生效的地方 —— 不是靠模型答应，而是靠这里拦住。
    if not state.may_store(conv):
        logger.debug("会话图片策略为 %s，跳过入库 conv=%s", state.image_mode(conv), conv)
        return None

    lib = await get_library()
    if not lib._rate_ok(conv):  # noqa: SLF001 - 同模块内的私有约定
        return None

    if preloaded is not None:
        data = preloaded
    else:
        source = image_source(seg)
        if not source:
            logger.debug("图片段没有可用地址，跳过：%s", seg)
            return None
        try:
            data = await asyncio.to_thread(_download, source)
        except Exception:  # noqa: BLE001 - 图片链接过期很常见，不值得报错
            logger.info("下载图片失败（链接可能已过期）conv=%s", conv)
            return None

    if not data:
        return None

    # 单张豁免：主人点名过「这张别存」的图，即便策略还是正常也不入库。
    digest = hashlib.sha256(data).hexdigest()[:16]
    if state.is_ignored(conv, digest):
        logger.info("这张图被单独点名过不保存，跳过 conv=%s hash=%s", conv, digest[:8])
        return None

    size_kb = len(data) / 1024
    if size_kb > float(settings.get("sticker_max_kb")):
        return None

    sub_type = int(seg.get("sub_type", 0) or 0)

    # ---------------------------------------------------------- 近似判重
    # 先算感知哈希，再判断是不是"内容一样但文件不一样"。
    # 顺序有讲究：限流与大小过滤已经在前面做完了，到这里才付解码的代价。
    phash_value = ""
    if perceptual.available():
        try:
            phash_value = await asyncio.to_thread(perceptual.phash, data)
        except Exception:  # noqa: BLE001 - 算不出来就只做精确去重
            logger.info("感知哈希计算失败，本张只做精确去重 conv=%s", conv)

    dup_limit = int(settings.get("sticker_dup_distance"))
    if settings.get("sticker_near_dup"):
        near, near_distance = perceptual.find_near(
            lib.items, digest=digest, phash_value=phash_value, limit=dup_limit
        )
        if near is not None:
            # 不新增，但把"又见到它一次"记下来 —— 一张被反复转发的图，
            # 说明它是群里的常用表情，值得在挑选时更靠前。
            near["seen"] = int(near.get("seen", 1)) + 1
            near["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
            await asyncio.to_thread(lib.save)
            logger.info(
                "内容重复，未入库 conv=%s 命中=%s 距离=%d（新图 %d 字节 / 已有 %d 字节）",
                conv,
                str(near.get("hash"))[:8],
                near_distance,
                len(data),
                int(near.get("size", 0)),
            )
            return None

    # 只有模型支持视觉、且图不太大时，才把图片本体送进打分请求
    vision = bool(settings.get("sticker_vision")) and size_kb <= float(
        settings.get("sticker_vision_max_kb")
    )

    if settings.get("sticker_judge"):
        score, reason = await judge_image(
            conv_label=conv_label,
            sender=name,
            is_master=is_master,
            sub_type=sub_type,
            size_kb=size_kb,
            context=context,
            persona=persona,
            image_data=data if vision else None,
            file_sent=file_sent,
        )
    else:
        score = 0.75 if sub_type in STICKER_LIKE_SUB_TYPES else 0.35
        reason = "未启用模型打分"

    # ---------------------------------------------------------- 权重折扣
    # 以文件形式发来的图降权。**降的是分数本身，不只是留个记录** ——
    # 后面按 score 比阈值，所以分数降下去自然就更难入库、更难被挑中。
    weight = 1.0
    if file_sent:
        penalty = float(settings.get("sticker_file_penalty"))
        weight = max(0.0, 1.0 - penalty)
        raw_score = score
        score = max(0.0, score - penalty)
        reason = f"{reason}（以文件发送，{raw_score:.2f}→{score:.2f}）"
        logger.info(
            "以文件形式发来的图，降权 %.2f conv=%s 分数 %.2f→%.2f", penalty, conv, raw_score, score
        )

    if score < float(settings.get("sticker_min_score")):
        logger.debug("表情包得分不足 %.2f < 阈值，丢弃", score)
        return None

    async with lib._lock:  # noqa: SLF001
        return await asyncio.to_thread(
            lib.add,
            data,
            conv=conv,
            uid=uid,
            name=name,
            is_master=is_master,
            sub_type=sub_type,
            score=score,
            reason=reason,
            phash_value=phash_value,
            size_wh=await asyncio.to_thread(perceptual.size_of, data),
            weight=weight,
            file_sent=file_sent,
        )


# 机器人自己发出图片后，在聊天记录里留的标记。
#
# **刻意写成一句自解释的话**（而不是 `[表情包]` 这种符号）：
# 符号要额外在 `record_legend` 里教一遍，而模型本来就在猜；一句话不用教。
# 它需要知道的是"我做过这件事"，不是"这张图长什么样"——后者每张图多一次成本，
# 还会把记录撑长。图本身在库里，真要看得另有通道（会话图片缓存）。
SENT_IMAGE_TEXT = "（我自己发了一张表情包/图片）"


async def record_sent_image(bot: Any, conv: str, uid: str = "") -> None:
    """把自己刚发出去的图片/表情**记进聊天记录**。

    改造前这支路发完就结束，聊天记录里没有任何"我发过一张图"的痕迹 ——
    于是群里说「这张图是你自己发的」时，它只能回「我什么时候发的，一点印象都没有」。
    它不是说谎，是**对自己的行为没有记录可依**。

    把"自己做的事"和"别人做的事"放进同一条记录流，归属问题就自动消失了：
    渲染时这一行会带上 `（你）`（靠 `bot_uid`），模型一眼能看出是自己发的。
    """
    if not conv:
        return
    try:
        from . import chatlog  # 局部导入：避免 stickers ↔ chatlog 顶部互导
    except Exception:  # noqa: BLE001
        return
    bot_uid = str(getattr(bot, "self_id", "") or "")
    if not bot_uid:
        return
    await chatlog.append_message(
        conv,
        int(bot_uid),
        config.bot_name(),
        SENT_IMAGE_TEXT,
        is_bot=True,
        bot_uid=int(bot_uid),
    )
    logger.info("已把「自己发了张图」记进聊天记录 conv=%s（%s）", conv, SENT_IMAGE_TEXT)

def _segment_from_bytes(data: bytes) -> Any:
    """把图片字节包成 OneBot 图片段。

    用 base64 而不是 file:// 路径：NapCat 那边的 enableLocalFile2Url 默认关闭，
    传本地路径不一定认，base64 最省心。
    """
    import base64

    from nonebot.adapters.onebot.v11 import MessageSegment

    return MessageSegment.image("base64://" + base64.b64encode(data).decode("ascii"))


async def _load_item_bytes(lib: StickerLibrary, item: dict[str, Any]) -> bytes | None:
    path = lib.path_of(item)
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError:
        logger.warning("表情包文件缺失：%s", path)
        return None


async def pick_as_segment() -> Any:
    """随机取一张表情包（**不看语境**）。库为空或文件缺失时返回 None。

    这个入口保留给"语境不明"的场景。对话里要发图请走 `pick_for_context()` ——
    随机发图正是"图文无关"的来源。
    """
    lib = await get_library()
    item = await asyncio.to_thread(lib.pick)
    if item is None:
        return None
    data = await _load_item_bytes(lib, item)
    if data is None:
        return None
    return _segment_from_bytes(data)


# --------------------------------------------------------------------- 按语境挑图
_PICK_PROMPT = """你要在一段聊天里发一张表情包。下面是当前语境，以及 {n} 张候选图（已按顺序编号附在后面）。

【当前语境】
{context}

【候选图说明】
{catalog}

【怎么判断】
- 只有当某张图和**眼下这句话的情绪、话题**确实合得上，才选它；
- 图必须"接得上"正在说的事。**宁可这张都不发，也不要发一张跟话题无关的图** ——
  发错图比不发图糟糕得多，会让人觉得你在乱刷屏；
- 不要因为"这张挺好看""这张挺好笑"就选它，那不是理由；
- 只看画面内容是否贴题，别想太多。

只输出 JSON，不要任何多余文字：
{{"pick": 序号或 null, "reason": "一句话"}}

`pick` 填 null 表示这些图都不合适，这次不发。
"""


async def pick_for_context(
    context: str,
    *,
    conv: str = "",
    is_master: bool = False,
) -> tuple[Any | None, str]:
    """按当前语境挑一张贴题的表情包。返回 (图片段 或 None, 说明)。

    这是"绝对不要发图文无关的内容"的实现。跟老的随机取用有三点不同：

    1. **先本地抽样，再让模型定夺**。全库几十上百张不可能都附给模型；
       所以用 `lib.sample()` 取 `sticker_pick_candidates` 张（保持"少用的更容易进候选"），
       然后让模型看着**真实画面**挑；
    2. **模型有权说"都不合适"**。返回值 `pick: null` 就走"不发图"这条路径 ——
       这是关键：一套只能"必须挑一张"的机制，早晚会挑出无关的图；
    3. **失败与不确定都不发**。调模型出错、返回解析不了、序号越界，
       一律返回 None。**这个功能的失败方向必须是"不发图"**，不能是"随便发一张"。
    """
    if not settings.get("sticker_pick_by_context"):
        # 关掉语境挑选时退回随机 —— 但这条路径明确记录日志，便于事后归因
        logger.debug("未启用按语境挑图，退回随机取用 conv=%s", conv)
        return await pick_as_segment(), "未启用语境挑选"

    if not config.API_KEY:
        return None, "未配置 Key"

    lib = await get_library()
    want = int(settings.get("sticker_pick_candidates"))
    candidates = await asyncio.to_thread(lib.sample, want)
    if not candidates:
        return None, "库为空"

    # 给每张候选写一行说明。reason 是入库时模型给的画面点评，
    # 老条目可能没有，那就只报类型与尺寸 —— 反正真正判断靠的是随后附上的画面。
    catalog_lines: list[str] = []
    images: list[bytes] = []
    vision = bool(settings.get("sticker_vision"))
    max_kb = float(settings.get("sticker_vision_max_kb"))
    for idx, item in enumerate(candidates, start=1):
        reason = str(item.get("reason") or "").strip()
        label = str(item.get("sub_type_label") or "")
        catalog_lines.append(f"{idx}. {label}{'：' + reason if reason else ''}")
        if not vision:
            continue
        data = await _load_item_bytes(lib, item)
        if data is None:
            continue
        if len(data) / 1024 > max_kb:
            continue
        images.append(data)

    prompt = _PICK_PROMPT.replace("{n}", str(len(candidates))).replace(
        "{context}", _clip_context(context)
    ).replace("{catalog}", "\n".join(catalog_lines))

    try:
        resp = await asyncio.wait_for(
            _client.chat.completions.create(
                model=settings.get("model"),
                messages=[
                    {
                        "role": "user",
                        "content": content_with_images(prompt, images),
                    }
                ],
                response_format={"type": "json_object"},
                # 推理模型会先思考，max_tokens 给小了 content 会是空的
                max_tokens=800,
            ),
            timeout=config.TIMEOUT,
        )
        raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        payload = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001 - 挑图失败绝不能影响已经发出去的文字
        logger.info("按语境挑图失败，这次不发图 conv=%s", conv)
        return None, "挑选失败"

    pick = payload.get("pick")
    reason = str(payload.get("reason") or "")[:120]
    if pick is None:
        logger.info("模型判定没有贴题的图，不发 conv=%s：%s", conv, reason)
        return None, f"都不合适：{reason}"

    try:
        index = int(pick)
    except (TypeError, ValueError):
        logger.info("挑图返回的序号无法解析：%r conv=%s", pick, conv)
        return None, "序号无法解析"
    if not 1 <= index <= len(candidates):
        logger.info("挑图序号越界：%s（共 %d 张）conv=%s", index, len(candidates), conv)
        return None, "序号越界"

    chosen = candidates[index - 1]
    data = await _load_item_bytes(lib, chosen)
    if data is None:
        return None, "文件缺失"
    # 记一次使用，供"用得少的优先"与"最近用过别再发"参考
    chosen["uses"] = int(chosen.get("uses", 0)) + 1
    chosen["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
    await asyncio.to_thread(lib.save)
    logger.info("按语境选中第 %d 张（%s）conv=%s：%s", index, str(chosen.get("hash"))[:8], conv, reason)
    return _segment_from_bytes(data), reason


def _clip_context(context: str) -> str:
    """把语境压到模型看的那几行 —— 太长反而会让它忽略真正的"当前这句话"。"""
    text = "\n".join(line for line in str(context or "").splitlines() if line.strip())
    limit = int(settings.get("sticker_pick_context_chars"))
    if limit > 0 and len(text) > limit:
        # 保留尾部：最近说的才是"当前语境"
        return text[-limit:]
    return text
