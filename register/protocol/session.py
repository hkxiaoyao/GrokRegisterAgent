"""Shared HTTP session for protocol registration."""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover
    curl_requests = None
    import requests as std_requests


# Transient proxy/TLS blips (curl 35 reset, broken pipe, timeout, SSL) that a
# flaky sing-box / proxy hop throws mid-request. Retrying the same request on a
# fresh connection usually succeeds — a single reset must NOT kill the round.
_TRANSIENT_NEEDLES = (
    "broken pipe",
    "connection reset",
    "connection aborted",
    "reset by peer",
    "recv failure",
    "send failure",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "network is unreachable",
    "unexpected_eof",
    "eof occurred",
    "ssl",
    "handshake",
    "remote end closed",
    "bad gateway",
    "connection refused",
    "failed to perform",
    "curl: (35)",
    "curl: (52)",
    "curl: (56)",
)


def _is_transient_net_error(exc: BaseException) -> bool:
    """True for proxy/TLS blips that should be retried, not surfaced."""
    if isinstance(
        exc,
        (
            TimeoutError,
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            ConnectionRefusedError,
        ),
    ):
        return True
    try:
        import ssl

        if isinstance(exc, ssl.SSLError):
            return True
    except Exception:
        pass
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in {
        32,
        104,
        110,
        111,
        113,
        101,
    }:
        return True
    msg = str(exc).lower()
    return any(n in msg for n in _TRANSIENT_NEEDLES)


class ProtocolSession:
    def __init__(self, proxy: str = "", user_agent: str = "", impersonate: str = "chrome131"):
        self.proxy = (proxy or "").strip()
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        # Prefer a concrete chrome profile; "chrome" alone is often blocked by CF.
        self.impersonate = impersonate or "chrome131"
        if curl_requests is not None:
            self.session = curl_requests.Session()
        else:
            self.session = std_requests.Session()
        self.session.headers.update(
            {
                "user-agent": self.user_agent,
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "origin": "https://accounts.x.ai",
                "referer": "https://accounts.x.ai/sign-up?redirect=grok-com",
                "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }
        )
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
        # reset-retry knobs (proxy blips): total attempts = retries + 1
        self.net_retries = 2
        self.net_retry_sleep = 1.5

    def _with_retry(self, do: Callable[[], Any], label: str = "") -> Any:
        """Run an HTTP call, retrying only on transient proxy/TLS blips.

        curl_cffi raises curl (35) 'Recv failure: Connection reset by peer' when
        the sing-box / proxy hop drops mid-flight. That is transient — retry on a
        fresh attempt. HTTP status errors are returned by curl_cffi (not raised),
        so they pass straight through untouched.
        """
        attempts = max(int(self.net_retries), 0) + 1
        last: BaseException | None = None
        for i in range(attempts):
            try:
                return do()
            except BaseException as e:  # noqa: BLE001
                last = e
                if not _is_transient_net_error(e) or i + 1 >= attempts:
                    raise
                time.sleep(self.net_retry_sleep * (i + 1))
        assert last is not None
        raise last

    def get(self, url: str, timeout: int = 30) -> Any:
        def _do():
            if curl_requests is not None:
                return self.session.get(
                    url, timeout=timeout, impersonate=self.impersonate
                )
            return self.session.get(url, timeout=timeout)

        return self._with_retry(_do, label="get")

    def bootstrap(self, timeout: int = 30) -> Any:
        return self.get("https://accounts.x.ai/sign-up?redirect=grok-com", timeout=timeout)

    def set_cookies(self, cookies: dict, domain: str = ".x.ai"):
        if not cookies:
            return
        # Drop leftover session cookies from previous accounts in shared chrome profile.
        skip = set()
        for name, value in cookies.items():
            if not name:
                continue
            n = str(name)
            # keep cf / anon; still allow sso if present (should be empty for fresh signup)
            try:
                self.session.cookies.set(n, value, domain=domain)
            except Exception:
                try:
                    self.session.cookies.set(n, value)
                except Exception:
                    pass
            # also set accounts.x.ai host cookies for CF
            try:
                self.session.cookies.set(n, value, domain="accounts.x.ai")
            except Exception:
                pass

    def cookies_dict(self) -> dict:
        try:
            return dict(self.session.cookies)
        except Exception:
            jar = {}
            try:
                for c in self.session.cookies:
                    jar[getattr(c, "name", "")] = getattr(c, "value", "")
            except Exception:
                pass
            return {k: v for k, v in jar.items() if k}

    def post_bytes(self, url: str, data: bytes, headers: Optional[dict] = None, timeout: int = 30):
        h = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": "https://accounts.x.ai",
            "referer": "https://accounts.x.ai/sign-up?redirect=grok-com",
            "accept": "*/*",
        }
        if headers:
            h.update(headers)

        def _do():
            if curl_requests is not None:
                return self.session.post(
                    url, data=data, headers=h, timeout=timeout, impersonate=self.impersonate
                )
            return self.session.post(url, data=data, headers=h, timeout=timeout)

        return self._with_retry(_do, label="post_bytes")

    def post_raw(self, url: str, data: bytes, headers: Optional[dict] = None, timeout: int = 45):
        h = dict(headers or {})

        def _do():
            if curl_requests is not None:
                return self.session.post(
                    url, data=data, headers=h, timeout=timeout, impersonate=self.impersonate
                )
            return self.session.post(url, data=data, headers=h, timeout=timeout)

        return self._with_retry(_do, label="post_raw")
