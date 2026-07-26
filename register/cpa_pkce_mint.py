# -*- coding: utf-8 -*-
"""Pure-HTTP SSO cookie -> OAuth PKCE tokens (CreateCookieSetterLink path).

Ported/adapted from srcback/grok_reg-protocol_cpa-master/cpa_xai/pkce_mint.py

Key path:
  set sso → GET authorize → CreateCookieSetterLink → follow set-cookie
  → consent form POST (Allow) → code → token
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import cpa_grpcweb as grpcweb

# Keep in sync with CLIProxyAPI / our cpa_schema
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/oauth2/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/oauth2/token"
ACCOUNTS_ORIGIN = "https://accounts.x.ai"
CREATE_COOKIE_SETTER_RPC = (
    f"{ACCOUNTS_ORIGIN}/auth_mgmt.AuthManagement/CreateCookieSetterLink"
)
DEFAULT_REDIRECT_URI = "http://127.0.0.1:56121/callback"
DEFAULT_IMPERSONATE = "chrome131"

LogFn = Callable[[str], None]
_CONFIG = Path(__file__).resolve().parent / "config.json"


class PKCEMintError(RuntimeError):
    """PKCE protocol path failed; caller may fall back to other mint methods."""


def _noop_log(_: str) -> None:
    return None


def _load_conf() -> dict[str, Any]:
    try:
        if _CONFIG.is_file():
            conf = json.loads(_CONFIG.read_text(encoding="utf-8"))
            return conf if isinstance(conf, dict) else {}
    except Exception:
        pass
    return {}


def load_mint_impersonate() -> str:
    """TLS fingerprint for curl_cffi mint. Default chrome131 (srcback proven)."""
    conf = _load_conf()
    env = (os.environ.get("CPA_MINT_IMPERSONATE") or "").strip()
    if env:
        return env
    raw = (
        conf.get("cpa_mint_impersonate")
        or conf.get("cpaMintImpersonate")
        or conf.get("protocol_impersonate")
        or DEFAULT_IMPERSONATE
    )
    return str(raw or DEFAULT_IMPERSONATE).strip() or DEFAULT_IMPERSONATE


def allow_device_flow_fallback() -> bool:
    """Align srcback cpa_allow_device_flow_fallback (default False = cleaner)."""
    conf = _load_conf()
    env = (os.environ.get("CPA_ALLOW_DEVICE_FLOW_FALLBACK") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    for k in (
        "cpa_allow_device_flow_fallback",
        "cpaAllowDeviceFlowFallback",
        "allow_device_flow_fallback",
    ):
        if k in conf:
            v = conf.get(k)
            if isinstance(v, bool):
                return v
            return str(v).lower() in ("1", "true", "yes", "on")
    return False


def prefer_cookie_setter_pkce() -> bool:
    """Default True: try CreateCookieSetterLink PKCE first."""
    conf = _load_conf()
    env = (os.environ.get("CPA_PREFER_COOKIE_SETTER_PKCE") or "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    for k in (
        "cpa_prefer_cookie_setter_pkce",
        "cpaPreferCookieSetterPkce",
        "cpa_prefer_protocol",
        "cpaPreferProtocol",
    ):
        if k in conf:
            v = conf.get(k)
            if isinstance(v, bool):
                return v
            return str(v).lower() in ("1", "true", "yes", "on")
    return True


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _code_verifier() -> str:
    return _b64url(secrets.token_bytes(48))


def _code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _session(proxy: str | None, impersonate: str | None = None):
    try:
        from curl_cffi import requests as cf_requests
    except ImportError as e:
        raise PKCEMintError("curl_cffi not installed; cannot run PKCE mint") from e

    imp = (impersonate or load_mint_impersonate()).strip() or DEFAULT_IMPERSONATE
    kwargs: dict[str, Any] = {"impersonate": imp}
    resolved = (proxy or "").strip() or None
    if resolved:
        kwargs["proxies"] = {"http": resolved, "https": resolved}
    return cf_requests.Session(**kwargs)


def _set_sso_cookie(session: Any, sso_cookie: str) -> None:
    sso_cookie = (sso_cookie or "").strip()
    if not sso_cookie:
        raise PKCEMintError("empty sso cookie")
    for domain in ("accounts.x.ai", ".accounts.x.ai", ".x.ai", "auth.x.ai"):
        try:
            session.cookies.set("sso", sso_cookie, domain=domain, path="/")
        except Exception:
            pass
        try:
            session.cookies.set("sso-rw", sso_cookie, domain=domain, path="/")
        except Exception:
            pass


def _grpc_headers(referer: str) -> dict[str, str]:
    return {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "accept": "*/*",
        "origin": ACCOUNTS_ORIGIN,
        "referer": referer,
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def _extract_urls_from_fields(fields: list[dict[str, Any]]) -> list[str]:
    urls: list[str] = []
    for field in fields:
        if field.get("type") == "string":
            value = str(field.get("value") or "")
            if value.startswith(("http://", "https://")):
                urls.append(value)
        elif field.get("type") == "bytes" and field.get("hex"):
            try:
                urls.extend(
                    _extract_urls_from_fields(
                        grpcweb.decode_message(bytes.fromhex(field["hex"]))
                    )
                )
            except Exception:
                pass
    return urls


def _parse_grpc_error(headers: dict[str, str], body: bytes) -> tuple[int | None, str]:
    status = headers.get("grpc-status")
    message = unquote(headers.get("grpc-message") or "")
    if status is not None:
        try:
            return int(status), message
        except ValueError:
            return None, message
    try:
        parsed = grpcweb.parse_response(body)
    except Exception:
        return None, message
    if parsed.get("grpc_status") is not None:
        return int(parsed["grpc_status"]), message or str(parsed.get("trailers") or "")
    return None, message


def _build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    code_challenge: str,
    scope: str,
    referrer: str = "cli-proxy-api",
    plan: str = "generic",
) -> str:
    params = {
        "client_id": client_id,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "nonce": nonce,
        "plan": plan,
        "redirect_uri": redirect_uri,
        "referrer": referrer,
        "response_type": "code",
        "scope": scope,
        "state": state,
    }
    return AUTHORIZATION_ENDPOINT + "?" + urlencode(params)


def _code_from_url(url: str, state: str) -> str:
    qs = parse_qs(urlparse(url).query)
    if (qs.get("state") or [""])[0] != state:
        raise PKCEMintError("authorization failed: state mismatch")
    code = (qs.get("code") or [""])[0]
    if not code:
        raise PKCEMintError(f"authorization failed: missing code in {url[:200]}")
    return code


def _create_cookie_setter_link(session: Any, success_url: str) -> str:
    msg = grpcweb.encode_string(1, success_url) + grpcweb.encode_string(
        2, f"{ACCOUNTS_ORIGIN}/sign-in"
    )
    resp = session.post(
        CREATE_COOKIE_SETTER_RPC,
        headers=_grpc_headers(f"{ACCOUNTS_ORIGIN}/sign-in?redirect=oauth2-provider"),
        data=grpcweb.frame_request(msg),
        timeout=45,
    )
    hdrs = {k.lower(): v for k, v in resp.headers.items()}
    header_status, header_msg = _parse_grpc_error(hdrs, resp.content)
    try:
        parsed = grpcweb.parse_response(resp.content)
    except Exception:
        parsed = {"messages": [], "trailers": {}, "grpc_status": None}
    grpc_status = parsed.get("grpc_status")
    if grpc_status is None:
        grpc_status = header_status
    grpc_msg = header_msg or unquote(
        str((parsed.get("trailers") or {}).get("grpc-message") or "")
    )
    fields = parsed["messages"][0] if parsed.get("messages") else []
    urls = _extract_urls_from_fields(fields)
    cookie_setter = next((u for u in urls if "set-cookie" in u), None) or (
        urls[0] if urls else ""
    )
    if grpc_status not in (None, 0) or not cookie_setter:
        raise PKCEMintError(grpc_msg or "CreateCookieSetterLink failed")
    return cookie_setter


def _submit_consent(
    session: Any,
    *,
    page_url: str,
    page_html: str,
    state: str,
) -> str:
    """Submit OAuth2 consent via HTML form POST (plain POST == Allow)."""
    fm = re.search(
        r'<form\b[^>]*method="POST"[^>]*action="([^"]+)"[^>]*>(.*?)</form>',
        page_html,
        re.I | re.S,
    )
    if not fm:
        fm = re.search(
            r'<form\b[^>]*action="([^"]+)"[^>]*method="POST"[^>]*>(.*?)</form>',
            page_html,
            re.I | re.S,
        )
    if not fm:
        raise PKCEMintError("consent page has no POST form; cannot submit OAuth2 consent")
    action_url, form_inner = fm.group(1), fm.group(2)

    fields: dict[str, str] = {}
    for inp in re.findall(r"<input\b([^>]*)/?>", form_inner, re.I):
        nm = re.search(r'name="([^"]*)"', inp, re.I)
        vl = re.search(r'value="([^"]*)"', inp, re.I)
        if nm:
            fields[nm.group(1)] = vl.group(1) if vl else ""
    if not fields:
        raise PKCEMintError("consent form has no input fields")

    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "origin": ACCOUNTS_ORIGIN,
        "referer": page_url,
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }
    resp = session.post(
        action_url, data=fields, headers=headers, allow_redirects=False, timeout=45
    )

    loc = resp.headers.get("location") or resp.headers.get("Location") or ""
    if "code=" in loc:
        return _code_from_url(urljoin(action_url, loc), state)

    text = resp.text or ""
    m = re.search(r'"code"\s*:\s*"([^"]+)"', text)
    if m:
        return m.group(1)
    m = re.search(r"code=([A-Za-z0-9._~\-]+)", text)
    if m and "error" not in m.group(0):
        return m.group(1)
    # access_denied 等
    if "access_denied" in (loc + text).lower():
        raise PKCEMintError("access_denied")
    raise PKCEMintError(
        f"consent form POST returned no code: HTTP {resp.status_code} "
        f"loc={loc[:200]} body={text[:200]}"
    )


def _exchange_code_for_token(
    session: Any,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    client_id: str,
) -> dict[str, Any]:
    resp = session.post(
        TOKEN_ENDPOINT,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=45,
    )
    if resp.status_code != 200:
        raise PKCEMintError(
            f"token exchange failed HTTP {resp.status_code}: {resp.text[:300]}"
        )
    token = resp.json()
    if "expires_in" in token and "expires_at" not in token:
        try:
            token["expires_at"] = int(time.time()) + int(token["expires_in"])
        except Exception:
            pass
    if not token.get("access_token") or not token.get("refresh_token"):
        raise PKCEMintError(
            "token exchange response missing access_token/refresh_token"
        )
    return token


def mint_with_sso_pkce(
    *,
    sso_cookie: str,
    email: str = "",
    proxy: str | None = None,
    timeout: float = 30.0,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
    client_id: str = CLIENT_ID,
    scope: str = SCOPE,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    impersonate: str | None = None,
) -> dict[str, Any]:
    """Mint authorization-code OAuth tokens from an existing SSO cookie."""
    log = log or _noop_log
    resolved = (proxy or "").strip() or None
    imp = (impersonate or load_mint_impersonate()).strip()
    session = _session(resolved, impersonate=imp)
    _set_sso_cookie(session, sso_cookie)
    log(f"[pkce-cs] start impersonate={imp} proxy={'set' if resolved else 'none'}")

    state = secrets.token_hex(16)
    nonce = secrets.token_hex(16)
    verifier = _code_verifier()
    challenge = _code_challenge(verifier)
    auth_url = _build_authorization_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=challenge,
        scope=scope,
    )
    consent_url = (
        f"{ACCOUNTS_ORIGIN}/oauth2/consent?"
        + urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "nonce": nonce,
            }
        )
    )

    if cancel and cancel():
        raise PKCEMintError("cancelled")
    session.get(auth_url, allow_redirects=False, timeout=timeout)
    setter = _create_cookie_setter_link(session, consent_url)
    log("[pkce-cs] cookie-setter link ok")

    code = ""
    current = setter
    for _ in range(6):
        if cancel and cancel():
            raise PKCEMintError("cancelled")
        if "code=" in current and (
            current.startswith(redirect_uri) or "127.0.0.1" in current
        ):
            code = _code_from_url(current, state)
            break
        if "set-cookie" not in current:
            break
        resp = session.get(current, allow_redirects=False, timeout=timeout)
        loc = resp.headers.get("location") or resp.headers.get("Location") or ""
        log(f"[pkce-cs] set-cookie HTTP {resp.status_code}")
        if not loc:
            break
        current = urljoin(current, loc)

    if not code:
        if "consent" not in current and "code=" not in current:
            raise PKCEMintError(
                f"cookie-setter did not reach consent/code: {current[:180]}"
            )
        if "code=" in current:
            code = _code_from_url(current, state)
        else:
            page = session.get(current, allow_redirects=False, timeout=timeout)
            loc = page.headers.get("location") or page.headers.get("Location") or ""
            if loc and "code=" in loc:
                code = _code_from_url(urljoin(current, loc), state)
            else:
                code = _submit_consent(
                    session,
                    page_url=current,
                    page_html=page.text or "",
                    state=state,
                )
    log(f"[pkce-cs] authorization code ok{f' email={email}' if email else ''}")

    token = _exchange_code_for_token(
        session,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
    )
    return {
        "access_token": str(token["access_token"]).strip(),
        "refresh_token": str(token["refresh_token"]).strip(),
        "id_token": str(token.get("id_token") or "").strip() or None,
        "token_type": str(token.get("token_type") or "Bearer"),
        "expires_in": int(token.get("expires_in") or 21600),
        "mint_method": "pkce_cookie_setter",
        "mint_channel": "pkce",
    }
