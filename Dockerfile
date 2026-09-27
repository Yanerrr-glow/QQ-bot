# QQ AI 机器人（NoneBot2 + OneBot v11 + DeepSeek）容器镜像
#
# 构建上下文是项目根目录（QQ机器人\），由 deploy/docker-compose.yml 的
# `build.context: ..` 指定，所以下面的 COPY 路径都相对项目根。
#
# 手工构建：
#     cd QQ机器人
#     docker build -t ai-chat-bot:local .
FROM python:3.12-slim

# PYTHONUNBUFFERED：日志实时刷进 docker logs，否则会被缓冲到看不见。
# TZ：必须是上海时区 —— 主动发言的「活跃时间段」是按钟点判断的，
#     容器默认 UTC 会让它整体偏 8 小时（你以为设的 9:00~23:00 其实是 17:00~次日 7:00）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层：requirements.txt 没变时，重建镜像不会重装依赖。
# 国内直连 PyPI 会超时，走阿里云镜像。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
        -i https://mirrors.aliyun.com/pypi/simple/

# ---- 渲染兜底（可选层）------------------------------------------------------
# HTTP 抓不到正文时的最后手段：真浏览器渲染 → 读 DOM；DOM 没字才截图 + OCR。
# 删掉这一段就能退回"纯 HTTP 抓取"的轻量镜像（代码会自动降级，见 render.deps_ready）。
#
# 三部分缺一不可，每一部分都有实测原因：
#   1. fonts-noto-cjk —— **没有中文字体，截图里全是方块**，OCR 一个字都认不出。
#      装完必须 `fc-cache -f`：实测装完 `fc-list` 是空的，刷新后才有 80 个字体。
#   2. libgl1 / libglib2.0-0 / libxcb1 —— OpenCV（rapidocr 的依赖）运行时库。
#      实测缺 `libxcb.so.1` 时 `import cv2` 直接 ImportError。
#   3. Chromium —— 走国内镜像下载（默认源在国内基本下不动，114MB）。
COPY requirements-render.txt ./
RUN apt-get update \
        && apt-get install -y --no-install-recommends \
            fonts-noto-cjk fontconfig libgl1 libglib2.0-0 libxcb1 \
        && rm -rf /var/lib/apt/lists/* \
        && pip install --no-cache-dir -r requirements-render.txt \
            -i https://mirrors.aliyun.com/pypi/simple/ \
        && PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright \
            python -m playwright install chromium

# **这一步不能省，也不能改成"我手动列几个库"**：
#   * `fontconfig` 不装的话 `fc-cache`/`fc-list` 根本不存在 —— 实测字体文件明明在
#     (`/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`)，`fc-list` 却是 0 个，
#     Chromium 因此认不到任何字体（截图里中文会全变方块）；
#   * Chromium 需要 17 个系统库（libnss3 / libatk-1.0 / libcairo / libpango / libcups …），
#     实测自己列 `libgl1 libglib2.0-0 libxcb1` 三个远远不够，启动直接
#     `error while loading shared libraries: libnspr4.so`。
#     `install-deps` 用的是 playwright 自己维护的清单，跟着版本走，比手写靠谱。
RUN python -m playwright install-deps chromium \
        && fc-cache -f >/dev/null 2>&1 || true

# ---- PDF 读取（读文件那条路要用）--------------------------------------------
# 为什么选 PyMuPDF：它是**单个自带 wheel**（24.6MB，manylinux，不依赖 poppler 等系统库），
# 而且**同一个库既能抽文本层又能渲染页面** —— 不用再装 pdf2image + poppler。
# 扫描版 PDF 的 OCR 复用上面那层已有的 rapidocr（无需新增）。
RUN pip install --no-cache-dir pymupdf \
        -i https://mirrors.aliyun.com/pypi/simple/

# 代码。**人格三层文件必须一起进镜像** —— config.py 按项目根解析这三个路径：
#   persona_base.txt     底层人设（只有用户能改）
#   persona_forbidden.txt 禁止事项（只有用户能改）
#   persona_surface.txt  表层人设（只有自动迭代会写）
# 注意：.env 不进镜像（含密钥，由 compose 的 env_file 注入）；
#       data/ 也不进镜像（是运行时数据，由卷挂载）。
COPY bot.py persona_base.txt persona_forbidden.txt persona_surface.txt ./
COPY plugins ./plugins

# 运维脚本常驻镜像：`诊断状态.py` 是只读诊断（只用标准库），进镜像后
# `docker exec ai-chat-bot python /app/_工具链/诊断状态.py` 随时能跑。
# **为什么必须进镜像而不是临时 docker cp**：容器每次重建，手工放进去的路径就没了 ——
# 2026-09-24 排查性格跑偏时正撞上"诊断脚本不在容器里"，只能另写一份临时的。
#
# 两个验证脚本同理常驻，且**必须进镜像**才验得准：
#   * `离线验证.py` —— 全套回归（176 项），验的是"这份镜像能不能装起来"；
#   * `PDF端到端验证.py` —— 扫描版 PDF 那条 OCR 路只有容器里能走通（本机没 rapidocr）。
#     它验的必须是**镜像里这一份** pdf.py：若靠 docker cp 临时塞代码进去，
#     验的就是临时文件而不是交付物（2026-09-24 踩过：差点漏掉一个真缺陷）。
#   * `_搜索查询验证.py` —— 搜索词构造的回归（26 项）。线上那些误搜的句子抄在里面，
#     换模型、改人设都不该让它再退化。
COPY _工具链/诊断状态.py ./_工具链/诊断状态.py
COPY _工具链/离线验证.py ./_工具链/离线验证.py
COPY _工具链/PDF端到端验证.py ./_工具链/PDF端到端验证.py
COPY _工具链/_搜索查询验证.py ./_工具链/_搜索查询验证.py
# 水位线初始化（一次性，幂等）：升级到带水位线的版本后跑一次，把老会话的起点
# 对齐到「最后一次会话切分点」，避免后台补抽把已经抽过的历史重抽一遍。
# 详见 README 5.6.6.1。
COPY _工具链/_水位线初始化.py ./_工具链/_水位线初始化.py
# 记忆库迁移/回滚（一次性，幂等）：memories.json ↔ memory.db。
# 记忆是唯一一份不可再生数据，所以它必须能看见（--dry-run）也能回滚（--to json）。
COPY _工具链/_记忆库迁移.py ./_工具链/_记忆库迁移.py

# 卷挂载点：容器内若没有这个目录，Docker 会用 root 建它，属主可能不对
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "bot.py"]
