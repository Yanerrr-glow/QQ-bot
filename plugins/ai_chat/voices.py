"""语音包（音色）库：一份 JSON 管「用哪个训练权重 + 哪段参考音频」。

为什么单独一个文件而不是塞进 `settings.json`：
`settings.json` 的形状是 `key -> 标量`，由 `settings._SPECS` 驱动表单；
语音包是**列表 + 每项多个字段**，硬塞进去会把"加一个参数只要加一行"的约定破坏掉。
所以标量（开关/上限）留在 `settings.py`，结构化数据放这里。

存储：`data/runtime/tts_voices.json`

```json
{
  "active": "default",
  "items": [
    {"id": "default", "name": "默认（.env 参考音色）",
     "gpt_weights": "", "sovits_weights": "",
     "ref_audio": "E:\\...\\ref_zh.wav",
     "prompt_text": "……", "prompt_lang": "zh", "text_lang": "zh"}
  ]
}
```

权重留空 = 用引擎当前已加载的权重（不切换）。填了就必须是**TTS 服务端（Windows）**
能读到的绝对路径；bot 容器里没有这些文件，只负责把路径传给 relay。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from . import config

logger = logging.getLogger("ai_chat.voices")

_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_FIELDS = ("id", "name", "gpt_weights", "sovits_weights",
           "ref_audio", "prompt_text", "prompt_lang", "text_lang")
_MAX = {"name": 40, "gpt_weights": 400, "sovits_weights": 400,
        "ref_audio": 400, "prompt_text": 400}


def _path() -> Path:
    """每次现算：`config.LOG_DIR` 在测试里会被改写（离线桩用临时目录）。"""
    return Path(config.LOG_DIR) / "tts_voices.json"


def _defaults() -> dict:
    return {
        "id": "default",
        "name": "默认（.env 参考音色）",
        "gpt_weights": "",
        "sovits_weights": "",
        "ref_audio": config.TTS_REF_AUDIO,
        "prompt_text": config.TTS_PROMPT_TEXT,
        "prompt_lang": config.TTS_PROMPT_LANG,
        "text_lang": config.TTS_TEXT_LANG,
    }


def _clean(raw: dict, *, fallback_id: str = "") -> dict:
    """把一份输入夹成合法结构；**不抛异常**，坏字段退回默认。"""
    base = _defaults()
    out = {}
    for key in _FIELDS:
        value = raw.get(key, base.get(key, ""))
        value = "" if value is None else str(value).strip()
        limit = _MAX.get(key)
        if limit:
            value = value[:limit]
        out[key] = value

    if not out["name"]:
        out["name"] = "未命名语音"
    if not _ID_RE.match(out["id"] or ""):
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", out["name"])[:24].strip("-")
        out["id"] = (_ID_RE.match(slug) and slug) or fallback_id or f"voice{int(time.time()) % 100000}"
    for key in ("prompt_lang", "text_lang"):
        if out[key] not in {"zh", "en", "ja", "ko", "yue", "auto"}:
            out[key] = "zh"
    return out


def normalize(raw: dict) -> dict:
    """把一份表单输入夹成合法结构（不保存）。供控制台「试听未保存配置」用。"""
    return _clean(raw if isinstance(raw, dict) else {})


def load() -> dict:
    """读盘；文件不存在/损坏时返回播种默认（**不写盘**，写盘只发生在保存时）。"""
    path = _path()
    if not path.exists():
        return {"active": "default", "items": [_defaults()]}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("语音包文件损坏，按默认处理：%s（%s）", path, exc)
        return {"active": "default", "items": [_defaults()]}
    if not isinstance(raw, dict):
        return {"active": "default", "items": [_defaults()]}
    items = []
    for item in raw.get("items") or []:
        if isinstance(item, dict):
            items.append(_clean(item))
    if not items:
        items = [_defaults()]
    active = str(raw.get("active") or items[0]["id"])
    if active not in {x["id"] for x in items}:
        active = items[0]["id"]
    return {"active": active, "items": items}


def _save(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def snapshot() -> dict:
    data = load()
    return dict(data, path=str(_path()))


def active_id() -> str:
    return load()["active"]


def resolve(profile_id: str | None = None) -> dict:
    """取某个语音包；`None` = 当前激活的。找不到就退回默认。"""
    data = load()
    want = (profile_id or data["active"] or "").strip()
    for item in data["items"]:
        if item["id"] == want:
            return item
    return data["items"][0]


def upsert(raw: dict) -> tuple[bool, str, dict]:
    """新增或按 id 覆盖。返回 (ok, 说明, 快照)。"""
    if not isinstance(raw, dict):
        return False, "内容必须是对象", snapshot()
    with _LOCK:
        data = load()
        item = _clean(raw)
        ids = [x["id"] for x in data["items"]]
        replaced = item["id"] in ids
        data["items"] = [item if x["id"] == item["id"] else x for x in data["items"]]
        if not replaced:
            data["items"].append(item)
        if not data.get("active"):
            data["active"] = item["id"]
        _save(data)
    logger.info("语音包已%s：%s（%s）", "更新" if replaced else "新增", item["name"], item["id"])
    return True, ("已更新 " if replaced else "已新增 ") + item["name"], snapshot()


def delete(profile_id: str) -> tuple[bool, str, dict]:
    with _LOCK:
        data = load()
        if len(data["items"]) <= 1:
            return False, "至少要保留一个语音包", snapshot()
        remain = [x for x in data["items"] if x["id"] != profile_id]
        if len(remain) == len(data["items"]):
            return False, f"没有这个语音包：{profile_id}", snapshot()
        data["items"] = remain
        if data["active"] == profile_id:
            data["active"] = remain[0]["id"]
        _save(data)
    logger.info("语音包已删除：%s", profile_id)
    return True, f"已删除 {profile_id}", snapshot()


def set_active(profile_id: str) -> tuple[bool, str, dict]:
    with _LOCK:
        data = load()
        if not any(x["id"] == profile_id for x in data["items"]):
            return False, f"没有这个语音包：{profile_id}", snapshot()
        data["active"] = profile_id
        _save(data)
    logger.info("当前语音包：%s", profile_id)
    return True, f"当前语音包：{profile_id}", snapshot()
