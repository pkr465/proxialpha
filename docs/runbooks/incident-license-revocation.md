# Incident runbook: revoke a compromised agent license

This runbook covers the "we just discovered a customer agent token
in the wrong hands" scenario. The goal is to lock that token (and,
if needed, the entire agent identity) out of the heartbeat endpoint
WITHIN ONE HEARTBEAT CYCLE — without rotating the signing key, which
takes hours of overlap-window work (see `signing-key-rotation.md`).

## How revocation works

The `revoked_jti` table is checked at the top of every
`/agent/heartbeat` request, BEFORE the JTI replay set. Two row
shapes are supported:

- **Per-token** — exact match on the JWT's `jti`.
  Use this to kill ONE token (e.g. a token an attacker exfiltrated
  but the underlying agent identity is still trusted).
- **Per-agent** — `jti = NULL` (or the sentinel `'*'`) and a
  matching `agent_id`. Use this to kill an entire agent identity.
  Every subsequent heartbeat from any token bound to that agent_id
  fails until you delete the row.

The row is checked with a single indexed query on
`(org_id, jti)` plus `(org_id, agent_id)`. There is no caching
layer — revocation takes effect on the next request.

## Decision tree

```
Did the attacker get ONE token?
  └── per-token: insert by jti
Did the attacker get the agent's local state?
  └── per-agent: insert by agent_id
Is the customer's machine itself compromised?
  └── per-agent revoke + force re-enrollment
       (admin re-issues install token, agent runs enroll flow)
```

## Procedure: per-token revocation

1. Get the `jti` from your alert source (heartbeat audit log,
   security alert, etc.).
2. Insert the row directly into Postgres:
   ```
   INSERT INTO revoked_jti (org_id, jti, agent_id, reason, revoked_at)
   VALUES ('00000000-0000-0000-0000-000000000000', 'JTI_HERE',
           NULL, 'leaked-2026-04-11', now());
   ```
3. Verify the next heartbeat for that token returns 403:
   ```
   curl -i -H "Authorization: Bearer THE_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"now":"2026-04-11T12:00:00Z","metrics":{},"topology":"C"}' \
        https://api.proxiant.io/agent/heartbeat
   # → 403 license_revoked
   ```
4. Notify the customer and recommend they rotate the local agent
   install via the dashboard.

## Procedure: per-agent revocation

1. Identify the `agent_id` from the dashboard or the `agents` table.
2. Insert the wildcard row:
   ```
   INSERT INTO revoked_jti (org_id, jti, agent_id, reason, revoked_at)
   VALUES ('ORG_ID', NULL, 'AGENT_ID', 'compromised-host', now());
   ```
3. Confirm: every heartbeat from that agent now fails. The customer
   needs to issue a new install token from the dashboard and re-run
   the agent enroll flow.

## Procedure: lift a revocation

If you decide a revocation was a false positive:
```
DELETE FROM revoked_jti
 WHERE org_id = 'ORG_ID' AND (jti = 'JTI' OR agent_id = 'AGENT_ID');
```
Effect is immediate.

## What revocation does NOT do

- It does NOT invalidate the signing key. Other agents are unaffected.
- It does NOT re-issue licenses for unrelated tokens.
- It does NOT propagate to the customer's local agent process —
  the local process keeps trying until its current license expires
  (24h max) or it gets a 403 from heartbeat. If you need an
  immediate hard kill, also revoke the agent's IP at the edge.

## See also

- `docs/runbooks/signing-key-rotation.md` — full key rotation, used
  when the signing key itself is compromised.
- `docs/runbooks/control-plane-deploy.md` — how the API is deployed.
