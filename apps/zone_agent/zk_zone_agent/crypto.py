from __future__ import annotations

import base64
import platform


def protect_secret(secret: str) -> str:
    if not secret:
        return ""
    if platform.system() == "Windows":
        try:
            import win32crypt  # type: ignore

            encrypted = win32crypt.CryptProtectData(secret.encode("utf-8"), None, None, None, None, 0)
            return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
        except Exception:
            pass
    return "base64:" + base64.b64encode(secret.encode("utf-8")).decode("ascii")


def unprotect_secret(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("dpapi:"):
        raw = base64.b64decode(value.removeprefix("dpapi:"))
        import win32crypt  # type: ignore

        return win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1].decode("utf-8")
    if value.startswith("base64:"):
        return base64.b64decode(value.removeprefix("base64:")).decode("utf-8")
    return value
