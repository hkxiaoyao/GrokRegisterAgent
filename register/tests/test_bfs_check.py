# -*- coding: utf-8 -*-
"""BFS claim 检测：key 存在即标记；解不开必须是 unknown，不能伪造 clean。"""
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bfs_check import (  # noqa: E402
    BFS_CLEAN,
    BFS_FLAGGED,
    BFS_UNKNOWN,
    bfs_sidecar_fields,
    extract_bfs,
    read_bfs_from_auth_record,
    read_bfs_from_token,
)


def _jwt(payload: dict) -> str:
    def seg(obj) -> str:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.sig"


# ---------- 单 token ----------

def test_bfs_key_present_is_flagged():
    r = read_bfs_from_token(_jwt({"sub": "u1", "bfs": 2}))
    assert r["bfs_status"] == BFS_FLAGGED
    assert r["bfs_present"] is True
    assert r["bfs_value"] == 2


def test_bfs_zero_still_flagged():
    """key 存在即标记，值不参与判定 —— 与 bot_flag_source=0 语义相反。"""
    r = read_bfs_from_token(_jwt({"bfs": 0}))
    assert r["bfs_status"] == BFS_FLAGGED
    assert r["bfs_present"] is True
    assert r["bfs_value"] == 0


def test_bfs_null_still_flagged():
    r = read_bfs_from_token(_jwt({"bfs": None}))
    assert r["bfs_status"] == BFS_FLAGGED
    assert r["bfs_present"] is True


def test_bfs_empty_string_still_flagged():
    r = read_bfs_from_token(_jwt({"bfs": ""}))
    assert r["bfs_status"] == BFS_FLAGGED


def test_no_bfs_key_is_clean():
    r = read_bfs_from_token(_jwt({"sub": "u1", "bot_flag_source": 0}))
    assert r["bfs_status"] == BFS_CLEAN
    assert r["bfs_present"] is False
    assert r["bfs_value"] is None


def test_undecodable_is_unknown_not_clean():
    for bad in ("", None, "not-a-jwt", "a.b", "aaa.@@@@.ccc", "....", "x." + "!" * 9):
        r = read_bfs_from_token(bad)
        assert r["bfs_status"] == BFS_UNKNOWN, bad
        assert r["bfs_present"] is False


def test_payload_not_object_is_unknown():
    seg = base64.urlsafe_b64encode(b'["list"]').decode("ascii").rstrip("=")
    assert read_bfs_from_token(f"h.{seg}.s")["bfs_status"] == BFS_UNKNOWN


def test_sso_prefix_stripped():
    tok = _jwt({"bfs": 2})
    assert read_bfs_from_token(f"sso={tok}")["bfs_status"] == BFS_FLAGGED


def test_camel_and_snake_variants():
    assert read_bfs_from_token(_jwt({"botFlagScore": 3}))["bfs_status"] == BFS_FLAGGED
    assert read_bfs_from_token(_jwt({"bot_flag_score": 3}))["bfs_status"] == BFS_FLAGGED


# ---------- 多 token 优先级 ----------

def test_extract_prefers_access_and_records_source():
    r = extract_bfs(access=_jwt({"bfs": 2}), id_token=_jwt({}), sso=_jwt({}))
    assert r["bfs_status"] == BFS_FLAGGED
    assert r["bfs_source"] == "access_token"


def test_extract_flags_when_only_sso_has_bfs():
    """access 干净不代表整体干净，必须继续看 id_token / sso。"""
    r = extract_bfs(access=_jwt({"sub": "u"}), sso=_jwt({"bfs": 2}))
    assert r["bfs_status"] == BFS_FLAGGED
    assert r["bfs_source"] == "sso"


def test_extract_flags_when_only_id_token_has_bfs():
    r = extract_bfs(access=_jwt({}), id_token=_jwt({"bfs": 1}))
    assert r["bfs_status"] == BFS_FLAGGED
    assert r["bfs_source"] == "id_token"


def test_extract_clean_when_all_decodable_without_bfs():
    r = extract_bfs(access=_jwt({"sub": "u"}), sso=_jwt({"sub": "u"}))
    assert r["bfs_status"] == BFS_CLEAN


def test_extract_unknown_when_nothing_decodable():
    assert extract_bfs()["bfs_status"] == BFS_UNKNOWN
    assert extract_bfs(access="garbage", sso="also-bad")["bfs_status"] == BFS_UNKNOWN


def test_extract_unknown_access_but_clean_sso_is_clean():
    """至少一个能解开且无 bfs → clean。"""
    r = extract_bfs(access="garbage", sso=_jwt({"sub": "u"}))
    assert r["bfs_status"] == BFS_CLEAN


# ---------- 侧车字段 ----------

def test_sidecar_flagged_carries_value_and_source():
    out = bfs_sidecar_fields(access=_jwt({"bfs": 2}))
    assert out["bfs"] is True
    assert out["bfs_status"] == BFS_FLAGGED
    assert out["bfs_value"] == 2
    assert out["bfs_source"] == "access_token"


def test_sidecar_clean_has_no_value_key():
    out = bfs_sidecar_fields(access=_jwt({"sub": "u"}))
    assert out["bfs"] is False
    assert out["bfs_status"] == BFS_CLEAN
    assert "bfs_value" not in out


def test_sidecar_unknown_is_not_flagged():
    out = bfs_sidecar_fields()
    assert out["bfs"] is False
    assert out["bfs_status"] == BFS_UNKNOWN


# ---------- auth 记录回读 ----------

def test_auth_record_prefers_bfs_status_sidecar():
    r = read_bfs_from_auth_record(
        # 侧车说 flagged，token 干净 → 以侧车为准（mint 时的判定才是原始现场）
        {"bfs_status": BFS_FLAGGED, "bfs_value": 2, "access_token": _jwt({"sub": "u"})}
    )
    assert r["bfs_status"] == BFS_FLAGGED
    assert r["bfs_value"] == 2


def test_auth_record_legacy_bool_bfs():
    assert read_bfs_from_auth_record({"bfs": True})["bfs_status"] == BFS_FLAGGED
    assert read_bfs_from_auth_record({"bfs": False})["bfs_status"] == BFS_CLEAN


def test_auth_record_legacy_raw_claim_value():
    """旧写法可能把 claim 原值直接塞进 bfs。"""
    r = read_bfs_from_auth_record({"bfs": 2})
    assert r["bfs_status"] == BFS_FLAGGED
    assert r["bfs_value"] == 2


def test_auth_record_falls_back_to_tokens():
    r = read_bfs_from_auth_record({"access_token": _jwt({"bfs": 2})})
    assert r["bfs_status"] == BFS_FLAGGED


def test_auth_record_reads_sso_from_extra():
    r = read_bfs_from_auth_record({"extra": {"sso": _jwt({"bfs": 2})}})
    assert r["bfs_status"] == BFS_FLAGGED


def test_auth_record_empty_is_unknown():
    assert read_bfs_from_auth_record({})["bfs_status"] == BFS_UNKNOWN
    assert read_bfs_from_auth_record(None)["bfs_status"] == BFS_UNKNOWN
