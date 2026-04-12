# Task 02 — Stripe Webhook Handler

**Phase:** 1 (Entitlements + Billing)
**Est. effort:** 6–8 hours
**Prerequisites:** Task 01 complete (schema exists); Stripe account in test mode with products created per Phase 1 PRD §6.

## Objective

Implement a fully idempotent Stripe webhook handler at `POST /api/billing/webhook` that receives subscription and invoice events and mutates the local `organizations`, `subscriptions`, and `entitlements` tables accordingly.

## Context

- ADR-002 commits us to Stripe as the source of truth for billing and a local `entitlements` table as the hot-path cache.
- Full spec is in `docs/specs/phase1-entitlements-and-billing.md` §7.3 and §8.
- Tier definitions live in `config/tiers.yaml` (create this file if it doesn't exist; copy the YAML block verbatim from Phase 1 PRD §10).
- Idempotency model is in ADR-002 "Idempotency strategy" and Phase 1 PRD §7.3 — the `billing_raw.stripe_events` table is the dedupe key.

## Exact files to create or modify

1. `config/tiers.yaml` — **new file**. Copy from Phase 1 PRD §10 verbatim.
2. `api/billing/webhook.py` — **new file**. FastAPI router with `POST /webhook`.
3. `api/billing/handlers.py` — **new file**. One function per event type: `handle_checkout_completed`, `handle_subscription_updated`, `handle_subscription_deleted`, `handle_invoice_paid`, `handle_invoice_payment_failed`.
4. `api/billing/entitlement_seeder.py` — **new file**. `seed_entitlements(org_id, tier, period_start, period_end, session)` — loads `tiers.yaml`, upserts one row per feature into `entitlements`.
5. `api/server.py` — **modify**. Register the billing router: `app.include_router(billing_router, prefix="/api/billing")`.
6. `core/settings.py` — **modify**. Add `stripe_secret_key: str` and `stripe_webhook_secret: str` pydantic-settings fields.
7. `tests/test_webhook_handler.py` — **new file**. Test cases listed below.

## Acceptance criteria

Functional:
- `POST /api/billing/webhook` with a valid Stripe signature and a new event ID inserts a row into `billing_raw.stripe_events` and dispatches to the right handler.
- The same event ID POSTed twice inserts only once and the handler runs only once. The second request returns `200` without touching the DB beyond the initial dedupe SELECT.
- `checkout.session.completed` with mode=`subscription`:
  - Looks up `organizations.id` by `client_reference_id` (set to `org_id` at Checkout creation time — enforced in Task 03).
  - Sets `organizations.stripe_customer_id` if unset.
  - Upserts a row in `subscriptions`.
  - Calls `seed_entitlements(...)` with the tier inferred from the price ID.
- `customer.subscription.updated`:
  - Updates `subscriptions.status`, `current_period_end`, `cancel_at_period_end`, `seats`.
  - If the tier changed (price ID changed to a different tier), reseed entitlements.
- `customer.subscription.deleted`:
  - Sets org's tier to `free`.
  - Sets `subscriptions.status='canceled'`.
  - Calls `seed_entitlements(org_id, 'free', ...)` — Free tier values become the new entitlements.
- `invoice.paid`:
  - For recurring invoices (subscription renewal): reseed entitlements for the new period.
  - For metered usage invoices: mark the contributing `usage_events.reported_at` (this crosses into Task 05 — just leave a TODO comment here).
- `invoice.payment_failed`:
  - Sets `subscriptions.status='past_due'`.
  - Does **not** touch entitlements.

Idempotency and safety:
- Handler runs inside a single DB transaction per event. If any step fails, the whole transaction rolls back and `stripe_events.processed_at` stays NULL.
- Webhook endpoint returns `200` in <3 seconds for all success cases (Stripe timeout is 30s but we want headroom).
- Signature verification failure returns `400` and does **not** insert into `stripe_events`.
- Handler does not trust the event payload for price-to-tier mapping — it verifies each price ID against `tiers.yaml`.

Tests:
- `test_signature_verification_rejects_invalid` — bad signature → 400, no DB writes.
- `test_checkout_completed_seeds_entitlements` — fire the event, assert `entitlements` has the right rows for Trader tier.
- `test_duplicate_event_is_idempotent` — fire the same event twice, assert exactly one row in each table.
- `test_subscription_updated_reseeds_on_tier_change` — Trader → Pro, assert signals quota goes from 500 to 5000.
- `test_subscription_deleted_downgrades_to_free` — assert tier='free' and signals quota = 20.
- `test_invoice_payment_failed_keeps_entitlements` — entitlements unchanged, subscription status='past_due'.
- `test_webhook_returns_200_under_3_seconds` — timing assertion.

## Do not

- Do not call Stripe API from this handler. It is a pure consumer — we only read from the webhook payload, never fetch.
- Do not implement the Checkout endpoint here. That is Task 03.
- Do not implement metering. Task 05.
- Do not add a queue or async task runner. The handler must complete synchronously inside the HTTP request so Stripe sees a correct status code.
- Do not log the full webhook payload at INFO level — it contains customer emails and amounts. Log event ID, type, and outcome only. Full payload goes to DEBUG.

## Hints and gotchas

- Use `stripe.Webhook.construct_event(payload, sig_header, webhook_secret)`. It raises `ValueError` on malformed and `stripe.error.SignatureVerificationError` on bad signature. Catch both.
- `client_reference_id` is a string field you set when creating the Checkout session. It's the cleanest way to correlate a session back to an org. **Enforce** that it's set in Task 03 so this handler can rely on it.
- Stripe can deliver events out of order. `subscription.created` and `checkout.session.completed` both carry enough info to set up the org; whichever lands first wins, and the second is effectively a no-op.
- For price-to-tier mapping, build a dict once at module load: `{env["STRIPE_PRICE_TRADER_MONTHLY"]: "trader", ...}`. Test helper: expose it so tests can patch.
- The `tier` stored on `organizations.tier` is the *effective* tier right now. For display purposes in the dashboard, always read from here, not from the latest subscription (because `past_due` is a real state).

## Test command

```bash
pytest tests/test_webhook_handler.py -v
```

All 7 tests must pass. Also run:

```bash
pytest tests/test_schema.py tests/test_webhook_handler.py -v
```

To confirm Task 01 still passes.
