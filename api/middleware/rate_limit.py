"""Lightweight in-process rate limiting for the agent surface (P1-8).

This module gives the control plane a "good enough" first line of
defense against brute-force enrollment attempts and heartbeat floods
without pulling in another dependency. It is **not** a substitute for
a real edge-layer rate limiter (Cloudflare / AWS WAF / Envoy) — those
should sit in front of the API in production. The in-process limiter
catches:

* Bursts that slip past the edge during a misconfiguration window.
* Single-tenant runaway agents that fan out faster than the heartbeat
  cadence allows.
* Brute-force install-token guessing from a single IP.

Algorithm
---------

Token bucket per (route, key). Each bucket starts with ``capacity``
tokens, refills at ``refill_per_second`` tokens/sec, and a request
consumes one token. When the bucket is empty we return a 429 with a
``Retry-After`` header. The bucket state is held in process memory —
horizontally scaled deployments get N independent buckets, which is
fine because the per-pod budget multiplies by the pod count and the
edge limiter handles the global budget.

Keys
----

* **enroll** — keyed on the client IP plus a 12-char prefix of the
  install token. The token prefix prevents an attacker who controls
  one IP from mass-guessing many tokens — each guess gets its own
  bucket and they all share the IP bucket too.
* **heartbeat** — keyed on the agent_id derived from the JWT (when
  decodable, without verifying — we just want a key) plus IP. A
  legitimate agent never bursts past its hourly cadence, so even a
  small bucket here is plenty.

We never raise from the limiter — on any internal error it logs at
WARNING and lets the request through. Failure modes should never
take down auth surface.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request

log = logging.getLogger(__name__)


@dataclass
class _Bucket:
    """A single token bucket. Mutated in place under the GIL — fine
    for in-process Python because every operation is a few attribute
    writes with no awaits in between."""

    tokens: float
    last_refill: float
    capacity: float
    refill_per_second: float

    def take(self, now: float, cost: float = 1.0) -> Tuple[bool, float]:
        # Refill first.
        delta = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + delta * self.refill_per_second)
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        # Compute Retry-After in seconds for the response header.
        deficit = cost - self.tokens
        retry_after = (
            deficit / self.refill_per_second if self.refill_per_second > 0 else 60.0
        )
        return False, retry_after


class RateLimiter:
    """Multi-bucket in-process rate limiter.

    Buckets live in a plain dict keyed on ``(scope, key)``. We never
    evict — for the agent surface the keyspace is small and bounded
    (number of agents per pod), and even with 100k unique keys the
    dict footprint is well under 50 MB.
    """

    def __init__(self) -> None:
        self._buckets: Dict[Tuple[str, str], _Bucket] = {}

    def check(
        self,
        *,
        scope: str,
        key: str,
        capacity: float,
        refill_per_second: float,
    ) -> Tuple[bool, float]:
        now = time.monotonic()
        bk = (scope, key)
        bucket = self._buckets.get(bk)
        if bucket is None:
            bucket = _Bucket(
                tokens=capacity,
                last_refill=now,
                capacity=capacity,
                refill_per_second=refill_per_second,
            )
            self._buckets[bk] = bucket
        return bucket.take(now)

    def reset_for_tests(self) -> None:
        self._buckets.clear()


# Module-level singleton — the agent surface is small enough that one
# limiter instance is the right granularity.
_limiter = RateLimiter()


def get_limiter() -> RateLimiter:
    return _limiter


def _client_ip(request: Request) -> str:
    """Best-effort client IP extraction.

    Honours ``X-Forwarded-For`` so that a request behind an HTTPS
    load balancer is keyed on the original client and not the LB
    address. Falls back to the immediate peer when the header is
    absent.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # Take the first hop — that's the client per RFC 7239.
        return xff.split(",")[0].strip()
    if request.client is None:
        return "unknown"
    return request.client.host or "unknown"


def enforce_enroll_limit(request: Request, install_token: str) -> None:
    """Apply enroll-route limits, raising 429 on overflow.

    Two buckets are checked in sequence: per-IP and per-token-prefix.
    Either one tripping returns 429 — we don't try to be clever about
    "which one tripped first" because the agent's response is the
    same either way (back off and retry).
    """
    try:
        ip = _client_ip(request)
        ok_ip, retry_ip = _limiter.check(
            scope="enroll:ip",
            key=ip,
            capacity=10,
            refill_per_second=10 / 60,  # 10 attempts / minute / IP
        )
        if not ok_ip:
            _raise_429(retry_ip)

        token_prefix = install_token[:12] if install_token else "anon"
        ok_tok, retry_tok = _limiter.check(
            scope="enroll:token",
            key=token_prefix,
            capacity=3,
            refill_per_second=3 / 60,  # 3 attempts / minute / token prefix
        )
        if not ok_tok:
            _raise_429(retry_tok)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("rate_limit: enroll limiter error: %s", exc)


def enforce_heartbeat_limit(request: Request, agent_key: Optional[str]) -> None:
    """Apply heartbeat-route limits, raising 429 on overflow.

    The bucket is generous — 30 hits / minute / agent — because a
    legitimate agent only sends one heartbeat per hour, so we have
    multiple orders of magnitude of headroom for retries and clock
    drift. The cap exists purely to stop a runaway loop.
    """
    try:
        ip = _client_ip(request)
        ok_ip, retry_ip = _limiter.check(
            scope="heartbeat:ip",
            key=ip,
            capacity=120,
            refill_per_second=120 / 60,  # 120 hits / minute / IP
        )
        if not ok_ip:
            _raise_429(retry_ip)

        if agent_key:
            ok_a, retry_a = _limiter.check(
                scope="heartbeat:agent",
                key=agent_key,
                capacity=30,
                refill_per_second=30 / 60,  # 30 hits / minute / agent
            )
            if not ok_a:
                _raise_429(retry_a)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("rate_limit: heartbeat limiter error: %s", exc)


def enforce_bundle_upload_limit(request: Request) -> None:
    """Apply support-bundle upload limits, raising 429 on overflow.

    Doctor bundle uploads (P2-7) sit on a much rarer cadence than the
    other agent surfaces — a single ticket typically produces one or
    two bundles, and a healthy fleet only sees uploads when an
    operator runs ``proxialpha doctor`` by hand. The cap exists to
    stop a runaway agent loop from filling object storage, NOT to
    throttle legitimate use, so the bucket is small and the refill is
    slow:

    * **per-IP**: 5 uploads / minute, capacity 5. A panicked operator
      can re-run the doctor command a handful of times in a row
      before hitting the wall, which matches the observed worst case.
    * **per-IP/hour**: 20 uploads / hour, capacity 20. Catches the
      "wedged retry loop" failure mode where a script hammers the
      endpoint between minute-level windows.

    There is no per-org bucket here because the auth path supports
    install-token uploads from unenrolled agents — at that point we
    do not yet have a stable org key without an extra DB hit, and the
    IP bucket is sufficient defense given the rarity of legitimate
    traffic.
    """
    try:
        ip = _client_ip(request)
        ok_min, retry_min = _limiter.check(
            scope="bundle:ip:min",
            key=ip,
            capacity=5,
            refill_per_second=5 / 60,  # 5 uploads / minute / IP
        )
        if not ok_min:
            _raise_429(retry_min)

        ok_hr, retry_hr = _limiter.check(
            scope="bundle:ip:hr",
            key=ip,
            capacity=20,
            refill_per_second=20 / 3600,  # 20 uploads / hour / IP
        )
        if not ok_hr:
            _raise_429(retry_hr)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("rate_limit: bundle limiter error: %s", exc)


def _raise_429(retry_after: float) -> None:
    raise HTTPException(
        status_code=429,
        detail={"error": "rate_limited", "reason": "too_many_requests"},
        headers={"Retry-After": str(max(1, int(retry_after) + 1))},
    )


__all__ = [
    "RateLimiter",
    "enforce_bundle_upload_limit",
    "enforce_enroll_limit",
    "enforce_heartbeat_limit",
    "get_limiter",
]
