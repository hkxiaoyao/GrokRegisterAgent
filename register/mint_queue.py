# -*- coding: utf-8 -*-
"""
U1 · Mint 双池：与注册/授权延迟队列分离的 mint worker 池。

架构:
  注册成功 → auth_export_queue（SSO→g2 等轻量步骤）
           → 或直接 enqueue_mint（本模块）做 SSO→CPA Auth mint

config.json / 环境变量:
  cpa_mint_workers / CPA_MINT_WORKERS: mint 并发，默认 1，范围 0～8
    0 = 内联（由 auth_export_queue 线程直接 mint，不启独立池）
  cpa_mint_queue_max / CPA_MINT_QUEUE_MAX: 队列上限，默认 max(2, 2×workers)
  cpa_mint_max_attempts / CPA_MINT_MAX_ATTEMPTS: 每账号 mint 预算（默认 2）
  cpa_mint_retry_queue_max / CPA_MINT_RETRY_QUEUE_MAX: 背压待重试队列上限（默认 200）
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

LogFn = Callable[[str], None]

_CONFIG = Path(__file__).resolve().parent / "config.json"

_q: queue.Queue[dict[str, Any] | None] | None = None
_workers: list[threading.Thread] = []
_lock = threading.Lock()
_started = False
_pending = 0
_done_ok = 0
_done_fail = 0
_worker_count = 0
_queue_max = 0
# mint 失败原因计数（与 auth_export_queue.classify_mint_status 对齐）
_fail_by_status: dict[str, int] = {}
# 每账号尝试次数（email 优先，否则 sso 指纹前缀）
_attempt_by_key: dict[str, int] = {}
# 背压待重试：不伪装成功；池有空位时再入主队列
_retry_q: list[dict[str, Any]] = []
_retry_queued = 0
_retry_requeued = 0
_retry_dropped = 0
_budget_exhausted = 0
_max_attempts = 2
_retry_queue_max = 200


def _log(msg: str, log: LogFn | None = None) -> None:
    fn = log or (lambda m: print(m, flush=True))
    try:
        fn(msg)
    except Exception:
        print(msg, flush=True)


def _bump_fail_status(status: str) -> None:
    key = (status or "unknown").strip() or "unknown"
    with _lock:
        _fail_by_status[key] = int(_fail_by_status.get(key) or 0) + 1


def _load_conf() -> dict[str, Any]:
    try:
        if _CONFIG.is_file():
            conf = json.loads(_CONFIG.read_text(encoding="utf-8"))
            return conf if isinstance(conf, dict) else {}
    except Exception:
        pass
    return {}


def load_mint_pool_settings() -> tuple[int, int]:
    """返回 (workers, queue_max)。workers=0 表示禁用独立 mint 池。"""
    conf = _load_conf()
    workers = 1
    try:
        workers = int(
            conf.get("cpa_mint_workers")
            if conf.get("cpa_mint_workers") is not None
            else conf.get("cpaMintWorkers")
            if conf.get("cpaMintWorkers") is not None
            else 1
        )
    except Exception:
        workers = 1
    env_w = os.environ.get("CPA_MINT_WORKERS", "").strip()
    if env_w.isdigit() or (env_w.startswith("-") and env_w[1:].isdigit()):
        workers = int(env_w)
    # -1 = auto → 1；0 = inline（不启池）
    if workers < 0:
        workers = 1
    workers = max(0, min(workers, 8))

    qmax = 0
    try:
        qmax = int(conf.get("cpa_mint_queue_max") or conf.get("cpaMintQueueMax") or 0)
    except Exception:
        qmax = 0
    env_q = os.environ.get("CPA_MINT_QUEUE_MAX", "").strip()
    if env_q.isdigit():
        qmax = int(env_q)
    if qmax <= 0:
        qmax = max(2, workers * 2) if workers > 0 else 4
    qmax = max(1, min(qmax, 64))
    return workers, qmax


def load_mint_budget_settings() -> tuple[int, int]:
    """返回 (max_attempts, retry_queue_max)。"""
    conf = _load_conf()
    max_attempts = 2
    try:
        max_attempts = int(
            conf.get("cpa_mint_max_attempts")
            if conf.get("cpa_mint_max_attempts") is not None
            else conf.get("cpaMintMaxAttempts")
            if conf.get("cpaMintMaxAttempts") is not None
            else 2
        )
    except Exception:
        max_attempts = 2
    env_a = os.environ.get("CPA_MINT_MAX_ATTEMPTS", "").strip()
    if env_a.isdigit():
        max_attempts = int(env_a)
    # 0 = 不限制（实验用）；上限 8 防止打爆
    max_attempts = max(0, min(max_attempts, 8))

    retry_max = 200
    try:
        retry_max = int(
            conf.get("cpa_mint_retry_queue_max")
            if conf.get("cpa_mint_retry_queue_max") is not None
            else conf.get("cpaMintRetryQueueMax")
            if conf.get("cpaMintRetryQueueMax") is not None
            else 200
        )
    except Exception:
        retry_max = 200
    env_r = os.environ.get("CPA_MINT_RETRY_QUEUE_MAX", "").strip()
    if env_r.isdigit():
        retry_max = int(env_r)
    retry_max = max(8, min(retry_max, 2000))
    return max_attempts, retry_max


def use_separate_mint_pool() -> bool:
    w, _ = load_mint_pool_settings()
    return w > 0


def _account_key(email: str, sso: str) -> str:
    em = (email or "").strip().lower()
    if em:
        return f"email:{em}"
    s = (sso or "").strip()
    if not s:
        return "sso:empty"
    return f"sso:{s[:24]}"


def _status_ratios(fail_by: dict[str, int]) -> dict[str, float]:
    keys = (
        "mint_queue_full",
        "mint_denied_castle",
        "mint_oauth_fail",
        "mint_skipped_bot",
        "mint_budget_exhausted",
        "mint_fail",
        "sso_g2_fail",
        "worker_error",
    )
    total = sum(int(fail_by.get(k) or 0) for k in keys) or 0
    if total <= 0:
        return {k: 0.0 for k in keys}
    return {k: round(100.0 * int(fail_by.get(k) or 0) / total, 1) for k in keys}


def queue_stats() -> dict[str, Any]:
    with _lock:
        fail_by = dict(_fail_by_status)
        retry_n = len(_retry_q)
        attempts_n = len(_attempt_by_key)
        budget_ex = _budget_exhausted
        retry_queued = _retry_queued
        retry_requeued = _retry_requeued
        retry_dropped = _retry_dropped
        max_att = _max_attempts
        retry_max = _retry_queue_max
    ratios = _status_ratios(fail_by)
    return {
        "pending": _pending,
        "queue_size": _q.qsize() if _q else 0,
        "done_ok": _done_ok,
        "done_fail": _done_fail,
        "workers": _worker_count,
        "queue_max": _queue_max,
        "separate_pool": use_separate_mint_pool(),
        "fail_by_status": fail_by,
        "fail_status_ratio_pct": ratios,
        "retry_pending": retry_n,
        "retry_queued_total": retry_queued,
        "retry_requeued_total": retry_requeued,
        "retry_dropped_total": retry_dropped,
        "retry_queue_max": retry_max,
        "mint_max_attempts": max_att,
        "budget_exhausted_total": budget_ex,
        "tracked_accounts": attempts_n,
    }


def _maybe_requeue_retries() -> None:
    """主队列有空位时，把待重试任务塞回去（不伪装成功）。"""
    global _pending, _retry_requeued
    if _q is None:
        return
    moved = 0
    while True:
        with _lock:
            if not _retry_q:
                break
            try:
                free = max(0, _q.maxsize - _q.qsize())
            except Exception:
                free = 0
            if free <= 0:
                break
            job = _retry_q.pop(0)
        try:
            _q.put_nowait(job)
        except queue.Full:
            with _lock:
                _retry_q.insert(0, job)
            break
        with _lock:
            _pending += 1
            _retry_requeued += 1
        moved += 1
        email = str(job.get("email") or "-")
        _log(
            f"[mint-queue] 待重试已回灌 email={email} "
            f"attempt={int(job.get('attempt') or 0)} pending≈{_pending}"
        )
    if moved:
        try:
            from auth_export_queue import queue_stats as auth_stats

            auth_stats()  # 刷新合并 metrics
        except Exception:
            pass


def _enqueue_retry(job: dict[str, Any], *, log: LogFn | None = None) -> dict[str, Any]:
    """背压：写入待重试队列，不丢号、不内联 mint。"""
    global _retry_queued, _retry_dropped, _retry_queue_max
    _, retry_max = load_mint_budget_settings()
    queued_ok = False
    with _lock:
        _retry_queue_max = retry_max
        if len(_retry_q) >= retry_max:
            _retry_dropped += 1
            n = len(_retry_q)
            status = "mint_retry_dropped"
        else:
            _retry_q.append(dict(job))
            _retry_queued += 1
            n = len(_retry_q)
            queued_ok = True
            status = "mint_queue_full"
    # 锁外计数/打日志，避免与 _bump_fail_status 重入死锁
    _bump_fail_status(status)
    if queued_ok:
        _log(
            f"[mint-queue] 背压入待重试 status=mint_queue_full "
            f"email={job.get('email') or '-'} retry_pending={n}/{retry_max}",
            log,
        )
        return {
            "queued": False,
            "error": "mint queue full",
            "backpressure": True,
            "status": "mint_queue_full",
            "retry_queued": True,
            "retry_pending": n,
        }
    _log(
        f"[mint-queue] 待重试队列已满 status=mint_retry_dropped "
        f"email={job.get('email') or '-'} retry_pending={n}/{retry_max}",
        log,
    )
    return {
        "queued": False,
        "error": "mint retry queue full",
        "backpressure": True,
        "status": "mint_retry_dropped",
        "retry_queued": False,
        "retry_pending": n,
    }


def _process_mint_job(job: dict[str, Any]) -> None:
    global _done_ok, _done_fail, _budget_exhausted, _max_attempts
    from auth_export_queue import _run_mint_and_auth_push, classify_mint_status

    email = str(job.get("email") or "")
    sso = str(job.get("sso") or "")
    key = _account_key(email, sso)
    max_attempts, _ = load_mint_budget_settings()
    with _lock:
        _max_attempts = max_attempts
        used = int(_attempt_by_key.get(key) or 0)
        over_budget = max_attempts > 0 and used >= max_attempts
        if not over_budget:
            used += 1
            _attempt_by_key[key] = used
            # 防止字典无限涨
            if len(_attempt_by_key) > 5000:
                for drop_k in list(_attempt_by_key.keys())[:2500]:
                    _attempt_by_key.pop(drop_k, None)
        else:
            _done_fail += 1
            _budget_exhausted += 1
    if over_budget:
        _bump_fail_status("mint_budget_exhausted")
        _log(
            f"[mint-queue] ✘ mint 跳过 status=mint_budget_exhausted "
            f"email={email or '-'} attempts={used}/{max_attempts}"
        )
        return

    wid = threading.current_thread().name
    attempt = used
    job["attempt"] = attempt
    _log(
        f"[mint-queue][{wid}] ▶ mint email={email or '-'} "
        f"attempt={attempt}"
        + (f"/{max_attempts}" if max_attempts > 0 else "")
    )
    try:
        r = _run_mint_and_auth_push(
            sso=sso,
            email=email,
            proxy=str(job.get("proxy") or ""),
            mint_mode=str(job.get("mint_mode") or "pkce"),
            push_cpa=bool(job.get("push_cpa")),
            password=str(job.get("password") or ""),
            cloudflare_cookies=str(job.get("cloudflare_cookies") or ""),
            log=_log,
        )
        if r and r.get("ok"):
            _done_ok += 1
            with _lock:
                # 成功后清预算计数，避免同邮箱日后补签被误伤
                _attempt_by_key.pop(key, None)
            _log(f"[mint-queue][{wid}] ✔ mint OK email={email or '-'}")
        else:
            _done_fail += 1
            status = str(
                (r or {}).get("status")
                or classify_mint_status(r if isinstance(r, dict) else None)
            )
            _bump_fail_status(status)
            _log(
                f"[mint-queue][{wid}] ✘ mint fail email={email or '-'} "
                f"status={status} attempt={attempt} "
                f"err={(r or {}).get('error') or 'unknown'}"
            )
            # 预算未用尽且非明确 bot skip：失败不再自动重入队
            # （背压重试只服务 queue full；策略失败重复烧池）
    except Exception as e:
        _done_fail += 1
        _bump_fail_status("worker_error")
        _log(f"[mint-queue][{wid}] ✘ 异常 status=worker_error: {e}")
    finally:
        _maybe_requeue_retries()


def _worker_loop() -> None:
    global _pending
    assert _q is not None
    while True:
        job = _q.get()
        try:
            if job is None:
                break
            _process_mint_job(job)
        finally:
            with _lock:
                _pending = max(0, _pending - 1)
            _q.task_done()
            _maybe_requeue_retries()


def ensure_mint_workers() -> None:
    global _q, _workers, _started, _worker_count, _queue_max, _max_attempts, _retry_queue_max
    workers, qmax = load_mint_pool_settings()
    max_attempts, retry_max = load_mint_budget_settings()
    if workers <= 0:
        return
    with _lock:
        alive = [t for t in _workers if t.is_alive()]
        _workers = alive
        _max_attempts = max_attempts
        _retry_queue_max = retry_max
        if _started and alive and _q is not None:
            return
        _worker_count = workers
        _queue_max = qmax
        if _q is None:
            _q = queue.Queue(maxsize=qmax)
        need = workers - len(alive)
        for i in range(need):
            t = threading.Thread(
                target=_worker_loop,
                name=f"mint-w{len(alive) + i + 1}",
                daemon=True,
            )
            t.start()
            _workers.append(t)
        _started = True
        _log(
            f"[mint-queue] mint 池已启动 workers={workers} queue_max={qmax} "
            f"max_attempts={max_attempts or '∞'} retry_max={retry_max}"
        )


def enqueue_mint(
    *,
    sso: str,
    email: str = "",
    password: str = "",
    proxy: str = "",
    mint_mode: str = "pkce",
    push_cpa: bool = False,
    cloudflare_cookies: str = "",
    log: Optional[LogFn] = None,
    block_sec: float = 120.0,
) -> dict[str, Any]:
    """入 mint 池。若 workers=0，返回 use_inline=True 由调用方内联 mint。"""
    global _pending, _budget_exhausted, _max_attempts
    sso = (sso or "").strip()
    if not sso:
        return {"queued": False, "error": "empty sso"}

    workers, _qmax = load_mint_pool_settings()
    if workers <= 0:
        return {"queued": False, "use_inline": True, "reason": "cpa_mint_workers=0"}

    max_attempts, _ = load_mint_budget_settings()
    key = _account_key(email, sso)
    with _lock:
        _max_attempts = max_attempts
        used = int(_attempt_by_key.get(key) or 0)
        over_budget = max_attempts > 0 and used >= max_attempts
        if over_budget:
            _budget_exhausted += 1
    if over_budget:
        _bump_fail_status("mint_budget_exhausted")
        _log(
            f"[mint-queue] 跳过入队 status=mint_budget_exhausted "
            f"email={email or '-'} attempts={used}/{max_attempts}",
            log,
        )
        return {
            "queued": False,
            "error": "mint budget exhausted",
            "status": "mint_budget_exhausted",
            "attempts": used,
            "max_attempts": max_attempts,
        }

    ensure_mint_workers()
    assert _q is not None
    # 先尽量消化待重试
    _maybe_requeue_retries()

    job = {
        "sso": sso,
        "email": (email or "").strip(),
        "password": (password or "").strip(),
        "proxy": (proxy or "").strip(),
        "mint_mode": (mint_mode or "pkce").strip().lower(),
        "push_cpa": bool(push_cpa),
        "cloudflare_cookies": (cloudflare_cookies or "").strip(),
        "enqueued_at": time.time(),
        "attempt": used + 1,
    }
    try:
        # 背压时不要长阻塞占 auth worker：短等后进待重试
        wait = min(8.0, max(0.05, float(block_sec)))
        _q.put(job, timeout=wait)
    except queue.Full:
        return _enqueue_retry(job, log=log)

    with _lock:
        _pending += 1
    _log(
        f"[mint-queue] 已入队 email={email or '-'} pending≈{_pending} "
        f"qsize={_q.qsize()} attempt≈{used + 1}"
        + (f"/{max_attempts}" if max_attempts > 0 else ""),
        log,
    )
    return {
        "queued": True,
        "pending": _pending,
        "workers": workers,
        "attempt": used + 1,
        "max_attempts": max_attempts,
    }


def wait_mint_idle(timeout: float = 600.0) -> bool:
    """等待 mint 主队列 + 待重试清空。"""
    end = time.time() + timeout
    while time.time() < end:
        _maybe_requeue_retries()
        q_idle = True
        if _q is not None:
            q_idle = _q.unfinished_tasks == 0
        with _lock:
            retry_n = len(_retry_q)
            pend = _pending
        if q_idle and retry_n <= 0 and pend <= 0:
            return True
        time.sleep(0.5)
    _maybe_requeue_retries()
    with _lock:
        retry_n = len(_retry_q)
    if _q is None:
        return retry_n <= 0
    return _q.unfinished_tasks == 0 and retry_n <= 0
