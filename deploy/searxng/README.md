# 自建 SearXNG（bot 的搜索后端）

一套**独立于 bot 的 compose 栈**，只通过外部网络 `qq-ai-chat_botnet` 与 bot 互通。

**为什么自建**：公共 SearXNG 实例（searx.be / baresearch.org / priv.au…）在国内服务器上
DNS 只给 IPv6，容器直接报 `Network is unreachable`；而且它们绝大多数把 JSON 输出关了，
连上了也是 403。Tavily 可用但要 key 且按量计费。自建免费、不出境、不依赖第三方配额。

**为什么要独立一份 compose**：升级/重启搜索后端**不能碰 NapCat**。
NapCat 的扫码状态和 `napcat` 容器生命周期绑死，任何 `docker compose down` /
`--force-recreate napcat` 都可能触发重新扫码甚至风控。所以搜索引擎自己一套编排，
只共享网络。

## 文件

| 文件 | 用途 |
|---|---|
| `docker-compose.yml` | 服务定义：`searxng` 容器、`mem_limit 512m`、宿主 `127.0.0.1:8888` |
| `settings.yml` | SearXNG 配置：**开了 json 输出**、**只启用实测能用的引擎** |
| `limiter.toml` | 关掉限流（自用实例；开着会因缺 `X-Forwarded-For` 挡掉 bot 的请求） |
| `.env.example` | 需要的密钥变量模板，复制成 `.env` 并填随机值 |

> `settings.yml` 入库时用的是占位符 `__SECRET_KEY__`。
> 服务器上的那份才是真值，**不要把这个占位符直接部署上去**。

## 落地步骤

```bash
# 1. 建目录（服务器上）
mkdir -p /opt/searxng/config
cd /opt/searxng

# 2. 传这三个配置文件
#    docker-compose.yml → /opt/searxng/docker-compose.yml
#    settings.yml       → /opt/searxng/config/settings.yml
#    limiter.toml       → /opt/searxng/config/limiter.toml

# 3. 生成密钥（写成 .env，compose 读它做 ${SEARXNG_SECRET} 插值）
echo "SEARXNG_SECRET=$(openssl rand -hex 32)" > /opt/searxng/.env
chmod 600 /opt/searxng/.env

# 4. 把 settings.yml 里的占位符换成同一串密钥
sed -i "s|__SECRET_KEY__|$(grep -oP '(?<=SEARXNG_SECRET=).*' .env)|" config/settings.yml

# 5. 起服务（bot 的栈必须先在跑，外部网络才存在）
docker compose up -d

# 6. 验证：必须回 JSON，不能是 403
curl -s "http://127.0.0.1:8888/search?q=鲸落&format=json" | head -c 200
```

bot 那边把端点填成 **容器名**（不是 `127.0.0.1`）：

```
AI_CHAT_SEARCH_ENABLED=true
AI_CHAT_SEARCH_BACKEND=searxng
AI_CHAT_SEARCH_ENDPOINT=http://searxng:8080
```

## 两个必读的坑

### 1. 镜像必须带加速器前缀，且不能用日期 tag

```bash
# ❌ not found —— 直连 Docker Hub 不通
docker pull searxng/searxng:latest
# ❌ 403 —— daocloud 对按日期打的 tag 返回 403
docker pull docker.m.daocloud.io/searxng/searxng:2024.1.1-abc
# ✅
docker pull docker.m.daocloud.io/searxng/searxng:latest
```

### 2. 引擎要按实测挑，默认全开等于搜不到

`settings.yml` 里默认启用一大堆引擎。这台服务器上逐个探过（`q=鲸落`）：

| 引擎 | 结果 |
|---|---|
| **sogou** | ✅ 8 条 |
| **360search** | ✅ 3 条 |
| baidu | ❌ 对机房 IP 返回 `CAPTCHA` |
| wikipedia / wikidata / brave / google cse | ❌ `timeout`（域名不可达） |
| bing / duckduckgo / google / startpage / mojeek / qwant / chinaso / presearch | ❌ 无结果 |

**默认全开时总返回 0 条** —— 出网被墙的引擎先超时，把整次请求拖死。
所以本配置只 `disabled: false` 这两个，其余显式关掉。以后换机房或加代理，
可以逐个打开再测。

## 日常运维

```bash
cd /opt/searxng
docker compose ps                 # 看它活着没
docker compose logs --tail 50     # 看引擎报错
docker compose restart            # 改完 settings.yml 要重启
docker compose up -d              # 升级镜像后
```

> **永远不要**在 `/opt/qq-bot/deploy` 里执行 `docker compose down` ——
> 那会连 NapCat 一起停掉。
