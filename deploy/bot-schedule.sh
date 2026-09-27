#!/bin/bash
# 只对 bot 容器生效的定时开关（夜里关、早上开）。
#
# 为什么单独一个脚本：NapCat 的扫码登录态跟它的容器生命周期绑死，
# 任何多带一个服务名的命令（`docker compose down` / `restart`）都可能把它带下水 ——
# 而"夜里关掉机器人、早上再起来"根本不需要动 NapCat。
# 所以这里**每一条 docker 命令都点名 bot**，napcat 一行都不碰。
#
# 用法：
#   ./bot-schedule.sh start                # 起 bot（重建容器，顺手恢复重启策略）
#   ./bot-schedule.sh stop                 # 停 bot（并关掉自动重启，免得宿主重启把它带回来）
#   ./bot-schedule.sh restart              # 重开 bot
#   ./bot-schedule.sh status               # 看现状
#   ./bot-schedule.sh install [关] [开]    # 装/更新定时器（默认 23:30 关、07:00 开，24 小时制 HH:MM）
#                                          # 某一侧写 `-` = 不装那一侧，例如：install - 07:00
#   ./bot-schedule.sh uninstall            # 卸掉定时器
#
# 定时器（systemd，按服务器本地时间走）：
#   qqbot-bot@.service    模板单元，%i 就是 start / stop / restart
#   qqbot-bot-off.timer   每天 <关> 跑 stop
#   qqbot-bot-on.timer    每天 <开> 跑 start（Persistent：错过了在开机后补跑）
#
# 查看：
#   systemctl list-timers | grep qqbot-bot
#   journalctl -u 'qqbot-bot@*' -n 50
set -u

DEPLOY_DIR="${QQBOT_DEPLOY_DIR:-/opt/qq-bot/deploy}"
CONTAINER="${QQBOT_BOT_CONTAINER:-ai-chat-bot}"
HTTP_PORT="${QQBOT_HTTP_PORT:-8080}"
UNIT_DIR="/etc/systemd/system"
DOCKER="/usr/bin/docker"

cd "$DEPLOY_DIR" || { echo "找不到目录：$DEPLOY_DIR"; exit 1; }

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

# --------------------------------------------------------------------- 动作
wait_console() {
    # 起来之后确认控制台真的在应答 —— "容器 Up" 不等于"应用起来了"
    # （NoneBot 起监听要几秒，插件加载失败时容器还是 Up）。
    local i=0
    while [ "$i" -lt 30 ]; do
        if curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:${HTTP_PORT}/ai/"; then
            echo "控制台已就绪：http://127.0.0.1:${HTTP_PORT}/ai/"
            return 0
        fi
        i=$((i + 1))
        sleep 2
    done
    echo "控制台 60 秒内没应答 —— 看日志：docker compose logs --tail 50 bot"
    return 1
}

cmd_start() {
    echo "[$(date '+%F %T')] 起 bot（NapCat 不动）"
    # --force-recreate 有两个作用：
    #   ① 按 compose 定义把重启策略恢复成 always（stop 时被改成了 no）；
    #   ② 早上这一次是**全新进程** —— 这正是"重启"该有的样子。
    "$DOCKER" compose up -d --force-recreate bot
    wait_console
}

cmd_stop() {
    echo "[$(date '+%F %T')] 停 bot（NapCat 不动）"
    "$DOCKER" compose stop bot
    # 容器本身的重启策略是 always：手动停掉之后它不会自己回来，
    # 但**宿主重启（Docker 守护进程重启）会把它带起来**。"暂时关着"就得把这条也关掉，
    # 早上 start 时再由 --force-recreate 恢复。
    "$DOCKER" update --restart=no "$CONTAINER" >/dev/null
    echo "已停。重启策略改成 no（宿主重启也不会把它带回来）"
}

cmd_status() {
    echo "--- 容器 ---"
    "$DOCKER" compose ps -a
    echo "--- bot 的重启策略 ---"
    "$DOCKER" inspect "$CONTAINER" \
        -f 'RestartPolicy={{.HostConfig.RestartPolicy.Name}} Running={{.State.Running}}' 2>/dev/null || true
    echo "--- 定时器 ---"
    systemctl list-timers --all --no-pager 2>/dev/null | grep -E 'qqbot-bot|NEXT' || echo "（没装定时器）"
}

# --------------------------------------------------------------------- 定时器
check_hhmm() {
    case "$1" in
        [01][0-9]:[0-5][0-9] | 2[0-3]:[0-5][0-9]) return 0 ;;
        *) echo "时间格式不对：$1（要 HH:MM，24 小时制，如 23:30 / 07:00）"; return 1 ;;
    esac
}

write_timer() {
    # $1=单元名前缀 $2=HH:MM $3=动作 $4=英文动词
    local persistent=""
    [ "$3" = "start" ] && persistent="Persistent=true"
    cat > "$UNIT_DIR/$1.timer" <<EOF
[Unit]
Description=$4 QQ Bot container at $2 every day

[Timer]
OnCalendar=*-*-* $2:00
$persistent
Unit=qqbot-bot@$3.service

[Install]
WantedBy=timers.target
EOF
}

cmd_install() {
    local off="${1:-23:30}" on="${2:-07:00}"
    local want_off=1 want_on=1
    [ "$off" = "-" ] && want_off=0
    [ "$on" = "-" ] && want_on=0
    if [ "$want_off" = 1 ]; then check_hhmm "$off" || return 1; fi
    if [ "$want_on" = 1 ]; then check_hhmm "$on" || return 1; fi

    cat > "$UNIT_DIR/qqbot-bot@.service" <<EOF
[Unit]
Description=QQ Bot container: %i (only the bot, never NapCat)
Documentation=file://$DEPLOY_DIR/README.md
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
# %i 是 start / stop / restart，原样透给脚本；脚本里每条命令都只点名 bot
ExecStart=$DEPLOY_DIR/bot-schedule.sh %i
EOF

    if [ "$want_off" = 1 ]; then
        write_timer qqbot-bot-off "$off" stop Stop
    else
        systemctl disable --now qqbot-bot-off.timer 2>/dev/null || true
        rm -f "$UNIT_DIR/qqbot-bot-off.timer"
    fi
    if [ "$want_on" = 1 ]; then
        write_timer qqbot-bot-on "$on" start Start
    else
        systemctl disable --now qqbot-bot-on.timer 2>/dev/null || true
        rm -f "$UNIT_DIR/qqbot-bot-on.timer"
    fi

    systemctl daemon-reload
    local units=""
    [ "$want_off" = 1 ] && units="$units qqbot-bot-off.timer"
    [ "$want_on" = 1 ] && units="$units qqbot-bot-on.timer"
    if [ -n "$units" ]; then
        # shellcheck disable=SC2086
        systemctl enable --now $units
    fi
    echo "定时器已装：$( [ "$want_off" = 1 ] && echo "每天 $off 停、" )$( [ "$want_on" = 1 ] && echo "每天 $on 起" )（服务器本地时间）"
    echo "改了时间就再跑一次 install，例如：$0 install 00:30 08:00；只装启动：$0 install - 07:00"
    cmd_status
}

cmd_uninstall() {
    systemctl disable --now qqbot-bot-off.timer qqbot-bot-on.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/qqbot-bot-off.timer" "$UNIT_DIR/qqbot-bot-on.timer" \
          "$UNIT_DIR/qqbot-bot@.service"
    systemctl daemon-reload
    echo "定时器已卸掉（容器本身没动）"
}

case "${1:-}" in
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_start ;;
    status)    cmd_status ;;
    install)   shift; cmd_install "$@" ;;
    uninstall) cmd_uninstall ;;
    "" | -h | --help | help) usage ;;
    *) echo "不认识的动作：$1"; echo; usage; exit 2 ;;
esac
