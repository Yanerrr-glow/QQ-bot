"""Web 控制台：调参数、管人设要求、翻记忆库、管图片策略、看表情包库。

挂在 NoneBot 自己的 FastAPI 实例上（driver.server_app），所以不需要另起进程。
前提是 driver 不是纯 websockets —— 见 .env 里的 DRIVER=~fastapi+~websockets。
若 driver 不支持，这里会打一条日志并跳过注册，机器人本身照常工作。

参数表由 settings.describe() 动态生成，加参数不用改这个文件。
人设要求 / 记忆 / 图片策略是改造后新增的三块 —— 它们跟参数的区别在于
**是运行时长出来的数据，不是配置**，所以各有各的读写接口，不塞进 settings。

标签页与后端能力的对应：

| 标签 | 后端 | 说明 |
|---|---|---|
| 参数 | `settings.describe()` | .env 可调项，改完即时生效 |
| 人格三层 | `persona.layers()` | **只读**：看三层、看自动改动日志、手动触发一次反思、撤回最近一次改动 |
| 记忆库 | `memory.all_facts()` | 看 / 搜 / 删 / 保护 / 手动补一条 |
| 图片策略 | `state.snapshot()` | 哪些会话被设成了不存图 |
"""

import asyncio
import hmac
import json
import logging
import time
from pathlib import Path

from nonebot import get_driver

from . import (
    clock,
    config,
    greetings,
    llm,
    memory,
    packs,
    persona,
    persona_eval,
    persona_examples,
    persona_iter,
    persona_relation,
    persona_vector,
    search,
    settings,
    state,
    stickers,
    tts,
    voices,
)

logger = logging.getLogger("ai_chat.webui")

_HTML = (Path(__file__).parent / "web" / "console.html").read_text(encoding="utf-8")


def auth_decision(path: str, prefix: str, token: str, given: str) -> tuple[bool, str]:
    """控制台认证的**唯一判据**（纯函数，便于离线验证）。

    返回 `(是否放行, 不能放行的原因)`。

    规则只有三条，能一句话说清：

    1. **不是控制台的路径一律放行** —— `app` 是 NoneBot 共享的 FastAPI 实例，
       还承载 OneBot 反向 WS（`/onebot/v11/ws`，NapCat 主动连入）。按前缀过滤是硬要求，
       拦错了就是"机器人掉线"。
    2. `AI_CHAT_WEBUI_AUTH_TOKEN` 没配 = 不认证（维持现状），但启动日志会醒目告警 ——
       此时唯一的安全边界是宿主侧把端口绑在回环上（见 `deploy/README.md` 的安全清单）。
    3. 配了就**必须**对上：`?token=`、`X-Auth-Token` 头、或已种下的 cookie，三者任一即可；
       比较用 `hmac.compare_digest`（恒定时间），失败只回一句"未认证"，不回显任何东西。

    ## 为什么不做"非回环就拒绝"

    容器里 `HOST` 必须是 `0.0.0.0`（否则端口映射不通），而经由
    `ssh -L 8080:127.0.0.1:8080` 进来的请求，在容器看来**源地址是 docker 网关**
    （不是 127.0.0.1）—— 按源地址判"是不是本机"会把正当的 SSH 隧道访问一起挡在门外。
    所以这道防线只能建在**凭证**上，不能建在 IP 上。
    """
    if not path.startswith(prefix):
        return True, ""
    if not token:
        return True, ""
    if given and hmac.compare_digest(str(given), str(token)):
        return True, ""
    return False, "未认证：在 URL 后带 ?token=…（或加 X-Auth-Token 头）"


def _register() -> bool:
    driver = get_driver()
    app = getattr(driver, "server_app", None)
    if app is None:
        logger.warning(
            "当前 driver 没有 server_app，Web 控制台未启用。"
            "把 .env 的 DRIVER 改成 ~fastapi+~websockets 后重启即可。"
        )
        return False

    try:
        from fastapi import Request
        from fastapi.responses import HTMLResponse, JSONResponse, Response
    except ImportError:
        logger.warning("未安装 fastapi，Web 控制台未启用（pip install fastapi uvicorn）")
        return False

    prefix = config.WEBUI_PREFIX.rstrip("/")
    token = config.WEBUI_AUTH_TOKEN
    cookie_name = "ai_chat_webui_token"

    @app.middleware("http")
    async def _auth_guard(request: Request, call_next):  # noqa: ANN001, ANN202
        """控制台认证（2026-09-28 补）。

        改造前控制台**没有任何认证**：只靠"端口绑回环 + SSH 隧道"这一层部署约定，
        一旦有人把 8080 映射到公网，读全部群聊/改全部配置/以机器人身份发言全都敞开。
        这里把约定升级成技术约束 —— 配了 token 就一律要凭证。
        """
        given = (
            request.query_params.get("token")
            or request.headers.get("x-auth-token")
            or request.cookies.get(cookie_name)
            or ""
        )
        ok, why = auth_decision(request.url.path, prefix, token, given)
        if not ok:
            logger.warning("控制台拒绝未认证请求：%s %s", request.method, request.url.path)
            return JSONResponse({"ok": False, "error": why}, status_code=401)
        resp = await call_next(request)
        # 首次用 `?token=` 打开页面时种一个 cookie，之后前端那些 fetch 不用再带参数
        _q = request.query_params.get("token") or ""
        if token and _q and hmac.compare_digest(_q, token):
            resp.set_cookie(cookie_name, token, httponly=True, samesite="strict")
        return resp

    async def _body(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - 空 body 也允许
            return {}
        return body if isinstance(body, dict) else {}

    @app.get(prefix + "/", response_class=HTMLResponse)
    async def _index() -> HTMLResponse:  # noqa: ANN202
        return HTMLResponse(_HTML.replace("__PREFIX__", prefix))

    @app.get(prefix + "/api/state")
    async def _state():  # noqa: ANN202
        lib = await stickers.get_library()
        return JSONResponse(
            {
                "settings": settings.describe(),
                # 模型档案：哪家接口 + 哪个模型（**只给掩码，不给密钥明文**）
                "models": llm.snapshot(),
                # 编辑框那份文本是单独一份（api_key 显示成 ***，保存时原样保留）
                "models_editor": llm.editor_text(),
                # 人设：三层结构。`catalog()`（旧槽位目录）已随分层一起删除 ——
                # 控制台上的人设页现在只展示三层 + 自动改动日志 + 手动触发反思。
                # 包化（2026-10）之后多了一维：**当前用的是哪个包**，
                # 以及可以切到哪些包。切换是这个页面上唯一会改变"它是谁"的动作。
                "persona": {
                    "stats": persona.stats(),
                    "forbidden": persona.forbidden_items(),
                    "layers": persona.layers(),
                    "changelog": persona.changelog(30),
                    "iter": persona_iter.stats(),
                    # 评估台现状：开关、素材/基线覆盖、各特质分数（只读）
                    "eval": persona_eval.status(),
                    "vector": {
                        "enabled": bool(settings.get("vector_enabled")),
                        "values": persona_vector.state().get("values") or {},
                        "axes": config.load_vector().get("axes") or [],
                        "modes": config.load_vector().get("modes") or [],
                        # 解释链（#0011）：操作者视角的最近几条 + 每个对象的三层值。
                        # 聊天注入走的是 explain(uid, conv)，那条路径严格按 uid+conv 过滤；
                        # 这里给面板看全量是刻意的——面板本来就是操作者的视角。
                        "recent": persona_vector.recent(5),
                        "effective_samples": persona_vector.effective_samples(5),
                    },
                    "relation": persona_relation.snapshot(),
                    "examples": persona_examples.stats(),
                    "pack": packs.stats(),
                    "packs": packs.list_packs(),
                },
                "memory": {
                    "facts": memory.all_facts(),
                    "events": memory.all_events(),
                    "profile": [
                        dict(info, key=key) for key, info in sorted(memory._db.profile.items())  # noqa: SLF001
                    ],
                    "stats": memory.stats(),
                },
                "image": state.snapshot(),
                # 语音：音色包（权重 + 参考音频）与运行开关。令牌**不回传**，只报是否配置。
                "voice": dict(
                    voices.snapshot(),
                    enabled=bool(settings.get("tts_enabled")),
                    endpoint=config.TTS_ENDPOINT,
                    token_configured=bool(config.TTS_TOKEN),
                ),
                "status": {
                    "stickers": lib.stats(),
                    "greet": greetings.status(),
                    "model": llm.model_name(),
                    "model_profile": llm.active_id(),
                    # 时间状态：宿主时钟漂了、或 NTP 没通，控制台上这里是唯一能看见的地方
                    "time": {
                        "now": clock.strftime("%Y-%m-%d %H:%M:%S"),
                        "raw": time.strftime("%Y-%m-%d %H:%M:%S"),
                        **clock.status(),
                    },
                    # 联网搜索状态：开着但没配端点是最常见的"看起来没用"的原因
                    "search": search.stats(),
                },
                "stickers_list": sorted(
                    lib.items, key=lambda x: float(x.get("ts", 0)), reverse=True
                ),
            }
        )

    # ---- 模型档案：看 / 切 / 测 / 整份改。接口是**新增的**，旧的 /api/settings 形状不变 ----
    @app.post(prefix + "/api/model/active")
    async def _model_active(request: Request):  # noqa: ANN202
        body = await _body(request)
        pid = str(body.get("id") or "").strip()
        had = str(settings.get("model") or "").strip()
        ok, why = llm.set_active(pid)
        note = f"当前用 {why}" + (f"（顺带清掉了模型名覆盖「{had}」）" if ok and had else "")
        return JSONResponse({"ok": ok, "detail": why if not ok else note, "state": llm.snapshot()})

    @app.post(prefix + "/api/model/test")
    async def _model_test(request: Request):  # noqa: ANN202
        body = await _body(request)
        pid = str(body.get("id") or "").strip()
        prof = llm.get(pid) if pid else None
        if pid and prof is None:
            return JSONResponse({"ok": False, "detail": f"没有这个档案：{pid}", "models": []})
        try:
            got = await llm.probe(prof)
        except Exception as exc:  # noqa: BLE001 - 探测本身不该把控制台打成 500
            got = {"ok": False, "detail": f"{type(exc).__name__}: {exc}", "models": []}
        got["profile"] = (prof or llm.active())["id"]
        return JSONResponse(got)

    @app.post(prefix + "/api/model/save")
    async def _model_save(request: Request):  # noqa: ANN202
        body = await _body(request)
        try:
            data = json.loads(str(body.get("json") or ""))
        except ValueError as exc:
            return JSONResponse({"ok": False, "detail": f"JSON 解析失败：{exc}"})
        if not isinstance(data, dict):
            return JSONResponse({"ok": False, "detail": "顶层必须是一个对象"})
        ok, why = llm.replace_all(data)
        return JSONResponse({"ok": ok, "detail": why if not ok else f"当前用 {why}",
                             "state": llm.snapshot() if ok else None})

    @app.post(prefix + "/api/model/delete")
    async def _model_delete(request: Request):  # noqa: ANN202
        body = await _body(request)
        ok, why = llm.del_profile(str(body.get("id") or "").strip())
        return JSONResponse({"ok": ok, "detail": why, "state": llm.snapshot() if ok else None})

    @app.post(prefix + "/api/settings")
    async def _update(request: Request):  # noqa: ANN202
        # 注意：本文件不能加 `from __future__ import annotations`。
        # 那会把注解变成字符串，FastAPI 就解析不出 Request 依赖，转而当成查询参数，
        # 结果所有 POST 都返回 422（missing query "request"）。这个坑踩过一次。
        body = await _body(request)
        if not body:
            return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
        applied: dict[str, object] = {}
        for key, value in body.items():
            try:
                applied[key] = settings.set_value(key, value)
            except KeyError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "applied": applied})

    @app.post(prefix + "/api/settings/reset")
    async def _reset():  # noqa: ANN202
        settings.reset()
        return JSONResponse({"ok": True})

    # ---- 语音包（音色）：列表在 data/runtime/tts_voices.json，和 settings 分开 ----
    @app.post(prefix + "/api/voice/save")
    async def _voice_save(request: Request):  # noqa: ANN202
        body = await _body(request)
        ok, why, snap = voices.upsert(body.get("profile") if isinstance(body.get("profile"), dict) else body)
        return JSONResponse({"ok": ok, "detail": why, "voice": snap})

    @app.post(prefix + "/api/voice/delete")
    async def _voice_delete(request: Request):  # noqa: ANN202
        body = await _body(request)
        ok, why, snap = voices.delete(str(body.get("id") or "").strip())
        return JSONResponse({"ok": ok, "detail": why, "voice": snap})

    @app.post(prefix + "/api/voice/active")
    async def _voice_active(request: Request):  # noqa: ANN202
        body = await _body(request)
        ok, why, snap = voices.set_active(str(body.get("id") or "").strip())
        return JSONResponse({"ok": ok, "detail": why, "voice": snap})

    @app.post(prefix + "/api/voice/test")
    async def _voice_test(request: Request):  # noqa: ANN202
        """合成一句试听，直接回 WAV 字节。**不发送到 QQ**。"""
        body = await _body(request)
        text = str(body.get("text") or "你好，这是当前语音包的试听。").strip()[:120]
        inline = body.get("profile")
        prof = (voices.normalize(inline) if isinstance(inline, dict)
                else voices.resolve(str(body.get("id") or "").strip() or None))
        try:
            result = await tts.synthesize(
                text,
                endpoint=config.TTS_ENDPOINT,
                token=config.TTS_TOKEN,
                ref_audio=prof["ref_audio"] or config.TTS_REF_AUDIO,
                prompt_text=prof["prompt_text"] or config.TTS_PROMPT_TEXT,
                prompt_lang=prof["prompt_lang"] or config.TTS_PROMPT_LANG,
                text_lang=prof["text_lang"] or config.TTS_TEXT_LANG,
                gpt_weights=prof["gpt_weights"],
                sovits_weights=prof["sovits_weights"],
                connect_timeout=config.TTS_CONNECT_TIMEOUT,
                total_timeout=config.TTS_TOTAL_TIMEOUT,
                max_chars=max(120, int(settings.get("tts_max_chars") or 150)),
                max_bytes=int(settings.get("tts_max_bytes") or config.TTS_MAX_BYTES),
                max_seconds=float(settings.get("tts_max_seconds") or config.TTS_MAX_SECONDS),
            )
        except tts.TTSError as exc:
            return JSONResponse({"ok": False, "kind": exc.kind, "detail": exc.detail[:300]},
                                status_code=502)
        return Response(content=result.data, media_type="audio/wav")

    # -------------------------------------------------------- 人格（三层，只读 + 收尾）
    # 人格分层之后，控制台上的「改人设」入口**整个删掉**。
    # 为什么连控制台也不留：边界要由代码保证 —— 底层人设与禁止事项"只有用户能改"，
    # 而"用户能改"的准确定义就是**直接编辑文件**（见 README 5.6.7.2）。
    # 控制台保留三件事：看三层、看自动改动日志、**手动触发一次反思**。
    # 下面是替代原来 `/api/persona` 的只读端点，旧路径显式返回 410，
    # 这样"还能不能改"这个问题在 API 层面也有明确答案（而不是 404 让人以为写错了）。
    @app.post(prefix + "/api/persona")
    async def _persona_gone() -> JSONResponse:  # noqa: ANN202
        return JSONResponse(
            {
                "ok": False,
                "error": "改人设内容的接口已删除：现在只能直接编辑**当前人格包**里的"
                         "base.txt / forbidden.txt（表层由评估修订写：先隔离准备、复核通过才提交）。"
                         "**换一个角色**走 /api/persona/switch，不用重启",
            },
            status_code=410,
        )

    @app.post(prefix + "/api/persona/clear")
    async def _persona_clear_gone() -> JSONResponse:  # noqa: ANN202
        return JSONResponse(
            {"ok": False, "error": "撤销人设要求的接口已删除；自动改动用 /人设 撤回"},
            status_code=410,
        )

    # ---- 修订提案：A+ 之后评估只提案，人确认才落地 ----------------------
    # 权限与「撤回」同一级（都改线上人格资产），桌面端 permissions 再拦一次。
    @app.get(prefix + "/api/persona/proposals")
    async def _proposals() -> JSONResponse:  # noqa: ANN202
        from . import persona_revision_batch as batch
        return JSONResponse({"items": batch.list_proposals()})

    @app.get(prefix + "/api/persona/proposal")
    async def _proposal_detail(rev: str = "") -> JSONResponse:  # noqa: ANN202
        """一条提案的完整内容（ops + before/after）。审核靠它，不靠摘要。"""
        from . import persona_revision_batch as batch
        if not str(rev or "").strip():
            return JSONResponse({"ok": False, "why": "缺 rev"}, status_code=400)
        return JSONResponse(batch.proposal_detail(str(rev).strip()))

    @app.post(prefix + "/api/persona/proposal")
    async def _proposal_act(request: Request) -> JSONResponse:  # noqa: ANN202
        from . import persona_revision_batch as batch
        body = await _body(request)
        rev = str(body.get("rev") or "").strip()
        action = str(body.get("action") or "").strip()
        if not rev:
            return JSONResponse({"ok": False, "why": "缺 rev"}, status_code=400)
        if action == "commit":
            return JSONResponse(batch.commit_proposal(rev))
        if action == "reject":
            return JSONResponse(batch.reject_proposal(rev))
        return JSONResponse({"ok": False, "why": "action 只能是 commit / reject"},
                            status_code=400)

    # ---- 人格包：看有哪些、切到哪一个 ------------------------------------
    # 这是控制台上**唯一会改变"它是谁"的动作**，所以单独两个端点、单独一条权限规则
    # （见 desktop/sdk/permissions.py）。切完立即生效：三层是每轮现读的。
    @app.get(prefix + "/api/persona/packs")
    async def _persona_packs() -> JSONResponse:  # noqa: ANN202
        return JSONResponse({"active": packs.active_id(), "items": packs.list_packs()})

    @app.post(prefix + "/api/persona/switch")
    async def _persona_switch(request: Request) -> JSONResponse:  # noqa: ANN202
        body = await _body(request)
        want = str(body.get("id") or "").strip()
        if not want:
            return JSONResponse({"ok": False, "error": "要给一个包 id"}, status_code=400)
        target = packs.resolve_id(want) or want
        # 先给新包播种表层：切完立刻就有内容（幂等）。失败不拦切换 ——
        # 少了表层只是少一层，而"切不过去"是更严重的事。
        seed_note = config.seed_surface_for(target)
        got = config.switch_persona(target)
        if not got.get("ok"):
            return JSONResponse(
                {"ok": False, "errors": got.get("errors") or [], "active": packs.active_id()},
                status_code=400,
            )
        return JSONResponse({
            "ok": True,
            "from": got.get("from"),
            "active": packs.active_id(),
            "name": packs.active_pack().get("name", ""),
            "identity": got.get("identity") or {},
            "identity_note": got.get("identity_note") or "",
            "warnings": got.get("warnings") or [],
            "registry_written": bool(got.get("registry_written")),
            "seed": seed_note,
            "persona": {
                "stats": persona.stats(),
                "forbidden": persona.forbidden_items(),
                "layers": persona.layers(),
                "changelog": persona.changelog(30),
                "iter": persona_iter.stats(),
                "eval": persona_eval.status(),
                "vector": {
                    "enabled": bool(settings.get("vector_enabled")),
                    "values": persona_vector.state().get("values") or {},
                    "axes": config.load_vector().get("axes") or [],
                    "modes": config.load_vector().get("modes") or [],
                    # 与 /api/state 保持同一份体（两处手抄，改一处必须同步另一处）
                    "recent": persona_vector.recent(5),
                    "effective_samples": persona_vector.effective_samples(5),
                },
                "relation": persona_relation.snapshot(),
                "examples": persona_examples.stats(),
                "pack": packs.stats(),
                "packs": packs.list_packs(),
            },
        })

    @app.post(prefix + "/api/persona/undo")
    async def _persona_undo() -> JSONResponse:  # noqa: ANN202
        """整版回滚最近一次评估驱动的修订（三层一起）。"""
        got = persona.undo_revision()
        if not got.get("ok"):
            legacy = persona.undo_last()
            if legacy.get("ok"):
                return JSONResponse({"ok": True, "note": legacy.get("text", "")})
        return JSONResponse({"ok": bool(got.get("ok")),
                             "note": got.get("why") or f"已回滚 {got.get('rev')}",
                             "rev": got.get("rev")})

    @app.post(prefix + "/api/persona/reflect")
    async def _persona_reflect() -> JSONResponse:  # noqa: ANN202
        """手动跑一次「语料评估 → 修订」：优先补示例，旧特质分只是兜底。"""
        from . import persona_revise

        window = await persona_eval.evaluate_window()
        graded = await persona_eval.run_round()
        revised = await persona_revise.revise_from_eval(source="手动", force=True)
        return JSONResponse({"ok": bool(revised.get("ok")), "window": window,
                             "eval": graded, "revision": revised,
                             "why": revised.get("why", "")})

    @app.post(prefix + "/api/persona/eval")
    async def _persona_eval(request: Request) -> JSONResponse:  # noqa: ANN202
        """人设评估台：`mode=artifacts` 生成测评素材；`mode=round` 跑一轮基线分。

        **总闸是 `eval_enabled`**（控制台「参数 → 人设评估」）—— 关着时这里会直接
        返回 `ok=False` 且一次模型都不调。评估本身只打分；自动修订由定时任务
        或 `/api/persona/reflect` 触发。
        """
        body = await _body(request)
        mode = str(body.get("mode") or "round")
        if mode == "artifacts":
            return JSONResponse(await persona_eval.generate_artifacts())
        return JSONResponse(await persona_eval.run_round())

    @app.post(prefix + "/api/persona/boundary")
    async def _persona_boundary() -> JSONResponse:  # noqa: ANN202
        """手动跑一次边界检查（v2）。只诊断，不改人设。"""
        return JSONResponse(await persona_eval.check_boundary())

    @app.post(prefix + "/api/persona/likeness")
    async def _persona_likeness() -> JSONResponse:  # noqa: ANN202
        """手动跑一次「像人」判别（需要 likeness_samples.jsonl）。"""
        return JSONResponse(await persona_eval.measure_likeness())

    @app.get(prefix + "/api/persona/likeness/candidates")
    async def _likeness_candidates() -> JSONResponse:  # noqa: ANN202
        """候选样本（真实聊天 + 模型生成），供控制台勾选。"""
        return JSONResponse({
            "ok": True,
            "items": persona_eval.likeness_candidates(),
            "selected": persona_eval.selected_likeness_ids(),
        })

    @app.post(prefix + "/api/persona/likeness/generate")
    async def _likeness_generate(request: Request) -> JSONResponse:  # noqa: ANN202
        """让模型补一批「普通群友」样本，追加到候选。"""
        body = await _body(request)
        return JSONResponse(await persona_eval.generate_likeness_samples(int(body.get("n") or 10)))

    @app.post(prefix + "/api/persona/likeness/select")
    async def _likeness_select(request: Request) -> JSONResponse:  # noqa: ANN202
        """把勾选的候选写进 likeness_samples.jsonl。"""
        body = await _body(request)
        ids = body.get("ids")
        return JSONResponse(persona_eval.save_likeness_selection(
            [str(x) for x in ids] if isinstance(ids, list) else []))

    @app.post(prefix + "/api/persona/window")
    async def _persona_window() -> JSONResponse:  # noqa: ANN202
        """评估「上次评估 → 现在」的连续语料里 bot 的发言。"""
        return JSONResponse(await persona_eval.evaluate_window())

    @app.post(prefix + "/api/persona/vector")
    async def _persona_vector(request: Request) -> JSONResponse:  # noqa: ANN202
        """重置情绪（body: {"action":"reset","axis":"mood"}）或改一个轴参数。"""
        body = await _body(request)
        action = str(body.get("action") or "reset")
        if action == "set_param":
            return JSONResponse(persona_vector.set_param(
                str(body.get("axis") or ""), str(body.get("field") or ""), body.get("value")))
        return JSONResponse(persona_vector.reset(str(body.get("axis") or "")))

    @app.post(prefix + "/api/persona/relation")
    async def _persona_relation(request: Request) -> JSONResponse:  # noqa: ANN202
        """改某人的关系值（play / intimacy / tease_topics / no_go）。"""
        body = await _body(request)
        return JSONResponse(persona_relation.set_manual(
            str(body.get("uid") or ""), str(body.get("field") or ""), body.get("value")))

    @app.post(prefix + "/api/persona/examples")
    async def _persona_examples(request: Request) -> JSONResponse:  # noqa: ANN202
        """删一条示例（action=delete + id）或加一条（action=add + item）。"""
        body = await _body(request)
        action = str(body.get("action") or "")
        if action == "delete":
            return JSONResponse(persona_examples.delete(str(body.get("id") or "")))
        if action == "add":
            item = body.get("item")
            return JSONResponse(persona_examples.append(item if isinstance(item, dict) else {}))
        return JSONResponse({"ok": False, "why": "action 只能是 delete/add"})

    # -------------------------------------------------------- #0015 评估台（只读 + 反馈）
    # 这一组**都不改人格**：案例与反馈只进 `data/runtime/persona/<包>/eval/`，
    # 采纳仍走 `/api/persona/proposal`（唯一提交入口）。
    @app.get(prefix + "/api/persona/eval/cases")
    async def _eval_cases() -> JSONResponse:  # noqa: ANN202
        from . import eval_cases

        items = []
        for case in eval_cases.load_all():
            items.append({
                "case_id": case.get("case_id"),
                "version": case.get("version"),
                "failure_type": case.get("failure_type"),
                "provenance": case.get("provenance"),
                "split": eval_cases.split_of(case),
                "split_group": case.get("split_group"),
            })
        return JSONResponse({"ok": True, "stats": eval_cases.stats(), "items": items})

    @app.get(prefix + "/api/persona/eval/feedback")
    async def _eval_feedback_list() -> JSONResponse:  # noqa: ANN202
        from . import eval_cases

        return JSONResponse({"ok": True, "items": eval_cases.feedback_list(50),
                             "contract": eval_cases.feedback_contract()})

    @app.post(prefix + "/api/persona/eval/feedback")
    async def _eval_feedback_add(request: Request) -> JSONResponse:  # noqa: ANN202
        """写一条反馈。**入库不等于已学会**，也不改任何人格文件。"""
        from . import eval_cases

        body = await _body(request)
        return JSONResponse(eval_cases.save_feedback(body if isinstance(body, dict) else {}))

    @app.get(prefix + "/api/persona/eval/sampling")
    async def _eval_sampling() -> JSONResponse:  # noqa: ANN202
        from . import eval_sampling

        return JSONResponse({"ok": True, "stats": eval_sampling.stats(),
                             "due": eval_sampling.due(10)})

    # -------------------------------------------------------- 记忆
    @app.post(prefix + "/api/memory")
    async def _memory_add(request: Request) -> JSONResponse:  # noqa: ANN202
        body = await _body(request)
        text = str(body.get("text") or "").strip()
        if len(text) < 2:
            return JSONResponse({"ok": False, "error": "内容太短"}, status_code=400)
        item, created = await memory.remember(
            text,
            subject=str(body.get("subject") or "主人"),
            importance=float(body.get("importance") or 0.85),
            source="manual",
        )
        return JSONResponse({"ok": True, "created": created, "id": item.get("id")})

    @app.delete(prefix + "/api/memory/{item_id}")
    async def _memory_delete(item_id: int) -> JSONResponse:  # noqa: ANN202
        return JSONResponse({"ok": await memory.forget(item_id)})

    @app.post(prefix + "/api/memory/{item_id}/protect")
    async def _memory_protect(item_id: int, request: Request) -> JSONResponse:  # noqa: ANN202
        body = await _body(request)
        locked = bool(body.get("locked", True))
        return JSONResponse({"ok": await memory.set_protected(item_id, locked)})

    # -------------------------------------------------------- 图片策略
    @app.post(prefix + "/api/image-policy")
    async def _image_policy(request: Request) -> JSONResponse:  # noqa: ANN202
        body = await _body(request)
        global_mode = str(body.get("global_mode") or "").strip()
        if global_mode:
            if global_mode not in state.VALID_MODES:
                return JSONResponse(
                    {"ok": False, "error": f"可选：{'/'.join(state.VALID_MODES)}"}, status_code=400
                )
            state.set_global_image_policy(global_mode)
            return JSONResponse({"ok": True, "global_mode": global_mode})
        conv = str(body.get("conv") or "").strip()
        if not conv:
            return JSONResponse({"ok": False, "error": "缺少 conv"}, status_code=400)
        mode = str(body.get("mode") or "")
        if mode and mode not in state.VALID_MODES:
            return JSONResponse(
                {"ok": False, "error": f"可选：{'/'.join(state.VALID_MODES)}"}, status_code=400
            )
        return JSONResponse({"ok": True, "effective": state.set_from_web(conv, mode)})

    # -------------------------------------------------------- 表情包
    @app.get(prefix + "/api/stickers")
    async def _stickers():  # noqa: ANN202
        lib = await stickers.get_library()
        return JSONResponse({"items": lib.items, "stats": lib.stats()})

    @app.get(prefix + "/api/stickers/{digest}")
    async def _sticker_file(digest: str):  # noqa: ANN202
        lib = await stickers.get_library()
        item = lib.find(digest)
        if item is None:
            return Response(status_code=404)
        path = lib.path_of(item)
        if not path.exists():
            return Response(status_code=404)
        data = await asyncio.to_thread(path.read_bytes)
        ext = path.suffix.lstrip(".").lower()
        media = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "bmp": "image/bmp",
        }.get(ext, "application/octet-stream")
        return Response(content=data, media_type=media)

    @app.delete(prefix + "/api/stickers/{digest}")
    async def _sticker_delete(digest: str):  # noqa: ANN202
        lib = await stickers.get_library()
        ok = await asyncio.to_thread(lib.remove, digest)
        return JSONResponse({"ok": ok})

    @app.post(prefix + "/api/greet")
    async def _greet(request: Request) -> JSONResponse:  # noqa: ANN202
        """手动问候一次，用来确认话术与发送通道。

        绕开时间点与总开关，也**不写**"今天已发"状态 —— 手动触发多半是在试效果，
        不该把当天的自动问候吃掉。body 里带 {"slot": "morning|noon|night"} 可指定时段，
        不带（或给了非法值）就按当前钟点推断。
        """
        body = await _body(request)
        wanted = str(body.get("slot") or "")
        slot = wanted if greetings.label_of(wanted) else greetings.current_slot()
        said = await greetings.greet(slot, force=True)
        return JSONResponse(
            {
                "said": said,
                "slot": slot,
                "label": greetings.label_of(slot),
                "reason": "" if said else "生成或发送失败，看机器人日志（也可能是模型选择了 [SKIP]）",
            }
        )

    _host = str(getattr(driver.config, "host", "") or "127.0.0.1")
    _port = getattr(driver.config, "port", 8080)
    logger.info("Web 控制台已挂载：http://%s:%s%s/", _host, _port, prefix)
    if _host not in ("127.0.0.1", "localhost", "::1"):
        # 控制台目前**没有认证**（见 deploy/README.md 的 4.5 安全清单）：
        # 只靠"端口绑回环 + SSH 隧道"这一层。容器里 HOST 必须是 0.0.0.0（否则端口映射不通），
        # 所以真正的边界在宿主侧的 `127.0.0.1:8080:8080` 端口映射上 —— 这里只负责把事实说清楚，
        # 免得日志里那句硬编码的 127.0.0.1 让人误判成"只有本机能访问"。
        logger.warning(
            "Web 控制台监听在 %s（非回环）且没有任何认证：确认端口映射只绑了 127.0.0.1，"
            "或自己加一层代理/防火墙。",
            _host,
        )
    return True


try:
    _register()
except Exception:  # noqa: BLE001 - UI 挂了也不能影响机器人本体
    logger.exception("注册 Web 控制台失败")
