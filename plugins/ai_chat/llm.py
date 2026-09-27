"""模型档案（profiles）：一套「用哪家 API、哪个模型」的配置，可在控制台切换。

## 为什么需要它

改造前，「用哪个模型」只有 `settings.model` 一个字符串，而**接入点是写死在 `.env` 里的**
（`DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`）—— 想换一家 API（中转站、自建服务、
别的厂商的 OpenAI 兼容端点）必须改 `.env` 再重启；更麻烦的是**十个模块各自在 import 时
建了一个 client**（`AsyncOpenAI(...)`），所以运行期根本换不动。

现在：档案存在 `data/models.json`（卷内），`client()` 按「当前选中的档案」建并缓存，
调用方只写 `await llm.chat(messages=..., **kw)`。

## 档案长什么样

```json
{
  "active": "deepseek",
  "profiles": [
    {"id": "deepseek", "label": "DeepSeek 官方",
     "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY",
     "model": "deepseek-flash", "vision": true, "tools": true, "logprobs": false},
    {"id": "local", "label": "本地 Ollama（示例，按需改）",
     "base_url": "http://127.0.0.1:11434/v1", "api_key": "",
     "model": "qwen2.5:7b", "vision": false, "tools": false, "logprobs": false}
  ]
}
```

* **密钥优先从环境变量 / `.env` 取**（`api_key_env`），不写进这个 json ——
  那个文件比 `.env` 更容易被顺手同步或备份出去。不校验密钥的本地端点直接留空即可。
* **能力标记决定「哪些功能会用它」**：`vision` 决定图片进不进 prompt，
  `tools` 决定要不要带工具表，`logprobs` 决定人设评估台能不能拿它当裁判。
  它们不是装饰：一个不支持 tools 的端点收到 `tools=` 会直接报 400。

## 兼容：老部署一个字都不用改

首次启动会按 `.env` 里的 `DEEPSEEK_*` **播种**一个 `deepseek` 档案（幂等，已存在就不动），
所以升级后行为与改造前一致。`settings.model` 仍然有效，含义变成
**「当前档案的模型名覆盖」**（留空 = 用档案里写好的那个）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from nonebot import get_driver

from . import config, settings

logger = logging.getLogger("ai_chat.llm")

FILE_NAME = "models.json"
SEED_ID = "deepseek"

# 缺字段时按这套补齐，省得每个调用点都写 .get(..., 默认值)
_DEFAULTS: dict[str, Any] = {
    "label": "",
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "api_key_env": "",
    "model": "deepseek-flash",
    "vision": True,
    "tools": True,
    "logprobs": False,
}


def _data_dir() -> Path:
    """与聊天记录同一个目录（`AI_CHAT_LOG_DIR`，默认 `data`）。"""
    log_dir = getattr(config, "LOG_DIR", None)
    if log_dir:
        return Path(log_dir)
    raw = os.environ.get("AI_CHAT_LOG_DIR", "") or "data"
    path = Path(raw)
    if not path.is_absolute():
        path = Path(config.__file__).resolve().parent.parent.parent / path
    return path


def path() -> Path:
    return _data_dir() / FILE_NAME


# --------------------------------------------------------------------- 读
def _dotenv_value(name: str) -> str:
    """从 `.env` 里取一个变量。

    为什么不能只看 `os.environ`：NoneBot 读 `.env` 是把它喂给 pydantic 配置对象，
    **不会**写进进程环境变量 —— 所以 `api_key_env` 指向的名字得自己去文件里找。
    """
    if not name:
        return ""
    for base in (Path.cwd(), Path(config.__file__).resolve().parent.parent.parent):
        env_file = base / ".env"
        try:
            if not env_file.is_file():
                continue
            for raw in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                if key.strip() == name:
                    return val.strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def _normalize(item: dict[str, Any], *, fallback_id: str) -> dict[str, Any]:
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in (item or {}).items() if v is not None})
    out["id"] = str(out.get("id") or fallback_id).strip() or fallback_id
    out["label"] = str(out.get("label") or out["id"])
    out["base_url"] = str(out.get("base_url") or _DEFAULTS["base_url"]).rstrip("/")
    out["model"] = str(out.get("model") or _DEFAULTS["model"])
    for flag in ("vision", "tools", "logprobs"):
        out[flag] = bool(out.get(flag))
    return out


def seeded() -> dict[str, Any]:
    """按 `.env` 播种的那个档案（就是改造前的行为）。"""
    return _normalize({
        "id": SEED_ID,
        "label": "DeepSeek 官方（按 .env 播种）",
        "base_url": getattr(config, "BASE_URL", "") or _DEFAULTS["base_url"],
        # **密钥只留变量名，不把明文抄进来** —— 这个文件比 `.env` 更容易被顺手同步或
        # 备份出去。`api_key()` 会按 `api_key_env` 去 os.environ / `.env` 里取，
        # 所以行为与"直接写在这里"完全一样（这也正是当初差点写错的地方：
        # 播种时把 `config.API_KEY` 抄进 json，一保存就把密钥落盘了）。
        "api_key": "",
        "api_key_env": "DEEPSEEK_API_KEY",
        # `.env` 没写就用 Spec 的默认值（deepseek-flash）
        "model": getattr(config, "MODEL", "") or _DEFAULTS["model"],
        # DeepSeek：能读图、能工具、能给 logprobs —— 注意最后这条指的是
        # `deepseek-chat` 那个兼容别名（推理模型 flash/v4-pro 的 top_logprobs 是空的）。
        # 裁判固定走 `eval_judge_model`（默认 deepseek-chat），所以这个档案要标 True，
        # 否则聊天换成本地模型后，评估台会找不到任何一个能当裁判的档案。
        "vision": True, "tools": True, "logprobs": True,
    }, fallback_id=SEED_ID)


def load() -> dict[str, Any]:
    """读注册表：`{"active": id, "profiles": [...]}`。

    **读不出来就退回"只有播种档案"** —— 这个文件坏了不该让机器人起不来。
    真坏了会把原件改名留证（与 `summaries` / `settings` 同一套处理）。
    """
    p = path()
    data: Any = None
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            logger.warning("模型档案读不出来（%s），退回 .env 播种项：%s", type(exc).__name__, p)
            try:
                p.replace(p.with_suffix(".json.corrupt"))
            except OSError:
                pass
    if not isinstance(data, dict):
        data = {}
    items = data.get("profiles")
    if not isinstance(items, list) or not items:
        # 播种：老部署（没这个文件）与文件坏掉都走这里
        return {"active": SEED_ID, "profiles": [seeded()]}
    out = [_normalize(it, fallback_id=f"p{i + 1}") for i, it in enumerate(items) if isinstance(it, dict)]
    if not out:
        return {"active": SEED_ID, "profiles": [seeded()]}
    # 播种项不在里面就补上：不然"回不到默认"就没路了
    if all(p0["id"] != SEED_ID for p0 in out):
        out.insert(0, seeded())
    active = str(data.get("active") or "").strip()
    if active not in {p0["id"] for p0 in out}:
        active = out[0]["id"]
    return {"active": active, "profiles": out}


def save(reg: dict[str, Any]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    for item in reg.get("profiles") or []:
        if str(item.get("api_key") or "").strip():
            # 支持直接写密钥（有人就一个端点、懒得配 .env），但**每次都提醒一句** ——
            # 这个文件不像 .env 那样有"别提交"的肌肉记忆，很容易被顺手同步出去。
            logger.warning("模型档案 %s 把密钥直接存在 %s 里了；建议改用 api_key_env 指向 .env",
                           item.get("id"), p)
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def profiles() -> list[dict[str, Any]]:
    return load()["profiles"]


def active_id() -> str:
    return load()["active"]


def get(pid: str) -> dict[str, Any] | None:
    for item in profiles():
        if item["id"] == pid:
            return item
    return None


def active() -> dict[str, Any]:
    return get(active_id()) or seeded()


def set_active(pid: str) -> tuple[bool, str]:
    """切当前档案。**顺带清掉 `settings.model` 的模型名覆盖**（若设过）。

    为什么清：覆盖是**针对某一家接口**设的（"这个端点上我要用哪个名字"）。
    留着它换档案，等于换了接口却还用着上一个接口的模型名 —— 表现就是
    "切过去了但模型没变"，正是这个功能要解决的问题本身。
    调用方应当在回执里说明清掉了什么（控制台与 `/模型` 都这么做）。
    """
    reg = load()
    if pid not in {p0["id"] for p0 in reg["profiles"]}:
        return False, f"没有这个档案：{pid}"
    reg["active"] = pid
    save(reg)
    _CLIENTS.clear()                      # 换了档案，client 必须重建
    if str(settings.get("model") or "").strip():
        settings.set_value("model", "")
    return True, pid


def upsert(item: dict[str, Any]) -> tuple[bool, str]:
    """新增/覆盖一个档案（控制台编辑 JSON 时用）。"""
    reg = load()
    cand = _normalize(item, fallback_id=str(item.get("id") or "profile"))
    bad = [ch for ch in cand["id"] if not (ch.isalnum() or ch in "_.-")]
    if bad:
        return False, "档案 id 只能用字母数字与 _ . -（其它字符会在控制台上引发转义问题）"
    for i, old in enumerate(reg["profiles"]):
        if old["id"] == cand["id"]:
            reg["profiles"][i] = cand
            break
    else:
        reg["profiles"].append(cand)
    save(reg)
    _CLIENTS.clear()
    return True, cand["id"]


def del_profile(pid: str) -> tuple[bool, str]:
    """删掉一个档案。**播种档案不让删** —— 删了就没有"回到默认"的路了；当前用的不让删。"""
    if pid == SEED_ID:
        return False, f"「{SEED_ID}」是按 .env 播种的档案，不能删（它是回默认的路）"
    reg = load()
    keep = [p for p in reg["profiles"] if p["id"] != pid]
    if len(keep) == len(reg["profiles"]):
        return False, f"没有这个档案：{pid}"
    if reg["active"] == pid:
        return False, f"「{pid}」正在用，先切到别的档案再删"
    reg["profiles"] = keep
    save(reg)
    _CLIENTS.clear()
    return True, pid


# --------------------------------------------------------------------- 选中项
def model_name(profile: dict[str, Any] | None = None) -> str:
    """这次调用实际用哪个模型名。

    `settings.model` 是**覆盖**（留空就用档案里的）—— 保留它是为了兼容：
    改造前所有调用点写的都是 `settings.get("model")`，老 `data/settings.json` 里也存着它。
    """
    item = profile or active()
    override = str(settings.get("model") or "").strip()
    return override or str(item["model"])


def caps(profile: dict[str, Any] | None = None) -> dict[str, bool]:
    item = profile or active()
    return {k: bool(item.get(k)) for k in ("vision", "tools", "logprobs")}


# --------------------------------------------------------------------- 客户端
_CLIENTS: dict[tuple[str, str], AsyncOpenAI] = {}


def _cfg_value(name: str) -> str:
    """NoneBot 配置对象里的同名字段 —— **第三条来源**。

    为什么非要它：`.env` 是喂给 NoneBot 的 pydantic 配置对象的，**不一定会写进
    `os.environ`**（见 `_dotenv_value`）；而有的部署干脆是
    `nonebot.init(deepseek_api_key="sk-...")` 直接传的，那种连 `.env` 都没有。
    `config.API_KEY` 读的就是这个对象，所以这里补上它，等于**把改造前那条链完整保留**。
    """
    if not name:
        return ""
    try:
        return str(getattr(get_driver().config, name.lower(), "") or "")
    except Exception:  # noqa: BLE001 - 没初始化驱动时也不该炸
        return ""


def api_key(profile: dict[str, Any] | None = None) -> str:
    item = profile or active()
    direct = str(item.get("api_key") or "").strip()
    if direct:
        return direct
    env_name = str(item.get("api_key_env") or "").strip()
    return (os.environ.get(env_name, "")
            or _dotenv_value(env_name)
            or _cfg_value(env_name))


def client(profile: dict[str, Any] | None = None) -> AsyncOpenAI:
    """按档案建 client，**按（base_url, key）缓存** —— 控制台换了档案就自动重建。

    为什么要缓存：每个档案一个 client 就够，而群聊里每条消息都可能调；每次 new 一个
    连接池既慢又容易泄漏。key 变了自然落到新缓存键上，不用手工失效。
    """
    item = profile or active()
    key = api_key(item)
    cache_key = (str(item["base_url"]), key)
    got = _CLIENTS.get(cache_key)
    if got is None:
        got = AsyncOpenAI(api_key=key or "sk-not-configured", base_url=str(item["base_url"]))
        _CLIENTS[cache_key] = got
    return got


async def chat(messages: list[dict[str, Any]], *, profile: dict[str, Any] | None = None,
               model: str | None = None, **kw: Any) -> Any:
    """统一的对话入口：**调用方不再自己建 client，也不再自己写模型名。**

    返回原始响应对象（调用点一直在用 `resp.choices[0].message.content`）。
    工具 / 读图按档案的能力标记自动取舍 —— 不支持却传了参数，很多端点会直接 400。
    """
    item = profile or active()
    use = model or model_name(item)
    c = caps(item)
    if kw.get("tools") and not c["tools"]:
        logger.info("档案 %s 不支持 tools，这次不带工具表", item["id"])
        kw.pop("tools", None)
    resp = await client(item).chat.completions.create(model=use, messages=messages, **kw)  # type: ignore[arg-type]
    return resp


def mask_key(key: str) -> str:
    """密钥的显示形式。**任何界面都只能用这个** —— 明文密钥不该离开 .env / 档案文件。"""
    key = str(key or "")
    if not key:
        return ""
    if len(key) <= 10:
        return "已配置"
    return f"{key[:4]}…{key[-4:]}"


def snapshot() -> dict[str, Any]:
    """给控制台/自检看的档案快照。**不含密钥明文**（只有掩码）与掩码后的存在性。"""
    reg = load()
    override = str(settings.get("model") or "").strip()
    items = []
    for item in reg["profiles"]:
        key = api_key(item)
        items.append({
            "id": item["id"], "label": item["label"], "base_url": item["base_url"],
            "model": item["model"],
            # 生效的模型名 = 覆盖（若有）否则档案里的
            "effective_model": model_name(item),
            "override": override if item["id"] == reg["active"] else "",
            "has_key": bool(key), "key_hint": mask_key(key),
            "key_env": item.get("api_key_env") or "",
            "vision": bool(item["vision"]), "tools": bool(item["tools"]),
            "logprobs": bool(item["logprobs"]),
            # 播种档案与"正在用的那个"都删不得（见 del_profile），界面据此决定要不要给按钮
            "removable": item["id"] != SEED_ID and item["id"] != reg["active"],
        })
    return {"active": reg["active"], "profiles": items, "override": override,
            "path": str(path())}


def _looks_local(base_url: str) -> bool:
    """本机端点通常不校验密钥，不该因为"没配 key"就被判成不可用。"""
    low = str(base_url or "").lower()
    return any(tok in low for tok in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "host.docker"))


async def probe(profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """验一次「这个档案到底通不通」。

    **先问 `/models`，再退回"发一句 max_tokens=1"**：不是所有 OpenAI 兼容端点都实现了
    `/models`（自建/中转常见），但能对话就够了。这也是控制台上那个「测一下」按钮与
    `上手自检` 共用的同一段逻辑 —— 两处各写一套必然会漂。

    不打印、不返回密钥；失败信息里只带异常类型与文本（不含 Authorization 头）。
    """
    item = profile or active()
    key = api_key(item)
    if not key and not _looks_local(str(item["base_url"])):
        return {"ok": False, "detail": "没配密钥（api_key 与 api_key_env 都是空的）", "models": []}
    c = client(item)
    models_err = ""
    try:
        page = await c.models.list()
        names = [str(getattr(m, "id", "") or "") for m in (getattr(page, "data", None) or [])]
        names = [n for n in names if n]
        return {"ok": True, "models": names,
                "detail": f"/models 通了，列到 {len(names)} 个模型"}
    except Exception as exc:  # noqa: BLE001 - 端点不支持 /models 是常态，接着试对话
        models_err = f"{type(exc).__name__}: {exc}"
    try:
        await c.chat.completions.create(
            model=model_name(item),
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
    except Exception as exc:  # noqa: BLE001 - 两条路都不通才算不通
        return {"ok": False, "models": [],
                "detail": f"/models 失败（{models_err}）；对话也失败（{type(exc).__name__}: {exc}）"}
    return {"ok": True, "models": [],
            "detail": f"对话接口通了（/models 不可用：{models_err}）"}


def editor_text() -> str:
    """控制台编辑框里那份 JSON 文本。

    **明文密钥不出现在这里**：已有 `api_key` 的档案只显示 `***`，保存时按 `***` 原样保留
    （见 `replace_all`）。这样"能改档案"不必以"把密钥贴到页面上"为代价。
    """
    reg = load()
    safe: dict[str, Any] = {"active": reg["active"], "profiles": []}
    for item in reg["profiles"]:
        it = dict(item)
        it["api_key"] = "***" if str(item.get("api_key") or "").strip() else ""
        safe["profiles"].append(it)
    return json.dumps(safe, ensure_ascii=False, indent=2)


def replace_all(data: dict[str, Any]) -> tuple[bool, str]:
    """整份替换档案表（控制台直接编辑 JSON 用）。

    与逐条 `upsert` 分开，是因为编辑框里看到的是**一整份**：整份读、整份写、
    整份报错，中间不留"改了一半"的状态。
    """
    items = data.get("profiles")
    if not isinstance(items, list) or not items:
        return False, "profiles 必须是非空数组"
    old = {p["id"]: p for p in profiles()}
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            return False, f"第 {i + 1} 个档案不是对象"
        it = dict(raw)
        if str(it.get("api_key") or "").strip() == "***":
            it["api_key"] = str(old.get(str(it.get("id") or ""), {}).get("api_key") or "")
        cand = _normalize(it, fallback_id=f"p{i + 1}")
        bad = [ch for ch in cand["id"] if not (ch.isalnum() or ch in "_.-")]
        if bad:
            return False, f"档案 id「{cand['id']}」只能用字母数字与 _ . -"
        out.append(cand)
    ids = [p0["id"] for p0 in out]
    if len(set(ids)) != len(ids):
        return False, "有重复的档案 id"
    if SEED_ID not in ids:
        out.insert(0, seeded())      # 播种档案必须留着：它是"回到 .env 默认"唯一的路
        ids.insert(0, SEED_ID)
    active = str(data.get("active") or "").strip() or ids[0]
    if active not in set(ids):
        return False, f"active 指向的档案不存在：{active}"
    save({"active": active, "profiles": out})
    _CLIENTS.clear()
    return True, active


# --------------------------------------------------------------------- 给人看
def render_status() -> str:
    reg = load()
    lines = [f"模型档案：{len(reg['profiles'])} 个，当前用 **{reg['active']}**"]
    for item in reg["profiles"]:
        mark = "← 当前" if item["id"] == reg["active"] else ""
        key = api_key(item)
        tags = "".join(
            name for name, flag in (("读图", item["vision"]), ("工具", item["tools"]),
                                    ("logprobs", item["logprobs"])) if flag)
        lines.append(
            f"· {item['id']}（{item['label']}）"
            f" base_url={item['base_url']} model={model_name(item)}"
            f" key={'已配' if key else '**没配**'} 能力={tags or '（无）'} {mark}")
    return "\n".join(lines)
