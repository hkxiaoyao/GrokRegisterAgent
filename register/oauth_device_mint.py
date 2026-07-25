# -*- coding: utf-8 -*-
"""
Device Flow mint（mode=B）：SSO cookie → access/refresh。

对齐 7sso2auth / regkit：
- client_id = b1a00492-…（grok-build），禁止用 "app"（会 400 client_id is required）
- approve 阶段带 referrer=grok-build，服务端才把 claim 签进 access_token
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai"
DEVICE_CODE_URL = f"{ISSUER}/oauth2/device/code"
TOKEN_URL = f"{ISSUER}/oauth2/token"

def _sso_only_headers(sso: str, *, referer: str, origin: str = "https://accounts.x.ai") -> dict:
    """verify/approve 只带 sso，避免 session 里 cf_clearance 污染导致 invalid_grant。

    参考 srcback desensitized protocol_mint._sso_form_headers 注释。
    """
    sso = str(sso or "").strip()
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Origin": origin,
        "Referer": referer,
        "Cookie": f"sso={sso}",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


VERIFY_URL = f"{ISSUER}/oauth2/device/verify"
APPROVE_URL = f"{ISSUER}/oauth2/device/approve"
SCOPE = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)
GROK_REFERRER = "grok-build"

LogFn = Callable[[str], None]

# ---- 全局 device-flow 节流（抄 grok-register-fast）----
# 多 worker 同时 device/code+verify 易 429 slow_down

_DEVICE_FLOW_LOCK = threading.RLock()
_DEVICE_FLOW_LAST_TS = 0.0


def _device_flow_gap_sec() -> float:
    try:
        return max(0.0, float(os.getenv("GROK2API_SSO_DEVICE_GAP_SEC", "1.2") or 1.2))
    except (TypeError, ValueError):
        return 1.2


def _wait_device_flow_slot(log: Optional[LogFn] = None) -> None:
    """跨线程 device flow 启动最小间隔。"""
    global _DEVICE_FLOW_LAST_TS
    gap = _device_flow_gap_sec()
    with _DEVICE_FLOW_LOCK:
        now = time.time()
        wait = (_DEVICE_FLOW_LAST_TS + gap) - now
        if wait > 0:
            if log:
                try:
                    log(f"[mint-B] device gap wait {wait:.2f}s")
                except Exception:
                    pass
            time.sleep(wait)
        _DEVICE_FLOW_LAST_TS = time.time()


def _poll_interval_sec(raw: Any = None) -> float:
    """approve 后 poll 间隔：可立即/更短（默认 cap 1.5s）。"""
    env = (os.getenv("GROK2API_SSO_POLL_INTERVAL") or "").strip()
    if env:
        try:
            return max(0.2, min(10.0, float(env)))
        except ValueError:
            pass
    try:
        hinted = float(raw if raw is not None else 1)
    except (TypeError, ValueError):
        hinted = 1.0
    return max(0.4, min(hinted, 1.5))


def _noop(_: str) -> None:
    return None


def mint_tokens_device_flow(
    sso: str,
    *,
    proxy: str = "",
    log: Optional[LogFn] = None,
    poll_timeout: float = 120.0,
) -> dict[str, Any]:
    """
    使用 SSO cookie 完成 device approve 并 poll token。
    返回 {ok, access_token, refresh_token, id_token?, expires_in?, error?, mode}
    """
    lg = log or _noop
    sso = str(sso or "").strip()
    if not sso:
        return {"ok": False, "error": "empty sso", "mode": "device"}

    try:
        from curl_cffi import requests as cf_requests
    except ImportError as e:
        return {"ok": False, "error": f"curl_cffi required: {e}", "mode": "device"}

    proxies = {"http": proxy, "https": proxy} if proxy else None
    s = cf_requests.Session()
    if proxies:
        s.proxies = proxies
    for domain in (".x.ai", "accounts.x.ai", "auth.x.ai"):
        s.cookies.set("sso", sso, domain=domain)
        s.cookies.set("sso-rw", sso, domain=domain)

    # 探活 SSO
    try:
        r = s.get("https://accounts.x.ai/", impersonate="chrome120", timeout=15)
        if "sign-in" in str(r.url) or "sign-up" in str(r.url):
            return {"ok": False, "error": "sso invalid (sign-in redirect)", "mode": "device"}
    except Exception as e:
        return {"ok": False, "error": f"sso probe: {e}", "mode": "device"}

    _wait_device_flow_slot(lg)
    lg("[mint-B] device code…")
    try:
        r = s.post(
            DEVICE_CODE_URL,
            data={"client_id": CLIENT_ID, "scope": SCOPE},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            impersonate="chrome120",
            timeout=20,
        )
        body = r.json() if r.text else {}
    except Exception as e:
        return {"ok": False, "error": f"device code: {e}", "mode": "device"}
    if r.status_code != 200 or not isinstance(body, dict):
        return {
            "ok": False,
            "error": f"device code HTTP {r.status_code}: {body!r}"[:220],
            "mode": "device",
        }
    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    interval = max(int(body.get("interval") or 5), 1)
    expires_in = int(body.get("expires_in") or 1800)
    vuri = str(body.get("verification_uri") or f"{ISSUER}/oauth2/device").strip()
    vcomplete = str(
        body.get("verification_uri_complete") or f"{vuri}?user_code={user_code}"
    ).strip()
    if not device_code or not user_code:
        return {"ok": False, "error": "device code missing fields", "mode": "device"}
    lg(f"[mint-B] user_code={user_code}")

    try:
        # 2026-07 forum：OAuth device UI 迁到 accounts.x.ai
        #   /oauth2/device?user_code=… → 继续 → /oauth2/device/consent → 允许
        # verify/approve 后端仍可能在 auth.x.ai；优先 accounts 前端路径。
        if "accounts.x.ai" not in vcomplete and user_code:
            vcomplete = (
                f"https://accounts.x.ai/oauth2/device?user_code={user_code}"
            )
            lg(f"[mint-B] force accounts.x.ai device UI: {vcomplete}")
        lg("[mint-B] GET verification_uri_complete…")
        s.get(
            vcomplete,
            headers={
                "Origin": "https://accounts.x.ai",
                "Referer": "https://accounts.x.ai/",
            },
            impersonate="chrome120",
            timeout=20,
            allow_redirects=True,
        )
        # Prefer accounts.x.ai verify endpoint first (frontend migration)
        accounts_verify = "https://accounts.x.ai/oauth2/device/verify"
        accounts_approve = "https://accounts.x.ai/oauth2/device/approve"
        # accounts.x.ai /oauth2/device/* often SPA-only (POST → /account). Prefer auth.x.ai API.
        lg("[mint-B] POST device/verify…")
        vr = None
        for verify_url in (VERIFY_URL, accounts_verify):
            try:
                vr = s.post(
                    verify_url,
                    data={"user_code": user_code},
                    headers=_sso_only_headers(sso, referer=vcomplete),
                    impersonate="chrome120",
                    timeout=20,
                    allow_redirects=True,
                )
                lg(
                    f"[mint-B] verify via {verify_url.split('//',1)[-1][:48]} "
                    f"status={vr.status_code} url={str(vr.url)[:90]}"
                )
                # Skip SPA dead-ends that land on /account without consent
                if "/account" in str(vr.url or "") and "consent" not in str(vr.url or ""):
                    continue
                if vr.status_code < 500:
                    break
            except Exception as ve:
                lg(f"[mint-B] verify {verify_url}: {ve}")
        if vr is None:
            return {"ok": False, "error": "device verify failed", "mode": "device"}
        if "consent" not in str(vr.url):
            lg(f"[mint-B] verify no-consent-url status={vr.status_code} url={str(vr.url)[:80]}")
        consent_ref = str(vr.url or "") or (
            f"https://accounts.x.ai/oauth2/device/consent?user_code={user_code}"
        )
        # Hit accounts consent page (session cookies) before approve API
        try:
            cr = s.get(
                f"https://accounts.x.ai/oauth2/device/consent?user_code={user_code}",
                headers={
                    "Origin": "https://accounts.x.ai",
                    "Referer": vcomplete,
                },
                impersonate="chrome120",
                timeout=20,
                allow_redirects=True,
            )
            if "consent" in str(cr.url or ""):
                consent_ref = str(cr.url)
            else:
                consent_ref = (
                    f"https://accounts.x.ai/oauth2/device/consent?user_code={user_code}"
                )
        except Exception:
            pass
        lg("[mint-B] POST device/approve (referrer=grok-build)…")
        # allow_redirects=False：避免 303 到 done 时被库误 POST 到 done 页
        ar = None
        for approve_url in (APPROVE_URL, accounts_approve):
            try:
                ar = s.post(
                    approve_url,
                    data={
                        "user_code": user_code,
                        "action": "allow",
                        "principal_type": "User",
                        "principal_id": "",
                        # 关键：approve 带 referrer 才签进 access_token
                        "referrer": GROK_REFERRER,
                    },
                    headers=_sso_only_headers(sso, referer=consent_ref),
                    impersonate="chrome120",
                    timeout=20,
                    allow_redirects=False,
                )
                loc0 = str(
                    ar.headers.get("location") or ar.headers.get("Location") or ""
                )
                lg(
                    f"[mint-B] approve via {approve_url.split('//',1)[-1][:48]} "
                    f"status={ar.status_code} loc={loc0[:80]}"
                )
                # SPA /account is not a real approve success
                if loc0.rstrip("/").endswith("/account"):
                    continue
                if ar.status_code in (200, 302, 303, 307, 308) or loc0:
                    break
            except Exception as ae:
                lg(f"[mint-B] approve {approve_url}: {ae}")
        if ar is None:
            return {"ok": False, "error": "device approve failed", "mode": "device"}
        loc = str(ar.headers.get("location") or ar.headers.get("Location") or "")
        lg(
            f"[mint-B] approve status={ar.status_code} "
            f"loc={(loc or str(ar.url))[:100]}"
        )
        if loc and "denied" in loc.lower():
            return {
                "ok": False,
                "error": "device approve denied by user/server",
                "mode": "device",
            }
        if ar.status_code == 401 or "session expired" in (ar.text or "").lower():
            return {
                "ok": False,
                "error": f"device approve session expired: {(ar.text or '')[:120]}",
                "mode": "device",
            }
        if loc and ("done" in loc or loc.startswith("http")):
            try:
                done_url = (
                    loc
                    if loc.startswith("http")
                    else ("https://accounts.x.ai" + loc)
                )
                s.get(
                    done_url,
                    headers={"Referer": consent_ref},
                    impersonate="chrome120",
                    timeout=15,
                    allow_redirects=True,
                )
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "error": f"verify/approve: {e}", "mode": "device"}

    # approve 已完成：立即 poll，间隔缩短（抄 fast immediate + short interval）
    poll_iv = _poll_interval_sec(interval)
    lg(f"[mint-B] poll token… (interval={poll_iv:.2f}s, immediate)")
    deadline = time.time() + min(float(poll_timeout), float(expires_in), 180.0)
    first = True
    while time.time() < deadline:
        if not first:
            time.sleep(poll_iv)
        first = False
        try:
            tr = s.post(
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": CLIENT_ID,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                impersonate="chrome120",
                timeout=20,
            )
            tb = tr.json() if tr.text else {}
        except Exception as e:
            # 瞬态网络：短睡后继续，不立刻判死（已 approve）
            lg(f"[mint-B] poll net err: {e}")
            time.sleep(max(1.0, poll_iv))
            continue
        if tr.status_code == 200 and isinstance(tb, dict) and tb.get("access_token"):
            lg("[mint-B] token ok")
            return {
                "ok": True,
                "mode": "device",
                "access_token": str(tb.get("access_token") or ""),
                "refresh_token": str(tb.get("refresh_token") or ""),
                "id_token": tb.get("id_token"),
                "expires_in": tb.get("expires_in"),
                "raw": tb,
            }
        err = str((tb or {}).get("error") or "") if isinstance(tb, dict) else ""
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                poll_iv = min(5.0, poll_iv + 1.0)
                lg(f"[mint-B] slow_down → interval={poll_iv:.2f}s")
            continue
        if err:
            return {"ok": False, "error": f"poll: {err}", "mode": "device"}
        # 无 error 也无 token：继续
    return {"ok": False, "error": "poll timeout", "mode": "device"}
