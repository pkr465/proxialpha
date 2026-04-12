# Task 06 — Agent Heartbeat Endpoint

**Phase:** 2 (Customer Agent)
**Est. effort:** 5 hours
**Prerequisites:** Tasks 01–04. Can overlap with Task 05.

## Objective

Implement `POST /agent/heartbeat` on the control plane: accepts a heartbeat from a customer agent, verifies the license token, updates the agent's registration row, returns a refreshed license token with a current entitlements snapshot, and optionally returns a config bundle delta.

## Context

- Protocol defined in `docs/adr/ADR-003-license-token-and-heartbeat.md` §"Heartbeat protocol".
- Token format in the same ADR, §"Token claims".
- Phase 2 PRD `docs/specs/phase2-customer-agent.md` §9 (heartbeat loop from the agent side) and §15 (config bundle format).
- This is the central rendezvous point between control plane and agent. Every state change flows through it.

## Exact files to create or modify

1. `api/agent/__init__.py` — new package.
2. `api/agent/heartbeat.py` — **new file**. `POST /agent/heartbeat`.
3. `api/agent/license_issuer.py` — **new file**. `issue_license(org_id, agent_id, fingerprint) -> jwt_str`.
4. `api/agent/schemas.py` — **new file**. Pydantic request/response models.
5. `core/jwt_keys.py` — **new file**. Loads RS256 private key from KMS (for v1: a file path in env `AGENT_SIGNING_KEY_PATH`), exposes `sign(claims) -> jwt`. Fallback path: generate a dev keypair at startup and print the fingerprint if `AGENT_SIGNING_KEY_PATH` is unset and `ENV=dev`.
6. `tests/test_heartbeat_endpoint.py` — **new file**.
7. A migration to add `agents.metered_item_ids` if missing: `ALTER TABLE agents ADD COLUMN ...` — actually, that belongs on `subscriptions` per Task 05 context. `agents` stays as originally specced.

## Acceptance criteria

Request schema (`docs/adr/ADR-003-license-token-and-heartbeat.md` §"Heartbeat protocol"):
```json
{
  "agent_id": "agent_01...",
  "version": "1.0.3",
  "topology": "C",
  "hostname": "trader-dgx-01",
  "started_at": "...",
  "now": "...",
  "last_event_ts": "...",
  "metrics": {...}
}
```

Authorization: `Authorization: Bearer <current license token>`.

Endpoint behavior:
1. Parse and verify the Bearer JWT using `core/jwt_keys.verify()`.
2. On signature failure / expired / `nbf` not yet valid → 401 with `{"error": "invalid_token", "reason": "..."}`.
3. Extract `org_id`, `sub` (agent_id), `agent_fingerprint` from claims.
4. Fetch the org's current subscription.
   - If `subscriptions.status == 'canceled'` → 403 with `{"error": "license_revoked", "reason": "subscription_canceled"}`.
   - If `subscriptions.status == 'past_due'` → 402 with `{"error": "past_due", "reason": "payment_failed", "grace_ends_at": "..."}`.
5. Enforce `|agent.now - server.now| < 300 seconds`. Outside that → 401 with `{"error": "clock_skew"}`.
6. Upsert the `agents` row: update `last_heartbeat_at`, `version`, `status='active'`, `hostname`, plus a trailing JSON column `last_metrics` with the reported metrics.
7. Verify `agent_fingerprint` in the token matches `agents.fingerprint`. If no row exists yet (first heartbeat after install), accept and store. If mismatch → 409 with `{"error": "fingerprint_mismatch"}` and do not rotate the token.
8. Check if any new config bundle delta exists (field: `agents.config_version` vs `organizations.config_version`). If so, include it in the response.
9. Call `license_issuer.issue_license(...)` to mint a fresh 24h token with:
   - `entitlements_snapshot` populated from the current `entitlements` row
   - `grace_until = now + 7 days` **only if** the previous token's `grace_until` was more than 6 days ago — otherwise preserve the previous value. This means: if the customer has been consistently online, grace window slides forward; if they've been offline for a while, the window from their last contact is preserved.
10. Return `200` with `{"license": "<jwt>", "config_bundle": {...} or null, "rotate_token": true}`.

Tests:
- `test_heartbeat_valid_token_returns_refreshed` — happy path.
- `test_heartbeat_expired_token_returns_401`
- `test_heartbeat_bad_signature_returns_401`
- `test_heartbeat_canceled_subscription_returns_403`
- `test_heartbeat_past_due_returns_402_with_grace_info`
- `test_heartbeat_clock_skew_5_minutes_accepted`
- `test_heartbeat_clock_skew_6_minutes_rejected`
- `test_heartbeat_first_heartbeat_stores_fingerprint`
- `test_heartbeat_fingerprint_mismatch_returns_409`
- `test_heartbeat_returns_config_bundle_when_version_changed`
- `test_heartbeat_refreshed_token_has_updated_entitlements_snapshot`
- `test_heartbeat_grace_until_slides_forward_after_contact`
- `test_heartbeat_updates_agents_last_heartbeat_at`

## Do not

- Do not generate new signing keys in production. `AGENT_SIGNING_KEY_PATH` must be set or the server refuses to start (when `ENV=prod`).
- Do not use symmetric (HS256) tokens. RS256 only; the algorithm field is verified on every call.
- Do not trust `agent.now` for grace calculations. Use server clock.
- Do not leak the reason for token rejection in more detail than necessary. "invalid_token" + a short `reason` is enough for debugging; don't include stack traces or JWT contents in the response.
- Do not include the customer's Stripe customer ID, email, or other PII in the JWT claims. It's decodable by anyone who sees the token.

## Hints and gotchas

- Use `python-jose[cryptography]` or `pyjwt[crypto]` — both work. Pick one and stick with it.
- Clock skew tolerance is a common source of silent bugs. Always include a `leeway=5*60` parameter on JWT decode.
- The `grace_until` slide rule is the subtle one. Read it three times. The goal: a continuously-online customer always has 7 days of grace ahead of them; an intermittently-offline customer preserves whatever window they had when contact was lost.
- `fingerprint_mismatch` is a security event — log it at WARN with the `agent_id` and trigger an alert if the same agent_id produces more than 3 in 24 hours.
- The signing key for dev can be generated with: `openssl genrsa -out dev_key.pem 2048 && openssl rsa -in dev_key.pem -pubout > dev_pub.pem`. Commit `dev_pub.pem` to the repo under `dev-keys/`, never commit `dev_key.pem`.

## Test command

```bash
pytest tests/test_heartbeat_endpoint.py -v
```

All 13 tests must pass.
