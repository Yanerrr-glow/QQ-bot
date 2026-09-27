# 服务器部署（不依赖本地电脑）

把机器人搬到云服务器上 24 小时运行。本目录是**一套可直接照抄的模板**，我不替你买服务器，但照着做能跑通。

> 你还没买服务器，所以这份文档是按「从零开始」写的：买机器 → 装 Docker → 传代码 → 扫码 → 收工。

---

## 1. 先想清楚：为什么必须把 QQ 也搬上去

QQ 机器人**不是纯后端服务**。你的 `bot.py` 只是个"大脑"，它得通过 NapCat 挂在一个**真实登录的 QQ** 上。所以"不依赖本地"= **QQ + NapCat + bot 三样一起上云**，只搬 Python 是没用的。

```
云服务器（Ubuntu 22.04）
│
├─ Docker: napcat ────────── 登录 QQ 小号
│    ├─ 127.0.0.1:6099      NapCat 登录页（扫码用，SSH 隧道访问）
│    └─ 反向 WS 主动连出去 ──┐
│                            │  ws://ai-chat-bot:8080/onebot/v11/ws
└─ Docker: ai-chat-bot ──────┘
     ├─ /app/data           ← 聊天记录 / 表情包 / settings.json（卷）
     └─ 127.0.0.1:8080      Web 控制台（SSH 隧道访问）

安全组：只开 22（SSH）。别的端口一个都不开。
```

**为什么用反向 WS 而不是现在本地的正向 WS？**
本地是 bot 主动连 NapCat（正向）。容器里改成正向也能跑，但有个坑：NapCat 的 WS 服务器默认可能只监听 `127.0.0.1`，同网络的 bot 容器就连不上，得先去 WebUI 把监听地址改成 `0.0.0.0`。反向 WS 没这个问题，而且是 [NapCat 官方文档推荐的 NoneBot 接法](https://doc.napneko.icu/use/integration)。

**bot 端为这个改动做了什么？** 什么都没做 —— 我核对过你本地装的适配器源码（`.venv/Lib/site-packages/nonebot/adapters/onebot/v11/adapter.py:108-119`），它已经注册了 `/onebot/v11/`、`/onebot/v11/ws`、`/onebot/v11/ws/` 三个反向 WS 端点，条件是 driver 支持 `server_app`（你的 `~fastapi` 满足）。所以**不用改一行代码**。

---

## 2. 买机器前先知道的两个风险

### 风险一：QQ 风控（这是最大的运维痛点）

云服务器 IP 属于「异地登录」，容易触发验证，甚至掉登录要重新扫码。**所以挂小号**，别拿主号冒险 —— 主号被冻结的代价和"省一台机器"完全不成比例。

已经帮你做了两件事降低概率：
- compose 里固定了 `mac_address`（这是 NapCat 官方模板的做法，减少"设备变了"的判定）
- QQ 登录态持久化到卷里，重启容器**不用重新扫码**

### 风险二：掉登录需要人工扫码

服务器没有图形界面，只能靠 6099 那个网页扫码。所以**每次掉登录你都得：开 SSH 隧道 → 打开 6099 → 重新扫码**。这是云部署绕不开的成本，心里有数再买。

---

## 3. 部署步骤

### 3.1 服务器准备（Docker + swap + 镜像源）

**① 装 Docker**。阿里云轻量的默认账号就是 `root`，直接：

```bash
apt update
apt install -y docker.io docker-compose-v2
docker --version && docker compose version
```

两行版本号都打出来才算成功。实测 22.04 装出来是 Docker 29.1.3 + Compose 2.40.3，够用。

> 普通用户的话前面加 `sudo`，并 `usermod -aG docker $USER` 后重新登录。

**② 加 1G swap（2G 内存的机器必做）**

2G 的机器 `free -h` 里 `available` 只有 1.2G 左右，而 NapCat 里的 Linux QQ 是吃内存大头（400~900 MB），加上 bot 和 Docker 本身**就没有余量了**。阿里云轻量默认不给 swap —— 不补的话，进几个活跃群就可能被内核 OOM 杀掉，表现是"机器人莫名其妙不回消息"，而且日志里看不出明显原因，极难排查。

```bash
free -h                            # 先看 Swap 是不是 0B
fallocate -l 1G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h                            # Swap 行应变成 1.0Gi
```

（`fallocate` 报错就换 `dd if=/dev/zero of=/swapfile bs=1M count=1024`）

**③ 配镜像源（别跳过）**

这里有个真坑：**阿里云的个人加速器只代理 Docker 官方 library 镜像，第三方镜像它直接回 `not found`**。实测 `docker pull hello-world` 成功、`docker pull mlikiowa/napcat-docker` 报 `not found` —— 第一次部署就是卡在这。

```bash
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'EOF'
{
  "registry-mirrors": [
    "https://<你的ID>.mirror.aliyuncs.com",
    "https://docker.m.daocloud.io",
    "https://docker.1panel.live"
  ]
}
EOF
systemctl restart docker
docker info | grep -A3 "Registry Mirrors"
docker pull hello-world
```

加速器地址在：阿里云控制台 →「容器镜像服务」→「镜像工具」→「镜像加速器」。

**拉 NapCat 时直接写全地址**，别指望 mirror 自动 fallback（各版本行为不一致，已经失败过一次就别赌）：

```bash
docker pull docker.m.daocloud.io/mlikiowa/napcat-docker:latest
docker tag docker.m.daocloud.io/mlikiowa/napcat-docker:latest mlikiowa/napcat-docker:latest
```

镜像约 **2.1 GB**。DaoCloud 不行就换 `docker.1panel.live` 同样处理。

### 3.2 传代码上去

**在本地 Windows 的 PowerShell 里执行**（阿里云轻量用 `root` 登录，把 `你的公网IP` 换掉）：

```powershell
cd <项目根目录>

# 先在服务器上建好目录
ssh root@你的公网IP "mkdir -p /opt/qq-bot"

# 只传运行必需的东西 —— 注意这里没有 .venv、没有 data、没有 .env
scp -r bot.py persona_base.txt persona_forbidden.txt persona_surface.txt `
    requirements.txt Dockerfile .dockerignore plugins deploy `
    root@你的公网IP:/opt/qq-bot/
```

传完**立刻修目录权限**：

```bash
find /opt/qq-bot -type d -exec chmod 755 {} \;
rm -rf /opt/qq-bot/plugins/ai_chat/__pycache__
```

> ⚠️ 权限这条别省。Windows 的 ACL 经 scp 映射后，目录会变成 `drwx---rwx` (0707) —— 而 `deploy/` 要放 `.env`（含密钥）。目录可写意味着**别的用户能删掉你的 `.env`**，`chmod 600` 也挡不住。

> **不要传 `.venv\`**：那是 Windows 的虚拟环境（`Scripts\python.exe`），Linux 上完全不能用。依赖会在镜像里重新装。
>
> **不要传 `deploy\.env`**（如果本地有的话）—— 本地那份是给本地用的（`HOST=127.0.0.1`、正向 WS），服务器要用不同的配置。
>
> 如果 `deploy\napcat\` 已经存在并且很大，`scp -r` 会把它一起传上去，白等很久。传之前确认它是空的，或改用逐项列举的方式。

### 3.3 写配置

```bash
cd /opt/qq-bot/deploy
cp .env.server .env

# 生成两串随机令牌
echo "WEBUI_TOKEN=$(openssl rand -hex 16)"
echo "ONEBOT_ACCESS_TOKEN=$(openssl rand -hex 16)"

nano .env      # 填入上面两串 + DEEPSEEK_API_KEY + ACCOUNT + AI_CHAT_MASTER_QQ
chmod 600 .env # 只有自己能读
```

`.env` 里 5 个必填项：

| 键 | 填什么 |
|---|---|
| `DEEPSEEK_API_KEY` | 你的密钥 |
| `WEBUI_TOKEN` | 上面生成的串 |
| `ONEBOT_ACCESS_TOKEN` | 上面生成的另一串 —— 反向 WS 的握手令牌 |
| `ACCOUNT` | **机器人小号的 QQ 号** —— 见下面的说明 |
| `AI_CHAT_MASTER_QQ` | **你自己的 QQ 号**（不是小号！小号是机器人自己，这个"主人"是你） |

> **`ACCOUNT` 千万别漏，这是第一个大坑。**
> compose 把它当环境变量传给 NapCat，它 `entrypoint.sh` 的结尾是：
> ```bash
> if [ -n "${ACCOUNT}" ]; then
>     gosu napcat /opt/QQ/qq --no-sandbox -q $ACCOUNT   # 快速登录
> else
>     gosu napcat /opt/QQ/qq --no-sandbox                # 等你扫码
> fi
> ```
> 有值才自动快速登录，没值每次重启容器都要重新扫码。
>
> **注意它读的是「环境变量」，不是命令行参数。** 往 compose 的 `command:` 里塞 `-q` 是没用的 ——
> 而且会额外踩一个 Docker 的坑：`command` 是**替换整条 CMD**（不是往后面追加参数），
> 写 `command: ["-q", "机器人小号"]` 会让 Docker 去找一个叫 `-q` 的可执行文件，报
> `exec: "-q": executable file not found in $PATH`。

`HOST=0.0.0.0` 和 `ONEBOT_WS_URLS=[]` 这两项**别改**，原因写在 `.env.server` 的注释里了。

### 3.4 起服务

```bash
cd /opt/qq-bot/deploy
docker compose up -d
docker compose ps            # 两个容器都该是 running
docker compose logs -f bot   # Ctrl+C 退出跟踪
```

第一次会因为拉镜像 + 装依赖慢一点（几分钟）。

### 3.5 扫码登录小号

**先说清楚第二个大坑**：NapCat 的 WebUI 密码**不是**你设的 `WEBUI_TOKEN`。它自己生成一个随机 token 打在启动日志里，而且**一旦生成登录二维码，这个 token 还会再刷新一次**。所以别去猜、别拿 `.env` 里的值试 —— 试几次还会因为 `loginRate: 3`（每分钟只允许 3 次）被锁在外面，一直报 `token is invalid`。

**最省事的做法是根本别进 WebUI，直接扫终端里的二维码：**

```bash
cd /opt/qq-bot/deploy
docker compose restart napcat && docker compose logs -f napcat
```

等 15~20 秒，终端里会画出二维码（还有一行 `二维码解码URL`）。**看到就马上扫** —— QQ 登录二维码只有 **2 分钟**左右有效期。扫之前把终端**最大化**，二维码一折行就扫不出来了。

**找不到/扫不出来时的备选：**

- NapCat 同时把二维码存成了图片：`docker cp napcat:/app/napcat/cache/qrcode.png /opt/qq-bot/`，再在本地 `scp` 下来用图片查看器打开扫（拷完立刻 scp，别拖到过期）。
- 日志里那行 `二维码解码URL`（`https://txz.qq.com/p?k=...`）可以复制到任意二维码生成网站转成图片再扫。

**确实要进 WebUI 的话**（比如想改别的设置），先开隧道并保持不关：

```powershell
ssh -L 6099:127.0.0.1:6099 -L 8080:127.0.0.1:8080 root@你的公网IP
```

浏览器打开日志里那行**完整 URL**（带 `?token=`）。当前 token 也可以直接读文件：

```bash
cat /opt/qq-bot/deploy/napcat/config/webui.json
```

> 隧道的作用：把只绑了回环的端口"搬"到你本地，公网上扫不到。
>
> 登录页报 `token is invalid` 的排查顺序：**先停手等 2 分钟**（限流）→ 再确认 token 是不是被二维码刷新了 → 换无痕窗口试。

### 3.6 把 NapCat 接上 bot

两个办法，**推荐办法二**（不依赖 WebUI，实测更顺）。

**办法一：WebUI 里点**

网络配置 → 新建 → WebSocket 客户端：URL 填 `ws://ai-chat-bot:8080/onebot/v11/ws`，Token 填 `.env` 里的 `ONEBOT_ACCESS_TOKEN`，**勾选启用**。

**办法二：直接写配置文件**（登录成功后 NapCat 会生成 `onebot11_<小号QQ>.json`，config 目录是挂载在宿主上的，改起来很直接）

```bash
cd /opt/qq-bot/deploy
TOKEN=$(sed -n 's/^ONEBOT_ACCESS_TOKEN=//p' .env)
CFG=$(ls napcat/config/onebot11_*.json 2>/dev/null | head -1)
[ -z "$CFG" ] && CFG="napcat/config/onebot11.json"   # 老版本没有账号级文件时用默认配置
echo "写入目标: $CFG"

cat > "$CFG" << EOF
{
  "network": {
    "httpServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [
      {
        "name": "to-bot",
        "enable": true,
        "url": "ws://ai-chat-bot:8080/onebot/v11/ws",
        "messagePostFormat": "array",
        "reportSelfMessage": false,
        "reconnectInterval": 5000,
        "token": "$TOKEN",
        "debug": false,
        "heartInterval": 30000
      }
    ]
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false
}
EOF
docker compose restart napcat
```

几个容易漏的点：

| 项 | 为什么要紧 |
|---|---|
| `enable: true` | 最容易忘，false 的话配了也不生效 |
| `messagePostFormat: "array"` | NoneBot 的 OneBot 适配器要这个格式 |
| `url` 里的 `ai-chat-bot` | 是 compose 里的容器名，同网络内直接解析，不需要 IP |

### 3.7 验证

```bash
docker compose logs -f bot
```

看到 `Bot <你的小号QQ> connected` 就成了。然后在 QQ 里私聊小号，或者在群里 @ 它试一句。

顺便验证 Web 控制台（隧道还开着的话）：**http://127.0.0.1:8080/ai/**

---

## 4. 把本地的「调参成果」搬过去（可选但推荐）

以下几样是**你本地已经调好的东西**，不搬过去的话服务器上就是默认值：

| 路径 | 带不带 | 说明 |
|---|---|---|
| `data\settings.json` | **强烈建议带** | 44 个参数里你改过的值都在这。它优先级高于 `.env` |
| `data\chatlog_*.json` | 想保留聊天记忆就带 | 群聊记录 |
| `data\stickers\` | 想保留表情包库就带 | 表情包原图（注意来源与版权） |
| `persona_base.txt` / `persona_forbidden.txt` / `persona_surface.txt` | 已经会带 | 见 3.2 的 scp 命令 |
| `.env` | **不要带** | 用 `.env.server` 在服务器上重新生成 |
| `.venv\` | **不要带** | Windows 的，Linux 用不了 |
| `_工具链\` | 不要带 | 是 Windows 本机脚本，服务器上用不上。镜像里另有几个脚本走 Dockerfile 的 COPY |

传 `data` 之前先停掉本地机器人（否则可能在写盘），然后：

```powershell
cd <项目根目录>
scp -r data root@你的公网IP:/opt/qq-bot/
```

> ⚠️ `data` 里的 `settings.json` 是**配置，不是记忆**。清记忆的时候别把它删了 —— 这是之前踩过的坑。

---

## 5. 安全清单

| 项 | 状态 |
|---|---|
| Web 控制台 8080 | 只绑 `127.0.0.1`，走 SSH 隧道 |
| NapCat 登录页 6099 | 只绑 `127.0.0.1`，走 SSH 隧道 |
| 云安全组 | 只开 22，其余全关 |
| `.env` | `chmod 600`，不进镜像（`.dockerignore` 已排除），不入版本库 |
| NapCat WebUI | 已设 `WEBUI_TOKEN`，不是默认的 `napcat` |
| 反向 WS | 已设 `ONEBOT_ACCESS_TOKEN`，防止别人连你的 bot 端点 |

**为什么这么在意？** 你的 Web 控制台**没有任何认证**（我 grep 过 `auth`/`token`/`password`，零命中）。如果 `PORT` 暴露公网，别人能：看全部群聊记录、改你的配置、把 DeepSeek 额度刷光、**以你的 QQ 身份发言**。所以 compose 里两个端口都写死了 `127.0.0.1:` 前缀 —— **不要删掉那个前缀**。

---

## 6. 日常运维

```bash
cd /opt/qq-bot/deploy

docker compose logs -f bot          # 看日志
docker compose restart bot          # 改完 .env 后重启
docker compose up -d --build bot    # 改了代码后重建
docker compose down                 # 全停（登录态不会丢，在卷里）
docker compose ps                   # 看状态
```

**改参数**：不用动命令行。隧道开着的话，浏览器进 `http://127.0.0.1:8080/ai/` 改，即时生效。

### 6.1 改了代码后怎么只更新 bot（不碰 QQ 登录）

**关键：只重建 `bot` 容器，永远不要动 `napcat`。**

```bash
cd /opt/qq-bot/deploy
docker compose up -d --build bot      # 只重建 bot
docker compose ps                     # napcat 的 Up 时长应当没有归零
```

bot 是**反向 WS 的服务端**，NapCat 是客户端（`reconnectInterval: 5000`），所以 bot 重建的十几秒里
NapCat 自己会重连 —— 不掉线、不重新登录、设备特征不变，**不增加风控暴露**。
真正有登录风险的操作只有 `restart napcat` / `down` / `--force-recreate napcat`，日常更新用不到。

传代码时**只传代码**：`scp -r plugins bot.py requirements.txt Dockerfile root@IP:/opt/qq-bot/`。
不要传 `data/`（那是服务器的运行数据 + 你的调参成果）、不要传本地的 `.env`（正向 WS 版）。

**备份**：整个 `data/` 目录 + `deploy/napcat/` 就是全部状态，打包拷走即可。

```bash
tar czf ~/qqbot-backup-$(date +%F).tar.gz -C /opt/qq-bot data deploy/napcat
```

**受资源限制**：2C2G 足够。聊天记录会一直长，`AI_CHAT_MAX_MESSAGES=0` 表示不限条数，长期跑建议改成比如 5000，或在控制台里调。

## 6.5 掉线自动恢复（看护任务）

**为什么需要它** —— 这是【实测】踩出来的一个结构性缺陷：

NapCat 被 QQ 风控踢下线后，走的是**内部 Worker 重启**，那条路径**不经过 `entrypoint.sh`**，所以 compose 里设的 `ACCOUNT` 在那一刻不生效 —— 它会退回"等你扫码"，然后每 2 分钟失败一次、**无限循环且不会自愈**。实测被踢一次就空转了两小时，没人知道。

`watchdog.sh` + systemd timer 每 3 分钟检查一次，发现掉线就自动 `restart` 容器（重启会让 entrypoint 重新执行，`ACCOUNT` 重新生效）。

**安装**（服务器上执行）：

```bash
cd /opt/qq-bot/deploy
chmod +x watchdog.sh
sed -i 's/\r$//' watchdog.sh          # 从 Windows 传上来的话，先转行尾

cp qqbot-watchdog.service /etc/systemd/system/
cp qqbot-watchdog.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now qqbot-watchdog.timer

# 手动跑一次看效果（一切正常时它不会有任何输出）
systemctl start qqbot-watchdog.service
journalctl -u qqbot-watchdog -n 30
```

**两道保险**（避免重启风暴）：

| 参数 | 默认 | 作用 |
|---|---|---|
| `COOLDOWN` | 900 秒 | 同一次故障 15 分钟内只重启一次 |
| `MAX_FAILS` | 3 | 连续 3 次重启都救不回来就**停手** —— 说明登录凭证已作废，重启多少次都没用，必须人工扫码 |

**看护停手之后**（journalctl 里会写明"需要人工扫码"）：扫一次码恢复，然后重置计数：

```bash
rm -rf /var/lib/qqbot-watchdog
```

**看护能做什么、不能做什么**：

| 情况 | 看护的表现 |
|---|---|
| 网络抖动、容器假死 | ✅ 几分钟内自动恢复，你无感 |
| 被风控踢下线、**凭证还有效** | ✅ 自动重启 + 快速登录，几分钟恢复 |
| 被风控踢下线、**凭证已作废**（如"风险设备"下线） | ⚠️ 试 3 次后停手，**仍需你人工扫码** —— 这种情况看护只能帮你"尽快发现"，不能替你解决 |

---

> **关于"为什么会被踢"**：机房 IP + Linux QQ 客户端被腾讯判为"风险设备"是**平台策略，改配置挡不住**。能降低频率的做法：让小号在手机上正常活跃一段时间"养号"、别频繁重建容器（每次重建设备特征都会变）、别点"下线所有设备"。

---

## 6.6 搜索后端：自建 SearXNG（**另一套 compose，别混进来**）

联网搜索需要一个能出 JSON 的 SearXNG 实例。公共实例在国内服务器上不可用
（DNS 只给 IPv6 + 大多关了 JSON 输出），所以在**同一台服务器上又开了一个独立容器**。

| | bot 栈 | 搜索栈 |
|---|---|---|
| 位置 | `/opt/qq-bot/deploy/docker-compose.yml` | `/opt/searxng/docker-compose.yml` |
| 项目名 | `qq-ai-chat` | `searxng` |
| 互相可见 | 通过外部网络 `qq-ai-chat_botnet` | 同左 |

**两套编排刻意分开**：升级或重启搜索后端**绝不能碰 NapCat** ——
扫码状态与 `napcat` 容器绑定，一次误操作就要重新扫码、甚至触发风控。

配置模板在本仓库的 `deploy/searxng/`（含 `settings.yml` 与引擎实测结论）。
完整落地步骤、两个必踩的坑（镜像 tag、引擎全开导致 0 结果）见
[`searxng/README.md`](searxng/README.md)。

bot 侧对应配置：

```
AI_CHAT_SEARCH_ENABLED=true
AI_CHAT_SEARCH_BACKEND=searxng
AI_CHAT_SEARCH_ENDPOINT=http://searxng:8080     # 容器名，不是 127.0.0.1
```

> **改完代码只重建 bot**，搜索栈不用管；反过来重启搜索栈也不需要动 bot。

---

## 7. 排错

> 下面带 **【实测】** 的，都是第一次真实部署时踩到过的坑，不是推测。

| 现象 | 原因 | 处理 |
|---|---|---|
| **【实测】** 容器里 `DEEPSEEK_API_KEY` 有值（35 字符），机器人却照旧回「我还没拿到 API Key」 | **NoneBot 的 Config 是 `extra="allow"`：自定义字段（`deepseek_api_key`、`ai_chat_*`）只从 `.env` **文件**读，环境变量里的同名键进不了 extra**。而 Dockerfile 故意不 COPY `.env`（防密钥进镜像），容器里只有环境变量、没有文件 → 所有自定义配置静默退回默认值 | 给 bot 挂 `./.env:/app/.env:ro`（见 `docker-compose.yml` 的注释），再 `docker compose up -d --force-recreate bot` |
| **【实测】** `docker pull mlikiowa/napcat-docker` 报 `not found` | 阿里云个人加速器**只代理 Docker 官方 library 镜像**，第三方镜像直接回 404 | 用全地址拉：`docker pull docker.m.daocloud.io/mlikiowa/napcat-docker:latest`，再 `docker tag` 回标准名（见 3.1 ③） |
| **【实测】** 每次重启容器都要重新扫码 | `.env` 里没设 `ACCOUNT` | 设上机器人小号的 QQ 号。它 `entrypoint.sh` 读的是**环境变量**，不是命令行 `-q` 参数 |
| **【实测】** `docker compose up` 报 `exec: "-q": executable file not found in $PATH` | `command:` 是**替换整条 CMD**，不是往后面追加参数 | 删掉 `command:`，改用 `ACCOUNT` 环境变量 |
| **【实测】** 改完 compose 却输出 `Container napcat Running` | compose 认为配置没变，**容器根本没重建**，改动没生效 | `docker compose up -d --force-recreate napcat` |
| **【实测】** 登录页一直 `token is invalid` | NapCat 的 WebUI 密码**不是**你设的 `WEBUI_TOKEN`，是它自己生成的；**生成登录二维码时还会刷新一次**；且 `loginRate: 3` 每分钟只允许试 3 次 | 从日志 `grep -i "webui token"` 或 `napcat/config/webui.json` 读当前值；先**停手等 2 分钟**破限流，再换无痕窗口 |
| **【实测】** 二维码扫不出来 / 已过期 | 终端折行导致图形错位，或超过 **2 分钟**有效期 | 把终端**最大化**；或 `docker cp napcat:/app/napcat/cache/qrcode.png /opt/qq-bot/` 拿图片扫；也可用日志里的 `二维码解码URL` |
| **【实测】** 传上去的目录权限是 `drwx---rwx` (0707) | Windows 的 ACL 被 `scp` 映射成了 Unix 权限（文件正常，只有目录坏） | `find /opt/qq-bot -type d -exec chmod 755 {} \;` |
| **【实测】** 机器人莫名不回消息、容器却还在 | 内存被 OOM 杀了 —— 2G 的机器没有余量 | 加 1G swap（见 3.1 ②），这是最容易被忽略的一步 |
| **【实测】** `docker compose logs -f` 之后敲命令没反应 | 终端被实时跟踪模式占住了，输入全被日志刷掉 | `Ctrl+C` 退出；实在不行关窗口重开（**不影响**已登录的 NapCat，登录态在卷里） |
| 控制台 8080 打不开 | 容器里 `HOST` 写成了 `127.0.0.1` | 改成 `0.0.0.0`，`docker compose up -d` |
| bot 日志每 3 秒一条 ERROR 重连 | `ONEBOT_WS_URLS` 没清空，还在连正向端口 | 设成 `[]` |
| NapCat 连不上 bot，报 403 | 两端 token 不一致 | 检查 `ONEBOT_ACCESS_TOKEN` 与配置文件里填的是否一模一样 |
| NapCat 连不上 bot，找不到主机 | 容器名写错 / 不在同一网络 | URL 必须是 `ws://ai-chat-bot:8080/onebot/v11/ws`；`docker exec napcat getent hosts ai-chat-bot` 一测便知 |
| 日志时间差 8 小时、主动发言时段不对 | 容器时区是 UTC | Dockerfile 里的 `TZ=Asia/Shanghai` 别删 |
| 改了 `.env` / 控制台参数不生效 | 没重启容器，或该项本就需要重启 | `docker compose restart bot` |
| 浏览器打不开 6099 | SSH 隧道窗口关了（那窗口看着"卡住"是正常的） | 重开 `ssh -L 6099:127.0.0.1:6099 root@IP` |
| 群里 @ 它没反应 | NapCat 掉登录了（风控/异地） | 重开隧道 → 6099 → 重新扫码 |
| 中文人设乱码 | `persona_*.txt` 传成了非 UTF-8 | 确认文件是 UTF-8 无 BOM |

---

## 8. 备选方案：不用 Docker 跑 bot（systemd）

如果你更想直接在服务器上跑 Python（比如想随时改代码不用 rebuild），可以只把 **NapCat 放 Docker**，bot 用 systemd 裸跑：

```bash
# 1. 只起 NapCat
cd /opt/qq-bot/deploy
# 编辑 docker-compose.yml，把 bot 服务整段注释掉
docker compose up -d napcat

# 2. 建虚拟环境（用 Linux 的，不是传上来的那个）
sudo apt install -y python3-venv
cd /opt/qq-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 3. 建专用账号
sudo useradd -r -s /usr/sbin/nologin -d /opt/qq-bot qqbot
sudo chown -R qqbot:qqbot /opt/qq-bot

# 4. 装服务
sudo cp deploy/ai-chat-bot.service /etc/systemd/system/qqbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now qqbot
journalctl -u qqbot -f
```

这种模式下 bot 跑在**宿主**上，NapCat 在容器里，所以：

- **改走正向 WS 更简单**：`.env` 里改 `ONEBOT_WS_URLS=["ws://127.0.0.1:6700"]`，并打开 compose 里注释掉的 `6700` 端口映射。
- `HOST` 要改回 `127.0.0.1`（不在容器里了，不需要 `0.0.0.0`）。

> systemd 单元文件**必须用 LF 换行**。如果是从 Windows 传过去的，先 `dos2unix /etc/systemd/system/qqbot.service`（或 `sed -i 's/\r$//' 文件`），否则 systemd 会因为行尾的 `\r` 找不到解释器。

---

## 9. 这套模板改了什么、没改什么

**新增（全部在本目录 + 项目根，不影响你在本地继续用）**

| 文件 | 作用 |
|---|---|
| `Dockerfile` | bot 镜像。含 `TZ=Asia/Shanghai`（关键） |
| `.dockerignore` | 挡住 `.env`/`data`/`.venv`，防止密钥和数据混进镜像 |
| `deploy/docker-compose.yml` | NapCat + bot 编排 |
| `deploy/.env.server` | 服务器配置模板（反向 WS 版） |
| `deploy/ai-chat-bot.service` | systemd 单元（备选方案用） |
| `deploy/watchdog.sh` + `deploy/qqbot-watchdog.{service,timer}` | NapCat 掉线自动恢复（见 6.5） |
| `deploy/searxng/` | 自建搜索后端（另一套 compose，见 6.6） |
| `deploy/README.md` | 本文档 |

**没改**：`bot.py` 与 `plugins/` 里的任何代码。本地那套 `.env` + `_工具链\*.ps1` 照旧能用 —— 服务器和本地是两条独立的配置路径。

> 后来另有一次**功能**改动（时间感知 + 定时问候，新增 `plugins/ai_chat/greetings.py`、
> `webui.py` 加了「现在问候一次」、`settings.py` 加了 10 个可调项），与部署模板无关，
> 但更新方式同上：`up -d --build bot`，不碰 NapCat。
