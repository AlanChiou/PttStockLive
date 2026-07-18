#!/usr/bin/env python3
"""Shared PTT helpers used by ptt_danmaku.py and test_ws.py."""

from __future__ import annotations

import os
import stat
from typing import Optional, Tuple

_UAO_READY = False


def _ensure_uao() -> bool:
    """Register uao codec once. Returns True if usable."""
    global _UAO_READY
    if _UAO_READY:
        return True
    try:
        import uao  # type: ignore

        if hasattr(uao, "register_uao"):
            uao.register_uao()
        _UAO_READY = True
        return True
    except Exception:
        return False


def try_decode(data: bytes) -> str:
    """Decode PTT WS bytes (Big5-UAO preferred)."""
    if _ensure_uao():
        for name in ("uao_unicode", "uao", "big5-uao"):
            try:
                return data.decode(name, errors="replace")
            except Exception:
                continue
    for enc in ("big5hkscs", "big5", "cp950"):
        try:
            return data.decode(enc, errors="replace")
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def encode_big5(text: str) -> bytes:
    """Encode unicode for PTT input (search keywords, etc.)."""
    if _ensure_uao():
        try:
            import uao  # type: ignore

            if hasattr(uao, "encode"):
                return uao.encode(text)
            return text.encode("uao_unicode", errors="replace")
        except Exception:
            pass
    return text.encode("big5hkscs", errors="replace")


def encode_login_field(text: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Encode account/password for PTT login.
    Returns (bytes, None) on success, or (None, error_message).
    """
    if text is None:
        return None, "empty credential"
    try:
        return text.encode("ascii"), None
    except UnicodeEncodeError:
        pass
    try:
        b = text.encode("latin-1")
        return b, None
    except UnicodeEncodeError:
        return None, "帳號/密碼含無法以 latin-1 傳送的字元，請改用 ASCII 相容密碼"


def _warn_credential_perms(path: str) -> None:
    try:
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            print(
                f"[WARN] 憑證檔權限過寬：{path} "
                f"(mode {stat.filemode(mode)})，建議 chmod 600"
            )
    except Exception:
        pass


def load_ptt_credentials() -> Tuple[Optional[str], Optional[str]]:
    """
    Load PTT account/password.
    Priority: env PTT_ACCOUNT/PTT_PASSWORD → .pttrc / ~/.pttrc / .env / ~/.env
    """
    account = os.environ.get("PTT_ACCOUNT") or os.environ.get("PTT_ID")
    password = os.environ.get("PTT_PASSWORD") or os.environ.get("PTT_PASS")
    if account and password:
        return account, password

    candidates = [
        ".pttrc",
        os.path.expanduser("~/.pttrc"),
        ".env",
        os.path.expanduser("~/.env"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            _warn_credential_perms(path)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            env = {}
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip("\"'")
            acc = env.get("PTT_ACCOUNT") or env.get("PTT_ID")
            pwd = env.get("PTT_PASSWORD") or env.get("PTT_PASS")
            if acc and pwd:
                return acc, pwd
        except Exception:
            continue
    return None, None


def env_kick_other_sessions() -> bool:
    """Whether to answer y on duplicate-login prompt (default True)."""
    v = (os.environ.get("PTT_KICK_OTHER") or "1").strip().lower()
    return v not in ("0", "false", "no", "n", "off")
