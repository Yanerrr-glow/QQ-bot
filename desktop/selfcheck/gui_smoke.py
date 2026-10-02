"""离屏 GUI 冒烟测试：把主窗口真的建出来、每个页面真的加载一遍。

为什么值得单独写一个（而不是只做 `import` 检查）：
    import 只证明模块语法没错。页面构造函数里的名字打错、信号连错、表格列数不对，
    全都要"真的 new 出来并 load 一次"才会暴露。本机没显示器，所以用
    `QT_QPA_PLATFORM=offscreen` —— Qt 支持离屏渲染，不需要窗口可见。

覆盖的坑（都是这类代码最容易出事的点）：
    1. 九个内置页面的构造函数与首次加载
    2. 插件宿主把示范插件的页面**真的建出来**（含没有 Qt 时的纯数据回退）
    3. 连接管理器在桩服务上探测成功
    4. 切目标后所有页面 `invalidate()` 能重新加载（不炸、不残留旧数据）
    5. 退出时插件停用 + 隧道回收不抛异常

用法：
    .venv-desktop\\Scripts\\python.exe -m desktop.selfcheck.gui_smoke
退出码 0 = 通过。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _prepare_env() -> Path:
    from desktop.core import paths

    # 临时目录用 `paths.work_tmp`（先探可写再用）：既不会跟着被启动器改过的
    # `TMP/TEMP` 落进项目根，也不会因为环境变量是长路径形式而被沙箱拒写卡住。
    tmp = paths.work_tmp("qqbot-gui-smoke-")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 无显示器也能跑
    # **先钉环境变量、再 reset 缓存**：`ConnectionManager()` 的默认参数会构造真实的
    # `CredentialStore`，它在 `__init__` 里就解析配置目录 —— 顺序反了就会往用户真实的
    # `.console\` 写一个 `secrets.dat`（自检里踩过同一个坑）。
    os.environ["QQBOT_CONSOLE_HOME"] = str(tmp / "home")
    os.environ["QQBOT_CONSOLE_DATA"] = str(tmp / "data")
    paths.reset_cache()
    paths.ensure_dirs()
    return tmp


def _pump(app, seconds: float) -> None:  # noqa: ANN001
    """跑事件循环若干秒：让 QThreadPool 里的后台任务有机会回来。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    tmp = _prepare_env()
    failures: list[str] = []

    from PySide6 import QtWidgets

    from desktop.core.connection import ConnectionManager
    from desktop.core.targets import Target, TargetStore
    from desktop.selfcheck.stub_server import StubConfig, StubServer

    cfg = StubConfig()
    server = StubServer(cfg).start()
    print(f"离屏 GUI 冒烟测试（桩服务 {server.base_url}，临时目录 {tmp}）")
    print("-" * 64)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])

    store = TargetStore(Path(os.environ["QQBOT_CONSOLE_HOME"]) / "targets.json")
    store.load()
    local = store.active()
    local.http.base_url = server.base_url
    local.http.token_ref = TargetStore.suggest_token_ref(local)
    store.update(local)
    other = Target(name="离线目标")
    other.http.base_url = "http://127.0.0.1:1"
    other.http.token_ref = TargetStore.suggest_token_ref(other)
    store.add(other)

    manager = ConnectionManager(store)
    manager.set_token(cfg.token, target=local)

    def step(name: str, fn) -> None:  # noqa: ANN001
        try:
            detail = fn()
        except BaseException as exc:  # noqa: BLE001
            failures.append(f"{name}：{type(exc).__name__}: {exc}")
            print(f"  [FAIL] {name}")
            for line in "".join(traceback.format_exception(exc)).strip().splitlines()[-8:]:
                print("          " + line)
        else:
            print(f"  [OK  ] {name}" + (f" —— {detail}" if detail else ""))

    from desktop.gui.main_window import BUILTIN_PAGES, MainWindow

    window = None

    def _build_window():  # noqa: ANN202
        nonlocal window
        window = MainWindow(manager)
        window.show()
        _pump(app, 0.4)
        return (f"内置页面 {len(window.pages)} 个，导航顶层 {window.nav.topLevelItemCount()} 项、"
                f"插件子项 {window.plugins_item.childCount() if window.plugins_item else 0} 项")

    step("建主窗口（含插件发现/登记/激活）", _build_window)
    if window is None:
        server.stop()
        print("\n主窗口没建起来，后面的检查跳过。")
        return 1

    def _probe():  # noqa: ANN202
        result = manager.probe()
        assert result.ok, f"探测失败：{result.detail} {result.hint}"
        _pump(app, 0.2)
        return f"{result.elapsed_ms} ms"

    step("连接探测（桩服务）", _probe)

    def _load_all_pages():  # noqa: ANN202
        for index, page in enumerate(window.pages):
            window.nav.setCurrentItem(window.nav.topLevelItem(index))
            _pump(app, 0.35)
            assert window.stack.currentWidget() is page, f"{page.title} 没切过去"
        _pump(app, 1.2)  # 等最后一页的后台任务回来
        return "、".join(p.title for p in window.pages)

    step("逐个页面加载（含后台请求）", _load_all_pages)

    def _plugin_pages():  # noqa: ANN202
        assert window.plugin_pages, "插件页面没有进导航"
        node = window.plugins_item
        assert node is not None, "导航里没有「插件」节点"
        assert node.childCount() == len(window.plugin_pages), (
            f"插件子项数 {node.childCount()} 与登记数 {len(window.plugin_pages)} 不一致"
        )
        assert node.isExpanded(), "有插件页面时「插件」目录应当默认展开"
        made: list[str] = []
        for offset in range(node.childCount()):
            window.nav.setCurrentItem(node.child(offset))
            _pump(app, 0.5)
            widget = window.stack.currentWidget()
            assert widget is not None, "插件页面为空"
            made.append(type(widget).__name__)
        # 目录可收起：收起来之后子项不可见，但当前页与选中项都不受影响
        node.setExpanded(False)
        assert not node.isExpanded(), "「插件」目录收不起来"
        node.setExpanded(True)
        return f"{len(made)} 个插件页面（挂在「插件」节点下，可收起）：{', '.join(made)}"

    step("插件页面建出来（挂在「插件」节点下、可收起）", _plugin_pages)

    def _rescan_plugins():  # noqa: ANN202
        """「重新扫描」**不能**把已加载的插件打成"加载失败"。

        用户实测报回（2026-10-02）：`discover()` 把已加载插件忘成"刚发现"，
        `load_all()` 于是二次 `register()`，插件的 `register_page` 撞上
        `ValueError: 页面 id 重复：memory_stats:overview`。
        """
        from desktop.gui.page_plugins import PluginsPage

        page = next((p for p in window.pages if isinstance(p, PluginsPage)), None)
        assert page is not None, "找不到插件页"
        assert window.plugin_host is not None
        before = {pid: rec.state.value for pid, rec in window.plugin_host.records.items()}
        page._rescan()  # noqa: SLF001 - 直接调按钮回调
        _pump(app, 0.4)
        after = {pid: rec.state.value for pid, rec in window.plugin_host.records.items()}
        assert "failed" not in after.values(), f"重扫把插件打成了失败：{after}"
        assert {k: after[k] for k in before if k in after} == before, (
            f"重扫后插件状态变了：{before} → {after}"
        )
        assert window.plugins_item is not None
        assert window.plugins_item.childCount() == len(window.plugin_pages) > 0, "重扫后插件子项丢了"
        return f"状态不变（{after}），子项 {window.plugins_item.childCount()} 个"

    step("插件「重新扫描」不重复登记（回归：页面 id 重复）", _rescan_plugins)

    def _status_cards():  # noqa: ANN202
        assert window.plugin_host is not None
        cards = window.plugin_host.status_cards()
        assert cards and all(c["ok"] for c in cards), f"状态卡异常：{cards}"
        return "；".join(f"{c['title']}={c['value']}" for c in cards)

    step("插件状态卡取数", _status_cards)

    def _switch_target():  # noqa: ANN202
        window.nav.setCurrentItem(window.nav.topLevelItem(0))
        manager.switch_target(other.id)
        _pump(app, 0.3)
        # 切到离线目标：探测必须失败，且**不能**把失败当成"没有数据"
        result = manager.probe()
        assert not result.ok, "离线目标竟然探测成功"
        params = next(i for i, p in enumerate(window.pages) if p.title == "参数")
        window.nav.setCurrentItem(window.nav.topLevelItem(params))  # 参数页：应显示错误而不是空表
        _pump(app, 0.4)
        # 切回桩服务目标。**显式把桩地址写回去** —— `manager.store.load()` 是从文件读的
        # 一份独立副本，早先对 `local` 对象的改写不会自动同步（这个冒烟脚本原先就吃在这）。
        local.http.base_url = server.base_url
        local.http.token_ref = TargetStore.suggest_token_ref(local)
        manager.store.update(local)
        manager.switch_target(local.id)
        _pump(app, 0.3)
        result2 = manager.probe()
        assert result2.ok, (
            f"切回来之后探测应恢复：{result2.detail} {result2.hint}"
            f"｜local.base_url={local.http.base_url!r} state={manager.state.base_url!r}"
        )
        window.nav.setCurrentItem(window.nav.topLevelItem(0))
        _pump(app, 0.4)
        return "离线不伪装成空数据，切回后恢复"

    step("切目标 → 页面作废重载 → 切回", _switch_target)

    # ---- 点按钮的路径（2026-10-02 补：之前这些路径没有任何测试走过） ----
    def _async_pipeline():  # noqa: ANN202
        """后台任务的完成回调必须真的被调到。

        这条是"管道自检"：踩过一次 —— `QThreadPool.start(task)` 不在 Python 侧保活
        `QRunnable`，`Async.run` 一返回任务就被 GC，**信号随之消失、回调一次都不触发**，
        而且没有任何报错。表现是"后台请求发出去了，界面永远停在旧状态"。
        """
        from desktop.gui.widgets import Async

        got: list[str] = []
        runner = Async()
        runner.run(lambda: "payload", on_done=lambda r: got.append(f"ok:{r.value}"))

        def _boom():  # noqa: ANN202
            raise ValueError("故意炸的")

        runner.run(_boom, on_done=lambda r: got.append(f"err:{r.message}"))
        for _ in range(200):
            app.processEvents()
            time.sleep(0.01)
            if len(got) >= 2:
                break
        assert any(x == "ok:payload" for x in got), f"成功回调没被调用：{got}"
        assert any(x.startswith("err:") and "故意炸的" in x for x in got), f"失败回调不对：{got}"
        return "成功/失败两条回投都到位（" + "；".join(got) + "）"

    step("后台任务回调管道", _async_pipeline)

    def _connections_page_ready():  # noqa: ANN202
        """连接设置页**首屏就可用**：目标列表有条目、选中项非空、地址框反映当前目标。

        踩过一次：两个独立原因叠加 —— ① 导航高亮没设（`currentRow` 停在 -1），
        页面永远不 `load()`；② `_refresh_list()` 依赖 `currentRowChanged` 信号，
        行号没变时信号不发，`_target` 一直是 None。
        结果就是用户看到的"目标列表空着、点启动隧道没反应"。
        """
        from desktop.core.targets import MODE_LOCAL, Target
        from desktop.gui.page_connections import ConnectionsPage

        page = next((p for p in window.pages if isinstance(p, ConnectionsPage)), None)
        assert page is not None, "找不到连接设置页"
        assert page.target_list.count() > 0, "连接页首屏目标列表是空的"
        assert page._target is not None, "连接页首屏没有选中目标"  # noqa: SLF001

        # 切到一个本机目标，检查地址框按模式可编辑 / 隧道目标只读
        local_ids = [t.id for t in manager.targets() if t.mode == MODE_LOCAL]
        assert local_ids, "冒烟环境里应该有本机目标"
        # 桩地址写回文件再切 —— 切换是按**当前配置**重建连接的
        freshened = manager.store.get(local_ids[0])
        if freshened is not None:
            freshened.http.base_url = server.base_url
            manager.store.update(freshened)
        manager.switch_target(local_ids[0])
        _pump(app, 0.3)
        row = [t.id for t in manager.targets()].index(local_ids[0])
        page.target_list.setCurrentRow(row)
        _pump(app, 0.2)
        assert page._target is not None and page._target.id == local_ids[0], "连接页没切到本机目标"  # noqa: SLF001
        assert page.base_url.isEnabled(), "本机目标的地址框应该是可编辑的"
        assert page.base_url.text().startswith("http"), (
            f"地址框没填上：{page.base_url.text()!r}"
            f"｜page._target={page._target.name if page._target else None} "
            f"mode={page._target.mode if page._target else '-'} "
            f"file_url={page._target.http.base_url if page._target else '-'!r} "
            f"active={manager.store.active_id} state={manager.state.base_url!r} "
            f"tunnel={getattr(manager, '_tunnel', None) and manager._tunnel.status.state} "  # noqa: SLF001
            f"cur_row={page.target_list.currentRow()}"
        )

        tunnel_target = Target(name="冒烟用隧道目标", mode="ssh_tunnel")
        tunnel_target.ssh.alias = "nonexistent-alias-for-smoke"
        tunnel_target.ssh.use_alias = True
        manager.store.add(tunnel_target)
        page.load(force=True)
        _pump(app, 0.2)
        # 注意：`store.add()` **不会**改 active 目标，所以 `_refresh_list()` 仍停在原来那一行。
        # 要断言"隧道目标的地址框只读"，必须先把这一行真的选中 —— 否则断的是本机目标
        # （这条断言原先就漏了这一步，靠前一条失败遮着，从没真正跑过）。
        row = [t.id for t in manager.targets()].index(tunnel_target.id)
        page.target_list.setCurrentRow(row)
        _pump(app, 0.2)
        assert page._target is not None and page._target.id == tunnel_target.id, "连接页没切到隧道目标"
        assert not page.base_url.isEnabled(), "隧道目标的地址框应该是只读的（派生值）"
        return "首屏有目标、地址框按模式可编辑/只读"

    step("连接设置页首屏可用（列表/选中/地址框）", _connections_page_ready)

    def _tunnel_autofill():  # noqa: ANN202
        """点「启动隧道」→ 隧道的本地转发地址必须**自动出现在地址框里**。

        用户实测报回的就是这条。当时是两个独立问题叠加：① 页面首屏没加载
        （见上一条）；② 地址框只在 `_fill()` 里填一次，而端口是启动时才分配的 ——
        现在状态一变就同步。

        设计说明：**SSH 端指向本机 sshd，转发目标指向桩服务端口**。
        这样"隧道通不通"是可断言的（不依赖机器人是否在跑），
        而且完全不碰真实服务器。本机没有可用别名/没有 sshd 就跳过 ——
        不把"环境没有 sshd"算成失败。
        """
        from desktop.core.targets import MODE_SSH, SshConfig, Target
        from desktop.gui.page_connections import ConnectionsPage

        page = next((p for p in window.pages if isinstance(p, ConnectionsPage)), None)
        assert page is not None

        names = ConnectionsPage.read_ssh_aliases()
        if not names:
            return "跳过：本机 ~/.ssh/config 里没有可用的 Host 别名"
        alias = names[0]

        target = Target(name="冒烟隧道目标", mode=MODE_SSH)
        target.ssh = SshConfig(use_alias=True, alias=alias, remote_host="127.0.0.1",
                               remote_port=server.port, local_port="auto", connect_timeout=8)
        target.http.prefix = "/ai"
        target.http.token_ref = cfg.token if hasattr(cfg, "token") else ""
        manager.store.add(target)
        manager.switch_target(target.id)
        page.load(force=True)
        _pump(app, 0.3)
        row = [t.id for t in manager.targets()].index(target.id)
        page.target_list.setCurrentRow(row)
        _pump(app, 0.2)
        assert page._target is not None and page._target.id == target.id, "连接页没切到冒烟隧道目标"  # noqa: SLF001
        assert not page.base_url.text(), f"启动前地址框应为空，实际 {page.base_url.text()!r}"

        page._start_tunnel()  # noqa: SLF001 - 直接调按钮回调
        # 本机 sshd 可能没起（或被策略禁掉），隧道会走完自己的重试与超时预算
        # —— 等够时间再判，避免把"环境没有 sshd"记成失败。
        for _ in range(1500):
            app.processEvents()
            time.sleep(0.02)
            tunnel = getattr(manager, "_tunnel", None)  # noqa: SLF001
            if page.base_url.text() or (tunnel is not None and tunnel.status.state != "starting"):
                break
        filled = page.base_url.text()
        tunnel = getattr(manager, "_tunnel", None)  # noqa: SLF001
        state = tunnel.status.state if tunnel else "无"
        if not filled:
            return f"跳过：本机 ssh 端不可用（隧道 {state}；本机没有可用的 sshd）"

        assert filled.startswith("http://127.0.0.1:"), f"启动隧道后地址没自动填入：{filled!r}"
        assert state == "running", f"地址填了但隧道状态是 {state}"
        assert filled == tunnel.status.base_url, "地址框与隧道实际地址不一致"
        # 经这条隧道真的能取到数据（桩服务在转发的另一头）
        probe = manager.probe()
        assert probe.ok, f"经隧道探测失败：{probe.detail} {probe.hint}"

        page._stop_tunnel()  # noqa: SLF001
        _pump(app, 1.2)
        assert not page.base_url.text(), f"停掉隧道后地址框该清空，实际 {page.base_url.text()!r}"
        assert getattr(manager, "_tunnel").status.state == "stopped", "隧道该已停止"  # noqa: SLF001
        return f"别名 {alias} → 自动填入 {filled} → 可连通 → 停止后清空"

    step("启动隧道 → 地址自动填入并可连通", _tunnel_autofill)

    def _close():  # noqa: ANN202
        window.close()
        _pump(app, 0.3)
        return "插件已停用、隧道已回收"

    step("关闭窗口（停插件 + 回收隧道）", _close)

    server.stop()
    print("-" * 64)
    if failures:
        print(f"冒烟测试失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("冒烟测试全部通过（界面能建、页面能加载、切目标能恢复、退出能清理）")
    return 0

if __name__ == "__main__":
    sys.exit(main())
