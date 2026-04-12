# Task 04 — Entitlement Decorator and Atomic Consume

**Phase:** 1 (Entitlements + Billing)
**Est. effort:** 3–4 hours
**Prerequisites:** Tasks 01–03.

## Objective

Implement `requires_entitlement(feature, consume)` as a FastAPI decorator and the underlying atomic `try_consume` function, plus the `GET /api/entitlements` read endpoint. This is the machinery that actually gates paid features.

## Context

- Full spec in `docs/specs/phase1-entitlements-and-billing.md` §7.4 and §7.5.
- User stories US-1.2 and US-1.3 describe the quota-exhausted and overage flows.
- The decorator must be usable as `@requires_entitlement("signals", consume=1)` on any FastAPI route.

## Exact files to create or modify

1. `core/entitlements.py` — **new file**. Contains `try_consume`, `peek`, and the `requires_entitlement` decorator.
2. `api/billing/read.py` — **new file**. `GET /api/entitlements` endpoint.
3. `api/server.py` — **modify**. Register the read route.
4. `tests/test_entitlements.py` — **new file**.
5. Apply the decorator to at least one existing route as a sentinel — suggest: `POST /api/ai/signal` (or any existing AI-signal-generating route). Add it behind a feature flag `ENTITLEMENTS_ENABLED` so it can be rolled out gradually.

## Acceptance criteria

`try_consume(org_id, feature, qty)`:
- Runs as a single atomic UPDATE:
  ```sql
  UPDATE entitlements
  SET remaining = remaining - $qty, updated_at = now()
  WHERE org_id = $org_id
    AND feature = $feature
    AND period_end > now()
    AND (remaining >= $qty OR overage_enabled = true)
  RETURNING remaining, overage_enabled, id;
  ```
- Returns a dataclass `ConsumeResult(allowed: bool, remaining: int, is_overage: bool)`.
- If the UPDATE returns no rows: `allowed=False, remaining=0`.
- If the UPDATE returns a row with `remaining >= 0`: `allowed=True, is_overage=False`.
- If the UPDATE returns a row with `remaining < 0`: `allowed=True, is_overage=True`, and inserts a `usage_events` row with `billed=true, quantity=$qty, cost_usd` computed from `config/tiers.yaml` overage price. This part is where Task 05 will take over for reporting to Stripe, but the row insert happens here.
- Takes an optional `idempotency_key` arg; if provided, the `usage_events` row uses it; if not, a random UUID is generated.

`peek(org_id, feature)` — read-only version returning current state.

`requires_entitlement("signals", consume=1)` decorator:
- Reads `org_id` from `request.state.org_id`.
- Calls `try_consume`.
- On `allowed=False`, raises `HTTPException(status_code=402, detail={...})` per §7.5.
- On `allowed=True`, passes through to the route.
- Attaches `X-Entitlement-Remaining` response header with the new remaining count.

`GET /api/entitlements`:
- Returns the response body shape from §7.4 exactly.
- Boolean features (`live_trading`, `custom_strategies`) are returned as JSON booleans, not integers.
- `api_access` is mapped: 0=none, 1=read, 2=read_write, 3=full.

Tests:
- `test_try_consume_decrements_atomically` — start with remaining=100, consume 10, assert 90.
- `test_try_consume_blocks_when_exhausted_without_overage` — remaining=5, consume 10, assert allowed=False.
- `test_try_consume_allows_overage_when_enabled` — remaining=5, overage_enabled=true, consume 10, assert allowed=True, is_overage=True, remaining=-5.
- `test_try_consume_logs_usage_event_on_overage` — assert exactly one row in `usage_events` with billed=true.
- `test_try_consume_concurrent_does_not_oversell` — spawn 20 threads each consuming 1 against remaining=10; exactly 10 succeed.
- `test_peek_is_read_only` — peek 100 times, assert remaining is unchanged.
- `test_decorator_returns_402_on_exhaustion` — integration test hitting a gated route.
- `test_decorator_sets_response_header` — response has `X-Entitlement-Remaining: N`.
- `test_get_entitlements_shape_matches_spec` — returns the exact JSON shape from §7.4.

## Do not

- Do not hold a transaction open across the decorator. The UPDATE is atomic on its own; don't wrap it in a longer-lived transaction.
- Do not cache entitlement state in memory. The decorator must read the DB on every call. Caching is for a later optimization pass.
- Do not decrement `remaining` below `-overage_soft_cap`. For Phase 1, there's no soft cap — unlimited overage when enabled. Hard cap is a Phase 3 feature.
- Do not use SQLAlchemy's ORM for the atomic UPDATE. Use raw SQL via `session.execute(text(...))`. The ORM's UPDATE...RETURNING support is fiddly and we want the behavior to be unambiguous.

## Hints and gotchas

- The concurrency test is the most important one. If you use Python threads, each must open its own session from the engine and the test should assert the total of `remaining` reads == starting value. This catches any race where the UPDATE isn't actually atomic (e.g., read-then-update pattern).
- `period_end > now()` in the WHERE clause is load-bearing: it prevents consuming from last month's row when the new period hasn't been seeded yet. If it doesn't find a row for the current period, that's a seeding bug — fail loudly.
- The overage cost in `usage_events.cost_usd` is informational in Phase 1 — Stripe holds the actual price. We store it so support can answer "why was I charged $X?" without talking to Stripe.
- `X-Entitlement-Remaining` header is a convenience for the UI. Don't let users depend on it from client code — it's purely diagnostic.

## Test command

```bash
pytest tests/test_entitlements.py -v
```

All 9 tests must pass. The concurrent test must run at least 3 times without flakes — if it flakes, the atomicity is wrong.
