"""令牌保管：优先 Windows 凭据管理器，退化为受限权限文件。

设计取向（与方案第 6 节一致）：**令牌不进明文配置文件**。

- `dpapi`：Windows 上把密文交给 DPAPI（当前用户作用域），落到 `secrets.dat`。
  这不是"加密算法自己造"，密钥由系统托管；文件拷到别的机器/别的用户下解不开。
- `file`：非 Windows 或 DPAPI 不可用时的兜底 —— **明确标记为降级**，界面上要提示，
  并同时提示"更稳的做法是把令牌只放在会话内存里"。
- `session`：只存内存，进程退出即失效（自检与一次性使用）。

所有后端都实现 `get/set/delete/backend`，上层（连接设置页）不需要知道区别。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from pathlib import Path

from . import paths

logger = logging.getLogger("qqbot.desktop.credentials")

# 前缀告诉后续版本"这个值是用哪条路径存进去的"，将来换实现能识别旧数据。
_FILE_PREFIX = "plainv1:"
_DPAPI_DESC = "QQ_bot 桌面控制台"


class CredentialError(RuntimeError):
    """凭据读写失败（上层提示用户改用会话内存或修权限）。"""


# --------------------------------------------------------------------- DPAPI


def _dpapi_available() -> bool:
    return sys.platform == "win32"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from(data: bytes) -> _DataBlob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_protect(text: str) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    blob_in = _blob_from(text.encode("utf-8"))
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), _DPAPI_DESC, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise CredentialError(f"CryptProtectData 失败：{ctypes.GetLastError()}")
    try:
        return _blob_bytes(blob_out)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> str:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    blob_in = _blob_from(data)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise CredentialError(
            "CryptUnprotectData 失败：密文不属于当前 Windows 用户，或文件被替换过"
        )
    try:
        return _blob_bytes(blob_out).decode("utf-8", errors="replace")
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ------------------------------------------------------------------ 存储后端


class CredentialStore:
    """按 `ref`（形如 `qqbot/server-prod/webui-token`）存取令牌。"""

    def __init__(self, backend: str = "auto", *, home: Path | None = None) -> None:
        self._home = Path(home) if home else paths.config_home()
        self._session: dict[str, str] = {}
        self._file_cache: dict[str, str] | None = None
        self._file_dirty = False
        self.backend = self._pick_backend(backend)

    # -- 后端选择 ---------------------------------------------------------
    def _pick_backend(self, wanted: str) -> str:
        if wanted == "auto":
            return "dpapi" if _dpapi_available() else "file"
        if wanted not in ("dpapi", "file", "session"):
            raise CredentialError(f"未知凭据后端：{wanted}")
        if wanted == "dpapi" and not _dpapi_available():
            logger.warning("DPAPI 只在 Windows 可用，改用 file 后端")
            return "file"
        return wanted

    @property
    def degraded(self) -> bool:
        """是否为降级存储（界面上要显示提示）。session 不算降级：它更安全但会丢。"""
        return self.backend == "file"

    def describe(self) -> str:
        return {
            "dpapi": "Windows DPAPI（当前用户加密，落在 secrets.dat）",
            "file": "降级：明文混淆文件（仅建议本机自用）",
            "session": "仅本次会话内存（退出即失效）",
        }[self.backend]

    # -- 文件后端 ---------------------------------------------------------
    def _file_path(self) -> Path:
        return self._home / "secrets.dat"

    def _load_file(self) -> dict[str, str]:
        if self._file_cache is not None:
            return self._file_cache
        path = self._file_path()
        data: dict[str, str] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {str(k): str(v) for k, v in raw.items()}
            except (OSError, ValueError):
                logger.warning("凭据文件损坏，按空处理：%s", path)
        self._file_cache = data
        return data

    def _save_file(self) -> None:
        path = self._file_path()
        payload = json.dumps(self._file_cache or {}, ensure_ascii=False)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if self.backend == "dpapi":
                blob = _dpapi_protect(payload)
                path.write_bytes(b"dpapiv1:" + base64.b64encode(blob))
            else:
                path.write_text(_FILE_PREFIX + payload, encoding="utf-8")
        except OSError as exc:
            # 令牌存不下**不能静默**：用户会以为"保存成功"了，下次连接才发现是空的。
            raise CredentialError(
                f"令牌存不下：{path}\n原因：{type(exc).__name__}: {exc}\n"
                "可以改用「仅本次会话」（只放内存，退出即失效），"
                "或用 `python -m desktop paths` 检查配置目录是否可写。"
            ) from exc
        _harden_permissions(path)

    # -- 对外接口 ---------------------------------------------------------
    def get(self, ref: str) -> str:
        if not ref:
            return ""
        if ref in self._session:
            return self._session[ref]
        if self.backend == "session":
            return ""
        try:
            data = self._load_file()
        except CredentialError as exc:
            logger.warning("读凭据失败（%s）：%s", ref, exc)
            return ""
        stored = data.get(ref, "")
        if not stored:
            return ""
        if stored.startswith("dpapiv1:"):
            try:
                return _dpapi_unprotect(base64.b64decode(stored[8:]))
            except CredentialError as exc:
                logger.warning("解不开该条凭据（%s）：%s", ref, exc)
                return ""
        if stored.startswith(_FILE_PREFIX):
            return stored[len(_FILE_PREFIX):]
        return stored  # 历史格式：直接是明文

    def set(self, ref: str, value: str, *, session_only: bool = False) -> str:
        """写入并返回实际生效的后端名。空值 = 删除该条。"""
        if not ref:
            raise CredentialError("credential ref 不能为空")
        if session_only or self.backend == "session":
            if value:
                self._session[ref] = value
            else:
                self._session.pop(ref, None)
            return "session"
        data = self._load_file()
        if value:
            data[ref] = ("dpapiv1:" + base64.b64encode(_dpapi_protect(value)).decode("ascii")
                         if self.backend == "dpapi" else _FILE_PREFIX + value)
        else:
            data.pop(ref, None)
        self._file_dirty = True
        self._save_file()
        return self.backend

    def delete(self, ref: str) -> bool:
        self._session.pop(ref, None)
        if self.backend == "session":
            return True
        data = self._load_file()
        if ref not in data:
            return False
        data.pop(ref, None)
        self._file_dirty = True
        self._save_file()
        return True

    def known_refs(self) -> list[str]:
        """已存过令牌的引用名（**不含值**，界面只用来显示"已保存"）。"""
        refs = set(self._session)
        if self.backend != "session":
            try:
                refs |= set(self._load_file())
            except CredentialError:
                pass
        return sorted(refs)


def _harden_permissions(path: Path) -> None:
    """文件兜底时尽量收权限：POSIX 下 600；Windows 下靠目录 ACL。"""
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
