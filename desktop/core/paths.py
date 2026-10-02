"""桌面端自己的文件落点：**先探可写再用**，且与机器人运行数据严格分开。

两条硬约束叠在一起，所以这里的写法比一般的"配置目录"要啰嗦：

1. **绝不读写机器人的运行数据**。`data/runtime/settings.json`、`data/runtime/memory.db`、
   `data/runtime/chatlog_*.json`、`data/runtime/stickers/` 都是服务端的事，桌面端只经 HTTP API 说话。
2. **不能假定 `%LOCALAPPDATA%` 可写**。实测踩过：在受限/被托管的会话里，
   `C:\\Users\\<你>\\AppData\\Local\\QQ_bot_console` 的 `mkdir` 直接
   `PermissionError: [WinError 5]`，于是"保存服务器目标"一按就炸。
   同一次会话里**项目目录却是可写的**。

所以落点不是"推导一个路径就完事"，而是**按候选链逐个写探针文件**，第一个成功的才算数。
默认就用**项目内**的 `.console\\`（跟着项目走、一定可写），系统配置目录只作备选；
选中的目录会缓存下来，并在界面上显示（用户有权知道配置到底存在哪）。

排查口径：`python -m desktop paths` 会把候选链和每个候选的可写性打出来。
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "QQ_bot_console"

#: 项目内的落点（首选）。理由是实测：本工作区里子进程在 `%LOCALAPPDATA%` 下
#: 建目录会被拒（`WinError 5`），而项目目录一定可写。放在这里的另一个好处是
#: **跟着项目走**：换机器、搬目录，配置与日志还在。
PROJECT_DIR_NAME = ".console"

#: 环境变量：显式指定配置/数据目录（自检与冒烟测试用它把状态隔离到临时目录）。
ENV_HOME = "QQBOT_CONSOLE_HOME"
ENV_DATA = "QQBOT_CONSOLE_DATA"

_resolved: "Store | None" = None


@dataclass(frozen=True)
class Store:
    """一次进程内确定下来的落点，以及它为什么是这个。"""

    config: Path
    data: Path
    reason: str
    candidates: tuple[str, ...] = ()
    fallback_used: bool = False
    in_project: bool = False

    def describe(self) -> str:
        if self.in_project:
            note = "（在项目内，跟着项目走）"
        elif self.fallback_used:
            note = "（系统配置目录不可写，已退到项目内）"
        else:
            note = "（在系统配置目录）"
        return f"配置：{self.config}{note}\n数据：{self.data}"


# --------------------------------------------------------------------- 探测


def _can_write(folder: Path) -> bool:
    """这个目录能不能写。目录本身不会被创建（这是纯探测）。"""
    probe = folder / f".probe-write-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _probe(folder: Path) -> bool:
    """判断落点是否可用。

    **探测不留副作用**：只看 `exists()` / `os.access()` 会骗人（目录可能只读、
    或者父目录不允许建子项），但天真的 `mkdir(parents=True)` 又会把我们自己
    "探"出来的空目录留在盘上 —— 实测出现过：候选链还没选中，项目里就先多了一个
    `.console\\data\\`。所以分三层：

    1. 目录已存在 → 直接试写探针文件，不动目录结构；
    2. 目录不存在 → 先看父目录能不能写：**父目录都写不了就直接判否**，绝不建；
    3. 确实要建 → 记下建了几层，探针失败时**原样删回去**（成功则保留，
       因为那就是本次选中的落点）。
    """
    if folder.exists():
        return _can_write(folder)

    parent = folder.parent
    if not parent.exists() and not _probe(parent):
        return False

    created: list[Path] = []
    try:
        # 逐层建，记下每一层，便于失败时回滚。
        chain: list[Path] = []
        cursor = folder
        while not cursor.exists():
            chain.append(cursor)
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        for path in reversed(chain):
            path.mkdir()
            created.append(path)
        if _can_write(folder):
            return True
        return False
    except OSError:
        return False
    finally:
        if created and not _can_write(folder):
            for path in reversed(created):
                try:
                    path.rmdir()  # 只在空目录时成功，正好是我们建的
                except OSError:
                    pass


def project_root() -> Path:
    """项目根（`QQ_bot\\`）。

    从本文件位置向上推：`desktop/core/paths.py` → 上三级。
    只用于找**随包内置**的东西（示范插件、README、默认配置目录），
    不用于找机器人运行数据。
    """
    return Path(__file__).resolve().parents[2]


def project_home() -> Path:
    """项目内的配置根：`<项目>\\.console\\`。"""
    return project_root() / PROJECT_DIR_NAME


def _system_home() -> Path | None:
    """系统配置目录（可写时的备选）。"""
    if sys.platform == "win32":
        raw = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(raw) / APP_DIR_NAME) if raw else None
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def _config_candidates() -> list[tuple[str, Path]]:
    """配置目录候选链。

    **项目内优先**（顺序是有理由的，不是随便排的）：
    1. 环境变量显式指定 —— 给了就用，写不了直接报错（不悄悄换地方）；
    2. 项目内 `.console\\` —— 本项目实际可靠的落点，且跟着项目走；
    3. 系统配置目录（`%LOCALAPPDATA%` / `$XDG_CONFIG_HOME`）—— 项目目录被搬到只读位置时的备选。
    """
    override = os.environ.get(ENV_HOME)
    if override:
        return [("环境变量 QQBOT_CONSOLE_HOME", Path(override))]
    out: list[tuple[str, Path]] = [("项目内 .console", project_home())]
    system = _system_home()
    if system is not None:
        out.append(("系统配置目录", system))
    return out


def _data_candidates(config: Path) -> list[tuple[str, Path]]:
    override = os.environ.get(ENV_DATA)
    if override:
        return [("环境变量 QQBOT_CONSOLE_DATA", Path(override))]
    out: list[tuple[str, Path]] = []
    if config == project_home():
        out.append(("项目内 .console\\data", config / "data"))
    else:
        out.append(("系统配置目录\\data", config / "data"))
        out.append(("项目内 .console\\data", project_home() / "data"))
    return out


def resolve(*, force: bool = False) -> Store:
    """确定落点（进程内缓存一次）。

    规则：
    * 配置与数据**各自**探可写，互不牵连（配置能写、数据不能写也要能跑起来）；
    * 候选全失败时不再把原始 `PermissionError` 抛给界面：退到项目内，
      并在 `describe()` / `diagnose()` 里说明白。
    """
    global _resolved
    if _resolved is not None and not force:
        return _resolved

    tried: list[str] = []
    config: Path | None = None
    for label, folder in _config_candidates():
        tried.append(f"{label}: {folder}")
        if _probe(folder):
            config = folder
            break
    fallback_used = config is None
    if config is None:
        # 连项目目录与系统目录都写不了：给一个确定性的路径，让写入时抛出有意义的错。
        config = project_home()

    data: Path | None = None
    for label, folder in _data_candidates(config):
        if _probe(folder):
            data = folder
            break
    if data is None:
        data = config / "data"

    store = Store(
        config=config,
        data=data,
        reason=f"配置落点：{config}",
        candidates=tuple(tried),
        fallback_used=fallback_used,
        in_project=(config == project_home()),
    )
    _resolved = store
    return store


def reset_cache() -> None:
    """测试/诊断用：丢掉缓存，下次重新探。"""
    global _resolved
    _resolved = None


# --------------------------------------------------------------------- 对外


def config_home() -> Path:
    """配置根目录（令牌密文、`targets.json`、插件启用状态）。"""
    return resolve().config


def data_home() -> Path:
    """运行数据目录（日志、隧道状态、自检临时目录的兜底）。"""
    return resolve().data


def targets_file() -> Path:
    return config_home() / "targets.json"


def plugins_file() -> Path:
    """插件启用状态（用户显式启用了哪些插件）。"""
    return config_home() / "plugins.json"


def log_file() -> Path:
    return data_home() / "console.log"


def describe() -> str:
    """给界面/`--check` 显示的一行说明。"""
    return resolve().describe()


def ensure_dirs() -> bool:
    """建目录。返回是否都成功（失败不抛异常：调用方负责提示用户）。"""
    store = resolve()
    ok = True
    for path in (store.config, store.data):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 目录建不出来不该让程序起不来：后面写文件时会再报一次，信息更具体。
            ok = False
    return ok


def work_tmp(prefix: str) -> Path:
    """给自检/冒烟测试用的临时目录，**保证可用、且保证在预期位置**。

    为什么不能直接 `tempfile.mkdtemp()`：实测踩过三种翻车方式 ——

    1. `tempfile.gettempdir()` 会在首次调用时缓存，之后 `启动控制台.ps1` 再改
       `TMP/TEMP` 不生效，临时目录就落进了项目根；
    2. 它还有个更阴的退化：**当所有候选目录都写不了时，它会返回 `os.getcwd()`**
       （`_get_default_tempdir` 的兜底），于是"临时目录"直接变成项目根；
    3. 环境变量里若给的是**长路径**形式（`C:\\Users\\<你>\\...`），
       在文件沙箱受限的会话里会被拒写，`mkdtemp` 甚至会卡住不返回。

    所以这里不猜：**逐个候选写探针文件**，成功才用；全都不行就退到项目内的
    `.work-tmp\\`（项目目录一定是可写的，且已被导出/`dockerignore` 排除）。
    并且**永远显式传 `dir=`**，绝不让 `mkdtemp` 去解析相对路径。
    最后把选中的目录钉进 `tempfile.tempdir`，避免调用方不带 `dir` 时又飘走。

    ⚠ **这里刻意不碰 `data_home()`**：那会触发一次落点解析，而解析本身会去探
    `.console\\` 可不可写（`mkdir` + 写探针）—— 于是"只是拿个临时目录"就把真实配置
    目录建出来了。自检要的是"绝不碰真实配置"，所以候选只用环境变量和项目根。
    """
    probe_name = f".probe-{os.getpid()}"
    candidates: list[Path] = []
    for key in ("LOCALAPPDATA", "TEMP", "TMP"):
        raw = os.environ.get(key)
        if raw:
            candidates.append(Path(raw) / "Temp")
            candidates.append(Path(raw))
    candidates += [
        Path(r"C:\Windows\Temp") if os.name == "nt" else Path("/tmp"),
        project_root() / ".work-tmp",   # 项目内兜底：一定可写
    ]

    chosen: Path | None = None
    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / probe_name
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue
        chosen = folder
        break
    if chosen is None:
        # 连项目目录都写不了：极端情况，返回一个固定路径让调用方自己报错，不抛异常。
        chosen = project_root() / ".work-tmp"
        chosen.mkdir(parents=True, exist_ok=True)

    tempfile.tempdir = str(chosen)
    try:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(chosen)))
    except OSError:
        # 真到这一步就退回一个确定性的名字，至少不会把东西散到别处。
        fallback = chosen / prefix.rstrip("-")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def diagnose() -> list[str]:
    """`python -m desktop paths` 打印的落点体检报告。

    用户报"存不了服务器目标"时，第一件事就是看这里 —— 它会明确说是哪个路径写不了。
    """
    store = resolve(force=True)
    lines = [
        "桌面控制台落点体检",
        "-" * 56,
        f"环境变量覆盖：{ENV_HOME}={os.environ.get(ENV_HOME) or '（未设）'}　"
        f"{ENV_DATA}={os.environ.get(ENV_DATA) or '（未设）'}",
        "",
        "候选链（按顺序试探，✓ = 可写）：",
    ]
    for item in store.candidates:
        label, _, raw = item.partition(": ")
        path = Path(raw)
        lines.append(f"  {'✓' if _probe(path) else '✗'} {label} → {raw}")
    lines += [
        "",
        f"最终配置目录：{store.config}",
        f"最终数据目录：{store.data}",
        f"是否在项目内：{'是' if store.in_project else '否'}",
        "",
        "结论：" + (
            "配置写在**项目内**的 `.console\\`：换机器/搬目录时配置与日志跟着走，"
            "系统用户配置目录没有被依赖。该目录已被开源导出与 .dockerignore 排除。"
            if store.in_project
            else f"配置写在系统配置目录（{store.config}），项目内落点不可写。"
        ),
    ]
    return lines
