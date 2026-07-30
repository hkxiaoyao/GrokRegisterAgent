# -*- coding: utf-8 -*-
"""Cloud Mail(skymail) provider：建号请求形状 + 列表解析 + 别名归一。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import email_register as er


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _stub(monkeypatch, payload, status=200):
    """拦截 _do_request，记录调用参数。"""
    seen = {}

    def fake(session, use_cffi, method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        seen["headers"] = kwargs.get("headers") or {}
        return _Resp(payload, status)

    monkeypatch.setattr(er, "_do_request", fake)
    monkeypatch.setattr(er, "_create_session", lambda: (object(), False))
    return seen


def _conf(monkeypatch, **over):
    base = {
        "mail_provider": "cloudmail",
        "mail_api_base": "https://mail.example.com",
        "mail_admin_auth": "tok-123",
        "mail_domain": "example.com",
    }
    base.update(over)
    monkeypatch.setattr(er, "_reload_mail_conf", lambda: None)
    monkeypatch.setattr(er, "_conf", base)
    monkeypatch.setattr(er, "MAIL_API_BASE", base["mail_api_base"])
    monkeypatch.setattr(er, "MAIL_ADMIN_AUTH", base["mail_admin_auth"])
    monkeypatch.setattr(er, "MAIL_DOMAIN", base["mail_domain"])
    monkeypatch.setattr(er, "next_mail_domain", lambda fallback="": fallback)
    return base


def test_provider_aliases_map_to_cloudmail(monkeypatch):
    for alias in ("cloudmail", "cloud-mail", "cloud_mail", "skymail", "CloudMail"):
        _conf(monkeypatch, mail_provider=alias)
        assert er._mail_provider() == "cloudmail"


def test_create_posts_addUser_with_bare_token(monkeypatch):
    _conf(monkeypatch)
    seen = _stub(monkeypatch, {"code": 200, "message": "success", "data": None})
    email, token = er._create_cloudmail()
    assert seen["method"] == "post"
    assert seen["url"] == "https://mail.example.com/api/public/addUser"
    # 裸 Token，不带 Bearer 前缀
    assert seen["headers"]["Authorization"] == "tok-123"
    body = seen["json"]
    assert isinstance(body["list"], list) and len(body["list"]) == 1
    assert body["list"][0]["email"] == email
    assert body["list"][0]["password"]
    assert email.endswith("@example.com")
    assert token == "tok-123"


def test_create_strips_bearer_prefix(monkeypatch):
    _conf(monkeypatch, mail_admin_auth="Bearer tok-xyz")
    seen = _stub(monkeypatch, {"code": 200})
    er._create_cloudmail()
    assert seen["headers"]["Authorization"] == "tok-xyz"


def test_create_requires_domain(monkeypatch):
    _conf(monkeypatch, mail_domain="")
    _stub(monkeypatch, {"code": 200})
    try:
        er._create_cloudmail()
    except Exception as e:
        assert "mail_domain" in str(e)
    else:
        raise AssertionError("空域名应当报错，Cloud Mail 不会自动分配域名")


def test_create_raises_on_business_error(monkeypatch):
    _conf(monkeypatch)
    _stub(monkeypatch, {"code": 500, "message": "token 无效"})
    try:
        er._create_cloudmail()
    except Exception as e:
        assert "cloudmail 创建失败" in str(e)
    else:
        raise AssertionError("code!=200 必须抛错，不能返回假邮箱")


def test_fetch_emails_parses_list_and_maps_id(monkeypatch):
    _conf(monkeypatch)
    seen = _stub(
        monkeypatch,
        {
            "code": 200,
            "data": [
                {
                    "emailId": 999,
                    "sendEmail": "no-reply@x.ai",
                    "subject": "Your verification code",
                    "toEmail": "john47@example.com",
                    "content": "<div>code 123-456</div>",
                    "text": "code 123-456",
                }
            ],
        },
    )
    msgs = er.fetch_emails("tok-123", limit=5, email="john47@example.com")
    assert seen["url"] == "https://mail.example.com/api/public/emailList"
    assert seen["json"]["toEmail"] == "john47@example.com"
    assert seen["json"]["type"] == 0
    assert len(msgs) == 1
    # 轮询循环按 msg["id"] 去重，必须从 emailId 映射出来
    assert msgs[0]["id"] == 999
    assert er.extract_verification_code(er._message_body_text(msgs[0])) is not None


def test_fetch_emails_empty_on_business_error(monkeypatch):
    _conf(monkeypatch)
    _stub(monkeypatch, {"code": 401, "message": "unauthorized"})
    assert er.fetch_emails("tok-123", email="a@example.com") == []


def test_detail_is_skipped_for_cloudmail(monkeypatch):
    _conf(monkeypatch)
    called = {"n": 0}

    def fake(*a, **k):
        called["n"] += 1
        return _Resp({"code": 200})

    monkeypatch.setattr(er, "_do_request", fake)
    monkeypatch.setattr(er, "_create_session", lambda: (object(), False))
    assert er.fetch_email_detail("tok-123", 999, email="a@example.com") is None
    assert called["n"] == 0, "Cloud Mail 无详情接口，不该发请求"


def test_api_base_normalizer_strips_public_paths():
    assert (
        er._normalize_mail_api_base("https://mail.example.com/api/public/emailList")
        == "https://mail.example.com"
    )
    assert (
        er._normalize_mail_api_base("https://mail.example.com/api/public/")
        == "https://mail.example.com"
    )
