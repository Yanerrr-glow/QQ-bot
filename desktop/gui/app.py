"""桌面控制台进程入口。

    python -m desktop.gui                 # 正常启动
    python -m desktop.gui --check         # 只检查 Qt 与页面模块能不能 import（不建窗口）

环境变量（可选）：

    QQBOT_CONSOLE_BASE=http://127.0.0.1:8080   首选的连接地址（写进"本机"目标）
    QQBOT_CONSOLE_PREFIX=/ai                   API 前缀
    QQBOT_CONSOLE_TOKEN=...                    令牌（**不落盘**，只在本次进程里用）
    QQBOT_CONSOLE_HOME / QQBOT_CONSOLE_DATA    配置与数据目录（自检用）

为什么要 `--check`：装完 PySide6 想确认"能不能起来"但又不想弹窗时，
它是唯一能在无头/远程会话里跑的入口。**退出码 0/1 说明一切。**
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from ..core import paths
from ..core.logsetup import setup_logging
from ..core.connection import ConnectionManager
from ..core.targets import TargetStore
from .. import APP_NAME, APP_VERSION


def _apply_env_overrides(store: TargetStore) -> None:
    """把环境变量合并进当前目标（只改本机目标，不动服务器目标）。"""
    base = os.environ.get("QQBOT_CONSOLE_BASE")
    prefix = os.environ.get("QQBOT_CONSOLE_PREFIX")
    if not base and not prefix:
        return
    target = store.active()
    if base:
        target.http.base_url = base
    if prefix:
        target.http.prefix = prefix
    store.update(target)


def build_manager() -> ConnectionManager:
    paths.ensure_dirs()
    store = TargetStore()
    store.ensure()
    _apply_env_overrides(store)
    manager = ConnectionManager(store)
    token = os.environ.get("QQBOT_CONSOLE_TOKEN", "")
    if token:
        # 环境变量里的令牌只进内存：**不写凭据存储**，避免"导出环境变量顺手泄露"。
        manager.set_token(token, session_only=True)
        logging.getLogger("qqbot.desktop.gui").info("已从环境变量注入令牌（仅本次会话）")
    return manager


def preflight() -> tuple[bool, str]:
    """检查 Qt 可用性。返回 (是否可用, 说明)。"""
    try:
        from PySide6 import QtCore, QtWidgets  # noqa: F401, PLC0415
    except ImportError as exc:
        return False, (
            f"没装 PySide6（{exc}）。装法：\n"
            "    pip install -r requirements-desktop.txt -i https://mirrors.aliyun.com/pypi/simple/\n"
            "或者直接跑项目里的 `_工具链\\启动\\启动控制台.ps1`（它会替你装）。"
        )
    return True, "PySide6 可用"


def check() -> int:
    ok, why = preflight()
    print(f"{APP_NAME} {APP_VERSION} 自检")
    print("-" * 56)
    print(("[OK] " if ok else "[FAIL] ") + why)
    # 落点体检放在这里：用户报"存不了目标/存不了令牌"，第一件事就是看配置写在哪、写不写得进。
    store = paths.resolve(force=True)
    print(("[OK] " if paths.ensure_dirs() else "[FAIL] ") + "配置/数据目录可写")
    print("     配置：" + str(store.config) + ("（项目内，跟着项目走）" if store.in_project else ""))
    print("     数据：" + str(store.data))
    if not paths.ensure_dirs():
        print("     → 跑 `python -m desktop paths` 看候选链；也可用 $env:QQBOT_CONSOLE_HOME 指定")
        return 1
    if not ok:
        return 1
    # 页面模块全部 import 一遍：语法/名称错误在这一步就会暴露，而不是点开某个页面才炸。
    from . import main_window  # noqa: PLC0415

    print(f"[OK] 页面模块可 import（内置页面 {len(main_window.BUILTIN_PAGES)} 个）")
    from ..sdk.loader import PluginHost  # noqa: PLC0415

    print("[OK] 插件宿主可 import（" + PluginHost.__name__ + "）")
    print("")
    print("注意：这里只验证『能不能起来』，界面是否好看要人眼看。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m desktop.gui", description="QQ_bot 桌面控制台")
    parser.add_argument("--check", action="store_true", help="只检查依赖与模块，不打开窗口")
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    args = parser.parse_args(argv)

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    if args.check:
        return check()

    ok, why = preflight()
    if not ok:
        print(why, file=sys.stderr)
        return 1

    from PySide6 import QtCore, QtWidgets  # noqa: PLC0415

    from .main_window import MainWindow  # noqa: PLC0415

    # 高 DPI：Qt6 默认已按分数缩放，但显式设置舍入策略能让 125%/150% 下字体更清楚。
    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QtWidgets.QApplication(sys.argv[:1] + (argv or []))
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    manager = build_manager()
    window = MainWindow(manager)
    window.show()
    # 启动即探一次，让状态条在第一眼就是真话（而不是"未探测"）。
    QtCore.QTimer.singleShot(200, window._probe)  # noqa: SLF001 - 同一模块内的入口
    # 打开控制台就把当前服务器目标的隧道默认连起来（非隧道目标时它什么都不做）。
    # 排在探测之后：先让状态条显示真实连接状态，再起隧道。
    QtCore.QTimer.singleShot(350, window.auto_start_tunnel)
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
