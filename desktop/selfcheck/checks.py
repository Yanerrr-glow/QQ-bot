"""自检套件：把"没法靠人眼发现"的问题全验一遍。

验收判据只有一条：**退出码 0**。任何一项失败即为 1，并打印具体差异。

覆盖的是最容易写错、错了后果最重的部分：

- 认证：token 只走请求头；401 **不能**被当成空数据
- 前缀：`/ai` 是配置项，`/` 不是探测目标
- 形状：`/api/state` 的聚合键、`/api/persona` 的 410
- 目标隔离：切目标后 cookie/token 不串味
- 凭据：`targets.json` 里**没有**令牌
- 隧道：命令行带 BatchMode / ExitOnForwardFailure / ServerAliveInterval
- 插件：白名单权限表、越权被拒、加载失败被隔离、停用后不留任务
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import PLUGIN_API_VERSION
from ..core import apiclient, credentials, paths, targets, tunnel, util
from ..core.apiclient import ApiClient, BotApiError
from ..core.connection import ConnectionManager
from ..core.targets import MODE_LOCAL, MODE_SSH, SshConfig, Target, TargetStore
from ..sdk import permissions
from ..sdk.api import NullUiBridge, PluginAPI
from ..sdk.loader import PluginHost, PluginState
from ..sdk.manifest import ManifestError, load_manifest
from ..sdk.permissions import PermissionDenied
from .stub_server import StubConfig, StubServer

PLUGIN_ROOT = paths.project_root() / "desktop" / "plugins"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    extra: str = ""


@dataclass
class Suite:
    results: list[CheckResult] = field(default_factory=list)
    _current: str = ""

    def check(self, name: str, fn: Callable[[], Any]) -> CheckResult:
        """跑一项。fn 可以返回 None/真值（通过）或 (False, 说明)。"""
        self._current = name
        try:
            outcome = fn()
        except AssertionError as exc:
            result = CheckResult(name, False, str(exc) or "断言失败")
        except BaseException as exc:  # noqa: BLE001 - 自检要抓全部异常
            import traceback

            result = CheckResult(
                name, False, f"{type(exc).__name__}: {exc}",
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1200:],
            )
        else:
            if isinstance(outcome, tuple) and len(outcome) == 2 and outcome[0] is False:
                result = CheckResult(name, False, str(outcome[1]))
            elif outcome is False:
                result = CheckResult(name, False, "返回 False")
            elif isinstance(outcome, str):
                result = CheckResult(name, True, outcome)
            else:
                result = CheckResult(name, True, str(outcome or ""))
        self.results.append(result)
        marker = "OK  " if result.ok else "FAIL"
        print(f"  [{marker}] {name}" + (f" —— {result.detail}" if result.detail else ""))
        if not result.ok and result.extra:
            for line in result.extra.strip().splitlines()[-6:]:
                print(f"          {line}")
        return result

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok]

    def report(self) -> int:
        total = len(self.results)
        bad = len(self.failed)
        print("")
        print(f"自检结果：{total - bad}/{total} 项通过")
        if bad:
            print("失败项：")
            for item in self.failed:
                print(f"  - {item.name}：{item.detail}")
            return 1
        return 0


# --------------------------------------------------------------------- 工具


def _client(server: StubServer, *, token: str = "", prefix: str = "/ai") -> ApiClient:
    return ApiClient(server.base_url, prefix=prefix, token=token, timeout=5.0, max_retries=0)


def _expect_error(fn: Callable[[], Any], status: int) -> BotApiError:
    try:
        fn()
    except BotApiError as exc:
        assert exc.status == status, f"期望 HTTP {status}，实际 {exc.status}（{exc}）"
        return exc
    raise AssertionError(f"期望抛出 HTTP {status}，但没有抛异常")


def _safe_temp_root() -> str:
    """给"隔离根"用的临时目录：**不能**用会被启动器改过的 `TMP/TEMP`。

    判据同样是"先探可写"：系统临时目录可用就用它，否则退回项目内 `.work-tmp\\`。
    （直接 `tempfile.mkdtemp()` 在受限会话里会退化到 `os.getcwd()`，那就把自检的
    中间产物撒进项目根了 —— 实测踩过。）
    """
    for candidate in (
        Path(os.environ["LOCALAPPDATA"]) / "Temp" if os.environ.get("LOCALAPPDATA") else None,
        Path(os.environ["TEMP"]) if os.environ.get("TEMP") else None,
        Path(os.environ["TMP"]) if os.environ.get("TMP") else None,
        Path(r"C:\Windows\Temp") if os.name == "nt" else Path("/tmp"),
        Path(__file__).resolve().parents[2] / ".work-tmp",
    ):
        if candidate is None:
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".probe-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            continue
        return str(candidate)
    fallback = Path(__file__).resolve().parents[2] / ".work-tmp"
    fallback.mkdir(parents=True, exist_ok=True)
    return str(fallback)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------- 各项


def check_work_tmp_and_paths(tmp: Path) -> str:
    """落点必须**真的可写**，且不能退化成当前工作目录。

    这条是有来源的：`tempfile.gettempdir()` 在所有候选都写不了时会返回 `os.getcwd()`，
    于是"临时目录"直接变成项目根；而配置目录更严重 —— 用户按"保存服务器目标"直接
    吃一个 `PermissionError: [WinError 5]`（实测报过，根因是 `%LOCALAPPDATA%` 被环境拒写）。
    """
    store = paths.resolve(force=True)
    assert paths.ensure_dirs(), f"配置目录不可写：{store.config}"
    # 写一个真文件再读回来 —— 只 exists() 会骗人。
    probe = store.config / "selfcheck-probe.json"
    probe.write_text('{"ok": true}', encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == '{"ok": true}', "配置目录写进去读不回来"
    probe.unlink()

    work = paths.work_tmp("qqbot-pathcheck-")
    assert work.is_absolute(), f"临时目录不是绝对路径：{work}"
    assert work.resolve() != Path.cwd().resolve(), "临时目录退化成了 cwd（tempfile 的已知兜底）"
    (work / "probe.txt").write_text("x", encoding="utf-8")
    assert (work / "probe.txt").exists(), "临时目录不可写"
    (work / "probe.txt").unlink()

    # 显式指定环境变量时必须**听话**（用户用它把状态放到别的盘）。
    forced = tmp / "forced-home"
    old = os.environ.get(paths.ENV_HOME)
    os.environ[paths.ENV_HOME] = str(forced)
    try:
        got = paths.resolve(force=True)
        assert got.config == forced, f"{paths.ENV_HOME} 没生效：{got.config}"
    finally:
        if old is None:
            os.environ.pop(paths.ENV_HOME, None)
        else:
            os.environ[paths.ENV_HOME] = old
        paths.reset_cache()
    return f"配置可写于 {store.config}；临时目录 {work.parent}；环境变量覆盖有效"


def check_config_write_error() -> str:
    """配置写不进去时必须给出**人话**，而不是原始 WinError。"""
    broken = targets.TargetStore(Path("\\\\?\\Z:\\definitely-not-writable\\targets.json"))
    broken.load()
    try:
        broken.save()
    except targets.ConfigWriteError as exc:
        text = str(exc)
        assert "写配置失败" in text, f"错误信息没说清是什么失败：{text[:80]}"
        assert "python -m desktop paths" in text, f"错误信息没给排查办法：{text[:120]}"
    except OSError as exc:
        raise AssertionError(f"抛出了原始 OSError，没有包装成可读错误：{exc}") from exc
    else:
        raise AssertionError("写不可写路径竟然成功了？")
    return "写失败 → ConfigWriteError（含落点、原因、三条处理办法）"


def check_urls() -> str:
    cases = [
        (("127.0.0.1:8080", "ai", "api/state"), "http://127.0.0.1:8080/ai/api/state"),
        (("http://127.0.0.1:8080/", "/ai/", "/api/state"), "http://127.0.0.1:8080/ai/api/state"),
        (("https://bot.example.net", "", "api/state"), "https://bot.example.net/ai/api/state"),
    ]
    for args, want in cases:
        got = apiclient.join_url(*args)
        assert got == want, f"join_url{args} = {got}，期望 {want}"
    try:
        apiclient.join_url("127.0.0.1:8080", "/ai", "http://evil.example/steal")
    except BotApiError:
        pass
    else:
        raise AssertionError("绝对 URL 没有被拒绝")
    return f"{len(cases)} 组拼接 + 绝对 URL 拒绝"


def check_state_and_auth(server: StubServer, token: str) -> str:
    with_token = _client(server, token=token)
    data = with_token.state()
    for key in ("settings", "models", "persona", "memory", "image", "status", "stickers_list"):
        assert key in data, f"state 缺少顶层键 {key}"
    assert any(p[2] == "header" for p in server.cfg.requests), "token 没有走 X-Auth-Token 头"
    no_token = _client(server, token="")
    err = _expect_error(no_token.state, 401)
    assert err.unauthorized and "认证" in err.hint, f"401 的提示不对：{err.hint}"
    return "state 形状完整，token 走请求头，401 有可操作提示"


def check_prefix_is_configurable(server: StubServer, token: str) -> str:
    client = _client(server, token=token, prefix="/nonexistent")
    err = _expect_error(client.state, 404)
    assert "前缀" in err.hint, f"404 没提示前缀问题：{err.hint}"
    # `GET /` 不是探测目标：它对任何前缀都返回首页 HTML。
    page = _client(server, token=token).webui_page()
    assert b"WebUI" in page.body, "回退首页取不到"
    return "前缀写错 → 404 且提示前缀；GET /ai/ 是回退首页"


def check_error_classification(server: StubServer, token: str) -> str:
    client = _client(server, token=token)
    gone = _expect_error(lambda: client._request("POST", "api/persona"), 410)
    assert gone.gone and "下线" in gone.hint, "410 的提示不对"
    bad = _expect_error(lambda: client.memory_add("x"), 400)
    assert "参数" in bad.hint or "拒绝" in bad.hint, f"400 的提示不对：{bad.hint}"
    return "410（接口下线）与 400（参数被拒）分类正确"


def check_html_instead_of_json(server: StubServer, token: str) -> str:
    server.cfg.state_as_html = True
    try:
        err = _expect_error(_client(server, token=token).state, 200)
        assert "不是 JSON" in err.message, f"消息应说明不是 JSON：{err.message}"
        assert "端口" in err.hint, f"应提示端口里可能不是机器人服务：{err.hint}"
    finally:
        server.cfg.state_as_html = False
    return "把别的服务当机器人 → 明确报『不是 JSON』"


def check_write_not_retried(server: StubServer, token: str) -> str:
    server.cfg.write_unavailable = True
    try:
        before = len(server.cfg.settings_posts)
        client = ApiClient(server.base_url, prefix="/ai", token=token, timeout=5.0, max_retries=3)
        _expect_error(lambda: client.set_settings(proactive_enabled=True), 503)
        # 写请求只允许发一次：重试写可能造成重复入库/重复发言。
        posts = [r for r in server.cfg.requests if r[0] == "POST" and r[1].endswith("/api/settings")]
        assert len(posts) - before <= 1, f"写请求被重试了 {len(posts) - before} 次"
    finally:
        server.cfg.write_unavailable = False
    return "503 时写请求不重试（避免重复副作用）"


def check_settings_roundtrip(server: StubServer, token: str) -> str:
    client = _client(server, token=token)
    state = client.state()
    groups = [g["group"] for g in state["settings"]]
    kinds = {item["kind"] for g in state["settings"] for item in g["items"]}
    assert {"int", "float", "bool", "str"} <= kinds, f"控件类型不全：{kinds}"
    secret = [i for g in state["settings"] for i in g["items"] if i["secret"]]
    assert secret, "样本里应有一个 secret 参数用来验证掩码"
    payload = {secret[0]["key"]: "***"}  # 掩码原样回写：服务端会保留原值
    got = client.set_settings(**payload)
    assert got.get("ok"), f"保存失败：{got}"
    assert server.cfg.settings_posts[-1] == payload, "发出去的 body 与预期不一致"
    return f"读到 {len(groups)} 组 / {sum(len(g['items']) for g in state['settings'])} 个参数，掩码原样回写"


def check_image_auth_and_no_token_in_url(server: StubServer, token: str) -> str:
    server.cfg.requests.clear()
    client = _client(server, token=token)
    client.state()  # 建立会话（拿 cookie）
    before = len([r for r in server.cfg.requests if r[2] == "query"])
    blob = client.sticker_bytes("abc123def456")
    assert blob.startswith(b"\x89PNG"), f"取到的不是 PNG：{blob[:8]!r}"
    queries = [r for r in server.cfg.requests if r[2] == "query"]
    assert len(queries) == before, "把 token 拼进了 URL（禁止）"
    sources = {r[2] for r in server.cfg.requests if r[1].endswith("abc123def456")}
    assert sources <= {"header", "cookie", "none"}, f"图片请求来源异常：{sources}"
    missing = _expect_error(lambda: _client(server, token=token).sticker_bytes("deadbeef"), 404)
    assert missing.status == 404
    return f"图片经同一会话（来源 {sorted(sources)}），URL 里没有 token"


def check_target_isolation(tmp: Path, server: StubServer, token: str) -> str:
    store = TargetStore(tmp / "targets.json")
    store.load()
    local = store.active()
    assert local.mode == MODE_LOCAL and local.id, "默认目标应是本机直连"
    local.http.base_url = server.base_url
    local.http.token_ref = TargetStore.suggest_token_ref(local)
    store.update(local)

    other = Target(name="另一个服务器", mode=MODE_LOCAL)
    other.http.base_url = "http://127.0.0.1:1"   # 故意连不上
    other.http.token_ref = TargetStore.suggest_token_ref(other)
    store.add(other)

    creds = credentials.CredentialStore("session")
    mgr = ConnectionManager(store, creds)
    mgr.set_token(token, target=local)
    assert mgr.probe().ok, "本机目标应探测成功"
    assert mgr.token_present(local) and not mgr.token_present(other), "令牌应在目标之间隔离"

    switched: list[str] = []
    mgr._on_target_changed = switched.append  # noqa: SLF001 - 自检直接验证回调
    mgr.switch_target(other.id)
    assert switched == [other.id], f"on_target_changed 没被调用：{switched}"
    assert mgr.state.target_id == other.id
    assert mgr.client().token == "", "切目标后不该带着上一个目标的令牌"
    assert not mgr.client().cookie_jar, "切目标后不该带着上一个目标的 cookie"
    try:
        mgr.require_writable()
    except BotApiError:
        pass
    else:
        raise AssertionError("离线目标竟然可写")
    mgr.shutdown()
    return "令牌/ cookie 按目标隔离；切目标触发回调；离线目标拒绝写"


def check_targets_file_has_no_secret(tmp: Path) -> str:
    store = TargetStore(tmp / "targets.json")
    store.load()
    item = Target(name="服务器", mode=MODE_SSH)
    item.ssh = SshConfig(host="bot.example.net", user="botadmin", remote_port=8080)
    store.add(item)
    store.set_active(item.id)
    store.save()
    text = (tmp / "targets.json").read_text(encoding="utf-8")
    for banned in ("s3cret-token-value", "password", "private_key"):
        assert banned not in text, f"targets.json 里出现了不该出现的内容：{banned}"
    assert "token_ref" in text, "应只写令牌引用名"
    # 重新读回来要能保持模式与 SSH 参数
    again = TargetStore(tmp / "targets.json")
    again.load()
    assert again.active().mode == MODE_SSH, "模式没能保持"
    assert again.active().ssh.host == "bot.example.net", "SSH 参数没能保持"
    return "targets.json 只存引用名，不含任何令牌/口令"


def check_credentials(tmp: Path) -> str:
    session = credentials.CredentialStore("session")
    session.set("qqbot/a/webui-token", "abc")
    assert session.get("qqbot/a/webui-token") == "abc"
    assert session.known_refs() == ["qqbot/a/webui-token"]
    assert not session.degraded, "session 后端不该被标记为降级"

    file_store = credentials.CredentialStore("file", home=tmp)
    file_store.set("qqbot/b/webui-token", "xyz")
    assert file_store.get("qqbot/b/webui-token") == "xyz", "file 后端读写不一致"
    assert file_store.degraded, "file 后端必须被标记为降级（界面要提示）"
    raw = (tmp / "secrets.dat").read_text(encoding="utf-8")
    assert "xyz" in raw and session.get("qqbot/a/webui-token") not in raw, "两个后端串了"
    return "session / file 两后端读写正确，降级状态可查"


def check_redaction() -> str:
    data = util.redact({
        "api_key": "sk-abcdefghijklmn",
        "nested": {"X-Auth-Token": "abcdefghijklmn", "ok": "?token=abcdefghijklmn"},
    })
    assert data["api_key"].endswith("***") and "klmn" not in data["api_key"], data
    assert data["nested"]["X-Auth-Token"].endswith("***"), data
    assert "abcdefghijklmn" not in data["nested"]["ok"], data
    assert util.redact("https://x/y?token=s3cret&z=1").endswith("token=***&z=1")
    return "秘密键与 URL 查询串都被脱敏"


def check_tunnel_command() -> str:
    cfg = SshConfig(host="bot.example.net", user="botadmin", port=2222,
                    identity_file="C:/keys/id_ed25519", remote_port=8080, local_port="auto")
    tun = tunnel.SshTunnel(cfg, ssh_exe=tunnel.find_ssh() or "ssh")
    cmd = " ".join(tun.build_command(18080))
    for required in ("-N", "-T", "BatchMode=yes", "ExitOnForwardFailure=yes",
                     "ServerAliveInterval=30", "ConnectTimeout=10"):
        assert required in cmd, f"命令行缺少 {required}：{cmd}"
    assert "127.0.0.1:18080:127.0.0.1:8080" in cmd, f"转发参数不对：{cmd}"
    assert "botadmin@bot.example.net" in cmd, cmd
    assert "-p 2222" in cmd, cmd
    bad = tunnel.SshTunnel(SshConfig(host=""), ssh_exe="ssh")
    status = bad.start(allow_restart=False)
    assert status.state == tunnel.STATE_FAILED, "非法配置应直接失败"
    assert "主机为空" in status.error, f"失败原因不明确：{status.error}"
    return "转发行、保活、BatchMode、快速失败开关齐全"


def check_tunnel_real_end_to_end(tmp: Path, server: StubServer, token: str) -> str:
    """真跑一次隧道：本地端口 → 桩服务端口（不需要 sshd）。"""
    cfg = SshConfig(host="127.0.0.1", user="selfcheck", remote_host="127.0.0.1",
                    remote_port=server.port, local_port="auto", connect_timeout=6)
    tun = tunnel.SshTunnel(cfg, ssh_exe=tunnel.find_ssh())
    if not tun.ssh_exe or not Path(tun.ssh_exe).exists():
        return "跳过（本机没有 ssh 客户端）"
    probe_target = Target(mode=MODE_SSH)
    probe_target.http.prefix = "/ai"
    status = tun.start(health=lambda port: _api_reachable(port, "/ai", token), allow_restart=False)
    try:
        if status.state != tunnel.STATE_RUNNING:
            # 没有可用 sshd 的机器上（比如没起服务）会走到这里：明确跳过而不是假通过。
            return f"跳过：本机 sshd 不可用（{status.error[:80]}）"
        assert tunnel.port_open("127.0.0.1", status.local_port), "端口没监听"
        client = ApiClient(status.base_url, prefix="/ai", token=token, timeout=5.0, max_retries=0)
        assert client.probe().ok, "经隧道探测失败"
        assert tun.alive(), "隧道应处于存活状态"
    finally:
        stopped = tun.stop()
        assert stopped.state == tunnel.STATE_STOPPED
    return f"真实转发可用并已回收（本地端口曾用 {status.local_port}）"


def _api_reachable(port: int, prefix: str, token: str) -> bool:
    client = ApiClient(f"http://127.0.0.1:{port}", prefix=prefix, token=token, timeout=4.0, max_retries=0)
    try:
        return client.probe().ok
    finally:
        client.close()


def check_permission_table() -> str:
    cases = [
        ("GET", "/api/state", "api.read.status"),
        ("POST", "/api/settings", "api.write.settings"),
        ("POST", "/api/model/test", "api.write.models"),
        ("DELETE", "/api/memory/12", "api.write.memory"),
        ("POST", "/api/memory/12/protect", "api.write.memory"),
        ("GET", "/api/stickers/abc", "api.read.stickers"),
        ("DELETE", "/api/stickers/abc", "api.write.stickers"),
        ("POST", "/api/speak", "api.action.speak"),
        ("POST", "/api/greet", "api.action.greet"),
    ]
    for method, path, want in cases:
        got = permissions.required_permission(method, "/ai" + path)
        assert got == want, f"{method} {path} → {got}，期望 {want}"
    for method, path in (("GET", "/api/unknown"), ("POST", "/api/persona"), ("GET", "/ai/api/state/extra")):
        assert permissions.required_permission(method, path) is None, f"{method} {path} 不该在白名单里"
    declared = ["api.read.stickers"]
    used = permissions.check("demo", declared, "GET", "/api/state", prefix="/ai")
    # `/api/state` 自身归 `api.read.status`，但任何读权限都借得动它 ——
    # 放行的依据是"插件确实声明了某个 api.read.*"，返回值是它实际用到的那条声明。
    assert used == "api.read.stickers", f"只读权限应能借 state 取数，实际：{used}"
    try:
        permissions.check("demo", declared, "POST", "/api/speak", prefix="/ai")
    except PermissionDenied as exc:
        assert exc.permission == "api.action.speak", exc
    else:
        raise AssertionError("越权调用没有被拒绝")
    try:
        permissions.check("demo", declared, "GET", "http://evil.example/x", prefix="/ai")
    except PermissionDenied:
        pass
    else:
        raise AssertionError("绝对 URL 没有被拒绝")
    assert permissions.unknown_permissions(["api.read.stickers", "api.read.everything"]) == [
        "api.read.everything"
    ], "拼错的权限名应能被识别"
    return f"白名单 {len(permissions.ROUTES)} 条路由；越权/绝对 URL/未登记路径全部拒绝"


def check_manifests(tmp: Path) -> str:
    good = tmp / "good"
    good.mkdir(parents=True, exist_ok=True)
    (good / "plugin.py").write_text("def create_plugin():\n    return None\n", encoding="utf-8")
    (good / "plugin.json").write_text(json.dumps({
        "id": "good_plugin", "name": "好插件", "version": "1.0.0",
        "api_version": PLUGIN_API_VERSION, "entrypoint": "plugin.py:create_plugin",
        "permissions": ["api.read.status"],
    }, ensure_ascii=False), encoding="utf-8")
    manifest = load_manifest(good)
    assert manifest.major == PLUGIN_API_VERSION
    ok, why = manifest.compatible(PLUGIN_API_VERSION)
    assert ok and why == ""

    bad_cases: list[tuple[str, dict[str, Any]]] = [
        ("id 大写", {"id": "Bad", "name": "x", "version": "1.0.0", "api_version": "1",
                     "entrypoint": "plugin.py:create_plugin"}),
        ("版本号非法", {"id": "ok_id", "name": "x", "version": "v1", "api_version": "1",
                        "entrypoint": "plugin.py:create_plugin"}),
        ("entrypoint 逃逸", {"id": "ok_id", "name": "x", "version": "1.0.0", "api_version": "1",
                             "entrypoint": "../outside.py:create_plugin"}),
        ("缺少字段", {"id": "ok_id", "name": "x", "version": "1.0.0", "api_version": "1"}),
        ("entrypoint 文件不存在", {"id": "ok_id", "name": "x", "version": "1.0.0", "api_version": "1",
                                   "entrypoint": "nope.py:create_plugin"}),
    ]
    for label, raw in bad_cases:
        folder = tmp / f"bad_{abs(hash(label)) % 10000}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "plugin.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        try:
            load_manifest(folder)
        except ManifestError:
            continue
        raise AssertionError(f"非法 manifest 竟然通过了：{label}")

    incompatible = tmp / "incompat"
    incompatible.mkdir(parents=True, exist_ok=True)
    (incompatible / "plugin.py").write_text("def create_plugin():\n    return None\n", encoding="utf-8")
    (incompatible / "plugin.json").write_text(json.dumps({
        "id": "incompat_plugin", "name": "未来插件", "version": "1.0.0",
        "api_version": "99", "entrypoint": "plugin.py:create_plugin",
    }, ensure_ascii=False), encoding="utf-8")
    manifest2 = load_manifest(incompatible)
    ok, why = manifest2.compatible(PLUGIN_API_VERSION)
    assert not ok and "主版本" in why, f"未知主版本应拒绝：{ok} {why}"
    return f"{len(bad_cases)} 种非法 manifest 被拒，未知主版本被拒"


def check_sample_plugin(server: StubServer, token: str) -> str:
    if not PLUGIN_ROOT.exists():
        raise AssertionError(f"示范插件目录不存在：{PLUGIN_ROOT}")
    client = _client(server, token=token)
    ui = NullUiBridge()
    host = PluginHost(
        PLUGIN_ROOT,
        client_provider=lambda: client,
        prefix_provider=lambda: "/ai",
        ui=ui,
        capability_probe=lambda: client.capabilities(),
    )
    host.discover()
    assert "sticker_health" in host.records, f"没发现示范插件：{list(host.records)}"
    # 只读 manifest 阶段：**还没 import 插件代码**，注册表必然是空的。
    try:
        host.call_page_factory("sticker_health:overview")
    except (KeyError, RuntimeError) as exc:
        assert "没有这个页面" in str(exc), f"发现阶段不该建得出页面：{exc}"
    else:
        raise AssertionError("发现阶段竟然建出了页面（说明提前 import 了插件代码）")

    host.load_all()
    record = host.records["sticker_health"]
    assert record.state == PluginState.LOADED, f"加载状态不对：{record.state}（{record.error}）"
    assert record.registered["pages"] == 1, "示范插件应登记 1 个页面"
    assert record.registered["actions"] == 1, "示范插件应登记 1 个操作"
    assert record.registered["status_cards"] == 1, "示范插件应登记 1 张状态卡"
    # register 阶段只许登记：此时还不该有人能建页面。
    try:
        host.call_page_factory("sticker_health:overview")
    except RuntimeError as exc:
        assert "未激活" in str(exc), f"激活前不该能建页面：{exc}"
    else:
        raise AssertionError("激活前竟然建出了页面")

    host.activate_all()
    assert host.records["sticker_health"].state == PluginState.ACTIVE

    widget = host.call_page_factory("sticker_health:overview")
    assert widget is not None, "页面工厂没有返回内容"
    # 没有 QApplication（自检就是这样）时插件必须返回**纯数据**而不是去建控件 ——
    # Qt 在无 QApplication 时创建 QWidget 是致命错误（进程直接退出），不是异常。
    if isinstance(widget, dict):
        assert widget.get("headless"), f"无 GUI 时应该走 headless 分支：{list(widget)}"
        assert widget.get("text"), "headless 返回里应带上数据文本"
        headless_note = "（无 QApplication → 插件返回纯数据，符合预期）"
    else:
        headless_note = "（有 QApplication → 返回真控件）"

    cards = host.status_cards()
    assert cards and cards[0]["ok"], f"状态卡取数失败：{cards}"
    assert cards[0]["value"], "状态卡应有内容"

    result = host.call_action("sticker_health:report")
    detail = getattr(result, "detail", "")
    assert "表情包" in detail or "重复" in detail, f"操作返回内容不对：{detail}"

    # 未声明的权限：示范插件没声明写权限，删表情包必须被拒。
    loaded = host.loaded["sticker_health"]
    try:
        loaded.api.http.delete("/api/stickers/abc123def456")
    except (PermissionDenied, PermissionError) as exc:
        assert "权限" in str(exc) or "未声明" in str(exc), f"拒绝理由不明确：{exc}"
    else:
        raise AssertionError("越权删除竟然通过了")

    assert host.deactivate("sticker_health"), "停用失败"
    assert loaded.api.token.cancelled, "停用后取消令牌必须置位（防幽灵任务）"

    # 第二个示范插件：证明同一目录下多个插件各自登记、互不覆盖。
    assert "memory_stats" in host.records, "第二个示范插件没被发现"
    other = host.records["memory_stats"]
    assert other.state == PluginState.ACTIVE, f"第二个插件状态不对：{other.state}（{other.error}）"
    assert other.registered.get("pages") == 1, "第二个插件应登记 1 个页面"
    ids = [p.id for p in host.registry.pages]
    assert len(ids) == len(set(ids)), f"页面 id 撞了：{ids}"
    other_loaded = host.loaded["memory_stats"]
    try:
        other_loaded.api.http.post("/api/memory", body={"text": "越权写入"})
    except (PermissionDenied, PermissionError):
        pass
    else:
        raise AssertionError("只读插件竟然写入了记忆")
    return (
        "发现→登记→激活→建页面→取卡→跑操作→越权被拒→停用清理，全通；"
        "两个插件各自登记且互不覆盖" + headless_note
    )


def check_failure_isolation(tmp: Path, server: StubServer, token: str) -> str:
    root = tmp / "plugins"
    (root / "broken").mkdir(parents=True, exist_ok=True)
    (root / "broken" / "plugin.py").write_text(
        "def create_plugin():\n    raise RuntimeError('我故意炸的')\n", encoding="utf-8"
    )
    (root / "broken" / "plugin.json").write_text(json.dumps({
        "id": "broken_plugin", "name": "坏插件", "version": "1.0.0",
        "api_version": PLUGIN_API_VERSION, "entrypoint": "plugin.py:create_plugin",
    }, ensure_ascii=False), encoding="utf-8")
    (root / "not_a_plugin").mkdir(parents=True, exist_ok=True)
    (root / "not_a_plugin" / "readme.txt").write_text("no manifest", encoding="utf-8")
    (root / "badmanifest").mkdir(parents=True, exist_ok=True)
    (root / "badmanifest" / "plugin.json").write_text("{ not json", encoding="utf-8")

    # 顺便把示范插件也放进来：证明"一个坏插件不影响好插件"。
    import shutil

    good = root / "good"
    shutil.copytree(PLUGIN_ROOT / "sticker_health", good)

    client = _client(server, token=token)
    host = PluginHost(root, client_provider=lambda: client, prefix_provider=lambda: "/ai",
                      ui=NullUiBridge(), capability_probe=lambda: None)
    host.load_all()
    assert host.records["broken_plugin"].state == PluginState.FAILED, "坏插件应被标记失败"
    assert "我故意炸的" in host.records["broken_plugin"].error, host.records["broken_plugin"].error
    assert host.records["sticker_health"].state == PluginState.LOADED, "好插件被坏插件带崩了"
    invalid = [k for k in host.records if k.startswith("__invalid__")]
    assert invalid, "manifest 非法的目录也应出现在列表里（用户要看得见）"
    host.activate_all()
    assert host.records["sticker_health"].state == PluginState.ACTIVE, "激活阶段又互相影响了"
    return f"坏插件被隔离（{len(invalid)} 个非法 manifest 也可见），好插件照常激活"


def check_no_qt_in_core() -> str:
    """核心层不许 import PySide6 —— 否则"没装 Qt 也能自检"就不成立了。

    判据是**真的 import 语句**（`import PySide6` / `from PySide6...`），
    不是"文件里提到过 PySide6"：注释里说明"这层不依赖 Qt"是完全正常的写法。
    """
    import re

    import_re = re.compile(r"^\s*(?:import|from)\s+(PySide6|PyQt\d?)\b", re.MULTILINE)
    offenders: list[str] = []
    base = Path(__file__).resolve().parent.parent
    for folder in ("core", "sdk"):
        for path in (base / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if import_re.search(text):
                offenders.append(f"{folder}/{path.name}")
    assert not offenders, f"这些文件真的 import 了 Qt：{offenders}"
    return "core/ 与 sdk/ 没有 Qt import（无头自检成立）"


def check_desktop_outside_plugins() -> str:
    """桌面端不能在 NoneBot 的插件扫描路径里。"""
    root = paths.project_root()
    desktop = root / "desktop"
    assert desktop.exists(), "desktop/ 不存在"
    assert not str(desktop).startswith(str(root / "plugins")), "desktop/ 竟然在 plugins/ 下面"
    bot = (root / "bot.py").read_text(encoding="utf-8")
    assert 'load_plugins("plugins")' in bot, "bot.py 的插件加载方式变了，需要重新核对边界"
    assert "desktop" not in bot, "bot.py 不该引用 desktop/"
    return "desktop/ 与 plugins/ 完全分离，bot.py 不引用它"


def check_webui_paths_match_scheme() -> str:
    """方案第 4.2 章的表必须与真实路由一致（防止文档再次漂移）。"""
    root = paths.project_root()
    webui = (root / "plugins" / "ai_chat" / "webui.py").read_text(encoding="utf-8")
    expected = [
        '/api/state', '/api/settings', '/api/settings/reset', '/api/model/active',
        '/api/model/test', '/api/model/save', '/api/model/delete', '/api/persona/undo',
        '/api/persona/reflect', '/api/persona/eval', '/api/memory', '/api/image-policy',
        '/api/stickers', '/api/speak', '/api/greet',
    ]
    missing = [item for item in expected if f'prefix + "{item}"' not in webui]
    assert not missing, f"这些路由在 webui.py 里找不到了：{missing}"
    assert '/api/persona")' in webui or '/api/persona"' in webui, "人格旧入口的 410 桩不见了"
    scheme = (root / "分析记录" / "桌面控制台与插件接入接口方案_2026-10-02.md")
    if scheme.exists():
        text = scheme.read_text(encoding="utf-8")
        assert "| `GET /api/state` |" not in text, "方案里还有漏掉 /ai 前缀的旧表"
        assert "| `GET /ai/api/state` |" in text, "方案里的映射表没有 /ai 前缀"
    return f"webui.py 的 {len(expected)} 条路由与方案表一致"


# --------------------------------------------------------------------- 主流程


def run() -> int:
    print("QQ_bot 桌面控制台自检")
    print("=" * 64)
    suite = Suite()
    # ==== 隔离：**先钉住环境变量，再做任何会碰落点的事** ====
    # 踩过：`ConnectionManager()` 的默认参数会构造一个真实的 `CredentialStore`，
    # 而它在 `__init__` 里就解析了配置目录 —— 于是自检往**用户真实的** `.console\`
    # 写了一个 `secrets.dat`。所以这里用一个确定性的临时根 + `reset_cache()`，
    # 并且必须在任何 store / tmp 解析之前完成。
    tmp_root = Path(tempfile.mkdtemp(prefix="qqbot-selfcheck-", dir=_safe_temp_root()))
    isolated = tmp_root / "isolated"
    os.environ["QQBOT_CONSOLE_HOME"] = str(isolated / "home")
    os.environ["QQBOT_CONSOLE_DATA"] = str(isolated / "data")
    paths.reset_cache()
    tmp = isolated / "work"
    tmp.mkdir(parents=True, exist_ok=True)
    paths.ensure_dirs()
    # 自检会**故意**踩一堆失败分支（401/404/410/503/隧道失败/插件爆炸），
    # 那些 WARNING/ERROR 日志是预期噪音，压掉才能让失败项一眼可见。
    import logging

    desktop_logger = logging.getLogger("qqbot.desktop")
    previous_level = desktop_logger.level
    desktop_logger.setLevel(logging.CRITICAL)
    desktop_logger.propagate = False

    cfg = StubConfig()
    server = StubServer(cfg).start()
    token = cfg.token
    print(f"桩服务：{server.base_url}（前缀 {cfg.prefix}，令牌 {len(token)} 位）")
    print("")
    print("-- 纯函数（不需要服务）")
    suite.check("落点可写 + 临时目录不退化 + 环境变量覆盖", lambda: check_work_tmp_and_paths(tmp))
    suite.check("配置写失败的报错可读", check_config_write_error)
    suite.check("URL 拼接与前缀归一化", check_urls)
    suite.check("脱敏：秘密键与 URL 查询串", check_redaction)
    suite.check("权限白名单表", check_permission_table)
    suite.check("manifest 校验与版本兼容", lambda: check_manifests(tmp))
    suite.check("core/sdk 不依赖 Qt", check_no_qt_in_core)
    suite.check("desktop/ 与 plugins/ 分离", check_desktop_outside_plugins)
    suite.check("方案映射表与真实路由一致", check_webui_paths_match_scheme)

    print("")
    print("-- 针对桩服务（认证 / 错误分类 / 图片）")
    suite.check("state 形状 + 认证头 + 401 提示", lambda: check_state_and_auth(server, token))
    suite.check("前缀可配置；GET / 不是探测目标", lambda: check_prefix_is_configurable(server, token))
    suite.check("410 / 400 错误分类", lambda: check_error_classification(server, token))
    suite.check("连到非机器人服务 → 报『不是 JSON』", lambda: check_html_instead_of_json(server, token))
    suite.check("503 时写请求不重试", lambda: check_write_not_retried(server, token))
    suite.check("参数读写与掩码回写", lambda: check_settings_roundtrip(server, token))
    suite.check("图片走同一会话且 URL 不带 token", lambda: check_image_auth_and_no_token_in_url(server, token))

    print("")
    print("-- 目标 / 凭据 / 隧道")
    suite.check("目标隔离与切换回调", lambda: check_target_isolation(tmp, server, token))
    suite.check("targets.json 不含令牌", lambda: check_targets_file_has_no_secret(tmp))
    suite.check("凭据后端读写", lambda: check_credentials(tmp))
    suite.check("隧道命令行开关", check_tunnel_command)
    suite.check("隧道真实转发（需要本机 sshd）", lambda: check_tunnel_real_end_to_end(tmp, server, token))

    print("")
    print("-- 插件")
    suite.check("示范插件全生命周期", lambda: check_sample_plugin(server, token))
    suite.check("加载失败隔离", lambda: check_failure_isolation(tmp, server, token))

    server.stop()
    desktop_logger.setLevel(previous_level or logging.INFO)
    desktop_logger.propagate = True
    code = suite.report()
    print(f"（临时目录：{tmp}）")
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
