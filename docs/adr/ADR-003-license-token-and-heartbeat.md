# ADR-003: License Token Format and Heartbeat Protocol

**Status:** Accepted
**Date:** 2026-04-11
**Deciders:** Pavan
**Depends on:** ADR-001 (hybrid topology), ADR-002 (entitlements)

---

## Context

The customer agent (ADR-001, Topology B and C) runs outside our infrastructure and must authenticate itself to the control plane, prove entitlement to run, receive configuration updates, and continue operating through short control-plane outages without putting a customer's live trades at risk.

A Topology-C customer may have a DGX in a network with restricted egress. The agent cannot assume always-on connectivity. At the same time, we must be able to revoke a license (subscription cancelled, payment failed, abuse, breach) within a bounded grace period.

Requirements:
1. **Authentication:** Agent proves identity to the control plane without leaking secrets.
2. **Entitlement caching:** Agent can enforce its own quota locally without round-tripping to the control plane on every action.
3. **Offline grace:** Agent continues to operate for N days of no heartbeat contact, then fails closed.
4. **Revocation:** Control plane can revoke an agent within bounded time (N days worst case).
5. **Clock skew tolerance:** 5 minutes on both sides.
6. **Replay resistance:** A stolen token for one agent can't be used on another, or at least the window is bounded.
7. **Trust root:** Control plane signs, agent verifies. No shared secrets.

## Decision

**We will use a short-lived JWT (RS256, 24-hour lifetime) as the license token, issued on subscription activation, refreshed on every heartbeat, and persisted to `~/.proxialpha/license` on the agent.**

- **Signing:** RS256 (RSA-2048). Control plane holds the private key in a KMS; agent embeds the public key at build time and falls back to fetching from a pinned JWKS endpoint on rotation.
- **Lifetime:** 24 hours. Short enough to bound revocation; long enough to survive overnight connectivity gaps without a support ticket.
- **Grace period:** 7 days of no successful heartbeat, after which the agent enters `DEGRADED` mode: paper trading and backtests continue, live order placement is blocked until contact is restored. This is a deliberate asymmetry — users losing paper access is annoying, users losing live access is dangerous. We err toward refusing live trades when we can't confirm entitlement.
- **Heartbeat cadence:** Every 60 seconds normal, every 10 seconds on startup for the first minute, exponential backoff to 5 minutes on failure.
- **Token refresh:** Every successful heartbeat returns a fresh 24h token. Agent rotates `~/.proxialpha/license` atomically (write-temp-then-rename).

### Token claims

```json
{
  "iss": "https://control.proxialpha.com",
  "sub": "agent_01HXYZ...",
  "aud": "proxialpha-agent",
  "iat": 1712880000,
  "exp": 1712966400,
  "nbf": 1712879700,
  "org_id": "org_01HXYZ...",
  "tier": "team",
  "topology": "C",
  "agent_fingerprint": "sha256:...",
  "entitlements_snapshot": {
    "period_end": 1715558400,
    "tickers_max": 10000,
    "backtests_remaining": 18432,
    "signals_remaining": 24871,
    "strategy_slots_max": 50,
    "live_trading_enabled": true,
    "live_perps_enabled": true,
    "custom_strategies_enabled": true,
    "overage_enabled": false
  },
  "grace_until": 1713484800,
  "jti": "lic_01HXYZ..."
}
```

- `agent_fingerprint` is a SHA-256 of a stable machine identifier + the agent's install-time generated UUID. Binds the token to one agent install; prevents trivial token theft from enabling a second free install.
- `entitlements_snapshot` is the cached hot-path data so the agent never needs a control-plane round trip to enforce quota.
- `grace_until` is `iat + 7 days`. The agent refuses live trades when `now > grace_until` regardless of `exp`, because a fresh token at that point is a sign of compromise or clock manipulation.
- `jti` is a monotonically-increasing license issuance ID. Enables fast revocation checks via a revocation bloom filter (optional; v1 uses naive lookup).

### Heartbeat protocol

```
POST https://control.proxialpha.com/agent/heartbeat
Authorization: Bearer <current license token>
Content-Type: application/json

{
  "agent_id": "agent_01HXYZ...",
  "version": "1.0.3",
  "topology": "C",
  "hostname": "trader-dgx-01",
  "started_at": "2026-04-11T09:00:00Z",
  "now": "2026-04-11T09:15:00Z",
  "last_event_ts": "2026-04-11T09:14:58Z",
  "metrics": {
    "live_trades_24h": 14,
    "paper_trades_24h": 112,
    "signals_24h": 47,
    "backtests_24h": 3,
    "errors_last_hour": 0
  }
}
```

**Responses:**

| Status | Meaning | Agent action |
|---|---|---|
| 200 | Healthy; body contains refreshed license token and optional config bundle delta | Rotate token, apply config if present |
| 401 | Token invalid, expired, signature failure | Stop live trading immediately, retry with hard backoff, surface error to user |
| 402 | Subscription past due | Enter `DEGRADED` mode, keep paper + backtest, block live, retry hourly |
| 403 | License revoked | Stop all trading immediately, clear local token, exit 1 with a clear message |
| 409 | Agent fingerprint mismatch (same token, different machine) | Stop all trading, surface compromise warning, exit 1 |
| 429 | Rate limited | Exponential backoff, do not drop existing token |
| 503 | Control plane temporarily unavailable | Keep current token, retry with backoff, stay fully operational until grace window closes |

## Options Considered

### Option 1: Static API keys per agent (rejected)

- Simpler to implement; well-understood.
- No way to carry entitlement state inline — every action needs a control-plane lookup.
- Revocation is a database flag — fine, but requires a live lookup the agent cannot cache safely.
- Verdict: worse in every dimension except initial simplicity.

### Option 2: mTLS client certificates (rejected)

- Strongest identity guarantee.
- Painful to rotate, painful to debug, painful to ship to a customer behind a corporate proxy.
- Does not solve entitlement caching — still need a second channel for quota.
- Verdict: too operationally heavy for the tier of customer we start with.

### Option 3 (Chosen): Short-lived JWT with entitlement snapshot + heartbeat refresh

- Good balance of security, operability, and offline tolerance.
- Self-contained: the token *is* the authentication, authorization, and entitlement cache.
- Well-trodden pattern (Tailscale, Supabase, Vercel all do variants of this).
- Cost: we must run a JWKS endpoint and treat signing keys as production secrets.

### Option 4: PASETO or Biscuit (rejected for v1)

- Stronger by some metrics, less ecosystem support.
- Revisit if JWT footguns become a problem in practice. Unlikely.

## Consequences

### Positive

- Agent operates autonomously for 7 days with no control-plane contact — survives our outages without putting customer trades at risk.
- Revocation is bounded to 24 hours in the common case (the length of one token lifetime), with a hard stop at 7 days worst case via `grace_until`.
- Every heartbeat is a natural point to ship config updates, quota refills, and metric collection — one round trip, many purposes.
- Local enforcement of quota means the agent hot path is zero network calls, which is critical for latency-sensitive live trading.

### Negative

- JWT verification on the agent requires shipping a public key. We must plan for key rotation (publish new key on JWKS, grace period of dual-valid, retire old). See follow-up work.
- The 7-day grace is a policy decision: shorter is safer, longer is friendlier. We pick 7 days because a customer going on vacation should not return to a broken bot. Revisit if abuse emerges.
- We must protect against clock manipulation on the agent side: if the customer sets their clock forward, they get a longer grace. Mitigation: heartbeat includes `agent.now` and control plane checks against its own clock, refuses to issue a token if skew > 5 minutes.
- `agent_fingerprint` must be stable across container restarts (persist to `~/.proxialpha/fingerprint`, regenerate on user-initiated re-registration).

### Security notes

1. **Key storage:** Private key in a KMS (AWS KMS, GCP KMS, or Vault). Control plane never sees the raw key; it asks KMS to sign. Rotate annually.
2. **JWKS endpoint:** `GET /.well-known/jwks.json`, cached for 1 hour. Supports key rotation without agent rebuild.
3. **Token theft:** A stolen token works for at most 24h (then needs refresh, which requires the heartbeat to succeed from the same fingerprint). Worst-case damage is 24h of abuse on a paid tier, bounded.
4. **Revocation:** Set `subscriptions.status='revoked'` in control-plane DB. The next heartbeat returns 403 within 24h. For emergency revocation, publish the `jti` to a revocation list checked on every heartbeat — rarely used but required for abuse response.
5. **Replay:** `jti` is unique per token. We do not accept the same `jti` twice for heartbeat purposes.

## Open Questions

1. **Multi-agent per org:** A Team-tier customer may run multiple agents (one per seat). Each gets its own token and fingerprint. Heartbeat correlates them by `org_id`. Seat count enforced at issuance time.
2. **Key rotation cadence:** Annual is the plan; we'll want a runbook in `operations/` before Year 1.
3. **Agent resurrect after force-stop:** If a customer `docker rm` the container and starts fresh, the new container gets a new fingerprint. Control plane allows this under a `agents_per_org` cap equal to seats + 2 (grace for normal churn). Abuse detection triggers at `> 2× seats` new fingerprints in 24h.
