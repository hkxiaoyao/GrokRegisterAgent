# -*- coding: utf-8 -*-
"""Protocol Create/Verify email codes (no React UI).

UI Sign up on accounts.x.ai currently yields “Something went wrong” under automation;
curl_cffi chrome131 + proxy CreateEmailValidationCode works (grpc-status:0 + mail arrives).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent


def _load_proxy() -> str:
    try:
        conf = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        conf = {}
    p = str(
        conf.get("proxy")
        or conf.get("browser_proxy")
        or conf.get("resolved_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or ""
    ).strip()
    if p:
        return p
    if conf.get("singbox_enabled") is True or str(conf.get("singbox_enabled")).lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        port = conf.get("singbox_mixed_port") or 2080
        try:
            port = int(port)
        except Exception:
            port = 2080
        return f"http://127.0.0.1:{port}"
    return ""


def protocol_create_email_code(
    email: str,
    *,
    proxy: str = "",
    cookies: Optional[dict] = None,
    log: Any = print,
) -> dict:
    """POST CreateEmailValidationCode. Returns {ok, status, grpc, detail}."""
    email = str(email or "").strip()
    if not email:
        return {"ok": False, "error": "empty email"}
    proxy = (proxy or _load_proxy()).strip()
    try:
        from protocol.grpc_client import AuthManagementClient
        from protocol.session import ProtocolSession
    except Exception as e:
        return {"ok": False, "error": f"import:{e}"}

    try:
        sess = ProtocolSession(proxy=proxy, impersonate="chrome131")
        # Warm page for CF cookies when jar empty
        if not cookies:
            try:
                w = sess.bootstrap(timeout=30)
                log(f"[protocol] warm signup status={getattr(w, 'status_code', '?')}")
            except Exception as we:
                log(f"[protocol] warm skip: {we}")
        else:
            sess.set_cookies(dict(cookies))
            try:
                sess.bootstrap(timeout=25)
            except Exception:
                pass
        client = AuthManagementClient(sess)
        r = client.create_email_validation_code(email, "")
        status = int(r.get("status") or 0)
        raw = r.get("raw") or b""
        grpc = ""
        try:
            # trailer often in body: grpc-status:0
            if b"grpc-status:0" in raw or b"grpc-status: 0" in raw:
                grpc = "0"
            else:
                for line in raw.decode("latin-1", errors="ignore").splitlines():
                    if line.lower().startswith("grpc-status"):
                        grpc = line.split(":", 1)[-1].strip()
        except Exception:
            pass
        hdrs = r.get("headers") or {}
        if not grpc:
            grpc = str(hdrs.get("grpc-status") or hdrs.get("Grpc-Status") or "")
        ok = status == 200 and (grpc in ("", "0") or grpc == "0")
        # empty body + 200 + grpc-status:0 frame is success
        if status == 200 and (b"grpc-status:0" in raw or grpc == "0"):
            ok = True
        log(
            f"[protocol] CreateEmail status={status} grpc={grpc!r} ok={ok} "
            f"proxy={'yes' if proxy else 'no'}"
        )
        return {
            "ok": ok,
            "status": status,
            "grpc": grpc,
            "raw_len": len(raw),
            "strings": (r.get("strings") or [])[:3],
        }
    except Exception as e:
        log(f"[protocol] CreateEmail exception: {e}")
        return {"ok": False, "error": str(e)}


def protocol_verify_email_code(
    email: str,
    code: str,
    *,
    proxy: str = "",
    cookies: Optional[dict] = None,
    log: Any = print,
) -> dict:
    email = str(email or "").strip()
    code = str(code or "").replace("-", "").strip()
    if not email or not code:
        return {"ok": False, "error": "empty email/code"}
    proxy = (proxy or _load_proxy()).strip()
    try:
        from protocol.grpc_client import AuthManagementClient
        from protocol.session import ProtocolSession
    except Exception as e:
        return {"ok": False, "error": f"import:{e}"}
    try:
        sess = ProtocolSession(proxy=proxy, impersonate="chrome131")
        if cookies:
            sess.set_cookies(dict(cookies))
        try:
            sess.bootstrap(timeout=20)
        except Exception:
            pass
        client = AuthManagementClient(sess)
        r = client.verify_email_validation_code(email, code)
        status = int(r.get("status") or 0)
        raw = r.get("raw") or b""
        ok = status == 200 and (
            b"grpc-status:0" in raw or status == 200
        )
        log(f"[protocol] VerifyEmail status={status} ok={ok}")
        return {"ok": ok, "status": status, "raw_len": len(raw)}
    except Exception as e:
        log(f"[protocol] VerifyEmail exception: {e}")
        return {"ok": False, "error": str(e)}
