#!/bin/bash
# NapCat 看护：发现掉线就自动重启容器，让它走 entrypoint 的快速登录。
#
# 背景：NapCat 被 QQ 风控踢下线后，走的是【内部 Worker 重启】—— 那条路径不经过
# entrypoint.sh，所以 compose 里设的 ACCOUNT 在那一刻不生效，它会退回"等你扫码"
# 并每 2 分钟失败一次，无限循环、不会自愈。重启容器能让 entrypoint 重新执行。
#
# 由 qqbot-watchdog.timer 每 3 分钟调用一次（oneshot）。
#
# 两道保险，避免无意义的重启风暴：
#   COOLDOWN    同一次故障 15 分钟内只重启一次
#   MAX_FAILS   连续 3 次重启都没救回来，就停手 —— 说明登录凭证已作废，
#               这种情况重启再多次也没用，必须人工扫码
set -u

DEPLOY_DIR="${QQBOT_DEPLOY_DIR:-/opt/qq-bot/deploy}"
STATE_DIR="${QQBOT_STATE_DIR:-/var/lib/qqbot-watchdog}"
COOLDOWN=900     # 秒：两次重启之间的最小间隔
MAX_FAILS=3      # 连续失败到几次就停手
WINDOW="4m"      # 看最近多久的日志（要比 timer 间隔长一点）

mkdir -p "$STATE_DIR"
LAST_RESTART="$STATE_DIR/last_restart"
FAIL_COUNT="$STATE_DIR/consecutive_failures"

cd "$DEPLOY_DIR" || { echo "找不到目录：$DEPLOY_DIR"; exit 1; }

# NapCat 的日志里出现这些，说明它当前【没登录】：
#   请扫描下面的二维码  → 退回了扫码模式
#   Login Error         → 登录尝试失败
#   KickedOffLine       → 刚被踢下线
recent="$(docker compose logs --since "$WINDOW" napcat 2>&1 || true)"

if ! printf '%s' "$recent" | grep -qE '请扫描下面的二维码|Login Error|KickedOffLine'; then
    # 一切正常：清掉失败计数，安静退出（不写日志，避免刷屏）
    printf '0' > "$FAIL_COUNT"
    exit 0
fi

fails="$(cat "$FAIL_COUNT" 2>/dev/null || echo 0)"
case "$fails" in ''|*[!0-9]*) fails=0 ;; esac

if [ "$fails" -ge "$MAX_FAILS" ]; then
    # 已经连续救不回来了，静默停手。想重新开始就删掉状态目录：
    #   rm -rf /var/lib/qqbot-watchdog
    exit 0
fi

now="$(date +%s)"
last="$(cat "$LAST_RESTART" 2>/dev/null || echo 0)"
case "$last" in ''|*[!0-9]*) last=0 ;; esac

if [ $((now - last)) -lt "$COOLDOWN" ]; then
    exit 0   # 刚重启过，等冷却
fi

echo "[看护] $(date '+%F %T') 检测到 NapCat 未登录，重启容器（第 $((fails + 1)) 次）"
docker compose restart napcat >/dev/null 2>&1
printf '%s' "$now" > "$LAST_RESTART"
printf '%s' "$((fails + 1))" > "$FAIL_COUNT"

# 给它 25 秒完成快速登录，再看一眼结果
sleep 25
if docker compose logs --since 1m napcat 2>&1 | grep -qE '请扫描下面的二维码|Login Error'; then
    if [ "$((fails + 1))" -ge "$MAX_FAILS" ]; then
        echo "[看护] $(date '+%F %T') 连续 $MAX_FAILS 次重启仍无法登录 ——"
        echo "[看护] 登录凭证很可能已被作废，需要人工扫码。看护已停手。"
        echo "[看护] 恢复后请执行：rm -rf $STATE_DIR   （重置失败计数）"
    else
        echo "[看护] $(date '+%F %T') 重启后仍未登录，稍后冷却期结束会再试"
    fi
else
    echo "[看护] $(date '+%F %T') 重启后已恢复登录"
fi
