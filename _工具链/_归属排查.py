r"""归属排查：用**真实记录**重建当时的 prompt，量出"它是不是读错了谁说的"。

什么时候用：它把自己说过的话安到对话对象头上（或反过来）时。
先读 `分析记录/排查档_机器人分不清自己的发言_2026-09-26.md`，那里有分流表。

跑法（**必须在容器内** —— 要真实 API key + 真实 chatlog）：

    scp '_工具链\_归属排查.py' qqbot:/opt/qq-bot/_工具链/
    ssh qqbot 'docker cp /opt/qq-bot/_工具链/_归属排查.py ai-chat-bot:/tmp/p.py; \
               docker exec -e PYTHONIOENCODING=utf-8 -w /app ai-chat-bot python /tmp/p.py g100000003'

不给会话号时会列出可用的 chatlog 让你挑。

判据：
  · 归属准确率 100% 且自认知三问全对 → 不是归属问题，去查"编造引用"（排查档 §7）
  · 某一边系统性判错 → 渲染层（看上方"背景里含（你）的行数"）
  · 三问答"看不到记录" → 先确认你没踩排查档 §6 的两个陷阱（本脚本已规避）
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import pathlib
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/plugins")

import nonebot  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter  # noqa: E402

nonebot.init()
nonebot.get_driver().register_adapter(OneBotV11Adapter)

from plugins.ai_chat import config, settings  # noqa: E402

_pkg = sys.modules["plugins.ai_chat"]
context = _pkg.context
chatlog = _pkg.chatlog

# 机器人自己的 QQ。chatlog 里每条 `uid` 就是这个号，_speaker 靠它认出"自己"。
# 想换成别的部署，改这一行（或从 .env 的 ACCOUNT 读）。
BOT_QQ = "100000002"

# ⚠ 陷阱二：离线验证会把 read_budget 注入成 300，那会把背景裁光造成假象。
# 这里显式设回真实部署值。
settings.set_value("read_budget", 2000)
settings.set_value("msg_clip", 120)

DATA = "/app/data"
CONVS = sorted(p.split("chatlog_")[-1][:-5]
               for p in glob.glob(os.path.join(DATA, "chatlog_*.json"))
               if ".archive." not in p)

if len(sys.argv) < 2:
    print("可用会话：")
    for c in CONVS:
        n = len(json.load(open("%s/chatlog_%s.json" % (DATA, c), encoding="utf-8")).get("messages") or [])
        print("   %-20s %d 条" % (c, n))
    print("\n用法：python %s <会话号>  例：python %s %s" % (sys.argv[0], sys.argv[0], CONVS[0] if CONVS else "g123"))
    raise SystemExit(0)

CONV = sys.argv[1]
path = pathlib.Path("%s/chatlog_%s.json" % (DATA, CONV))
if not path.exists():
    print("没有这个会话：%s" % path)
    raise SystemExit(1)

log = chatlog.ConversationLog(CONV, path)
log.load()
tail = log.messages[-40:]
if not tail:
    print("这个会话没有记录")
    raise SystemExit(1)
# 全标已读 → 进背景（模拟"刚被叫到"那一刻的取数）
log.messages = [dict(m, read=True) for m in tail]
users = [m for m in log.messages if not m.get("is_bot")]
if not users:
    print("这段里没有别人的发言，换个会话或换一段")
    raise SystemExit(1)
last_user = users[-1]

print("=" * 78)
print("会话 %s ｜ 取最后 %d 条作背景 ｜ 本次要回应：%s：%s"
      % (CONV, len(tail), last_user["name"], (last_user["text"] or "")[:40]))
print("=" * 78)

# ---- 先看渲染层（陷阱一：绝不能替换 m[-1]，只读取） ----
bg = log.render_background(bot_uid=BOT_QQ)
bot_lines = sum(1 for m in log.messages if m.get("is_bot"))
print("渲染检查：")
print("  背景里机器人发言 %d 条 ｜ 背景里出现（你）%d 处 ｜ 出现（可能是你）%d 处"
      % (bot_lines, bg.count("（你）"), bg.count("可能是你")))
if bot_lines and bg.count("（你）") < bot_lines:
    print("  ⚠ （你）数量少于自己的发言数 —— 可能被 read_budget 裁掉了，或 _speaker 判定漏了")
if bg.count("可能是你"):
    print("  ⚠ 有「可能是你」：老记录缺字段或同名，模型已被提示拿不准（不应据此质问对方）")

built, user_text = context.build(
    conv=CONV, log=log, speaker=last_user["name"], question=last_user["text"],
    current_id=None, trigger="addressed", bot_uid=BOT_QQ,
)

# ---- 配对：机器人一句 + 随后群友一句 ----
pairs = []
for i, m in enumerate(tail):
    if m.get("is_bot") and (m.get("text") or "").strip():
        for nxt in tail[i + 1:]:
            if not nxt.get("is_bot") and (nxt.get("text") or "").strip():
                pairs.append((m, nxt))
                break
pairs = pairs[-7:]
print("配对 %d 组（每组 = 自己一句 + 随后群友一句）" % len(pairs))


async def main() -> int:
    # 接口与模型都取自**控制台当前选中的档案**：排查的必须是线上那套配置，
    # 否则查出来的结论跟机器人实际行为对不上。
    from plugins.ai_chat import llm
    total = wrong = 0

    print("\n--- 归属判定 ---（档案 %s / 模型 %s）" % (llm.active_id(), llm.model_name()))
    for b, u in pairs:
        q = (
            "判断下面两句是「鲸鱼娘（也就是你自己）」说的，还是「群里的其他人」说的。"
            '只输出 JSON：{"1":"鲸鱼娘"或"其他人","2":"鲸鱼娘"或"其他人"}\n\n'
            "句子1：%s\n句子2：%s" % ((b["text"] or "").strip(), (u["text"] or "").strip())
        )
        # ⚠ 陷阱一：追加，不替换
        m = list(built) + [{"role": "user", "content": q}]
        try:
            r = await llm.chat(
                messages=m, stream=False,
                response_format={"type": "json_object"}, max_tokens=2000)
            d = json.loads(r.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001
            print("   调用/解析失败：%s" % exc)
            continue
        ok1 = "鲸鱼娘" in str(d.get("1", ""))
        ok2 = "其他" in str(d.get("2", ""))
        total += 2
        wrong += (not ok1) + (not ok2)
        print("   句1(真=自己)→%-6s %s ｜ 句2(真=别人)→%-6s %s"
              % (d.get("1"), "OK" if ok1 else "**错**", d.get("2"), "OK" if ok2 else "**错**"))

    if total:
        print("   → 准确率 %.1f%%（%d/%d）" % (100 * (total - wrong) / total, total - wrong, total))

    print("\n--- 状态认知三问 ---")
    real_bot_last = [m for m in log.messages if m.get("is_bot")]
    n_bot = len(real_bot_last)
    print("   真值：自己发言 %d 条，最后一条 = %r"
          % (n_bot, (real_bot_last[-1]["text"] or "")[:50] if real_bot_last else ""))
    print("   真值：对方最后一条 = %r" % ((last_user["text"] or "")[:50]))
    for q in ("只回答一句：在这段记录里，**你自己**最后说的一句话是什么？原文抄出来。",
              "只回答一句：**对方**最后说的一句话是什么？原文抄出来。",
              "只回答一个数字：这段记录里**你自己**一共发了几条消息？"):
        m = list(built) + [{"role": "user", "content": q}]
        try:
            r = await llm.chat(messages=m, stream=False, max_tokens=3000)
            out = (r.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            out = "调用失败：%s" % exc
        print("   Q: %s" % q)
        print("   A: %s" % out[:300])

    print("\n判读：")
    if total and wrong == 0:
        print("  · 归属判定全对 → 不是归属问题；若确有错认，更像「编造引用」，查排查档 §7 第三行")
    elif total:
        print("  · 有错判 → 看上面渲染检查的（你）计数，以及是否被 read_budget 裁掉")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
