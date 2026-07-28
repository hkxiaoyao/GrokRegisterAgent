
from __future__ import annotations
import os

import json
import random
import re
import string
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    curl_requests = None

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# 自建邮件服务配置（兼容 dreamhunter2333/cloudflare_temp_email / vmail）
# ============================================================

_config_path = Path(__file__).parent / "config.json"
_conf: Dict[str, Any] = {}
if _config_path.exists():
    with _config_path.open("r", encoding="utf-8") as _f:
        _conf = json.load(_f)


def _normalize_mail_api_base(raw: str) -> str:
    """规范化邮件后端根地址。

    cloudflare_temp_email 的 admin 接口挂在 Worker 根：
      POST {base}/admin/new_address
    常见误填：
      - 前端 Pages 域名（仅 GET，POST 会 405 且 body 为空）
      - 末尾带 /admin、/api、多余斜杠
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    # 去掉误粘贴的路径后缀
    for suffix in ("/admin/new_address", "/admin", "/api/mails", "/api"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base


def _reload_mail_conf() -> None:
    """每轮创建前热读 config.json，避免进程内常量过期。"""
    global _conf, MAIL_API_BASE, MAIL_ADMIN_AUTH, MAIL_DOMAIN, PROXY, MAIL_API_USE_PROXY
    try:
        if _config_path.exists():
            with _config_path.open("r", encoding="utf-8") as f:
                _conf = json.load(f)
    except Exception:
        pass
    MAIL_API_BASE = _normalize_mail_api_base(str(_conf.get("mail_api_base", "")))
    MAIL_ADMIN_AUTH = str(_conf.get("mail_admin_auth", "") or "")
    MAIL_DOMAIN = str(_conf.get("mail_domain", "") or "").strip().lstrip("@")
    PROXY = str(_conf.get("proxy", "") or "")
    MAIL_API_USE_PROXY = bool(_conf.get("mail_api_use_proxy", False))


MAIL_API_BASE = _normalize_mail_api_base(str(_conf.get("mail_api_base", "")))
MAIL_ADMIN_AUTH = str(_conf.get("mail_admin_auth", ""))
MAIL_DOMAIN = str(_conf.get("mail_domain", "")).strip().lstrip("@")
PROXY = str(_conf.get("proxy", ""))
# 邮件 API 是否走代理（默认 False）。默认关：多数临时邮箱后端在代理出口下
# 常被连接重置（curl 35 Recv failure），且邮箱申请无需与注册同出口。
MAIL_API_USE_PROXY = bool(_conf.get("mail_api_use_proxy", False))

# 邮箱域名池（可选）；轮换逻辑见 pools.next_mail_domain
try:
    from pools import next_mail_domain, next_proxy, reload_pools
except Exception:  # pragma: no cover
    def next_mail_domain(fallback: str = "") -> str:
        return fallback

    def next_proxy(fallback: str = "") -> str:
        return fallback

    def reload_pools(force: bool = False) -> None:
        return None

# ============================================================
# 适配层：为 DrissionPage_example.py 提供简单接口
# ============================================================


def _mail_provider() -> str:
    _reload_mail_conf()
    p = str(_conf.get("mail_provider") or _conf.get("email_provider") or "cloudflare").strip().lower()
    if p in ("cf", "temp_email", "vmail", "cloudflare_temp_email"):
        return "cloudflare"
    if p in ("duck", "duckmail"):
        return "duckmail"
    if p in ("yyds", "yydsmail"):
        return "yyds"
    if p in ("gptmail", "gpt", "chatgpt_mail", "chatgpt-mail"):
        return "gptmail"
    return p or "cloudflare"


def get_email_and_token() -> Tuple[Optional[str], Optional[str]]:
    """
    创建临时邮箱，返回 (email, token)。
    provider: cloudflare（默认）| duckmail | yyds
    token 用于后续轮询验证码（CF=jwt，duck/yyds=jwt 或 account token）。

    本地手动邮箱：
      env REGISTER_FORCE_EMAIL=user@domain
      → 跳过创建，token 记为 "manual"（配合 get_oai_code 手输 OTP）
    """
    force = (os.environ.get("REGISTER_FORCE_EMAIL") or os.environ.get("FORCE_EMAIL") or "").strip()
    if force and "@" in force:
        print(f"[*] REGISTER_FORCE_EMAIL={force}（跳过自动建邮，OTP 走手动/文件）", flush=True)
        return force, "manual"
    provider = _mail_provider()
    if provider == "duckmail":
        email, token = _create_duckmail()
        return _ensure_human_email(email, token, provider)
    if provider == "yyds":
        email, token = _create_yyds()
        return _ensure_human_email(email, token, provider)
    if provider == "gptmail":
        email, token = _create_gptmail()
        return _ensure_human_email(email, token, provider)
    email, _password, jwt = create_temp_email()
    if email and jwt:
        return _ensure_human_email(email, jwt, "cloudflare")
    return None, None


def _ensure_human_email(
    email: Optional[str], token: Optional[str], provider: str
) -> Tuple[Optional[str], Optional[str]]:
    """最终闸门：拒绝 e91i5aoj336 / yfyrdisol9q 这类随机 local，以及带 . 的别名风险前缀。"""
    if not email or not token:
        return email, token
    local = str(email).split("@", 1)[0].strip().lower()
    if "." in local or "_" in local or "-" in local:
        raise Exception(
            f"邮箱前缀含 . / _ / -（别名或剥字符风险）: {email} provider={provider}"
        )
    if not _looks_human_local(local):
        raise Exception(
            f"邮箱前缀不像人名（疑似 Worker 随机名或旧代码）: {email} "
            f"provider={provider} local={local} "
            f"— 请确认 Docker 已同步 ./register 并重启；CF 须允许自定义 name"
        )
    print(f"[mail] ready address={email} provider={provider} local={local}", flush=True)
    return email, token


def _create_duckmail() -> Tuple[Optional[str], Optional[str]]:
    """
    DuckMail（mail.tm 兼容 API）：
      POST {base}/accounts  {"address","password"}
      POST {base}/token     {"address","password"} → jwt
    读信：GET {base}/messages + Bearer jwt（列表 hydra:member）
    config: mail_api_base 必填；mail_domain 可选偏好；mail_admin_auth 仅自建鉴权可选。
    """
    _reload_mail_conf()
    base = MAIL_API_BASE.rstrip("/")
    admin = MAIL_ADMIN_AUTH.strip()
    if not base:
        raise Exception("duckmail: mail_api_base 未设置（如 https://api.duckmail.sbs）")
    domain = (MAIL_DOMAIN or "").strip().lstrip("@")
    # 未填域名时从 /domains 取第一个可用
    session, use_cffi = _create_session()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if admin:
        headers["Authorization"] = f"Bearer {admin}"
    if not domain:
        try:
            res = _do_request(
                session, use_cffi, "get", f"{base}/domains", headers=headers, timeout=15
            )
            if res.status_code == 200 and res.text:
                data = res.json()
                members = []
                if isinstance(data, dict):
                    members = data.get("hydra:member") or data.get("member") or data.get("domains") or []
                elif isinstance(data, list):
                    members = data
                for m in members or []:
                    if isinstance(m, dict) and m.get("domain") and m.get("isVerified", True):
                        domain = str(m["domain"]).strip().lstrip("@")
                        break
                    if isinstance(m, str) and m.strip():
                        domain = m.strip().lstrip("@")
                        break
        except Exception:
            pass
    if not domain:
        domain = "duckmail.sbs"
    last_err = ""
    for _ in range(4):
        local = _generate_local_part()
        address = f"{local}@{domain}"
        password = (
            random.choice(string.ascii_letters)
            + "".join(random.choice(string.ascii_letters + string.digits) for _ in range(11))
            + "!"
        )
        try:
            res = _do_request(
                session,
                use_cffi,
                "post",
                f"{base}/accounts",
                json={"address": address, "password": password},
                headers=headers,
                timeout=20,
            )
            if res.status_code not in (200, 201):
                last_err = f"accounts HTTP {res.status_code}: {(res.text or '')[:160]}"
                # 地址冲突再试
                if res.status_code in (400, 409, 422):
                    continue
                raise Exception(last_err)
            # 换 jwt
            tres = _do_request(
                session,
                use_cffi,
                "post",
                f"{base}/token",
                json={"address": address, "password": password},
                headers=headers,
                timeout=20,
            )
            if tres.status_code not in (200, 201):
                last_err = f"token HTTP {tres.status_code}: {(tres.text or '')[:160]}"
                raise Exception(last_err)
            tdata = tres.json() if tres.text else {}
            if not isinstance(tdata, dict):
                tdata = {}
            jwt = str(tdata.get("token") or tdata.get("jwt") or tdata.get("access_token") or "").strip()
            if not jwt:
                raise Exception(f"token 响应无 jwt: {tdata}")
            print(f"[*] duckmail 创建成功: {address}")
            return address, jwt
        except Exception as e:
            last_err = str(e)
            continue
    raise Exception(f"duckmail 创建失败: {last_err}")


def _normalize_yyds_base(raw: str) -> str:
    """YYDS 官方根默认带 /v1：https://maliapi.215.im/v1"""
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    # 去掉误粘贴的业务路径
    for suffix in ("/accounts", "/messages", "/api/email/create"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    # 裸域名补 /v1（与 FlowPilot DEFAULT_YYDS_MAIL_BASE_URL 对齐）
    try:
        from urllib.parse import urlparse
        p = urlparse(base if "://" in base else f"https://{base}")
        path = (p.path or "").rstrip("/")
        if path in ("", "/"):
            base = f"{p.scheme}://{p.netloc}/v1"
        elif not path.endswith("/v1"):
            # 用户已写其它前缀则尊重；仅当明确是官方主机且无 v1 时补
            if p.netloc.endswith("215.im") and "/v1" not in path:
                base = f"{p.scheme}://{p.netloc}/v1"
    except Exception:
        pass
    return base.rstrip("/")


def _create_yyds() -> Tuple[Optional[str], Optional[str]]:
    """
    YYDS Mail（对齐 FlowPilot yyds-mail-provider）：
      创建：POST {base}/accounts  header X-API-Key  body {localPart}
      读信：GET  {base}/messages?address=…  header Authorization Bearer <temp token>
    config: mail_api_base（默认 https://maliapi.215.im/v1）, mail_admin_auth = API Key
    """
    _reload_mail_conf()
    base = _normalize_yyds_base(MAIL_API_BASE or "https://maliapi.215.im/v1")
    api_key = MAIL_ADMIN_AUTH.strip()
    # 去掉误粘贴的 Bearer 前缀（YYDS 只认 X-API-Key）
    if len(api_key) >= 7 and api_key[:7].lower() == "bearer ":
        api_key = api_key[7:].strip()
    if not base:
        raise Exception("yyds: mail_api_base 未设置（如 https://maliapi.215.im/v1）")
    if not api_key:
        raise Exception("yyds: mail_admin_auth 未设置（填 YYDS X-API-Key）")
    local = _generate_local_part()
    # FlowPilot localPart: 6 字母 + 4 数字
    if len(local) < 8:
        local = local + "".join(random.choice(string.digits) for _ in range(4))
    session, use_cffi = _create_session()
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{base}/accounts"
    last_err = ""
    for attempt in range(3):
        body = {"localPart": local if attempt == 0 else _generate_local_part()}
        try:
            res = _do_request(
                session, use_cffi, "post", url, json=body, headers=headers, timeout=25
            )
            if res.status_code in (200, 201):
                data = res.json() if res.text else {}
                if not isinstance(data, dict):
                    data = {}
                if data.get("success") is False:
                    last_err = str(data.get("error") or data.get("message") or data)[:200]
                    raise Exception(last_err)
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                email = (
                    inner.get("address")
                    or inner.get("email")
                    or inner.get("mail")
                    or ""
                )
                jwt = (
                    inner.get("token")
                    or inner.get("tempToken")
                    or inner.get("accessToken")
                    or inner.get("jwt")
                    or ""
                )
                if email and jwt:
                    print(f"[*] yyds 创建成功: {email}")
                    return str(email), str(jwt)
                last_err = f"无 address/token: {data}"
            else:
                last_err = f"HTTP {res.status_code}: {(res.text or '')[:200]} | {url}"
                # 错误 key 时常见 401/403；web_app_only 表示未带 X-API-Key
                if res.status_code in (401, 403):
                    break
        except Exception as e:
            last_err = f"{e} | {url}"
    raise Exception(f"yyds 创建失败: {last_err}")




def _normalize_gptmail_base(raw: str) -> str:
    """GPTMail site root: https://mail.chatgpt.org.uk (no /api path)."""
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    for suffix in (
        "/api/generate-email",
        "/api/emails",
        "/api/email",
        "/api/stats",
        "/api",
    ):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    try:
        from urllib.parse import urlparse

        p = urlparse(base if "://" in base else f"https://{base}")
        path = (p.path or "").rstrip("/")
        if path in ("", "/") or path.startswith("/api"):
            base = f"{p.scheme}://{p.netloc}"
        else:
            base = f"{p.scheme}://{p.netloc}{path}"
    except Exception:
        pass
    return base.rstrip("/")


def _create_gptmail() -> Tuple[Optional[str], Optional[str]]:
    """
    GPTMail (mail.chatgpt.org.uk):
      create: POST {base}/api/generate-email  header X-API-Key  body {prefix?, domain?}
      list:   GET  {base}/api/emails?email=   header X-API-Key
      detail: GET  {base}/api/email/{id}      header X-API-Key
    Returns (email, api_key). Poll reuses api_key as X-API-Key (not Bearer).
    config: mail_api_base default https://mail.chatgpt.org.uk, mail_admin_auth = API Key
    """
    _reload_mail_conf()
    base = _normalize_gptmail_base(MAIL_API_BASE or "https://mail.chatgpt.org.uk")
    api_key = MAIL_ADMIN_AUTH.strip()
    if len(api_key) >= 7 and api_key[:7].lower() == "bearer ":
        api_key = api_key[7:].strip()
    if not base:
        raise Exception("gptmail: mail_api_base unset (e.g. https://mail.chatgpt.org.uk)")
    if not api_key:
        raise Exception("gptmail: mail_admin_auth unset (GPTMail X-API-Key)")
    domain = (MAIL_DOMAIN or "").strip().lstrip("@")
    session, use_cffi = _create_session()
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{base}/api/generate-email"
    last_err = ""
    for attempt in range(3):
        prefix = _generate_local_part()
        body: Dict[str, Any] = {"prefix": prefix}
        if domain:
            body["domain"] = domain
        try:
            res = _do_request(
                session, use_cffi, "post", url, json=body, headers=headers, timeout=25
            )
            if res.status_code in (200, 201):
                data = res.json() if res.text else {}
                if not isinstance(data, dict):
                    data = {}
                if data.get("success") is False:
                    last_err = str(data.get("error") or data.get("message") or data)[:200]
                    if "api key" in last_err.lower() or "invalid" in last_err.lower():
                        break
                    raise Exception(last_err)
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                email = (
                    (inner or {}).get("email")
                    or (inner or {}).get("address")
                    or (inner or {}).get("mail")
                    or ""
                )
                if email:
                    print(f"[*] gptmail create ok: {email}")
                    return str(email), str(api_key)
                last_err = f"no email in response: {data}"
            else:
                last_err = f"HTTP {res.status_code}: {(res.text or '')[:200]} | {url}"
                if res.status_code in (401, 403):
                    break
        except Exception as e:
            last_err = f"{e} | {url}"
    raise Exception(f"gptmail create failed: {last_err}")


def get_oai_code(dev_token: str, email: str, timeout: int = 30) -> Optional[str]:
    """
    轮询邮箱获取 Grok/x.ai 发来的 OTP 验证码。
    返回去掉连字符后的字符串（如 "MM0SF3"），失败返回 None。

    手动 OTP：
      - token == "manual" 或 env REGISTER_MANUAL_OTP=1
      - 读 env REGISTER_OTP / REGISTER_FORCE_OTP
      - 或轮询文件：register/_manual_otp.txt / out/local_test/manual_otp.txt
        内容写 6 位码即可（可带连字符）
    """
    import time as _time
    import re as _re

    manual = (
        str(dev_token or "").strip().lower() == "manual"
        or (os.environ.get("REGISTER_MANUAL_OTP") or "").strip().lower() in ("1", "true", "yes", "on")
    )
    if manual:
        deadline = _time.time() + max(30, int(timeout or 30))
        paths = [
            Path(__file__).resolve().parent / "_manual_otp.txt",
            Path(__file__).resolve().parent.parent / "out" / "local_test" / "manual_otp.txt",
        ]
        print(
            f"[*] 手动 OTP 模式 email={email or '-'} · 请把验证码写入: "
            f"{paths[0]} 或 {paths[1]} · 也可设 env REGISTER_OTP",
            flush=True,
        )
        # clear stale file once
        for fp in paths:
            try:
                if fp.is_file() and fp.stat().st_size > 0:
                    # keep content if freshly written (<2s) else clear hint only
                    pass
            except Exception:
                pass
        last_hint = 0.0
        while _time.time() < deadline:
            env_code = (
                os.environ.get("REGISTER_OTP")
                or os.environ.get("REGISTER_FORCE_OTP")
                or os.environ.get("FORCE_OTP")
                or ""
            ).strip()
            raw = env_code
            if not raw:
                for fp in paths:
                    try:
                        if fp.is_file():
                            raw = fp.read_text(encoding="utf-8", errors="replace").strip()
                            if raw:
                                break
                    except Exception:
                        continue
            if raw:
                m = _re.search(r"([A-Za-z0-9]{3})-?([A-Za-z0-9]{3})", raw)
                if not m:
                    m = _re.search(r"([A-Za-z0-9]{6})", raw)
                if m:
                    if m.lastindex and m.lastindex >= 2:
                        code = (m.group(1) + m.group(2)).upper()
                    else:
                        code = m.group(1).upper()
                    code = code.replace("-", "")
                    print(f"[*] 手动 OTP 已读到: {code[:3]}-{code[3:]}", flush=True)
                    # consume file so next run won't reuse
                    for fp in paths:
                        try:
                            if fp.is_file():
                                fp.write_text("", encoding="utf-8")
                        except Exception:
                            pass
                    os.environ.pop("REGISTER_OTP", None)
                    os.environ.pop("REGISTER_FORCE_OTP", None)
                    os.environ.pop("FORCE_OTP", None)
                    return code
            now = _time.time()
            if now - last_hint > 8:
                left = int(deadline - now)
                print(f"[*] 等待手动 OTP… 剩余约 {left}s（写文件或 REGISTER_OTP）", flush=True)
                last_hint = now
            _time.sleep(1.0)
        print("[!] 手动 OTP 超时", flush=True)
        return None

    code = wait_for_verification_code(jwt=dev_token, timeout=timeout, email=email or "")
    if code:
        code = code.replace("-", "")
    return code


# ============================================================
# 核心：与 vmail (https://github.com/...) 后端交互
# ============================================================


def _create_session():
    """优先 curl_cffi 走 chrome131 指纹，避免 Cloudflare 拦截"""
    if curl_requests:
        session = curl_requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        if PROXY and MAIL_API_USE_PROXY:
            session.proxies = {"http": PROXY, "https": PROXY}
        return session, True

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    if PROXY and MAIL_API_USE_PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
    return s, False


def _do_request(session, use_cffi, method, url, **kwargs):
    if use_cffi:
        kwargs.setdefault("impersonate", "chrome131")
    return getattr(session, method)(url, **kwargs)


# 常见英文名（小写）——邮箱本地部分用人名+数字，避免 0dk0tgkzw2nj 这类随机串
_EMAIL_FIRST = (
    "aaron", "adam", "adrian", "alan", "alex", "alice", "allen", "amy", "andrew",
    "anna", "anthony", "ashley", "austin", "ben", "brian", "caleb", "carl", "carol",
    "charles", "chris", "claire", "cody", "daniel", "david", "dean", "diana", "dylan",
    "edward", "eli", "ella", "emily", "eric", "ethan", "eva", "evan", "felix", "frank",
    "gabriel", "grace", "grant", "hannah", "harry", "henry", "ian", "isaac", "jack",
    "jacob", "james", "jane", "jason", "jay", "jennifer", "jessica", "john", "jordan",
    "joseph", "josh", "julia", "justin", "karen", "kate", "kevin", "kyle", "laura",
    "lauren", "leo", "linda", "logan", "lucas", "lucy", "luke", "mark", "martin",
    "mary", "mason", "matt", "megan", "mike", "nancy", "nathan", "noah", "olivia",
    "oscar", "owen", "paul", "peter", "rachel", "ralph", "ray", "rebecca", "robert",
    "rose", "ryan", "sam", "sarah", "scott", "sean", "sophia", "steve", "susan",
    "thomas", "tim", "tyler", "victor", "vincent", "wayne", "will", "william", "zoe",
)
_EMAIL_LAST = (
    "adams", "allen", "anderson", "baker", "bell", "brooks", "brown", "campbell",
    "carter", "chen", "clark", "collins", "cook", "cooper", "davis", "edwards",
    "evans", "fisher", "foster", "garcia", "green", "hall", "harris", "hill",
    "howard", "jackson", "james", "johnson", "jones", "kelly", "kim", "king",
    "lee", "lewis", "lin", "lopez", "martin", "miller", "moore", "morgan",
    "morris", "nelson", "nguyen", "parker", "patel", "perez", "phillips", "price",
    "reed", "roberts", "robinson", "ross", "scott", "smith", "taylor", "thomas",
    "thompson", "turner", "walker", "wang", "ward", "white", "williams", "wilson",
    "wood", "wright", "young", "zhang",
)


def _generate_local_part(min_len=8, max_len=18) -> str:
    """生成更像真人的邮箱本地部分：姓名 + 数字。

    硬性约束（CF / 别名安全）：
      - **只用 [a-z0-9]**，禁止 `.` `_` `-` 等
      - `.` 在多数邮件系统里是别名分隔（john.doe ≡ john doe），绝不用
      - `_` 会被 cloudflare_temp_email 默认 ADDRESS_REGEX 剥掉
      - 禁止 e91i5aoj336 这类字母数字交错的随机串

    示例：john47 / emilyw203 / mikechen88 / jsmith1992
    """
    # 多试几次，保证最终形态可读（字母前缀 + 尾部数字）
    for _ in range(8):
        first = random.choice(_EMAIL_FIRST)
        last = random.choice(_EMAIL_LAST)
        # 数字：2～4 位为主；偶尔像出生年
        style = random.random()
        if style < 0.55:
            digits = str(random.randint(10, 9999))
        elif style < 0.8:
            digits = str(random.randint(10, 99))
        else:
            digits = str(random.randint(1975, 2005))

        # 组合模式：纯字母数字；**永不插入 . / _**
        mode = random.random()
        if mode < 0.40:
            local = f"{first}{digits}"
        elif mode < 0.65:
            local = f"{first}{last[0]}{digits}"
        elif mode < 0.88:
            ln = last if len(last) <= 6 else last[: random.randint(3, 6)]
            local = f"{first}{ln}{digits}"
        else:
            local = f"{first[0]}{last}{digits}"

        # 双保险：剥掉任何非 a-z0-9（含 . _ -）
        local = re.sub(r"[^a-z0-9]", "", local.lower())
        if not local or not local[0].isalpha():
            local = first + digits
        if len(local) > max_len:
            core = re.sub(r"\d+$", "", local)[: max(4, max_len - len(digits))]
            if not core:
                core = first[:4]
            local = (core + digits)[:max_len]
            if not local or not local[0].isalpha():
                local = first[:3] + re.sub(r"^[^a-z]+", "", local or "")
        while len(local) < min_len:
            local += str(random.randint(0, 9))
        local = local[:max_len]
        # 形态：至少 3 个连续字母开头，后接可选字母，再接数字尾
        if re.fullmatch(r"[a-z]{3,}[a-z]*\d{1,4}", local):
            return local
    # 兜底：绝不可能落到随机串
    fb = random.choice(_EMAIL_FIRST) + str(random.randint(10, 9999))
    return re.sub(r"[^a-z0-9]", "", fb)[:max_len]


def _looks_human_local(local: str) -> bool:
    """判断 local-part 是否像人名前缀（非 e91i5aoj336 随机串）。"""
    s = (local or "").strip().lower()
    if not s or "." in s or "_" in s or "-" in s:
        return False
    if re.search(r"[^a-z0-9]", s):
        return False
    # 字母数字交错过多 → 随机串
    transitions = sum(
        1 for i in range(1, len(s)) if s[i].isdigit() != s[i - 1].isdigit()
    )
    if transitions >= 3:
        return False
    return bool(re.fullmatch(r"[a-z]{3,}[a-z]*\d{0,6}", s))


def _cf_auth_mode() -> str:
    """cloudflare_temp_email 创建接口鉴权：none|x-admin-auth|bearer|x-api-key|query-key。

    config: cloudflare_auth_mode / mail_auth_mode（默认 x-admin-auth 保持兼容）
    """
    _reload_mail_conf()
    m = str(
        _conf.get("cloudflare_auth_mode")
        or _conf.get("mail_auth_mode")
        or _conf.get("cf_auth_mode")
        or "x-admin-auth"
    ).strip().lower()
    if m in ("admin", "x-admin", "admin-auth"):
        return "x-admin-auth"
    if m in ("", "password", "admin_password"):
        return "x-admin-auth"
    if m in ("none", "anonymous", "anon", "public"):
        return "none"
    if m in ("bearer", "authorization", "jwt"):
        return "bearer"
    if m in ("x-api-key", "apikey", "api-key", "api_key"):
        return "x-api-key"
    if m in ("query-key", "query", "query_key", "key"):
        return "query-key"
    return m


def _cf_create_path(auth_mode: str) -> str:
    """创建路径：admin 模式用 /admin/new_address；匿名/public 用 /api/new_address。"""
    raw = str(
        _conf.get("cloudflare_create_path")
        or _conf.get("mail_create_path")
        or ""
    ).strip()
    if raw:
        return raw if raw.startswith("/") else f"/{raw}"
    if auth_mode == "none":
        return "/api/new_address"
    return "/admin/new_address"


def build_cf_auth_headers(
    auth_mode: str,
    api_key: str,
    *,
    content_type: bool = True,
) -> dict[str, str]:
    """按鉴权模式构造 Cloudflare 临时邮箱请求头。"""
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = "application/json"
    key = (api_key or "").strip()
    mode = (auth_mode or "none").strip().lower()
    if not key or mode in ("none", "anonymous", "anon", "public"):
        return headers
    if mode == "x-admin-auth":
        headers["x-admin-auth"] = key
    elif mode in ("x-api-key", "apikey", "api-key", "api_key"):
        headers["X-API-Key"] = key
    elif mode in ("bearer", "authorization"):
        headers["Authorization"] = f"Bearer {key}"
    # query-key 不写 header，由 URL 参数携带
    return headers


def create_temp_email() -> Tuple[str, str, str]:
    """
    创建 cloudflare_temp_email 地址。

    鉴权模式（cloudflare_auth_mode）:
      - x-admin-auth（默认）：POST /admin/new_address + x-admin-auth
      - none：POST /api/new_address 匿名（无需密钥）
      - bearer / x-api-key / query-key：兼容其它 Worker 配置

    返回 {jwt, address, password}，jwt 即用于读邮件的 Bearer。
    域名：优先邮箱域名池轮换，否则用 MAIL_DOMAIN。
    """
    # 每轮创建前热读邮件配置 + 池（支持 UI 改完立刻生效）
    _reload_mail_conf()
    try:
        reload_pools(force=True)
    except Exception:
        pass

    if not MAIL_API_BASE:
        raise Exception(
            "mail_api_base 未设置。请填 cloudflare_temp_email 的 **Worker API 根地址**"
            "（不是前端 Pages 域名），例如 https://xxx.workers.dev"
        )

    auth_mode = _cf_auth_mode()
    api_key = MAIL_ADMIN_AUTH.strip()
    if auth_mode not in ("none", "anonymous", "anon", "public") and not api_key:
        raise Exception(
            f"mail_admin_auth 未设置（cloudflare_auth_mode={auth_mode} 需要密钥）"
        )

    create_path = _cf_create_path(auth_mode)
    headers = build_cf_auth_headers(auth_mode, api_key, content_type=True)
    session, use_cffi = _create_session()
    base_url = f"{MAIL_API_BASE.rstrip('/')}{create_path}"
    print(
        f"[*] 邮件 API: {MAIL_API_BASE} → POST {create_path} "
        f"auth_mode={auth_mode}"
    )

    # 域名池状态（便于确认轮换是否生效）
    try:
        from pools import peek_status as _peek_mail_pools

        _st = _peek_mail_pools()
        _doms = list(_st.get("domains") or [])
        _idx = _st.get("domain_idx", "?")
        if _doms:
            print(
                f"[*] 邮箱域名池: {len(_doms)} 个 mode={_st.get('domain_mode') or '?'} "
                f"next_idx={_idx} → {', '.join(_doms[:8])}{'…' if len(_doms) > 8 else ''}"
            )
        else:
            _raw_pool = _conf.get("mail_domains") or _conf.get("mail_domain_pool")
            print(
                f"[*] 邮箱域名: 单域名 {MAIL_DOMAIN or '(未设置)'} "
                f"（config.mail_domains={_raw_pool!r}）"
            )
    except Exception as e:
        print(f"[Warn] 域名池状态读取失败: {e}")

    last_err = ""
    for _ in range(5):
        local = _generate_local_part()
        domain = next_mail_domain(MAIL_DOMAIN) or MAIL_DOMAIN
        # 匿名 /api/new_address 可不指定 domain（由 Worker 分配）
        is_admin = create_path.rstrip("/").lower().endswith("/admin/new_address")
        if is_admin and not domain:
            raise Exception("mail_domain / mail_domains 未设置，无法创建邮箱地址")
        # 始终提交自定义 name（人名前缀）。
        # CF admin API：name 必填；enablePrefix 仅控制是否再拼 Worker PREFIX 环境变量。
        # 匿名 /api/new_address 若不带 name，服务端会 generateRandomName() → 完全随机串。
        use_cf_prefix = _truthy_conf("cloudflare_enable_prefix") or _truthy_conf(
            "mail_enable_prefix"
        )
        if is_admin:
            if not domain:
                raise Exception("mail_domain / mail_domains 未设置，无法创建邮箱地址")
            payload: dict[str, Any] = {
                "name": local,
                "domain": domain,
                "enablePrefix": bool(use_cf_prefix),
            }
        else:
            # 公共 API 也必须带 name，否则 Worker 用随机名覆盖我们的人名前缀
            payload = {"name": local, "enablePrefix": True}
            if domain:
                payload["domain"] = domain

        create_url = base_url
        if auth_mode in ("query-key", "query", "query_key", "key") and api_key:
            sep = "&" if "?" in create_url else "?"
            create_url = f"{create_url}{sep}key={api_key}"

        try:
            print(
                f"[*] 邮箱申请 local={local} domain={domain or '-'} "
                f"path={create_path} enablePrefix={payload.get('enablePrefix')}",
                flush=True,
            )
            res = _do_request(
                session,
                use_cffi,
                "post",
                create_url,
                json=payload,
                headers=headers,
                timeout=15,
            )
            if res.status_code in (200, 201):
                data = res.json()
                jwt = data.get("jwt")
                address = str(
                    data.get("address")
                    or (f"{local}@{domain}" if domain else "")
                    or ""
                ).strip()
                password = data.get("password", "")
                if jwt and address:
                    # 校验返回地址是否仍含我们请求的 local（允许 PREFIX 前缀）
                    addr_local = address.split("@", 1)[0].lower()
                    req_local = local.lower()
                    if "." in addr_local or "_" in addr_local:
                        print(
                            f"[Warn] 返回地址含 . 或 _（别名/剥字符风险）: {address}，重试",
                            flush=True,
                        )
                        last_err = f"unsafe local chars in address: {address}"
                        continue
                    if req_local not in addr_local and not addr_local.endswith(
                        req_local
                    ):
                        print(
                            f"[Warn] 邮箱 API 未保留自定义名: requested={local} "
                            f"got={address}（将重试；若持续发生请查 Worker "
                            f"DISABLE_CUSTOM_ADDRESS_NAME / ADDRESS_REGEX）",
                            flush=True,
                        )
                        last_err = (
                            f"custom name not honored: want={local} got={address}"
                        )
                        continue
                    # 最终地址必须像人名（允许 Worker PREFIX 前缀后仍可读）
                    if not _looks_human_local(addr_local):
                        print(
                            f"[Warn] 返回 local 不像人名前缀: {address} "
                            f"(requested={local})，重试",
                            flush=True,
                        )
                        last_err = f"non-human local: {address}"
                        continue
                    print(
                        f"[*] 邮箱创建成功: {address}"
                        f"（requested={local} domain={domain or '-'} mode={auth_mode}）",
                        flush=True,
                    )
                    # 独立一行，便于 UI 过滤规则改动后仍可检索
                    print(
                        f"[mail] created address={address} requested={local} "
                        f"domain={domain or '-'} path={create_path} mode={auth_mode}",
                        flush=True,
                    )
                    return address, password, jwt
                last_err = f"响应缺少 jwt/address: {data}"
            else:
                body = (res.text or "").strip()
                if len(body) > 200:
                    body = body[:200] + "..."
                last_err = f"HTTP {res.status_code}: {body} | url={create_url}"
                if res.status_code == 405:
                    last_err += (
                        " | 提示: 405 多为 API 地址填成了前端/Pages 或错误路径；"
                        "admin 用 /admin/new_address，匿名用 /api/new_address"
                    )
                if res.status_code in (401, 403):
                    last_err += (
                        f" | 提示: 鉴权失败 auth_mode={auth_mode}，"
                        "可改 cloudflare_auth_mode=none|x-admin-auth|bearer|x-api-key"
                    )
                if res.status_code in (400, 409):
                    continue
                break
        except Exception as e:
            last_err = f"{e} | url={create_url}"

    raise Exception(f"创建邮箱失败: {last_err}")


def _truthy_conf(key: str) -> bool:
    v = _conf.get(key)
    if v is True:
        return True
    if v is False or v is None:
        return False
    return str(v).lower() in ("1", "true", "yes", "on")


def fetch_emails(jwt: str, limit: int = 20, email: str = "") -> List[Dict[str, Any]]:
    """获取邮件列表（cloudflare / duckmail mail.tm / yyds 官方路径）。"""
    _reload_mail_conf()
    provider = _mail_provider()
    session, use_cffi = _create_session()
    base = MAIL_API_BASE.rstrip("/")
    paths: List[Tuple[str, Dict[str, Any]]] = []
    if provider == "gptmail":
        base = _normalize_gptmail_base(base or "https://mail.chatgpt.org.uk")
        api_key = (jwt or MAIL_ADMIN_AUTH or "").strip()
        if len(api_key) >= 7 and api_key[:7].lower() == "bearer ":
            api_key = api_key[7:].strip()
        headers = {"X-API-Key": api_key, "Accept": "application/json"}
        params_g: Dict[str, Any] = {}
        if email:
            params_g["email"] = email
        paths = [("/api/emails", params_g)]
    elif provider == "yyds":
        headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/json"}
        base = _normalize_yyds_base(base or "https://maliapi.215.im/v1")
        params: Dict[str, Any] = {"limit": limit}
        if email:
            params["address"] = email
        paths = [("/messages", params)]
    elif provider == "duckmail":
        # DuckMail ≈ mail.tm：Bearer 账号 token；列表 hydra:member；详情 /messages/{id}
        headers = {
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/json, application/ld+json",
        }
        paths = [
            ("/messages", {"page": 1}),
            ("/messages", {}),
        ]
    else:
        headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/json"}
        paths = [("/api/mails", {"limit": limit, "offset": 0})]
    for path, params in paths:
        try:
            res = _do_request(
                session,
                use_cffi,
                "get",
                f"{base}{path}",
                params=params,
                headers=headers,
                timeout=15,
            )
            if res.status_code != 200:
                continue
            data = res.json() if res.text else {}
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            if not isinstance(data, dict):
                continue
            # 业务失败
            if data.get("success") is False:
                continue
            # 嵌套 data
            if isinstance(data.get("data"), dict):
                data = data["data"]
            for key in (
                "hydra:member",
                "results",
                "messages",
                "mails",
                "emails",
                "data",
                "items",
                "list",
                "records",
            ):
                arr = data.get(key)
                if isinstance(arr, list):
                    return [x for x in arr if isinstance(x, dict)]
                if isinstance(arr, dict):
                    inner = arr.get("results") or arr.get("items") or arr.get("list") or arr.get("hydra:member")
                    if isinstance(inner, list):
                        return [x for x in inner if isinstance(x, dict)]
        except Exception:
            continue
    return []


def fetch_email_detail(jwt: str, msg_id: Any, email: str = "") -> Optional[Dict]:
    """获取单封邮件详情（含正文）。"""
    _reload_mail_conf()
    provider = _mail_provider()
    session, use_cffi = _create_session()
    base = MAIL_API_BASE.rstrip("/")
    paths: List[str] = []
    params: Dict[str, Any] = {}
    if provider == "gptmail":
        base = _normalize_gptmail_base(base or "https://mail.chatgpt.org.uk")
        api_key = (jwt or MAIL_ADMIN_AUTH or "").strip()
        if len(api_key) >= 7 and api_key[:7].lower() == "bearer ":
            api_key = api_key[7:].strip()
        headers = {"X-API-Key": api_key, "Accept": "application/json"}
        paths = [f"/api/email/{msg_id}"]
    elif provider == "yyds":
        headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/json"}
        base = _normalize_yyds_base(base or "https://maliapi.215.im/v1")
        paths = [f"/messages/{msg_id}"]
        if email:
            params["address"] = email
    elif provider == "duckmail":
        headers = {
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/json, application/ld+json",
        }
        paths = [f"/messages/{msg_id}"]
    else:
        headers = {"Authorization": f"Bearer {jwt}", "Accept": "application/json"}
        paths = [f"/api/mail/{msg_id}", f"/api/mails/{msg_id}"]
    for path in paths:
        try:
            res = _do_request(
                session,
                use_cffi,
                "get",
                f"{base}{path}",
                params=params or None,
                headers=headers,
                timeout=15,
            )
            if res.status_code == 200:
                data = res.json() if res.text else None
                if isinstance(data, dict):
                    if data.get("success") is False:
                        continue
                    inner = data.get("data") if isinstance(data.get("data"), dict) else data
                    return inner if isinstance(inner, dict) else data
        except Exception:
            continue
    return None


def _message_body_text(msg: Dict[str, Any]) -> str:
    """从单封邮件 dict 抽出可用于 OTP 匹配的正文（兼容 mail.tm / DuckMail / CF）。"""
    if not isinstance(msg, dict):
        return ""
    parts: List[str] = []
    for key in ("raw", "text", "html", "body", "bodyText", "bodyHtml", "content"):
        v = msg.get(key)
        if v is None or v == "":
            continue
        if isinstance(v, list):
            parts.append("\n".join(str(x) for x in v if x is not None))
        elif isinstance(v, dict):
            # 少数实现把 text/html 再包一层
            for sk in ("text", "html", "body", "value", "content"):
                if v.get(sk):
                    parts.append(str(v.get(sk)))
        else:
            parts.append(str(v))
    return "\n".join(p for p in parts if p).strip()


def wait_for_verification_code(jwt: str, timeout: int = 120, email: str = "") -> Optional[str]:
    """
    轮询等待验证码邮件。

    DuckMail / mail.tm：GET /messages 列表通常只有 subject + intro（摘要），
    完整正文在 GET /messages/{id}。绝不能把 intro 当成全文后永久 seen，否则 OTP 永远提不出来。
    """
    start = time.time()
    # 已成功拉过详情且仍无码的 id（每轮仍可重试详情，避免首封正文延迟）
    detail_tried: set = set()
    last_list_n = 0

    try:
        poll_interval = max(0.5, float(_conf.get("mail_poll_interval", 1)))
    except (TypeError, ValueError):
        poll_interval = 1

    provider = _mail_provider()
    # mail.tm 兼容：列表几乎必有 intro，必须强制拉详情
    force_detail = provider in ("duckmail", "yyds")

    while time.time() - start < timeout:
        messages = fetch_emails(jwt, email=email or "")
        last_list_n = len(messages) if isinstance(messages, list) else 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id")
            if msg_id is None:
                continue

            full = _message_body_text(msg)
            preview = str(msg.get("bodyPreview") or msg.get("intro") or "").strip()
            subject = str(msg.get("subject") or "").strip()

            need_detail = force_detail or (not full)
            content = full
            if need_detail and msg_id not in detail_tried:
                detail = fetch_email_detail(jwt, msg_id, email=email or "")
                detail_tried.add(msg_id)
                if detail:
                    d_full = _message_body_text(detail)
                    if d_full:
                        content = d_full
                    if not subject:
                        subject = str(detail.get("subject") or "").strip()
            elif need_detail and msg_id in detail_tried:
                # 再试一次详情（邮件可能后到/延迟写库）
                detail = fetch_email_detail(jwt, msg_id, email=email or "")
                if detail:
                    d_full = _message_body_text(detail)
                    if d_full:
                        content = d_full
                    if not subject:
                        subject = str(detail.get("subject") or "").strip()

            if not content and preview:
                content = preview

            if subject:
                content = f"Subject: {subject}\n{content}"

            code = extract_verification_code(content)
            if code:
                print(f"[*] 提取到验证码: {code}")
                return code
        time.sleep(poll_interval)

    if last_list_n:
        print(
            f"[Warn] 验证码超时：收件箱有 {last_list_n} 封邮件但未能提取 OTP"
            f"（provider={provider}，已尝试详情 {len(detail_tried)} 封）",
            flush=True,
        )
    else:
        print(
            f"[Warn] 验证码超时：收件箱仍为空（provider={provider}，{timeout}s）",
            flush=True,
        )
    return None


def extract_verification_code(content: str) -> Optional[str]:
    """
    从邮件内容提取验证码。
    Grok/x.ai 格式：MM0-SF3（3位-3位字母数字混合）或 6 位纯数字。
    """
    if not content:
        return None

    # 模式 1: Grok 格式 XXX-XXX
    m = re.search(r"(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])", content)
    if m:
        return m.group(1)

    # 模式 2: 带标签的验证码
    m = re.search(r"(?:verification code|验证码|your code)[:\s]*[<>\s]*([A-Z0-9]{3}-[A-Z0-9]{3})\b", content, re.IGNORECASE)
    if m:
        return m.group(1)

    # 模式 3: HTML 样式包裹
    m = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?([A-Z0-9]{3}-[A-Z0-9]{3})[\s\S]*?</p>", content)
    if m:
        return m.group(1)

    # 模式 4: Subject 行 6 位数字
    m = re.search(r"Subject:.*?(\d{6})", content)
    if m and m.group(1) != "177010":
        return m.group(1)

    # 模式 5: HTML 标签内 6 位数字
    for code in re.findall(r">\s*(\d{6})\s*<", content):
        if code != "177010":
            return code

    # 模式 6: 独立 6 位数字
    for code in re.findall(r"(?<![&#\d])(\d{6})(?![&#\d])", content):
        if code != "177010":
            return code

    return None
