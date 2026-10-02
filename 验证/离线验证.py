"""离线验证：不连 QQ、不真的调 DeepSeek，验证装配与聊天记录逻辑。

改完代码先跑它，避免带着语法/导入错误去启动机器人（那种错误在群里表现为"没反应"，很难查）。

覆盖：
* 插件与子模块能加载、配置能注入；
* 会话按间隔切分，切分时上一轮的未读被归档为已读；
* 机器人自己的发言直接算已读，不会被当成"新发言"再回应一遍；
* 已读背景按字符预算压缩，越早的记录越先被丢弃；
* mark_read_until 用 id 上界，不误伤后到的消息；
* JSON 落盘 / 读取往返一致。

用法：
    .\\.venv\\Scripts\\python.exe '验证\\离线验证.py'
退出码：0 = 通过。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# 全程写临时目录，绝不碰真实的 data/runtime/chatlog_*.json
TMP = pathlib.Path(tempfile.mkdtemp(prefix="ai_chat_verify_"))

import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi+~websockets",
    deepseek_api_key="sk-offline-verify",
    deepseek_base_url="https://api.deepseek.com",
    deepseek_model="deepseek-chat",
    onebot_ws_urls=["ws://127.0.0.1:6700"],
    ai_chat_session_gap=300,
    ai_chat_read_budget=300,
    ai_chat_msg_clip=40,
    ai_chat_log_dir=str(TMP),
)

from nonebot.adapters.onebot.v11 import Adapter  # noqa: E402

nonebot.get_driver().register_adapter(Adapter)

passed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  [OK] {label}" + (f" —— {detail}" if detail else ""))
    else:
        print(f"  [失败] {label}" + (f" —— {detail}" if detail else ""))
        raise AssertionError(label)




# ---- 打桩点：十个模块各自的 `_client` 已收敛成 `llm.chat` 一处 ----
_LLM_CHAT_ORIG = None


def patch_chat(fake):
    """把 `llm.chat` 换成假实现。`fake` 是带 `chat.completions.create` 的假客户端。

    **可以反复调用**（每次换一个假回答），还原统一用 `unpatch_chat()` ——
    原先的写法是逐次替换 `gr._client`、最后把最初那份存下来还原，
    一旦中间哪次忘了存就会把假客户端留在模块里。这里把"最初那份"收进一个变量。
    """
    global _LLM_CHAT_ORIG
    from plugins.ai_chat import llm
    if _LLM_CHAT_ORIG is None:
        _LLM_CHAT_ORIG = llm.chat

    async def _fake_chat(messages, *, profile=None, model=None, **kw):  # noqa: ANN001, ANN003, ANN202
        return await fake.chat.completions.create(messages=messages, **kw)

    llm.chat = _fake_chat
    return fake


def unpatch_chat() -> None:
    from plugins.ai_chat import llm
    if _LLM_CHAT_ORIG is not None:
        llm.chat = _LLM_CHAT_ORIG


try:
    plugins = nonebot.load_plugins("plugins")
    names = [getattr(p, "name", "?") for p in plugins]
    check("插件加载", any("ai_chat" in str(n) for n in names), str(names))

    from plugins.ai_chat import chatlog, config, settings as st
    from plugins.ai_chat.chatlog import ConversationLog

    check(
        "配置注入",
        st.get('session_gap') == 300 and st.get('read_budget') == 300 and st.get('msg_clip') == 40,
        f"GAP={st.get('session_gap')} BUDGET={st.get('read_budget')} CLIP={st.get('msg_clip')}",
    )
    check("日志目录隔离", str(config.LOG_DIR) == str(TMP), str(config.LOG_DIR))

    # ---------------------------------------------------------- 会话切分
    print("\n-- 会话切分与已读/未读 --")
    log = ConversationLog("g1", TMP / "chatlog_g1.json")
    m1 = log.append(101, "张三", "甲方的层高要求是 4.2 米")
    m2 = log.append(102, "李四", "那净高就有点紧了")
    check("同一轮内不切分", log.session == 1, f"session={log.session}")
    check("新消息默认未读", not m1["read"] and not m2["read"])

    # 把最后一条的时间往前推，模拟"间隔超过阈值"
    log.messages[-1]["ts"] -= st.get('session_gap') + 30
    m3 = log.append(103, "王五", "@bot 层高和净高一般差多少")
    check("间隔超阈值开新会话", log.session == 2, f"session={log.session}")
    check(
        "旧未读被归档为已读",
        bool(m1["read"]) and bool(m2["read"]),
        "上一轮闲聊不再以未读身份送进模型",
    )
    check("新会话消息保持未读", not m3["read"])

    bot_msg = log.append(999, "机器人", "一般差 0.6~1.0 米，看是否有吊顶", is_bot=True)
    check("机器人发言直接算已读", bot_msg["read"] is True)

    # ---------------------------------------------------------- 已读上界
    print("\n-- mark_read_until 的 id 上界 --")
    late = log.append(104, "赵六", "我插一句", )  # 模拟回复期间新到的消息
    marked = log.mark_read_until(m3["id"])
    check("只标记到指定 id", marked == 1, f"标记了 {marked} 条")
    check("目标消息已读", m3["read"] is True)
    check("后到的消息不受影响", not late["read"], "不会被误标为已读")

    # ---------------------------------------------------------- 预算压缩
    print("\n-- 已读背景的预算压缩 --")
    log2 = ConversationLog("g2", TMP / "chatlog_g2.json")
    for i in range(60):
        msg = log2.append(200 + i, f"群友{i}", f"第 {i} 条闲聊内容" * 3)
        msg["read"] = True
    bg = log2.render_background()
    kept_lines = [ln for ln in bg.split("\n") if ln.startswith("[")]
    check(
        "压缩后未超预算太多",
        len(bg) < st.get('read_budget') * 2,
        f"预算 {st.get('read_budget')} → 实际 {len(bg)} 字符",
    )
    check("最早被丢弃而不是最新", "第 59 条" in bg and "第 0 条" not in bg)
    # 措辞在 2026-09-25 改过：原来只说"已省略"，容易被模型理解成"那些事没发生过"；
    # 现在要点明"只是没放进本节"并指向会话摘要。
    check("有丢弃提示，且说明只是没放进本节", "没有放进本节" in bg, bg.split("\n")[0])
    check("丢弃提示指向会话摘要", "摘要" in bg.split("\n")[0], bg.split("\n")[0])

    # ---------------------------------------------------------- 文件往返
    print("\n-- JSON 落盘 / 读取往返 --")
    log3 = ConversationLog("g3", TMP / "chatlog_g3.json")
    log3.append(301, "张三", "你好")
    log3.save()
    check("文件已生成", (TMP / "chatlog_g3.json").exists())

    log4 = ConversationLog("g3", TMP / "chatlog_g3.json")
    log4.load()
    check("消息数一致", len(log4.messages) == 1)
    check("内容一致", log4.messages[0]["text"] == "你好")
    check("next_id 续接正确", log4.next_id == 2, f"next_id={log4.next_id}")

    # 损坏文件不应让机器人起不来
    bad = TMP / "chatlog_broken.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    log5 = ConversationLog("broken", bad)
    log5.load()
    check("损坏文件降级为空记录", log5.messages == [], "不让插件加载失败")

    # ---------------------------------------------------------- 长回复切分
    from plugins.ai_chat import _split_for_qq

    long_text = "\n".join(["这是一段用于验证切分逻辑的中文句子。" * 20] * 15)
    chunks = _split_for_qq(long_text, limit=900)
    check("长回复分段不超限", all(len(c) <= 900 for c in chunks), f"{len(chunks)} 段")
    check(
        "分段后内容无丢失",
        "".join(chunks).replace("\n", "") == long_text.replace("\n", ""),
    )

    # ---------------------------------------------------------- 运行时设置
    print("\n-- 运行时设置（settings.json 热覆盖）--")
    from plugins.ai_chat import settings as st

    check("默认值来自 .env", st.get("read_budget") == 300, f"read_budget={st.get('read_budget')}")
    st.set_value("sticker_chance", 0.77)
    check("写入后立即生效", abs(st.get("sticker_chance") - 0.77) < 1e-6)
    check("已落盘", (TMP / "settings.json").exists())
    st.reload_from_disk()
    check("重载后仍是新值", abs(st.get("sticker_chance") - 0.77) < 1e-6, "模拟重启")
    st.set_value("sticker_chance", 5.0)
    check("超范围被夹紧", st.get("sticker_chance") == 1.0, "上限 1.0")
    st.set_value("sticker_chance", "0.25")
    check("字符串按类型转换", abs(st.get("sticker_chance") - 0.25) < 1e-6)
    try:
        st.set_value("no_such_key", 1)
        check("未知参数被拒绝", False)
    except KeyError:
        check("未知参数被拒绝", True)
    check("spec 表能驱动 UI", len(st.describe()) >= 4, f"{len(st.describe())} 个分组")
    # 模型名的来源变了：`.env` 的 DEEPSEEK_MODEL 现在**只用来播种档案**，
    # `settings.model` 退回"纯覆盖"（留空 = 用当前档案里写的那个）。
    # 原先这里验的是"spec 覆盖生效"，那条机制由上面的 read_budget 覆盖着。
    check(
        "模型名不再是全局覆盖（留空 = 用当前档案自带的）",
        st.get("model") == "",
        f"model={st.get('model')!r}（.env 的 DEEPSEEK_MODEL 只用于播种档案）",
    )
    from plugins.ai_chat import llm as _llm_mod

    check(
        "模型名改由「档案」承担",
        _llm_mod.model_name() == _llm_mod.active()["model"] == config.MODEL,
        f"档案 {_llm_mod.active_id()} → {_llm_mod.model_name()}（config.MODEL={config.MODEL}）",
    )

    # ---------------------------------------------------------- 表情包库
    print("\n-- 表情包库 --")
    from plugins.ai_chat.stickers import StickerLibrary, _guess_ext

    check("格式嗅探 PNG", _guess_ext(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "png")
    check("格式嗅探 JPG", _guess_ext(b"\xff\xd8\xff" + b"\x00" * 8) == "jpg")

    lib = StickerLibrary()
    lib.load()
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 200
    item = lib.add(
        png, conv="g1", uid=100000001, name="魔王", is_master=True,
        sub_type=1, score=0.9, reason="斗图素材",
    )
    check("入库成功", item is not None and item["score"] == 0.9)
    check(
        "同图去重",
        lib.add(png, conv="g1", uid=1, name="别人", is_master=False,
                sub_type=1, score=0.9, reason="重复") is None,
    )
    check("图片已落盘", (lib.dir / item["file"]).exists(), item["file"])
    check("索引文件已生成", lib.index_path.exists())
    check("统计正确", lib.stats()["count"] == 1)

    picked = lib.pick()
    check("能随机取用", picked is not None and picked["hash"] == item["hash"])
    check("使用次数递增", picked["uses"] == 1)

    lib2 = StickerLibrary()
    lib2.load()
    check("索引往返一致", len(lib2.items) == 1 and lib2.items[0]["reason"] == "斗图素材")
    check("删除生效", lib2.remove(item["hash"]) is True and len(lib2.items) == 0)
    check("重复删除返回 False", lib2.remove(item["hash"]) is False)

    # ---------------------------------------------------------- 主动发言
    print("\n-- 主动发言限流 --")
    from plugins.ai_chat import proactive as pa

    st.set_value("proactive_max_per_day", 2)
    st.set_value("proactive_cooldown", 3600)
    pa._state.ensure()
    pa._state.last_spoke.clear()
    pa._state.day_count.clear()
    check("初始可发言", pa._state.budget_left("g1") and pa._state.cooled_down("g1"))
    pa._state.record_speak("g1")
    check("冷却生效", not pa._state.cooled_down("g1"))
    pa._state.record_speak("g1")
    check("日上限生效", not pa._state.budget_left("g1"), "上限 2")
    # 上面只有 g3 真正落过盘；再造一个群文件，验证按文件名解析群号
    (TMP / "chatlog_g777.json").write_text('{"messages": []}', encoding="utf-8")
    groups = sorted(asyncio.run(pa.known_groups()))
    check("能识别已知群", groups == [3, 777], str(groups))

    # ---------------------------------------------------------- 关键词唤醒
    print("\n-- 关键词唤醒 --")
    from plugins.ai_chat import _mentions_wake_word, _wake_cooldown_ok, _wake_words

    words = _wake_words()
    check("唤醒词解析", "肥鱼" in words and "鲸鱼娘" in words, str(words))

    class _FakeText:
        def __init__(self, t: str) -> None:
            self._t = t

        def get_plaintext(self) -> str:
            return self._t

    check("命中「大肥鱼」", _mentions_wake_word(_FakeText("大肥鱼在吗")))
    check("包含式命中「死肥鱼」", _mentions_wake_word(_FakeText("死肥鱼！")), "配一个「肥鱼」就够")
    check("无关消息不命中", not _mentions_wake_word(_FakeText("今天天气不错")))
    check("空消息不命中", not _mentions_wake_word(_FakeText("")))

    st.set_value("wake_enabled", False)
    check("关掉开关后不命中", not _mentions_wake_word(_FakeText("大肥鱼在吗")))
    st.set_value("wake_enabled", True)

    st.set_value("wake_cooldown", 300)
    check("首次唤醒放行", _wake_cooldown_ok("g_wake_test"))
    check("冷却期内拦截", not _wake_cooldown_ok("g_wake_test"))
    st.set_value("wake_cooldown", 0)
    check("冷却设为 0 则不拦", _wake_cooldown_ok("g_wake_test"))

    # ---------------------------------------------------------- 纯图片消息
    print("\n-- 纯图片消息（回归：不许再冒出「在的，@我想说什么？」）--")
    from nonebot.adapters.onebot.v11 import Message as OBMessage
    from nonebot.adapters.onebot.v11 import MessageSegment, PrivateMessageEvent
    from nonebot.exception import FinishedException

    from plugins.ai_chat import _question_text, handle_ai_chat, stickers

    img_event = PrivateMessageEvent(
        time=0,
        self_id="100000002",
        post_type="message",
        message_type="private",
        sub_type="friend",
        message_id=1,
        user_id=100000001,
        message=OBMessage([MessageSegment.image("file:///tmp/not-exist.jpg")]),
        raw_message="",
        font=0,
        sender={"user_id": 100000001, "nickname": "魔王"},
        # 必须显式给 to_me：NoneBot2 的 is_tome() 读的就是这个字段，
        # 而它平时由适配器解析真实事件时填充。手工构造忘了给的话，
        # 消息会被当成"没在跟它说话"，测试就会假通过（走唤醒掷骰碰运气）。
        to_me=True,
    )
    check("纯图片消息正文为空", _question_text(img_event) == "", "这正是当初踩坑的前提")
    check("但仍能识别出图片段", len(stickers.extract_images(img_event)) == 1)

    class _FakeBot:
        self_id = "100000002"

    try:
        asyncio.run(handle_ai_chat(_FakeBot(), img_event))
        check("纯图片消息静默不回", True, "没有再回兜底提示")
    except FinishedException:
        check("纯图片消息静默不回", False, "仍然会回「在的，@我想说什么？」")

    # 打开开关后不该再走"静默"，而是去读图。
    # 直接盯 _load_first_image 有没有被调用 —— 比"是否抛异常"精确得多，
    # 也不会因为离线环境里 API 调用失败而误判。
    st.set_value("reply_to_images", True)
    import plugins.ai_chat as _pkg

    calls = {"n": 0}
    original_loader = _pkg._load_first_image

    async def _spy(segments):
        calls["n"] += 1
        return None  # 假装下载失败，让流程退回纯文本，免得真去调 API

    _pkg._load_first_image = _spy
    handler_err = None
    try:
        asyncio.run(handle_ai_chat(_FakeBot(), img_event))
    except Exception as exc:
        handler_err = f"{type(exc).__name__}: {exc}"
    finally:
        _pkg._load_first_image = original_loader

    check(
        "开关打开后会去读图",
        calls["n"] >= 1,
        f"loader={calls['n']} flag={st.get('reply_to_images')} "
        f"vision={st.get('chat_vision')} tome={img_event.is_tome()} err={handler_err}",
    )
    st.set_value("reply_to_images", False)

    # ---------------------------------------------------------- 引用 / 回复
    print("\n-- 引用（QQ 的「回复」功能）--")
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    from plugins.ai_chat import _resolve_reply

    class _FakeSender:
        def __init__(self, uid: int, nickname: str = "张三", card: str = "") -> None:
            self.user_id = uid
            self.nickname = nickname
            self.card = card

    class _FakeReply:
        """模拟 NoneBot2 预处理后的 event.reply。"""

        def __init__(
            self, message, uid: int, nickname: str = "张三", card: str = ""
        ) -> None:
            self.message = message
            self.sender = _FakeSender(uid, nickname, card)

    def _group_event(msg_id: int, segments: list, to_me: bool = True) -> GroupMessageEvent:
        return GroupMessageEvent(
            time=0,
            self_id="100000002",
            post_type="message",
            message_type="group",
            sub_type="normal",
            message_id=msg_id,
            group_id=100000004,
            user_id=100000001,
            message=OBMessage(segments),
            raw_message="",
            font=0,
            sender={"user_id": 100000001, "nickname": "魔王", "role": "member"},
            to_me=to_me,  # 同上：is_tome() 看的就是它
        )

    # 关键：NoneBot2 的 _check_reply 已经调过 get_msg，**把 reply 段从 event.message
    # 里删掉了**，内容只留在 event.reply。所以这里刻意让 message 里没有 reply 段 ——
    # 这正是真机的样子，也正是之前几轮一直没测到的场景（旧代码在这里必然取空）。
    quoted_event = _group_event(2, [MessageSegment.text("那消防管道够走吗")])
    quoted_event.reply = _FakeReply(
        OBMessage(
            [
                MessageSegment.text("甲方要求层高 4.2 米"),
                MessageSegment.image("http://e.com/a.jpg"),
                MessageSegment("file", {"file": "报告.md", "file_id": "f1"}),
            ]
        ),
        1,
        nickname="zhangsan",
        card="张三",
    )
    qtext, qimages, qfiles, qfrombot = asyncio.run(_resolve_reply(quoted_event))
    check("从 event.reply 取回被引用的文字", "甲方要求层高 4.2 米" in qtext, qtext)
    check("带上引用者昵称（优先群名片）", "张三" in qtext, qtext)
    check("识别出引用里的图片", len(qimages) == 1, str(qimages))
    check("识别出引用里的文件", len(qfiles) == 1, str(qfiles))
    check("引用别人的消息 → 不算在跟它说话", not qfrombot)

    bot_msg_event = _group_event(3, [MessageSegment.text("那呢")])
    bot_msg_event.reply = _FakeReply(
        OBMessage([MessageSegment.text("米饭。白米饭最好。")]),
        100000002,
        nickname="鲸鱼女孩",
    )
    _, _, _, from_self = asyncio.run(_resolve_reply(bot_msg_event))
    check(
        "引用机器人自己发的消息 → 算在跟它说话",
        from_self,
        "这样追问时才不用靠概率才敢回",
    )

    check(
        "没有引用时返回空",
        asyncio.run(_resolve_reply(_group_event(4, [MessageSegment.text("普通消息，没引用")])))
        == ("", [], [], False),
    )

    # ---------------------------------------------------------- 带文字的图片
    print("\n-- 「@它 + 文字 + 图片」也必须读图 --")
    text_img_event = _group_event(
        4,
        [
            MessageSegment.at("100000002"),
            MessageSegment.text("这是什么"),
            MessageSegment.image("file:///tmp/none.jpg"),
        ],
    )
    check(
        "这条被判为在跟它说话",
        text_img_event.is_tome(),
        f"self_id={text_img_event.self_id!r} 句段={[(s.type, s.data) for s in text_img_event.message]}",
    )
    check("正文只有那半句文字", _question_text(text_img_event) == "这是什么")
    check("图片段确实存在", len(stickers.extract_images(text_img_event)) == 1)

    calls2 = {"n": 0}

    async def _spy2(segments):
        calls2["n"] += 1
        return None

    _pkg._load_first_image = _spy2
    try:
        asyncio.run(handle_ai_chat(_FakeBot(), text_img_event))
    except Exception:
        pass  # 后面要调真 API，离线环境必然失败
    finally:
        _pkg._load_first_image = original_loader

    check(
        "有文字时也会把图送进模型",
        calls2["n"] >= 1,
        "这正是「只读得了表情、读不了图片」的根因",
    )

    st.set_value("chat_vision", False)
    calls2["n"] = 0
    _pkg._load_first_image = _spy2
    try:
        asyncio.run(handle_ai_chat(_FakeBot(), text_img_event))
    except Exception:
        pass
    finally:
        _pkg._load_first_image = original_loader
    check("关掉 chat_vision 后不读图", calls2["n"] == 0)
    st.set_value("chat_vision", True)


    # ---------------------------------------------------------- 随机插话
    print("\n-- 随机插话（没被提到也按概率接话）--")
    from plugins.ai_chat import (
        _allowed,
        _is_addressed,
        _is_master,
        _mentions_wake_word,
        _random_cooldown_ok,
        _random_reply_eligible,
        _should_reply,
    )

    plain_group = _group_event(9, [MessageSegment.text("今天天气不错啊")], to_me=False)

    # **前置条件（2026-09-25 补）**：`_should_reply` 现在除了随机开关，还会读
    # `_allowed()` 与**对话租约**（全局状态）。前面的用例给同一个会话授予过租约，
    # 租约的属主正好也是这个 uid，于是"随机关着却仍然放行"。
    #
    # ⚠ 清的时候**必须用真实会话 id**：`_group_event` 的第一个参数是 `message_id`，
    # `group_id` 是它内部写死的 —— 曾经在这里写成 `conversation_id(9, ...)` 而
    # 清错了会话（`g9` ≠ `g100000004`），白查了很久。`clear_lease` 本来就返回 bool，
    # 所以顺手断言"确实有东西被清掉"，避免下次再清错。
    from plugins.ai_chat import attention as _attn
    _conv_here = chatlog.conversation_id(plain_group.group_id, plain_group.user_id)
    _attn.clear_lease(_conv_here)   # 返回 bool，这里不依赖它（可能本就没租约）
    check("该构造事件是被允许的会话（前置）", _allowed(plain_group))
    check("租约已清干净（前置）", not _attn.lease_peek(
        _conv_here, str(plain_group.user_id),
        is_master=_is_master(plain_group), text=plain_group.get_plaintext()))

    st.set_value("random_reply_enabled", False)
    check("关掉随机插话时不参与", not _random_reply_eligible(plain_group))
    check("关掉随机插话时 rule 不会因随机而放行（且无租约）",
          not _should_reply(plain_group), "随机关着 + 无租约 + 没叫它 = 安静")

    # 反向：有租约时，即使随机插话关着，rule 也该放行（这是新加的行为，要有守卫）
    _attn.lease_grant(_conv_here, str(plain_group.user_id))
    check("有租约时 rule 放行（不受随机开关影响）", _should_reply(plain_group))
    _attn.clear_lease(_conv_here)
    check("撤销租约后 rule 又安静了", not _should_reply(plain_group))

    st.set_value("random_reply_enabled", True)
    check("打开后群聊消息可插话", _random_reply_eligible(plain_group))
    check("打开后 rule 放行", _should_reply(plain_group))

    st.set_value("random_reply_min_chars", 20)
    check(
        "太短的消息被跳过",
        not _random_reply_eligible(plain_group),
        "「今天天气不错啊」只有 7 字，没什么可接的",
    )
    st.set_value("random_reply_min_chars", 4)

    check("私聊不参与随机插话", not _random_reply_eligible(img_event), "私聊本来就算在跟它说话")
    check(
        "提到唤醒词时仍走唤醒那条路",
        _should_reply(_group_event(10, [MessageSegment.text("大肥鱼在吗")], to_me=False)),
    )

    st.set_value("random_reply_cooldown", 300)
    check("首次插话放行", _random_cooldown_ok("g_rand"))
    check("冷却期内拦截", not _random_cooldown_ok("g_rand"))
    st.set_value("random_reply_cooldown", 0)
    check("冷却设为 0 则不拦", _random_cooldown_ok("g_rand"))

    st.set_value("random_reply_enabled", False)

    # ---------------------------------------------------------- 最近图片
    print("\n-- 「先发图，隔一条再问」的跨消息指代 --")
    from plugins.ai_chat import _RECENT_IMAGE_TTL, _recent_image, _remember_image

    _remember_image("g_recent", b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    got = _recent_image("g_recent")
    check("刚记下的图能取回", got is not None and len(got) > 0, f"{len(got or b'')} 字节")
    check("没记过的会话取回空", _recent_image("g_never") is None)

    # 时间戳设成 0（很久以前），应当过期失效
    _pkg._recent_images["g_stale"] = (0.0, b"old")
    check(
        "超过 TTL 后自动失效",
        _recent_image("g_stale") is None,
        f"TTL={_RECENT_IMAGE_TTL:.0f} 秒",
    )

    # ---------------------------------------------------------- 文件读取
    print("\n-- 消息里的文件 --")
    from plugins.ai_chat import files as fmod

    check("体积格式化", fmod.human_size(512) == "512 B" and "KB" in fmod.human_size(2048))

    check(
        "认出 UTF-8 文本",
        fmod.guess_text("层高 4.2 米".encode("utf-8"), "a.txt") == "层高 4.2 米",
    )
    check("认出 GBK 文本", fmod.guess_text("中文编码".encode("gbk"), "a.txt") == "中文编码")
    check("二进制扩展名直接拒绝", fmod.guess_text(b"\x89PNG\r\n\x1a\n", "a.png") is None)
    check(
        "陌生扩展名靠 NUL 字节判断",
        fmod.guess_text(b"\x00\x01\x02binary", "a.dat") is None,
    )
    check("代码文件按文本读", fmod.guess_text(b"print('hi')", "a.py") == "print('hi')")

    file_event = _group_event(
        11,
        [
            MessageSegment(
                "file", {"file": "notes.md", "url": "http://e.com/n.md", "file_size": 1234}
            )
        ],
    )
    fsegs = fmod.extract_files(file_event)
    check("能取出文件段", len(fsegs) == 1 and fsegs[0]["file"] == "notes.md", str(fsegs))

    class _SwapBot:
        """假 bot：记录 call_api 调用，并回一个下载地址。"""

        self_id = "100000002"

        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def call_api(self, api: str, **kwargs):
            self.calls.append((api, kwargs))
            return {"url": "https://e.com/got.md"}

    swap = _SwapBot()
    got = asyncio.run(
        fmod.resolve_url(swap, {"file": "a.md", "file_id": "fid1"}, 100000003, 999)
    )
    check("群文件能换取下载地址", got == "https://e.com/got.md")
    check(
        "群聊走 get_group_file_url，busid 默认 102",
        bool(swap.calls)
        and swap.calls[0][0] == "get_group_file_url"
        and swap.calls[0][1].get("busid") == 102,
        str(swap.calls),
    )

    swap2 = _SwapBot()
    asyncio.run(fmod.resolve_url(swap2, {"file": "a.md", "file_id": "fid2"}, None, 100000001))
    check(
        "私聊走 get_private_file_url（实测私聊 file 段确实没有 url）",
        bool(swap2.calls) and swap2.calls[0][0] == "get_private_file_url",
        str(swap2.calls),
    )

    swap3 = _SwapBot()
    same = asyncio.run(fmod.resolve_url(swap3, {"url": "https://x/y.md"}, 1, 1))
    check("自带 url 时不调接口", same == "https://x/y.md" and not swap3.calls)

    st.set_value("file_max_kb", 1)
    big = asyncio.run(
        fmod.read_segment(_SwapBot(), {"file": "big.zip", "file_size": 5 * 1024 * 1024})
    )
    check("声明超限的文件不下载", "上限" in (big or ""), (big or "").splitlines()[0])

    st.set_value("file_max_kb", 512)
    nourl = asyncio.run(fmod.read_segment(_SwapBot(), {"file": "x.txt"}))
    check("既没 file_id 也没 url 时给出说明", "下载地址" in (nourl or ""), (nourl or "").splitlines()[0])

    st.set_value("file_enabled", False)
    check(
        "总开关关掉后不处理",
        asyncio.run(fmod.read_segment(_SwapBot(), {"file": "x.txt"})) is None,
    )
    st.set_value("file_enabled", True)

    file_quote_event = _group_event(5, [MessageSegment.text("这个文件呢")])
    file_quote_event.reply = _FakeReply(
        OBMessage([MessageSegment("file", {"file": "笔记.md", "file_id": "fid9"})]),
        100000007,
        nickname="Endorphin.",
    )
    _, _, qf, _ = asyncio.run(_resolve_reply(file_quote_event))
    check("引用文件消息时能取出文件段", len(qf) == 1 and qf[0]["file"] == "笔记.md", str(qf))

    # reply.message 在真机上是 Message 对象（元素为 MessageSegment，不是 dict）
    obj_event = _group_event(6, [MessageSegment.text("再看看")])
    obj_event.reply = _FakeReply(
        OBMessage(
            [
                MessageSegment.text("甲方要求层高 4.2 米"),
                MessageSegment("file", {"file": "报告.md", "file_id": "f1"}),
                MessageSegment.image("http://e.com/a.jpg"),
            ]
        ),
        1,
        nickname="zhangsan",
        card="三哥",
    )
    ot, oi, of, _ = asyncio.run(_resolve_reply(obj_event))
    check(
        "reply.message 是 Message 对象时也能解析（真机就是这种）",
        "甲方要求层高 4.2 米" in ot and len(oi) == 1 and len(of) == 1,
        f"text={ot!r} 图片={len(oi)} 文件={len(of)}",
    )


    # ---------------------------------------------------------- 概率要素已摘除
    # 曾经有三条概率项（flavor / rice / fat）把「随机心情」注入 system prompt。
    # 它们的注入点是 config.system_prompt()，而对话改走 context.system_prompt() 之后
    # 那个函数就再没有调用方 —— 实测 200 轮命中 0 次，可 `/机制 风格` 还在如实汇报
    # 这几个概率。现在整条摘掉，这几条断言就是防止它悄悄长回来的守门人。
    print("\n-- 概率要素已整条摘除 --")
    check("config 里不再有随机要素函数", not hasattr(config, "_roll_flavor")
          and not hasattr(config, "_roll_persona_hints"))
    check("config 里不再有 system_prompt()", hasattr(config, "time_hint")
          and not hasattr(config, "system_prompt"))
    check("config 里不再有心情池", not any(
        hasattr(config, n) for n in ("_RICE_MOODS", "_FAT_MOODS", "_FLAVOR_MOODS")))
    # ⚠️ `settings.get()` 对**未注册的键返回 None 而不是抛 KeyError**
    #    （settings.py 里是 `_env.get(key, ... if key in _SPEC_BY_KEY else None)`），
    #    所以"取不到"不能当作"已删除"的证据 —— 必须直接查注册表。
    for _key in ("flavor_chance", "rice_chance", "fat_react_chance"):
        check(f"配置项 {_key} 已从 spec 表删除", _key not in st._SPEC_BY_KEY)

    # ---------------------------------------------------------- 注意力机制
    print("\n-- 注意力机制（按话题相关性主动接话）--")
    from plugins.ai_chat import attention as att

    st.set_value("attention_enabled", True)
    st.set_value("attention_interval", 0)  # 关掉限流，方便连续评估
    st.set_value("attention_min_chars", 1)
    st.set_value("attention_threshold", 0.8)
    st.set_value("attention_window", 600)

    att.clear("g_att")
    att.focus("g_att", "消防管道够不够走")
    s0 = att.state("g_att")
    check("唤醒后进入注意力状态", s0.get("topic") == "消防管道够不够走", str(s0))
    check(
        "初始注意力取参数值",
        abs(s0.get("value", 0) - float(st.get("attention_initial"))) < 1e-6,
        f"value={s0.get('value')} 参数 attention_initial={st.get('attention_initial')}",
    )

    # 打桩：把相关性打分固定成给定值，避免离线环境真去调 API
    original_score = att.score_relevance

    async def _fake_score(topic, text):
        return _fake_score.value

    def _rewind(conv):
        """把「上次评估时间」与「静默截止」都拨回去，绕过两道限流。

        注意 attention_interval 的合法最小值是 5 秒（防止每条消息都白烧 token），
        测试里 set_value 0 会被夹紧到 5，所以只能直接改内部状态。

        `muted_until` 是 2026-09-25 新增的「回复后静默期」——`relax()` 会推后它。
        本段测的是**衰减/增益的数值行为**，那与静默是两个独立变量，
        所以这里一并回拨（否则"连续第二条"永远触发不了，测的就不是衰减模型了）。
        """
        f = att._focus.get(conv)
        if f is not None:
            f.last_eval = 0.0
            f.muted_until = 0.0

    _fake_score.value = 1.0
    att.score_relevance = _fake_score
    try:
        _rewind("g_att")
        should, value = asyncio.run(att.should_speak("g_att", "那段管道还得再抬 200 吧"))
        check("刚唤醒时一条高度相关的发言就能触发", should, f"value={value:.2f}")

        before = att.state("g_att")["value"]
        att.relax("g_att")
        after = att.state("g_att")["value"]
        check("回话后注意力减半", abs(after - before / 2) < 1e-6, f"{before:.2f} → {after:.2f}")

        _rewind("g_att")
        should, value = asyncio.run(att.should_speak("g_att", "再确认一下标高"))
        check(
            "减半后单条满分不再触发",
            not should,
            f"value={value:.2f} —— 要连续两条高度相关才会再过线",
        )
        _rewind("g_att")
        should, value = asyncio.run(att.should_speak("g_att", "那排烟口位置也要跟着挪"))
        check("连续第二条高度相关才再次触发", should, f"value={value:.2f}")

        # 跑题会掉注意力。单独开一个会话测，免得跟上面的数值互相干扰 ——
        # 注意力一旦掉到 0.25，一条满分也只能拉回 0.675，不够过线（这正是设计意图）
        att.focus("g_drop", "消防管道够不够走")
        _fake_score.value = 0.0
        _rewind("g_drop")
        should, value = asyncio.run(att.should_speak("g_drop", "今天天气不错啊"))
        check("跑题的发言不触发", not should, f"value={value:.2f}")
        check("注意力随之下降", value < 0.5, f"value={value:.2f}")
        att.clear("g_drop")

        # ---------------------------------------------------------- 回复后静默期
        # 2026-09-25 新增：`relax()` 之后注意力静默一段时间，那几句交给对话租约。
        # 与「衰减」是**两个独立变量**，所以这里复位 muted_until 前后各测一次。
        _fake_score.value = 1.0
        att.focus("g_mute", "一段足够长的话题用于验证静默期")
        _rewind("g_mute")
        should_before, _ = asyncio.run(att.should_speak("g_mute", "静默前的一条相关发言"))
        check("静默期之前正常评估", should_before, "先确认这条路径本身是通的")
        att.relax("g_mute")            # 回复 → 进入静默
        _rewind("g_mute")              # 只拨 last_eval，**保留 muted_until**
        should_muted, _ = asyncio.run(att.should_speak("g_mute", "静默期内的一条相关发言"))
        check("静默期内即使相关也不接话（交给租约）", not should_muted,
              "这正是与租约的分工：刚回完，接下来归租约管")
        att._focus["g_mute"].muted_until = 0.0   # 静默结束
        _rewind("g_mute")
        should_after, _ = asyncio.run(att.should_speak("g_mute", "静默结束后的一条相关发言"))
        check("静默结束后恢复评估", should_after, "静默只是延后，不是永久关闭")
        att.clear("g_mute")
    finally:
        att.score_relevance = original_score

    # 活跃期超时。注意 attention_window 的合法最小值是 60 秒（set_value 0 会被夹紧），
    # 所以直接把它记的「开始时间」拨到很久以前 —— 比绕参数约束可靠。
    item = att._focus.get("g_att")
    check("超时前确实还在聚焦", item is not None)
    if item is not None:
        item.started_at = 0.0
    _rewind("g_att")
    check(
        "超过活跃期后不再评估",
        not asyncio.run(att.should_speak("g_att", "还在聊吗"))[0],
    )
    check("过期后状态被清掉", att.state("g_att") == {})

    # 阈值随时间抬高：话题拖得越久，越需要高相关性才接得上
    att.focus("g_rise", "消防管道够不够走")
    rise_item = att._focus["g_rise"]
    early = att._effective_threshold(rise_item)
    rise_item.started_at = time.time() - 570  # 假装已经过了 95% 的活跃期
    late = att._effective_threshold(rise_item)
    check(
        "阈值随话题推进而升高",
        late > early + 0.1,
        f"{early:.2f} → {late:.2f}（刚唤醒 vs 快到期）",
    )
    check("阈值不会超过 1.0", late <= 1.0, f"{late:.2f}")
    att.clear("g_rise")

    # 没被唤醒过的会话不参与
    att.clear("g_none")
    check(
        "没被唤醒过就不参与",
        not asyncio.run(att.should_speak("g_none", "随便说点什么"))[0],
    )

    # 开关
    st.set_value("attention_enabled", False)
    att.focus("g_off", "某个话题")
    check("总开关关掉时不聚焦", att.state("g_off") == {})
    st.set_value("attention_enabled", True)

    st.set_value("attention_interval", 20)
    st.set_value("attention_min_chars", 6)

    # ---------------------------------------------------------- 当前时间
    print("\n-- 当前时间（注入 prompt）--")
    import datetime as _dt

    from plugins.ai_chat import config as tcfg
    from plugins.ai_chat import greetings as gr

    def _ts(text: str) -> float:
        """把「2026-09-21 08:05」换成时间戳（本地时区），用来喂给定点判定。"""
        return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))

    stamp, weekday, period = tcfg.time_parts(_ts("2026-09-21 08:05"))
    check("时间文本格式", stamp == "2026-09-21 08:05", stamp)
    want_weekday = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[
        _dt.date(2026, 9, 21).weekday()
    ]
    check("星期换算（tm_wday 索引没错位）", weekday == want_weekday, f"{weekday} / 期望 {want_weekday}")
    check("时段词（08:05 → 早上）", period == "早上", period)

    period_cases = {
        0: "凌晨", 4: "凌晨", 5: "早上", 8: "早上", 9: "上午", 11: "中午", 12: "中午",
        13: "下午", 16: "下午", 17: "傍晚", 18: "傍晚", 19: "晚上", 22: "晚上", 23: "深夜",
    }
    wrong = {h: tcfg.period_of(h) for h, want in period_cases.items() if tcfg.period_of(h) != want}
    check("时段词边界", not wrong, str(wrong))


    # ---------------------------------------------------------- 问候时间点
    print("\n-- 问候时间点解析 --")
    check("08:00", gr.parse_hm("08:00") == (8, 0))
    check("8:00", gr.parse_hm("8:00") == (8, 0))
    check("0800", gr.parse_hm("0800") == (8, 0))
    check("全角冒号", gr.parse_hm("23：30") == (23, 30))
    check("空格容错", gr.parse_hm(" 07:05 ") == (7, 5))
    check("空字符串 = 关闭该时段", gr.parse_hm("") is None and gr.parse_hm(None) is None)
    for bad in ("8点", "25:00", "12:60", "1260", "abc", "12:3x", "-1:00"):
        check(f"非法值被拒：{bad!r}", gr.parse_hm(bad) is None)

    # ---------------------------------------------------------- 到点判定
    print("\n-- 到点判定（喂时间戳，不调模型）--")

    def _clear_greet() -> None:
        gr._state.sent.clear()
        gr._state.attempts.clear()

    st.set_value("greet_morning", "08:00")
    st.set_value("greet_noon", "12:30")
    st.set_value("greet_night", "23:00")
    st.set_value("greet_window", 90)
    st.set_value("greet_enabled", False)
    _clear_greet()
    check("总开关关掉时不发", gr.due_slots(_ts("2026-09-21 08:05")) == [])

    st.set_value("greet_enabled", True)
    check("到点当刻可发", gr.due_slots(_ts("2026-09-21 08:00")) == ["morning"])
    check("窗口内仍可发", gr.due_slots(_ts("2026-09-21 09:30")) == ["morning"])
    check("超窗口不补发", gr.due_slots(_ts("2026-09-21 09:31")) == [])
    check("还没到点不发", gr.due_slots(_ts("2026-09-21 07:59")) == [])
    check("中午只命中「午安」", gr.due_slots(_ts("2026-09-21 12:45")) == ["noon"])
    check("晚上只命中「晚安」", gr.due_slots(_ts("2026-09-21 23:10")) == ["night"])

    # 窗口开大时多段会同时到期；loop 只补最晚的那一个（免得一口气砸三条）
    st.set_value("greet_window", 900)
    _clear_greet()
    pending = gr.due_slots(_ts("2026-09-21 13:00"))
    check("多段同时到期时全部列出", pending == ["morning", "noon"], str(pending))
    check("loop 只补最晚的一个", pending[-1] == "noon")
    st.set_value("greet_window", 90)

    # 已发过 / 尝试次数用尽
    _clear_greet()
    gr._state.mark("morning", "2026-09-21")
    check("当天已发过就不再发", gr.due_slots(_ts("2026-09-21 08:30")) == [])
    check("换一天仍然可发", gr.due_slots(_ts("2026-09-22 08:30")) == ["morning"])
    _clear_greet()
    for _ in range(3):
        gr._state.count_try("morning", "2026-09-21")
    check("尝试次数用尽后放弃（不再烧钱重试）", gr.due_slots(_ts("2026-09-21 08:30")) == [])

    # 留空 / 非法值都视为"该时段不发"，而不是崩掉
    _clear_greet()
    st.set_value("greet_morning", "")
    check("时间点留空则不发", "morning" not in gr.due_slots(_ts("2026-09-21 08:05")))
    st.set_value("greet_morning", "8点")
    check("非法时间点视为关闭", "morning" not in gr.due_slots(_ts("2026-09-21 08:05")))
    st.set_value("greet_morning", "08:00")
    _clear_greet()

    check(
        "手动触发的默认时段按钟点推断",
        gr.current_slot(_ts("2026-09-21 08:00")) == "morning"
        and gr.current_slot(_ts("2026-09-21 13:00")) == "noon"
        and gr.current_slot(_ts("2026-09-21 22:00")) == "night",
    )

    # ---------------------------------------------------------- 问候状态落盘
    print("\n-- 问候状态落盘（重启 / 重建容器不重发）--")
    _clear_greet()
    gr._state.mark("morning", "2026-09-21")
    fresh = gr._State()
    fresh.load()
    check("落盘后新实例能读到", fresh.sent_today("morning", "2026-09-21"))
    check("换一天不算已发", not fresh.sent_today("morning", "2026-09-22"))
    check("状态文件已生成", gr._state.path == TMP / "greet_state.json" and gr._state.path.exists())
    _clear_greet()

    # ---------------------------------------------------------- 问候生成
    print("\n-- 问候生成（打桩，不调 API）--")

    class _FakeMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    class _FakeChoice:
        def __init__(self, content: str) -> None:
            self.message = _FakeMessage(content)

    class _FakeResponse:
        def __init__(self, content: str) -> None:
            self.choices = [_FakeChoice(content)]

    class _FakeClient:
        """假 AsyncOpenAI：completions.create 直接返回写死的文案。"""

        def __init__(self, content: str) -> None:
            self._content = content
            self.calls = 0

            class _Completions:
                def __init__(self, outer: "_FakeClient") -> None:
                    self._outer = outer

                async def create(self, **kwargs):  # noqa: ANN003, ANN201
                    self._outer.calls += 1
                    return _FakeResponse(self._outer._content)

            class _Chat:
                def __init__(self, outer: "_FakeClient") -> None:
                    self.completions = _Completions(outer)

            self.chat = _Chat(self)

    st.set_value("greet_max_chars", 200)
    # 「有没有 Key」改造后看的是**当前档案**，不再是 config.API_KEY ——
    # 所以造一个没密钥的档案来验兜底话术（否则只用本地端点的人永远拿不到问候）。
    from plugins.ai_chat import llm as _llm_mod

    _llm_mod.upsert({"id": "nokey", "label": "无密钥（验证用）", "base_url": "http://127.0.0.1:9/v1",
                     "api_key": "", "api_key_env": "", "model": "m",
                     "vision": False, "tools": False, "logprobs": False})
    _llm_mod.set_active("nokey")
    fallback = asyncio.run(gr.compose("morning"))
    check("档案没配 Key 时用内置话术（到点不能一个字都没有）", "早安" in fallback, fallback)
    _llm_mod.set_active("deepseek")

    fake = patch_chat(_FakeClient("早安主人~今天也要开开心心的呀。"))
    got = asyncio.run(gr.compose("morning"))
    check("模型文案被采用", got.startswith("早安主人"), got)

    st.set_value("greet_max_chars", 20)  # spec 允许的最小值，也算边界
    patch_chat(_FakeClient("早安主人~" * 30))
    got = asyncio.run(gr.compose("morning"))
    check("超长问候按 greet_max_chars 截断", len(got) == 20, f"{len(got)} 字符")
    st.set_value("greet_max_chars", 200)

    patch_chat(_FakeClient(""))
    got = asyncio.run(gr.compose("noon"))
    check("模型返回空则退回兜底话术", "午安" in got, got)

    # ---------------------------------------------------------- 发送通道
    print("\n-- 问候发送通道（打桩 bot）--")
    master = 100000001
    st.set_value("master_qq", master)

    class _GreetBot:
        self_id = "100000002"

        def __init__(self, fail_private: bool = False) -> None:
            self.private: list[tuple] = []
            self.group: list[tuple] = []
            self.fail_private = fail_private

        async def send_private_msg(self, user_id, message):  # noqa: ANN001, ANN201
            if self.fail_private:
                raise RuntimeError("不是好友 / 被限流")
            self.private.append((user_id, message))

        async def send_group_msg(self, group_id, message):  # noqa: ANN001, ANN201
            self.group.append((group_id, message))

    real_get_bots = gr.get_bots

    def _with_bot(bot: "_GreetBot") -> None:
        gr.get_bots = lambda: {"100000002": bot}  # type: ignore[assignment]

    bot = _GreetBot()
    _with_bot(bot)
    st.set_value("greet_target", "private")
    ok, conv = asyncio.run(gr.deliver("早安啦"))
    check("private 走私聊", ok and conv == f"u{master}", f"{conv} / {bot.private}")
    check("私聊是发给主人的", bot.private and bot.private[0][0] == master)

    bot = _GreetBot()
    _with_bot(bot)
    st.set_value("greet_target", "group")
    st.set_value("greet_group", 100000004)
    ok, conv = asyncio.run(gr.deliver("午安啦"))
    check("group 走群聊", ok and conv == "g100000004", conv)
    segs = list(bot.group[0][1]) if bot.group else []
    check(
        "群内问候会 @ 主人",
        any(s.type == "at" and str(s.data.get("qq")) == str(master) for s in segs),
        str(segs),
    )

    # auto：私聊通了就用私聊；私聊失败再退到主人说过话的群
    bot = _GreetBot()
    _with_bot(bot)
    st.set_value("greet_target", "auto")
    ok, conv = asyncio.run(gr.deliver("晚安啦"))
    check("auto 优先私聊", ok and conv == f"u{master}" and not bot.group, f"{conv} / {bot.group}")

    chatlog._logs.pop("g100000004", None)
    glog = ConversationLog("g100000004", TMP / "chatlog_g100000004.json")
    glog.append(master, "魔王", "在吗")
    glog.save()
    bot = _GreetBot(fail_private=True)
    _with_bot(bot)
    st.set_value("greet_group", 0)
    ok, conv = asyncio.run(gr.deliver("晚安啦"))
    check("auto 私聊失败后退回群", ok and conv == "g100000004", f"{conv} / {[g for g, _ in bot.group]}")
    check(
        "退群时同样 @ 主人",
        any(s.type == "at" for s in (list(bot.group[0][1]) if bot.group else [])),
    )

    # ---------------------------------------------------------- greet() 全链路
    print("\n-- greet() 全链路（状态 + 聊天记录）--")
    today = time.strftime("%Y-%m-%d")
    bot = _GreetBot()
    _with_bot(bot)
    st.set_value("greet_target", "private")
    st.set_value("greet_enabled", True)
    st.set_value("greet_allow_skip", False)
    st.set_value("greet_window", 90)
    _clear_greet()
    patch_chat(_FakeClient("早安主人，今天也一起加油~"))

    check("问候发出", asyncio.run(gr.greet("morning")) and bool(bot.private), str(bot.private))
    check("发出后记为今天已发", gr._state.sent_today("morning", today))
    check("当天同一时段不再重复发", "morning" not in gr.due_slots(_ts(f"{today} 08:30")))

    clog = asyncio.run(chatlog.get_log(f"u{master}"))
    check(
        "问候写进了聊天记录（主人接着回话时它知道自己在说什么）",
        any(m.get("is_bot") and "加油" in str(m.get("text")) for m in clog.messages),
    )

    _clear_greet()
    check("手动触发也能发出", asyncio.run(gr.greet("night", force=True)))
    check("手动触发不写「今天已发」（不抢当天的自动问候）", not gr._state.sent_today("night", today))

    # 允许跳过时，模型回 [SKIP] 就真的不发；但仍记成已处理，免得每 30 秒再问一次模型
    _clear_greet()
    st.set_value("greet_allow_skip", True)
    patch_chat(_FakeClient("[SKIP]"))
    check("允许跳过时模型可以拒发", not asyncio.run(gr.greet("noon")))
    check("拒发也记成今天已处理", gr._state.sent_today("noon", today))
    st.set_value("greet_allow_skip", False)

    # 收尾：别把打桩留在模块里，也别把开关留给真环境
    unpatch_chat()
    gr.get_bots = real_get_bots  # type: ignore[assignment]
    st.set_value("greet_enabled", False)
    st.set_value("greet_group", 0)
    st.set_value("greet_target", "auto")
    st.set_value("greet_morning", "08:00")
    st.set_value("greet_noon", "12:30")
    st.set_value("greet_night", "23:00")
    _clear_greet()

    # ---------------------------------------------------- 改自己的身份（真实环境）
    # 桩版（`离线验证_桩.py` §33）已经验过逻辑；这里只验"在真 nonebot 环境里装得起来、
    # 且拿不到接口时不会假装成功" —— 这正是两份脚本的分工。
    from plugins.ai_chat import identity as _ident  # noqa: PLC0415
    from plugins.ai_chat import instructions as _instr  # noqa: PLC0415

    check("identity 能在真环境里导入", callable(_ident.set_avatar))
    check("identity 有 set_qq_avatar / set_qq_profile / set_group_card 三条出口",
          all(callable(getattr(_ident, n)) for n in
              ("set_avatar", "set_nickname", "set_group_card")))
    _ni = asyncio.run(_instr.parse("/昵称", conv=f"u{master}", is_master=True))
    check("真环境里 /昵称 能报出当前名字", _ni.handled and config.bot_name() in _ni.reply,
          _ni.reply)
    _av = asyncio.run(_instr.parse("/头像", conv=f"u{master}", is_master=True))
    check("真环境里没有发消息接口时 /头像 明说改不了（且标成没办成）",
          _av.handled and not _av.ok and "拿不到" in _av.reply, _av.reply)

    print(f"\n全部 {passed} 项验证通过。")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
