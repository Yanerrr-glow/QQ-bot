"""上手自检：把「初始的验证 / 检测 / 配置」收成一条命令。

第一次拿到这个项目就跑它；之后改了配置、换了机器、机器人不听话时也跑它。
**只用标准库** —— 它要在"依赖还没装"的状态下也能跑出结论。

## 查什么

| 段 | 内容 | 能不能自动修 |
|---|---|---|
| 1 | 项目定位（三级查找：命令 → 向上 → 附近；都失败就**问路径**） | 会就地询问仓库位置 |
| 2 | 运行环境：系统 Python、`.venv`、关键依赖 | 打印出该敲的命令 |
| 3 | 配置：`.env` 与必填项、人格三文件、`AI_CHAT_BOT_NAME` 与人设是否**同名** | 生成 `.env` 模板，并**就地问 Key / 主人 QQ / 角色名** |
| 4 | 端口：NapCat 的 OneBot WS（**按 `.env` 里配的地址探**，不写死）与控制台端口 | — |
| 5 | 连通性：DeepSeek 可达性 + Key 实测 + **配置的模型名是否存在** | — |
| 6 | 代码完整性：语法检查 / 桩测试（`--deep`，约 1~2 分钟） | — |

## 用法

```powershell
python '_工具链\\启动\\上手自检.py'            # 检测 + 报告
python '_工具链\\启动\\上手自检.py' --fix      # 顺带做能自动做的（复制 .env、建 data/）
python '_工具链\\启动\\上手自检.py' --deep     # 再跑一遍语法检查与桩测试
python '_工具链\\启动\\上手自检.py' --offline  # 不联网（只查本地）
```

退出码：**0 = 可以启动**；1 = 存在阻塞项。`_工具链\\启动\\自检.ps1` 是它的 Windows 薄壳，
`启动机器人.ps1` / `一键启动.ps1` 都靠这个退出码决定要不要放行。

在真终端里它还会**就地问缺的简单项**（API Key / 主人 QQ / 角色名）并写回 `.env`，
而且**先过校验再落盘** —— 把文件路径粘进「API Key」那一栏会被拒绝并要求重填
（模拟"从 Release 下载 exe → 双击"时实测踩到过：不校验就照单写进了 `.env`）。
被重定向 / 管道 / CI 时绝不提问；`--no-input` 可彻底关掉交互。

## 为什么不是又一份 PowerShell 脚本

原来体检是 `自检.ps1` 独有的一份实现，只覆盖上面第 2~5 段的一部分，而且 PowerShell 在
Linux 上跑不了。现在**实现只有这一份**，`自检.ps1` 退化成三行薄壳 —— 同一个判据不需要
维护两遍（两份实现必然漂移，这个仓库已经吃过一次亏）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

# 控制台可能只有 GBK：一律用 ASCII 标签，不要用 ✅❌⚠ 这类字符
# （这个仓库实测被它坑过一次：编码失败会让整条日志/整行输出直接丢掉）。
OK, WARN, BLOCK, INFO = "[OK]  ", "[警告]", "[阻塞]", "[信息]"

results: list[tuple[str, str, str]] = []   # (级别, 标题, 补充)


def init_console() -> None:
    """把输出编码钉死，别让「控制台」和「管道」各说各话。

    * 真控制台上什么都不用做：Windows 下 Python 走 WriteConsoleW，中文一定对。
    * 被重定向 / 管道时，Python 改用系统区域编码（中文 Windows 上是 cp936），
      而抓它的人常按 UTF-8 解 —— 于是乱码。统一成 UTF-8。

    打包成 exe 之后这一点更要紧：它在控制台与管道之间切换时**没有 `.py` 那份
    上层脚本帮忙设环境变量**，只能自己钉。
    """
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


def say(level: str, title: str, detail: str = "") -> None:
    results.append((level, title, detail))
    line = f"  {level} {title}"
    if detail:
        line += f" —— {detail}"
    print(line)


def head(text: str) -> None:
    print()
    print(f"=== {text} ===")


# --------------------------------------------------------------------- 项目定位
def _looks_like_root(p: Path) -> bool:
    try:
        return (p / "bot.py").is_file() and (p / "plugins").is_dir()
    except OSError:
        return False


def find_root(cli_root: str | None) -> tuple[Path | None, list[str]]:
    """找到项目根：含 `bot.py` 且含 `plugins/`。返回（根, 找过的地方）。

    为什么要找而不是直接算：打包成 exe 之后 `__file__` 指向解包出来的临时目录，
    靠它反推项目位置必然错。

    三级查找，第三级是模拟"从 Release 下载 exe 后双击"时补上的：

    1. 命令行 `--root`；
    2. **向上**：exe 所在目录 / 脚本所在目录 / 当前目录，各自最多四层；
    3. **在附近找**（平级与下一层）—— 用户常常把 exe 下到 `Downloads\\`，而把仓库
       解压成 `Downloads\\QQ_bot\\` 或就放在旁边。不找的话他会看到
       「找不到项目根，用 --root 指定」，而双击的人**根本没法传参数**。
    """
    starts: list[Path] = []
    if cli_root:
        starts.append(Path(cli_root).expanduser())
    if getattr(sys, "frozen", False):
        starts.append(Path(sys.executable).resolve().parent)
    starts.append(Path(__file__).resolve().parent)
    starts.append(Path.cwd())

    tried: list[str] = []

    def note(p: Path) -> None:
        s = str(p)
        if s not in tried:
            tried.append(s)

    # ① 向上
    for start in starts:
        p = start.resolve()
        for _ in range(4):
            note(p)
            if _looks_like_root(p):
                return p, tried
            if p.parent == p:
                break
            p = p.parent

    # ② 平级与下一层（目录太大就跳过，别去遍历整个 C:\）
    # **只从「用户看得见的位置」扫**：命令行 / exe 所在目录 / 当前目录。
    # 特意排除 `__file__` 那一项 —— 打包后它指向 `%TEMP%\_MEIxxxx` 解包目录，
    # 扫它的父目录等于去扫整个 `%TEMP%`，那里可能躺着**别的**项目副本，会认错。
    nearby: list[Path] = []
    if cli_root:
        nearby.append(Path(cli_root).expanduser())
    if getattr(sys, "frozen", False):
        nearby.append(Path(sys.executable).resolve().parent)
    nearby.append(Path.cwd())
    for start in nearby:
        base = start.resolve()
        for around in (base, base.parent):
            try:
                children = sorted(c for c in around.iterdir() if c.is_dir())
            except OSError:
                continue
            if len(children) > 500:
                continue
            for child in children:
                if _looks_like_root(child):
                    note(child)
                    return child, tried
    return None, tried


# --------------------------------------------------------------------- .env
def parse_env(path: Path) -> dict[str, str]:
    """极简 .env 解析：`KEY=VALUE`，忽略注释与空行，去掉两侧引号。

    刻意不用 python-dotenv —— 这一段的全部意义就是"依赖还没装时也能读配置"。
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 10:
        return secret[:3] + "..."
    return f"{secret[:6]}...{secret[-4:]}"


def ask(prompt: str, hint: str = "") -> str:
    """就地问一项。**只在真终端里调用** —— 管道/重定向下 `input()` 会拿到 EOF。"""
    if hint:
        print(f"        （{hint}）")
    try:
        return input(f"  {prompt}：").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def ask_until(prompt: str, hint: str, validate, tries: int = 3) -> str:
    """问一项：**先过校验再返回值**，不合法就再问一次。

    为什么必须校验：这些值会被直接写进 `.env` 并生效。模拟"从 Release 下载 exe →
    双击"时实测踩到过 —— 用户在「请填 API Key」那里顺手粘了一个**文件路径**，
    脚本照单写进了 `.env`，配置里于是多了一条看着像 Key 的垃圾。
    """
    for i in range(tries):
        got = ask(prompt if i == 0 else prompt + "（重试）", hint)
        if not got:
            return ""
        ok, why = validate(got)
        if ok:
            return got
        print(f"  这个值不对：{why}")
        hint = why
    return ""


def _valid_key(v: str) -> tuple[bool, str]:
    if len(v) < 16:
        return False, "太短了 —— DeepSeek 的 Key 形如 sk- 加一长串"
    if re.search(r"[\s\\/]", v):
        return False, "不该含空格或斜杠 —— 看着像文件路径，不是 Key"
    return True, ""


def _valid_qq(v: str) -> tuple[bool, str]:
    if not v.isdigit():
        return False, "应该只有数字（QQ 号）"
    if len(v) < 5:
        return False, "太短了，QQ 号一般 5 位以上"
    return True, ""


def _valid_name(v: str) -> tuple[bool, str]:
    if re.search(r"[\s\\/]", v):
        return False, "不该含空格或斜杠"
    return True, ""


def write_env(path: Path, key: str, value: str) -> bool:
    """把 `KEY=VALUE` 写回 `.env`：**已有的行就地替换**，没有才追加。

    就地替换而不是重写整个文件：`.env` 里全是注释与调参说明，重写等于把它们抹掉。
    编码失败（文件不是 UTF-8）时**拒绝写入并返回 False** —— 宁可让用户自己改，
    也不能把一个本来是好的配置文件覆盖成乱码。
    """
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        say(WARN, f"没法自动写入 {key}", f"{type(exc).__name__}：请手工编辑 .env")
        return False
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        if lines and lines[-1] == "":
            lines.insert(len(lines) - 1, f"{key}={value}")
        else:
            lines.append(f"{key}={value}")
    path.write_text(eol.join(lines), encoding="utf-8", newline="")
    return True


def width(text: str) -> int:
    """显示宽度：中日韩全角字符按 2 列算 —— 摘要表用它对齐。

    按 `len()` 补空格在中文上必然错位（`f"{'模型':<8}"` 只补 6 个空格，
    但中文一个字符就占两列）。
    """
    return sum(2 if ("\u1100" <= ch <= "\u115f" or "\u2e80" <= ch <= "\ua4cf"
                     or "\uac00" <= ch <= "\ud7a3" or "\uf900" <= ch <= "\ufaff"
                     or "\ufe30" <= ch <= "\ufe6f" or "\uff00" <= ch <= "\uff60")
               else 1 for ch in text)


def pad(text: str, cols: int) -> str:
    return text + " " * max(0, cols - width(text))


def read_overrides(root: Path) -> dict[str, object]:
    """控制台改过的值存在 `data/runtime/settings.json`，**优先级高于 `.env`**。

    不看它就会误报：一个在控制台里配好主人 QQ 的实例，`.env` 里那一项仍是空的。
    """
    path = root / "data" / "runtime" / "settings.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def spec_default(root: Path, key: str) -> str:
    """从 `settings.py` 的 `_SPECS` 表里取某个键的**代码默认值**（原文 token）。

    为什么需要：`.env` 与控制台都没设时，生效的是代码默认值 —— 而副本里的默认值是
    **脱敏占位**（`master_qq` 之类）。不报出来的话，用户会以为"没配也能用"。
    解析失败一律返回空串，绝不让它把检查弄挂。
    """
    path = root / "plugins" / "ai_chat" / "settings.py"
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # 默认值有两种形态：带引号的字符串、以及裸 token（数字 / True / None / []）。
    # **字符串里可能含逗号**（`"大肥鱼,肥鱼,小鲸鱼,鲸鱼娘"`）—— 用 `[^,]+` 取会被截断，
    # 于是"唤醒词里没有角色名"这类判断会误报。所以先整体吃掉带引号的那种。
    m = re.search(
        r'Spec\(\s*"%s"\s*,\s*[^,]+,\s*"[^"]*"\s*,\s*("(?:[^"\\]|\\.)*"|[^,]+),'
        % re.escape(key), src)
    if not m:
        return ""
    token = m.group(1).strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        token = token[1:-1]
    return token


def effective(root: Path, env: dict[str, str], spec_key: str, env_key: str,
              overrides: dict[str, object]) -> tuple[str, str]:
    """算出一个配置项的**生效值**，并说明它从哪来（控制台 / .env / 代码默认）。"""
    if spec_key in overrides and overrides[spec_key] not in (None, ""):
        return str(overrides[spec_key]), "控制台"
    if env.get(env_key, "").strip():
        return env[env_key].strip(), ".env"
    token = spec_default(root, spec_key)
    if token in ("", "None"):
        return "", "未配置"
    return token.strip("\"'"), "代码默认"


# --------------------------------------------------------------------- 外部命令
def run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    # **必须给子进程指定 UTF-8**：它的输出是被管道抓回来的，此时 Python 会用系统
    # 区域编码（中文 Windows 上是 cp936）打印，而父进程按 UTF-8 解码 —— 不指定就乱码。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=env,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, "命令不存在"
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）"
    except OSError as exc:
        return 126, f"无法执行：{exc}"


def find_system_python() -> tuple[str, str] | None:
    """返回 (命令, 版本)。Windows 优先 `py -3`（它绕开 Microsoft Store 的假 python3）。"""
    candidates: list[list[str]] = []
    if os.name == "nt":
        candidates = [["py", "-3", "--version"], ["python", "--version"]]
    else:
        candidates = [["python3", "--version"], ["python", "--version"]]
    for cmd in candidates:
        code, out = run(cmd, timeout=30)
        if code == 0 and out.strip():
            return " ".join(cmd[:-1]), out.strip().splitlines()[0].strip()
    return None


# --------------------------------------------------------------------- 各段
def check_env(root: Path, venv_py: Path) -> bool:
    """返回「venv 里的依赖是否齐全」——第 6 段的子进程优先用它。"""
    head("2. 运行环境")
    usable = False
    # 用 Docker 部署时本机本来就不需要 Python 与 venv —— 那种情况报成"阻塞"会把
    # 部署者引到一条错误的路上（去装一个永远不会用的环境）。
    docker_deploy = (root / "deploy" / "docker-compose.yml").is_file()

    found = find_system_python()
    if found:
        say(OK, f"系统 Python：{found[1]}", found[0])
    elif docker_deploy:
        say(WARN, "没有配置 Python 环境",
            "检测到 deploy/docker-compose.yml —— 走 Docker 部署时本机不需要 Python；"
            "要在本机直接跑才需要装 3.12：https://www.python.org/downloads/")
    else:
        say(BLOCK, "没有配置 Python 环境",
            "装 3.12：https://www.python.org/downloads/（装的时候勾上 Add python.exe to PATH）")

    if venv_py.is_file():
        code, out = run([str(venv_py), "--version"], timeout=60)
        if code == 0:
            say(OK, f"虚拟环境：{out.strip()}", str(venv_py))
            probe = (
                "import nonebot, openai, dotenv;"
                " print('nonebot=' + nonebot.__version__);"
                " print('openai=' + openai.__version__)"
            )
            code2, out2 = run([str(venv_py), "-c", probe], timeout=120)
            if code2 == 0:
                for line in out2.strip().splitlines():
                    say(OK, f"依赖：{line.strip()}")
                usable = True
            else:
                say(BLOCK, "关键依赖不完整", "运行 _工具链/启动/安装依赖.ps1（Linux：.venv/bin/pip install -r requirements.txt）")
                print("        " + out2.strip().splitlines()[-1][:100])
            code3, _ = run([str(venv_py), "-c", "import fastapi"], timeout=120)
            if code3 != 0:
                say(WARN, "没装 fastapi", "机器人照常跑，但 Web 控制台打不开")
        else:
            say(BLOCK, ".venv 里的 python 跑不起来", str(venv_py))
    else:
        if docker_deploy:
            say(WARN, "本机没有虚拟环境",
                "检测到 deploy/docker-compose.yml —— 如果你走的是 Docker 部署，本机不需要建 .venv；"
                "要在本机直接跑才需要：& '.\\_工具链\\启动\\安装依赖.ps1'")
        else:
            say(BLOCK, "虚拟环境不存在（依赖没装）",
                "Windows：& '.\\_工具链\\启动\\安装依赖.ps1'；Linux：python -m venv .venv && .venv/bin/pip install -r requirements.txt")

    return usable


def active_persona_pack(root: Path, env: dict[str, str]) -> str:
    """当前激活的人格包 id：运行时标记 > 注册表 > `.env` 的 AI_CHAT_PERSONA_PACK > 唯一一个包。

    与 `plugins/ai_chat/packs.py` 的 `_resolve_active()` **同序**。这里复算一遍是因为
    自检脚本**故意不 import 插件**（它要在没装依赖的机器上跑，见文件头）。
    """
    packs_dir = root / "persona" / "packs"

    def usable(name: str) -> bool:
        return bool(name) and (packs_dir / name).is_dir()

    try:
        got = (root / "data" / "runtime" / "persona" / "_active").read_text(encoding="utf-8").strip()
        if usable(got):
            return got
    except OSError:
        pass
    try:
        got = str((json.loads((root / "persona" / "_registry.json").read_text(encoding="utf-8"))
                   or {}).get("active") or "").strip()
        if usable(got):
            return got
    except (OSError, ValueError, AttributeError):
        pass
    got = str(env.get("AI_CHAT_PERSONA_PACK", "") or "").strip()
    if usable(got):
        return got
    try:
        names = sorted(n for n in os.listdir(packs_dir)
                       if not n.startswith(("_", ".")) and (packs_dir / n).is_dir())
    except OSError:
        names = []
    return names[0] if len(names) == 1 else ""


def check_persona(root: Path, env: dict[str, str], overrides: dict[str, object],
                  env_path: Path, interactive: bool = False) -> None:
    head("3.2 人格三文件")

    pack = active_persona_pack(root, env)
    packs_dir = root / "persona" / "packs"
    try:
        available = sorted(n for n in os.listdir(packs_dir)
                           if not n.startswith(("_", ".")) and (packs_dir / n).is_dir())
    except OSError:
        available = []
    if pack:
        say(OK, f"当前人格包：{pack}", f"共 {len(available)} 个可用：{'、'.join(available) or '（无）'}")
    else:
        say(BLOCK, "没有可用的人格包",
            "照 persona\\_TEMPLATE\\README.md 建一个（persona\\packs\\<id>\\），"
            "或在 .env 里显式配 AI_CHAT_PERSONA_FILE / _FORBIDDEN_FILE / _SURFACE_FILE")

    def resolve(key: str, default_name: str) -> Path:
        """显式配置优先，否则取当前包里的同名文件。"""
        name = env.get(key, "").strip()
        if name:
            return Path(name) if Path(name).is_absolute() else (root / name)
        return packs_dir / pack / default_name if pack else root / "persona" / "packs" / default_name

    def shown(key: str, default_name: str) -> str:
        name = env.get(key, "").strip()
        if name:
            return f"{name}（.env 显式指定，绕过人格包）"
        return f"persona/packs/{pack or '<包>'}/{default_name}"

    layers = [
        ("底层人设（它是谁）", resolve("AI_CHAT_PERSONA_FILE", "base.txt"),
         shown("AI_CHAT_PERSONA_FILE", "base.txt")),
        ("禁止事项（铁律）", resolve("AI_CHAT_FORBIDDEN_FILE", "forbidden.txt"),
         shown("AI_CHAT_FORBIDDEN_FILE", "forbidden.txt")),
        ("表层人设（会被自动改写）", resolve("AI_CHAT_SURFACE_FILE", "surface.txt"),
         shown("AI_CHAT_SURFACE_FILE", "surface.txt")),
    ]
    base_text = ""
    for label, path, label_text in layers:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if label.startswith("底层"):
                base_text = text
            if text.strip():
                say(OK, f"{label}：{len(text.strip())} 字", label_text)
            else:
                say(WARN, f"{label}是空的", f"{label_text}（它会没有性格可言）")
        else:
            say(BLOCK, f"找不到 {label}", f"{label_text} —— 检查 .env 里的文件名或恢复该文件")

    # 运行数据的表层才是**实际生效**的那份（包里的只是首次播种模板）
    stage_surface = root / "data" / "runtime" / "persona" / pack / "surface.txt" if pack else None
    if stage_surface is not None:
        if stage_surface.is_file():
            say(OK, "运行数据的表层在", f"data/runtime/persona/{pack}/surface.txt（这一份才是生效的）")
        else:
            say(INFO, "还没有运行数据的表层",
                f"data/runtime/persona/{pack}/surface.txt —— 首次启动时会从包里的模板播种")

    traits = (packs_dir / pack / "traits.json") if pack else (packs_dir / "traits.json")
    if traits.is_file():
        say(OK, "特质注册表在", f"persona/packs/{pack}/traits.json")
    else:
        say(INFO, f"没有 persona/packs/{pack or '<包>'}/traits.json",
            "运行时不需要（有内置回退）；但 _人设结构检查.py / _自示监控.py 会报错，这是有意的")

    # `AI_CHAT_BOT_NAME` 与人设里的角色名**必须一致** —— 它决定"聊天记录里哪句话是
    # 机器人自己说的"，写错了它会对着自己接话。这一条最值得在首次自检里报出来。
    bot_name = env.get("AI_CHAT_BOT_NAME", "").strip()
    if not bot_name and interactive:
        print("  AI_CHAT_BOT_NAME 没填（它决定聊天记录里哪句话是它自己说的）")
        bot_name = ask_until("它叫什么（回车跳过）", "要和底层人设里的角色名一致", _valid_name)
        if bot_name:
            if write_env(env_path, "AI_CHAT_BOT_NAME", bot_name):
                env["AI_CHAT_BOT_NAME"] = bot_name
                say(OK, f"已写入 .env：AI_CHAT_BOT_NAME={bot_name}")
    if not bot_name:
        say(BLOCK, "AI_CHAT_BOT_NAME 没填", "它会分不清哪句话是自己说的，改成人设里的角色名")
    elif base_text and bot_name not in base_text:
        say(WARN, f"AI_CHAT_BOT_NAME（{bot_name}）没在人设里出现",
            "确认角色名一致；不一致时它会对着自己接话")
    else:
        say(OK, f"角色名与人设一致：{bot_name}")

    # 唤醒词也要认角色名 —— 但它多半不在 .env 里（默认写在 settings.py 的 spec 表里），
    # 所以要和"生效值"比，而不是只看 .env。
    wake, wake_from = effective(root, env, "wake_words", "AI_CHAT_WAKE_WORDS", overrides)
    if not wake or wake in ("[]",):
        say(INFO, "没有唤醒词", "那就只靠 @ 触发")
    elif bot_name and bot_name not in wake:
        say(WARN, f"唤醒词里没有角色名：{wake}（{wake_from}）",
            "群里叫它名字时可能叫不出来；在控制台「唤醒」组里加上")
    else:
        say(OK, f"唤醒词含角色名：{wake}", wake_from)


def check_config(root: Path, env: dict[str, str], env_path: Path, fix: bool,
                 overrides: dict[str, object], interactive: bool = False
                 ) -> tuple[bool, dict[str, str]]:
    """检查配置。返回（.env 是否可用, 可能已刷新的 env）。

    为什么要把 env 回传给调用者：`--fix` 会**当场生成** `.env`，此后要按新文件继续查
    （否则用户看到的仍是"没有 .env"，还得再跑一次才知道下一个问题是什么）。

    `interactive` 为真时，**缺的简单项就地问、直接写回 `.env`** ——
    "填一行配置"这种事不该让人去开编辑器。
    """
    head("3.1 配置文件 .env")
    existing = env_path.is_file()
    if not existing:
        example = root / ".env.example"
        if (fix or interactive) and example.is_file():
            shutil.copyfile(example, env_path)
            say(OK, "已从 .env.example 生成 .env", str(env_path))
            env = parse_env(env_path)
            existing = True
        else:
            say(BLOCK, ".env 不存在", "复制模板：Copy-Item .env.example .env（或加 --fix 自动做）")
            return False, env

    say(OK, ".env 存在", str(env_path))

    key = env.get("DEEPSEEK_API_KEY", "").strip()
    if not key and interactive:
        print("  没有填 DEEPSEEK_API_KEY（没有它机器人不会说话）")
        key = ask_until("请粘贴 DeepSeek API Key（回车跳过）",
                        "到 https://platform.deepseek.com/api_keys 申请，形如 sk-xxxxxxxx",
                        _valid_key)
        if key:
            if write_env(env_path, "DEEPSEEK_API_KEY", key):
                env["DEEPSEEK_API_KEY"] = key
                say(OK, f"已写入 .env：DEEPSEEK_API_KEY={mask(key)}")
    if not key:
        say(BLOCK, "DEEPSEEK_API_KEY 没填", "机器人会在群里回「未配置 Key」")
    elif not key.startswith("sk-"):
        say(WARN, f"Key 不以 sk- 开头（{mask(key)}）", "确认没填错")
    else:
        say(OK, f"DEEPSEEK_API_KEY 已填：{mask(key)}")

    model = env.get("DEEPSEEK_MODEL", "").strip() or "deepseek-flash"
    known = {"deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"}
    if model in known:
        say(OK, f"模型：{model}")
    else:
        say(WARN, f"模型名不在已知列表里：{model}", "注意旧名可能已下线；可用 /模型 或控制台热切换")

    master, master_from = effective(root, env, "master_qq", "AI_CHAT_MASTER_QQ", overrides)
    # 占位值 / 没填 / 不是数字 —— 都算"填一下就好"，交互时直接问。
    # **控制台改过就不问**：那才是真正的生效值，此时写 .env 会被它盖住，白填。
    if ((not master.isdigit()) or master == "100000001") and interactive and master_from != "控制台":
        print(f"  主人 QQ 现在是「{master or '空'}」（占位值或没填）")
        got = ask_until("请填你自己的 QQ 号（回车跳过）",
                        "它决定哪些指令只有你能用；不填则「限主人」的功能全部关闭",
                        _valid_qq)
        if got:
            if write_env(env_path, "AI_CHAT_MASTER_QQ", got):
                env["AI_CHAT_MASTER_QQ"] = got
                master, master_from = got, ".env"
                say(OK, f"已写入 .env：AI_CHAT_MASTER_QQ={got}")
    if master.isdigit() and master != "100000001":
        say(OK, f"主人 QQ：{master}", master_from)
    elif master == "100000001":
        # 副本里 spec 表的默认值是**脱敏占位**，不是真号。
        say(WARN, "主人 QQ 还是占位值 100000001",
            "它来自代码默认值 —— 在控制台「基础」组或 .env 里改成你自己的 QQ")
    elif master:
        say(WARN, f"主人 QQ 不是数字：{master}（{master_from}）")
    else:
        say(WARN, "没读到 AI_CHAT_MASTER_QQ",
            "定时问候、/dsh、以及所有「限主人」的指令都不会生效")

    wl = env.get("AI_CHAT_GROUP_WHITELIST", "").strip()
    if wl in ("", "[]"):
        say(INFO, "群白名单为空", "所有群都会响应；只想服务特定群就填 [群号]")
    else:
        say(OK, f"群白名单：{wl}")

    host = env.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host in ("127.0.0.1", "localhost", "::1"):
        say(OK, f"控制台只绑本机：{host}")
    else:
        # 控制台没有任何认证：能看全部聊天记录、改全部配置、以机器人身份发言。
        say(BLOCK, f"控制台绑到了 {host}",
            "它没有认证，暴露到公网等于把机器人交出去；改回 127.0.0.1，远程访问走 SSH 隧道")

    data_dir = root / "data" / "runtime"
    if data_dir.is_dir():
        say(OK, "data/ 已存在", "聊天记录、settings.json、记忆库都落在这里")
    elif fix:
        data_dir.mkdir(parents=True, exist_ok=True)
        say(OK, "已创建 data/", str(data_dir))
    else:
        say(INFO, "data/ 还没建", "首次启动会自动创建；它保存的是别人的聊天记录，别提交进仓库")
    return True, env


def check_ports(env: dict[str, str]) -> None:
    head("4. 端口与 NapCat")
    raw = env.get("ONEBOT_WS_URLS", "").strip() or '["ws://127.0.0.1:6700"]'
    urls = re.findall(r"wss?://[^\s\"'\[\],]+", raw)
    if not urls:
        say(WARN, f"读不出 WS 地址：{raw}", "应形如 [\"ws://127.0.0.1:6700\"]")
    for url in urls:
        parsed = urlparse(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        try:
            with socket.create_connection((host, port), timeout=2):
                say(OK, f"NapCat 的 OneBot WS 在听：{url}", "机器人能连上")
        except OSError as exc:
            say(WARN, f"{url} 连不上（{exc.__class__.__name__}）",
                "NapCat 还没起，或 WS 服务端没配 —— 机器人会一直重连（不阻塞配置检查）")

    port = env.get("PORT", "8080").strip() or "8080"
    host = env.get("HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        with socket.create_connection((host if host != "0.0.0.0" else "127.0.0.1", int(port)), timeout=1):
            say(WARN, f"控制台端口 {port} 已被占用", "若有别的实例在跑，先关掉它")
    except (OSError, ValueError):
        say(OK, f"控制台端口 {port} 空闲", f"启动后访问 http://{host}:{port}/ai/")


def model_profiles(root: Path, env: dict[str, str]) -> tuple[str, list[dict[str, object]]]:
    """读 `data/runtime/models.json`，返回 `(当前档案 id, 档案列表)`。

    **没有这个文件就按 `.env` 现推一个**：自检常常在第一次启动之前跑（那时还没有
    档案文件），不能因为"文件不存在"就说模型没配。推出来的那个与 `llm.seeded()`
    是同一份语义 —— 所以自检的结论与机器人真跑起来后的行为一致。
    """
    seed: dict[str, object] = {
        "id": "deepseek",
        "label": "DeepSeek 官方（按 .env 播种）",
        "base_url": (env.get("DEEPSEEK_BASE_URL", "") or "").strip() or "https://api.deepseek.com",
        "key": (env.get("DEEPSEEK_API_KEY", "") or "").strip(),
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": (env.get("DEEPSEEK_MODEL", "") or "").strip() or "deepseek-flash",
    }
    raw_dir = (env.get("AI_CHAT_LOG_DIR", "") or "").strip() or "data/runtime"
    data_dir = Path(raw_dir) if Path(raw_dir).is_absolute() else root / raw_dir
    if data_dir.resolve() == (root / "data").resolve():
        data_dir = data_dir / "runtime"
    try:
        data = json.loads((data_dir / "models.json").read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return "deepseek", [seed]
    items = [it for it in (data.get("profiles") or []) if isinstance(it, dict)] \
        if isinstance(data, dict) else []
    if not items:
        return "deepseek", [seed]

    out: list[dict[str, object]] = []
    for i, it in enumerate(items):
        env_name = str(it.get("api_key_env") or "").strip()
        key = str(it.get("api_key") or "").strip() or (env.get(env_name, "") or "").strip()
        out.append({
            "id": str(it.get("id") or f"p{i + 1}"),
            "label": str(it.get("label") or it.get("id") or f"p{i + 1}"),
            "base_url": str(it.get("base_url") or seed["base_url"]).rstrip("/"),
            "key": key,
            "api_key_env": env_name,
            "model": str(it.get("model") or seed["model"]),
        })
    active = str((data or {}).get("active") or "").strip()
    if active not in {str(p["id"]) for p in out}:
        active = str(out[0]["id"])
    return active, out


def probe_model(base: str, key: str, model: str) -> tuple[bool, str]:
    """发一个最小请求，判断这个模型**能不能真用**。

    为什么不能只看 `/models` 列表：实测 `deepseek-chat` **不在列表里**（列表只有
    `deepseek-flash` / `deepseek-v4-pro`），但调用完全正常 —— 它是未公开的兼容别名。
    只看列表就会把用户一个能用的配置判成"模型不可用"，把人引去改一处本来没错的设置。
    判据应该是"调得通吗"，代价是一个 `max_tokens=1` 的请求。
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "1"}],
        "max_tokens": 1,
    }).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            json.loads(resp.read().decode("utf-8", "replace"))
        return True, ""
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            err = json.loads(exc.read().decode("utf-8", "replace"))
            detail = str(((err or {}).get("error") or {}).get("message") or "")
        except Exception:  # noqa: BLE001
            pass
        return False, (f"HTTP {exc.code} {detail}").strip()[:140]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(exc)[:140]


def check_online(env: dict[str, str], offline: bool, root: Path) -> None:
    head("5. 模型接口连通性")
    if offline:
        say(INFO, "已按 --offline 跳过")
        return

    active, items = model_profiles(root, env)
    prof = next((p for p in items if p["id"] == active), items[0])
    base = str(prof["base_url"])
    key = str(prof["key"])
    model = str(prof["model"])
    if len(items) > 1:
        say(INFO, f"当前档案「{active}」（共 {len(items)} 个）",
            "控制台「模型」页 / /模型 指令可以随时切换")
    # 本机端点通常不校验密钥，不该因为"没配 key"就被判成不可用
    local = any(t in base.lower() for t in ("127.0.0.1", "localhost", "0.0.0.0", "::1"))
    if not key and not local:
        say(INFO, f"档案「{active}」没配密钥，跳过实测",
            f"填好 {prof['api_key_env'] or '密钥'} 后重跑（接口 {base}）")
        return

    # ① 先问 /models：顺手能列出对方有哪些模型，人看了好挑
    req = urllib.request.Request(f"{base.rstrip('/')}/models",
                                 headers={"Authorization": f"Bearer {key}"})
    ids: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        ids = [str(d.get("id", "")) for d in (payload.get("data") or [])]
        say(OK, f"接口通了（{base}），/models 列出 {len(ids)} 个模型", ", ".join(ids[:6]))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 402, 403):
            reason = {401: "密钥无效", 402: "余额不足", 403: "无权限"}[exc.code]
            say(BLOCK, f"接口拒绝：{reason}", f"检查档案「{active}」的密钥（{base}）")
            return
        # 404/405 很常见：自建与中转端点不一定实现 /models。这不是错，接着实测对话。
        say(INFO, f"/models 不可用（HTTP {exc.code}）", "很多自建/中转端点没实现它 —— 直接实测对话")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        say(BLOCK, f"连不上 {base}", f"{exc} —— 检查网络/代理，或档案里写的 base_url")
        return

    # ② 再实测一次对话：**列表里有也不代表这个模型名能用**（反之亦然）
    ok, why = probe_model(base, key, model)
    if ok:
        note = "不在 /models 列表里，但实测调得通（兼容别名很常见）" if ids and model not in ids else ""
        say(OK, f"模型可用：{model}", note)
    else:
        say(BLOCK, f"模型 {model} 调不通",
            f"{why} —— 在控制台「模型」页改这个档案，或用 /模型 切换")


def check_code(root: Path, python_cmd: list[str] | None, deep: bool) -> None:
    head("6. 代码完整性")
    if python_cmd is None:
        say(WARN, "没有可用的解释器，跳过", "先按第 2 段装好 Python/venv")
        return
    syntax = root / "_工具链" / "_语法检查.py"
    if syntax.is_file():
        code, out = run(python_cmd + [str(syntax)], timeout=600)
        tail = [ln for ln in out.strip().splitlines() if ln.strip()]
        if code == 0:
            say(OK, "语法检查通过", tail[-1].strip() if tail else "")
        else:
            say(BLOCK, "语法检查失败", tail[-1].strip() if tail else "")
            return
    if not deep:
        say(INFO, "桩测试未跑（加 --deep 会跑，约 1~2 分钟）")
        return
    stub = root / "_工具链" / "离线验证_桩.py"
    if not stub.is_file():
        say(WARN, "找不到 离线验证_桩.py")
        return
    print("  正在跑桩测试（无依赖、不联网，可能要一两分钟）...")
    code, out = run(python_cmd + [str(stub)], timeout=1800)
    tail = [ln for ln in out.strip().splitlines() if ln.strip().startswith("通过 ")]
    if code == 0:
        say(OK, "桩测试通过", tail[-1].strip() if tail else "")
    else:
        say(BLOCK, "桩测试未通过", tail[-1].strip() if tail else "（看完整输出定位）")


def print_summary(root: Path, env: dict[str, str], overrides: dict[str, object]) -> tuple[int, int]:
    head("配置摘要（当前生效值；控制台改过的以控制台为准）")
    master, master_from = effective(root, env, "master_qq", "AI_CHAT_MASTER_QQ", overrides)
    active, items = model_profiles(root, env)
    prof = next((p for p in items if p["id"] == active), items[0])
    override, override_from = effective(root, env, "model", "DEEPSEEK_MODEL", overrides)
    model_show = (f"{override}（{override_from} 覆盖）" if override
                  else f"{prof['model']}（档案「{active}」）")
    rows = [
        ("模型", f"{model_show} @ {prof['base_url']}"),
        ("接口档案", f"{len(items)} 个（{'／'.join(str(p['id']) for p in items[:5])}）"
                     "，控制台「模型」页可切换"),
        ("机器人名", env.get("AI_CHAT_BOT_NAME", "") or "（未填）"),
        ("主人 QQ", (master + f"（{master_from}）") if master else "（未配置：限主人功能全关）"),
        ("触发", "只靠 @ 与唤醒词" if not env.get("AI_CHAT_PREFIX") else f"前缀 {env['AI_CHAT_PREFIX']}"),
        ("会话切分", f"{env.get('AI_CHAT_SESSION_GAP', '300') or '300'} 秒"),
        ("长期记忆", "开" if (env.get("AI_CHAT_MEMORY_ENABLED", "true") or "true").lower() != "false" else "关"),
        ("联网搜索", "开" if (env.get("AI_CHAT_SEARCH_ENABLED", "") or "").lower() == "true" else "关（默认关，要自己配后端）"),
        ("定时问候", "开" if (env.get("AI_CHAT_GREET_ENABLED", "") or "").lower() == "true" else "关（默认关）"),
        ("控制台", f"http://{env.get('HOST', '') or '127.0.0.1'}:{env.get('PORT', '') or '8080'}/ai/"),
    ]
    for key, val in rows:
        print(f"    {pad(key, 10)} {val}")

    blocks = sum(1 for lv, _, _ in results if lv == BLOCK)
    warns = sum(1 for lv, _, _ in results if lv == WARN)
    return blocks, warns


def print_todo() -> None:
    """把阻塞项与警告项收成一张**可执行的待办单**。

    为什么要单独再列一遍：查完一轮之后，人只想看"我到底还差什么、下一步敲什么"，
    不想在几十行检测记录里自己找红字。`--fix` / 交互填过的项这时已经从 results 里
    变成 OK，所以这张单子天然只会剩下真没解决的。
    """
    todo = [(lv, t, d) for lv, t, d in results if lv in (BLOCK, WARN)]
    if not todo:
        return
    head("待办：还差什么、怎么补")
    for i, (level, title, detail) in enumerate(todo, 1):
        tag = "必须" if level == BLOCK else "建议"
        print(f"  {i}. [{tag}] {title}")
        if detail:
            print(f"       {detail}")
    print("  （解决后重跑一次：python '_工具链\\启动\\上手自检.py'）")


def main() -> int:
    ap = argparse.ArgumentParser(description="上手自检：检测环境、生成/核对配置、验证连通性")
    ap.add_argument("--fix", action="store_true", help="做能自动做的修复（复制 .env、建 data/）")
    ap.add_argument("--deep", action="store_true", help="额外跑桩测试（约 1~2 分钟）")
    ap.add_argument("--offline", action="store_true", help="不联网（跳过 DeepSeek 实测）")
    ap.add_argument("--root", default=None, help="项目根目录（默认自动向上查找）")
    ap.add_argument("--no-input", action="store_true",
                    help="完全不交互（脚本 / CI 用）；默认在终端里会就地问缺的简单项")
    args = ap.parse_args()

    # 交互开关：**只有真终端才问**。被重定向 / 管道时（脚本、CI、测试）绝不问 ——
    # 那种场合 `input()` 会立刻拿到 EOF，问了等于白问，还可能卡住别人的流水线。
    interactive = (not args.no_input) and sys.stdin is not None and sys.stdin.isatty()

    print("=" * 62)
    print("上手自检 · 检测环境 / 核对配置 / 验证连通性")
    print("=" * 62)

    head("1. 项目定位")
    root, tried = find_root(args.root)
    if root is None and interactive:
        # 双击 exe 的人没法传 --root，所以要能问。
        print("  没找到这个仓库在哪 —— 它是一个含 bot.py 与 plugins/ 的目录。")
        print("  （最省事的做法：把 exe 复制进仓库目录里再双击）")
        for _ in range(3):
            got = ask("仓库目录的完整路径（直接回车放弃）")
            if not got:
                break
            cand = Path(got.strip().strip('"').strip("'")).expanduser()
            if _looks_like_root(cand):
                root = cand.resolve()
                break
            print(f"  {cand} 里没有 bot.py，再试一次？")
    if root is None:
        say(BLOCK, "找不到项目根", "它要含 bot.py 与 plugins/")
        for where in tried[:4]:
            print(f"        找过：{where}")
        print("        两个办法：① 把 上手自检.exe 复制进仓库目录再双击；"
              "② 命令行加 --root <仓库路径>")
        print()
        print("=" * 62)
        print("有 1 项阻塞 —— 先处理它。")
        print("=" * 62)
        return 1
    say(OK, f"项目根：{root}")
    if getattr(sys, "frozen", False):
        say(INFO, "正由打包好的 exe 运行", "它自带解释器，检的是你机器上的 Python/venv")

    env_path = root / ".env"
    env = parse_env(env_path) if env_path.is_file() else {}
    overrides = read_overrides(root)

    venv_py = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    check_env(root, venv_py)

    has_env, env = check_config(root, env, env_path, args.fix, overrides, interactive)
    if has_env:
        check_persona(root, env, overrides, env_path, interactive)
    else:
        head("3.2 人格三文件")
        say(INFO, "等 .env 就位后再看人设（文件名写在 .env 里）")

    if has_env:
        check_ports(env)
        check_online(env, args.offline, root)
    else:
        # 不静默跳过：段号直接消失会让人以为工具漏跑了。
        head("4. 端口与 NapCat")
        say(INFO, "跳过 —— 要先有 .env（探哪个地址由它决定）")
        head("5. 模型接口连通性")
        say(INFO, "跳过 —— 要先有 .env 里的 Key")

    python_cmd = [str(venv_py)] if venv_py.is_file() else None
    if python_cmd is None:
        found = find_system_python()
        python_cmd = found[0].split() if found else None
    if has_env:
        check_code(root, python_cmd, args.deep)

    blocks, warns = print_summary(root, env, overrides)
    print_todo()

    print()
    print("=" * 62)
    if blocks:
        print(f"有 {blocks} 项阻塞、{warns} 项警告 —— 先处理阻塞项。")
        print("=" * 62)
        return 1
    if warns:
        print(f"没有阻塞项，{warns} 项警告可以以后再看。")
    else:
        print("全部通过。")
    print("下一步：")
    if os.name == "nt":
        print("    & '.\\_工具链\\启动\\启动机器人.ps1'        # 起机器人（会再体检一次）")
        print("    & '.\\_工具链\\启动\\一键启动.ps1'          # 连 NapCat 一起拉起并开控制台")
    else:
        print("    .venv/bin/python bot.py              # 起机器人")
        print("    （服务器部署见 deploy/README.md）")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    init_console()
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        # 输出被下游提前关掉（典型是 `| Select-Object -First 20`）。
        # 这是下游的正常行为，不该甩一段 traceback 出来；把 stdout 换成空设备再退出。
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        raise SystemExit(0)
