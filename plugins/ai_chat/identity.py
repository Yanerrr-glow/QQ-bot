"""它改自己的**身份**：昵称、头像（可选：群名片）。

## 为什么单独一个模块

这两件事在 OneBot v11 标准里都存在，但实现方式不一样，而且**都不是对话内容**：

| 动作 | 接口 | 参数 | 备注 |
|---|---|---|---|
| 改头像 | `set_qq_avatar` | `file`: `base64://…` / `file://…` | **NapCat 扩展接口**，OneBot v11 标准里没有 |
| 改昵称 | `set_qq_profile` | `nickname`（还有 `personal_note`） | 标准接口 |
| 改群名片 | `set_group_card` | `group_id` / `user_id` / `card` | 只影响本群 |

拿验证过的做法打底：`_工具链/设置头像.py` 早就用独立 WS 客户端调过 `set_qq_avatar`，
返回 `{"status":"ok","retcode":0}`，并且能从 QQ 头像 CDN 拉回新图自证。
机器人进程里可以直接走 `bot.call_api(...)`，不必再自己连一条 WS。

## 安全边界（这是设计前提，不是"以后再加"）

改昵称/头像是**身份级动作**：它对**所有群**可见，而且
"分清自己说的话"正是靠 `名字 + bot_uid` 判定的（`chatlog._speaker()` 的兜底分支）。
所以：

* 只由**主人**触发（指令层用 `is_master` 拦，与 `/人设`、`/模型`、`/dsh` 同级）；
* **不暴露成模型工具** —— 它现在的工具集是 `web_search` / `web_fetch`，
  让模型自主改身份会带来"它把自己改名成什么了"这类不可追溯的变化。
  想要它自主改，应当先加"改动日志 + 只限主人"两道闸门。
"""

from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger("ai_chat.identity")

# 头像上限（KB）。QQ 侧原图会先按比例存、客户端再裁圆，所以不必太大；
# 但太小会糊。1024 是"手机截图直接发过来也不用压"的量级。
AVATAR_MAX_KB = 1024
# 昵称长度上限（QQ 侧实际限制约 24 字节，这里按字符收得更保守，避免被接口拒）
NAME_MAX_CHARS = 24

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_GIF87 = b"GIF87a"
_GIF89 = b"GIF89a"
_WEBP = b"RIFF"
# QQ 头像支持 png/jpg/gif；webp 在部分客户端不认，所以不建议但也不硬拦


class IdentityError(Exception):
    """改身份失败。`message` 是给主人看的人话。"""


def unwrap(resp: Any) -> dict:
    """把接口返回摊平成"能直接取字段"的 dict。**两种形状都要认**。

    本项目两条路都在用：

    * 插件里走 `bot.call_api()` —— NoneBot2 已经把响应信封剥掉了，拿到的是 `data` 的内容；
    * `_工具链/设置头像.py` 直接连 WS 调 —— 拿到的是完整信封
      `{"status": "ok", "retcode": 0, "data": {...}}`。

    取字段前统一摊平，免得同一个接口因为"从哪调的"而解析出两种结果。
    """
    if not isinstance(resp, dict):
        return {}
    out = dict(resp)
    inner = out.pop("data", None)
    if isinstance(inner, dict):
        merged = dict(inner)
        # 信封里的 status / retcode 优先保留在结果里（给调用方判成败用）
        merged.update(out)
        return merged
    return out


async def fetch_self_id(bot) -> int:
    """问接口"我自己是谁"。拿不到就返回 0。

    为什么不能从会话键推：会话键里只有群号和**说话人**的号，没有机器人自己的号。
    """
    try:
        info = unwrap(await bot.call_api("get_login_info"))
    except Exception:  # noqa: BLE001
        logger.exception("调 get_login_info 失败")
        return 0
    try:
        return int(info.get("user_id") or 0)
    except (TypeError, ValueError):
        return 0


def sniff_image(data: bytes) -> str:
    """按**文件签名**认图片类型（不看扩展名、不看 Content-Type）。

    为什么要自己认：图片可能来自群消息的 base64 段、也可能来自引用消息，
    拿到的只有字节。签名是最可靠的判据，也顺带挡住"把非图片塞给接口"。
    """
    if len(data) < 12:
        return ""
    if data.startswith(_PNG):
        return "png"
    if data.startswith(_JPEG):
        return "jpg"
    if data.startswith(_GIF87) or data.startswith(_GIF89):
        return "gif"
    if data.startswith(_WEBP) and data[8:12] == b"WEBP":
        return "webp"
    return ""


async def set_avatar(bot, data: bytes) -> str:
    """把自己的 QQ 头像换成 `data`。返回一句人话结果。失败抛 `IdentityError`。"""
    if not data:
        raise IdentityError("没拿到图片字节")
    kind = sniff_image(data)
    if not kind:
        raise IdentityError("这不像是图片（认不出 png/jpg/gif/webp 的签名）")
    size_kb = len(data) / 1024
    if size_kb > AVATAR_MAX_KB:
        raise IdentityError("图太大了（%.0f KB，上限 %d KB）" % (size_kb, AVATAR_MAX_KB))
    encoded = base64.b64encode(data).decode("ascii")
    try:
        resp = await bot.call_api("set_qq_avatar", file=f"base64://{encoded}")
    except Exception as exc:  # noqa: BLE001 - 接口不存在/网络错都算失败
        raise IdentityError("调 set_qq_avatar 失败：%s" % type(exc).__name__) from exc
    resp = unwrap(resp)
    status = str(resp.get("status") or "")
    retcode = resp.get("retcode")
    # `or` 而不是 `and`：判定失败的**两种信号各自独立**都算数。
    # 用 `and` 时 `{"status":"failed"}`（没有 retcode 的那种）会被当成成功 ——
    # 那正是"嘴上说改了、其实没改"的入口。
    # 走 NoneBot2 的 `call_api()` 时失败本来就会抛 `ActionFailed`（被上面接住），
    # 这一段是给"直接连 WS 调"那条路兜底的（那时拿到的是原始信封）。
    if status == "failed" or retcode not in (0, None):
        raise IdentityError("接口拒绝了：%s" % str(resp)[:120])
    logger.info("头像已更换：%s，%.0f KB", kind, size_kb)
    return "头像换了（%s，%.0f KB）。QQ 客户端有缓存，可能过一会儿才刷新。" % (kind, size_kb)


def clean_name(name: str) -> str:
    """清理昵称：压空白、去换行与零宽字符、限长。空则抛。"""
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", str(name or ""))
    text = " ".join(text.split())
    if not text:
        raise IdentityError("名字不能是空的")
    if len(text) > NAME_MAX_CHARS:
        raise IdentityError("名字太长了（%d 字，上限 %d）" % (len(text), NAME_MAX_CHARS))
    return text


async def set_nickname(bot, name: str) -> str:
    """改**QQ 昵称**（全局）。`name` 会先被 `clean_name` 清理。"""
    text = clean_name(name)
    try:
        resp = await bot.call_api("set_qq_profile", nickname=text)
    except Exception as exc:  # noqa: BLE001
        raise IdentityError("调 set_qq_profile 失败：%s" % type(exc).__name__) from exc
    resp = unwrap(resp)
    status = str(resp.get("status") or "")
    retcode = resp.get("retcode")
    if status == "failed" or retcode not in (0, None):
        raise IdentityError("接口拒绝了：%s" % str(resp)[:120])
    logger.info("昵称已改为：%s", text)
    return text


def save_avatar(data: bytes, kind: str) -> Path:
    """把换上去的头像另存一份到 `data/`，作为**可追溯的凭证**。

    为什么留档：改头像是不可撤销的（旧图没有备份），而且 QQ 客户端有缓存 ——
    出问题时"刚才到底换上去了什么"必须能查。文件名带时间戳，保留最近若干份。

    顺带解决一个部署问题：容器重建会抹掉镜像里的文件，但 `data/` 是卷。
    所以这张图在重建后仍然在，可以重新贴回去（`/头像` 再发一次也行）。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = config.LOG_DIR / f"avatar_{stamp}.{kind}"
    try:
        path.write_bytes(data)
    except OSError as exc:  # noqa: BLE001 - 存不下不该让换头像失败
        logger.warning("头像留档失败（已经换上了）：%s", exc)
        return path
    try:
        keep = sorted(config.LOG_DIR.glob("avatar_*.*"))[:-5]
        for old in keep:
            old.unlink(missing_ok=True)
    except OSError:  # noqa: BLE001
        pass
    return path


def group_id_of(conv: str) -> int | None:
    """从会话键反解群号（`g<群号>` → 群号）。私聊返回 None。

    反解而不是另存一份：`conversation_id()` 是这套键的唯一定义处，
    群名片只对群聊有意义，而 `ctx` 里只有 `conv`。
    """
    text = str(conv or "")
    if text[:1] == "g" and text[1:].isdigit():
        return int(text[1:])
    return None


async def apply_nickname(bot, name: str) -> str:
    """改 QQ 昵称，**并把它写进设置** —— 两件事必须一起做。

    只改 QQ 侧、不改设置，会出现"名字对不上落盘记录"：聊天记录里它仍以旧名出现，
    而 `chatlog._speaker()` 判"这句是不是我自己说的"要拿 `config.bot_name()` 比。
    所以这里先改 QQ、成功后再落设置；**QQ 侧失败就完全不落**，
    免得设置说它叫新名字、QQ 上其实还叫旧名字。
    """
    from . import settings as _s

    text = await set_nickname(bot, name)
    try:
        _s.set_value("bot_name", text)
    except Exception:  # noqa: BLE001 - 设置写失败只告警：QQ 那边已经改了
        logger.exception("昵称已改但写入设置失败（聊天记录里的名字会滞后）")
    return text


async def apply_avatar(bot, data: bytes) -> str:
    """换头像 + 留档。返回一句人话。"""
    kind = sniff_image(data)
    note = await set_avatar(bot, data)
    path = save_avatar(data, kind or "bin")
    logger.info("头像留档：%s", path.name)
    return f"{note}（留档 {path.name}）"


async def set_group_card(bot, group_id: int, user_id: int, card: str) -> str:
    """改**群名片**（只影响一个群）。空串 = 撤回名片、显示原昵称。"""
    text = " ".join(re.sub(
        r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", str(card or "")).split())
    if len(text) > NAME_MAX_CHARS:
        raise IdentityError("群名片太长了（%d 字，上限 %d）" % (len(text), NAME_MAX_CHARS))
    try:
        resp = await bot.call_api("set_group_card", group_id=group_id,
                                  user_id=user_id, card=text)
    except Exception as exc:  # noqa: BLE001
        raise IdentityError("调 set_group_card 失败：%s" % type(exc).__name__) from exc
    resp = unwrap(resp)
    status = str(resp.get("status") or "")
    retcode = resp.get("retcode")
    if status == "failed" or retcode not in (0, None):
        raise IdentityError("接口拒绝了：%s" % str(resp)[:120])
    logger.info("群名片已改为：%r（群 %s）", text, group_id)
    return text
