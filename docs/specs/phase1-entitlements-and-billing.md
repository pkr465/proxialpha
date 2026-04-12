# Phase 1 PRD — Entitlements & Billing

**Status:** Draft, ready for implementation
**Phase:** 1 of 6
**Owner:** Pavan
**Target duration:** Weeks 3–5 (3 weeks)
**Depends on:** Phase 0 (Clerk/WorkOS auth, Postgres provisioned, Stripe account), ADR-002, ADR-005
**Blocks:** Phase 2 (customer agent needs license issuance), Phase 3 (LLM gateway needs entitlement checks)

---

## 1. Problem Statement

ProxiAlpha has a working multi-provider trading framework but no way to charge for it, gate features by tier, or track usage. To launch a subscription business we need:

- A billing system that accepts payment, manages subscriptions, and enforces the four-tier pricing model.
- An entitlements system that gates API routes and agent actions based on the customer's current subscription.
- A usage metering pipeline that reports AI signal consumption to Stripe for metered overage billing.
- A customer-facing billing UI (Stripe Customer Portal is fine for v1) for self-service plan changes.

This phase delivers all four, end-to-end, for a single user signing up through the dashboard and getting a working Trader or Pro subscription.

## 2. Goals

1. A new user can sign up, pick a tier, pay with a card, and have their subscription reflected in our DB within 5 seconds.
2. Every gated API endpoint either allows the request and decrements the relevant quota, or rejects it with a 402 and a clear error message.
3. AI signal usage is metered with full idempotency — Stripe invoice lines match `usage_events` 1:1 across webhook replays, backfills, and clock drift.
4. A user can upgrade, downgrade, and cancel through the Stripe Customer Portal and see the change reflected in the dashboard within one webhook cycle.
5. Private-beta coupon codes work: `BETA100` gives 100% off for 90 days.

## 3. Non-Goals (explicit)

- No admin panel for manually adjusting entitlements. All changes flow from Stripe.
- No custom billing UI. Stripe Customer Portal only.
- No annual → monthly switching mid-cycle. Supported at renewal only.
- No in-app invoice generation. Stripe handles invoices, we link out.
- No multi-currency. USD only for v1.
- No tax handling beyond enabling Stripe Tax (it does what we need).
- No Team-tier seat management UI. Seat count is a Stripe subscription item, managed via Customer Portal.
- No refund UI. Refunds happen manually through the Stripe dashboard, propagated to us via webhook.

## 4. User Stories

### US-1.1: New retail user signs up for Trader tier
**As** a retail trader visiting the marketing site
**I want** to click "Start Trader" and pay $49 with a credit card
**So that** I get immediate access to the dashboard, paper trading, and 500 AI signals for the month

**Acceptance criteria:**
- Clicking "Start Trader" takes me to a Stripe Checkout session with the Trader monthly price preselected.
- On successful payment, I land back on `/dashboard` and see `Tier: Trader` in the header within 5 seconds.
- My `entitlements` row shows `signals.remaining = 500`, `backtests.remaining = 200`, `tickers.max = 25`.
- I receive a Stripe-issued receipt by email (Stripe handles this).

### US-1.2: User hits their AI signal quota
**As** a Trader-tier user who has used 499 of 500 AI signals
**I want** my next AI signal to succeed and my 502nd to be rejected with a clear message
**So that** I know when to upgrade or wait for the next billing cycle

**Acceptance criteria:**
- The 500th signal succeeds; `entitlements.remaining` → 0.
- The 501st signal returns `402 Payment Required` with body `{"error": "quota_exhausted", "feature": "signals", "upgrade_url": "https://..."}`.
- No usage event is recorded for the rejected call.
- Dashboard shows "500 / 500 signals used — upgrade to get more."

### US-1.3: Pro-tier user enables overage
**As** a Pro-tier user
**I want** to opt in to paid overage beyond my 5,000 bundled signals
**So that** my trading does not stop when I hit the cap

**Acceptance criteria:**
- A toggle in Billing → Overage enables metered overage at $0.025 per signal.
- Turning it on sets `entitlements.overage_enabled = true`.
- After quota is exhausted, signals continue to succeed; each one appends a row to `usage_events` with `billed = true`.
- The next daily meter job reports aggregated usage to Stripe as a metered usage record.
- The user's next invoice shows a line item "AI signal overage: 127 × $0.025 = $3.18."

### US-1.4: Subscription cancellation
**As** a Pro-tier user who cancels through the Stripe Customer Portal
**I want** my access to continue until the end of the current period, then end cleanly
**So that** I get what I paid for and no more

**Acceptance criteria:**
- Stripe emits `customer.subscription.updated` with `cancel_at_period_end = true`.
- Our DB marks the subscription as `status='active', cancel_at_period_end=true`. No immediate downgrade.
- Dashboard shows a banner: "Your Pro subscription ends on April 30. You will be moved to Free tier."
- On period end, Stripe emits `customer.subscription.deleted`. We downgrade to Free: `tier='free'`, entitlements reset to Free-tier values.

### US-1.5: Failed payment
**As** the system
**I want** to handle a failed payment retry gracefully
**So that** we don't churn a customer over a single declined card

**Acceptance criteria:**
- On `invoice.payment_failed`, we set `subscriptions.status='past_due'`, entitlements stay intact (they paid for this period).
- Dashboard shows a payment warning. User can update card via Customer Portal.
- Stripe retries per its Smart Retries schedule.
- On successful retry, `invoice.paid` → `status='active'`.
- On final failure (after all retries), Stripe emits `customer.subscription.deleted`. We downgrade to Free. Dashboard shows "Your subscription was cancelled due to payment failure."

### US-1.6: Private-beta coupon
**As** a private-beta user
**I want** to use the `BETA100` code to get Pro tier free for 90 days
**So that** I can try the product

**Acceptance criteria:**
- The Pricing page has a "Got a beta code?" field.
- Entering `BETA100` and clicking Subscribe takes me to Stripe Checkout with the coupon pre-applied.
- Invoice shows $0.00 for the first 3 months, $199 thereafter.
- My entitlements reflect Pro tier from day one, regardless of $0 invoice.

## 5. Data Model

See ADR-005 for the RLS pattern. All tenant-scoped tables must use it.

### 5.1 `organizations`

```sql
CREATE TABLE organizations (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name                text NOT NULL,
  stripe_customer_id  text UNIQUE,
  tier                text NOT NULL DEFAULT 'free'
                      CHECK (tier IN ('free', 'trader', 'pro', 'team')),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_orgs_stripe_customer ON organizations (stripe_customer_id);

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;

CREATE POLICY org_self_read ON organizations
  USING (id = current_setting('app.current_org_id', true)::uuid);
```

### 5.2 `users`

```sql
CREATE TABLE users (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email        text NOT NULL UNIQUE,
  org_id       uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  role         text NOT NULL DEFAULT 'member'
               CHECK (role IN ('owner', 'admin', 'member')),
  clerk_user_id text UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_users_org ON users (org_id);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;

CREATE POLICY user_org_read ON users
  USING (org_id = current_setting('app.current_org_id', true)::uuid);
```

### 5.3 `subscriptions`

```sql
CREATE TABLE subscriptions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id                 uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  stripe_subscription_id text UNIQUE NOT NULL,
  stripe_price_id        text NOT NULL,
  status                 text NOT NULL
                         CHECK (status IN ('trialing', 'active', 'past_due',
                                           'canceled', 'incomplete', 'incomplete_expired')),
  tier                   text NOT NULL
                         CHECK (tier IN ('trader', 'pro', 'team')),
  seats                  int NOT NULL DEFAULT 1,
  current_period_start   timestamptz NOT NULL,
  current_period_end     timestamptz NOT NULL,
  cancel_at_period_end   boolean NOT NULL DEFAULT false,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_subs_org ON subscriptions (org_id);
CREATE INDEX idx_subs_status ON subscriptions (status);

ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY sub_org_read ON subscriptions
  USING (org_id = current_setting('app.current_org_id', true)::uuid);
```

### 5.4 `entitlements`

```sql
CREATE TABLE entitlements (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  feature         text NOT NULL,
  period_start    timestamptz NOT NULL,
  period_end      timestamptz NOT NULL,
  included        bigint NOT NULL DEFAULT 0,
  remaining       bigint NOT NULL DEFAULT 0,
  overage_enabled boolean NOT NULL DEFAULT false,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, feature, period_start)
);

CREATE INDEX idx_ent_org_feature_period ON entitlements (org_id, feature, period_start DESC);

ALTER TABLE entitlements ENABLE ROW LEVEL SECURITY;
CREATE POLICY ent_org_read ON entitlements
  USING (org_id = current_setting('app.current_org_id', true)::uuid);
```

**Features stored in the `feature` column:**
- `signals` — AI signal generations
- `backtests` — backtest runs
- `tickers` — number of tickers allowed in scan config (max, not remaining)
- `strategy_slots` — number of strategies (max, not remaining)
- `live_trading` — boolean-as-bigint (0 = off, 1 = on)
- `live_perps` — boolean-as-bigint
- `custom_strategies` — boolean-as-bigint
- `api_access` — 0 = none, 1 = read, 2 = read/write

For boolean and max features, `remaining` equals `included`. The consumption path only decrements `signals` and `backtests`.

### 5.5 `usage_events`

```sql
CREATE TABLE usage_events (
  id                       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id                   uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  feature                  text NOT NULL,
  quantity                 bigint NOT NULL,
  cost_usd                 numeric(12, 6),
  billed                   boolean NOT NULL DEFAULT false,
  idempotency_key          text NOT NULL,
  stripe_usage_record_id   text,
  occurred_at              timestamptz NOT NULL DEFAULT now(),
  reported_at              timestamptz,
  UNIQUE (idempotency_key)
);

CREATE INDEX idx_usage_org_feature_time ON usage_events (org_id, feature, occurred_at DESC);
CREATE INDEX idx_usage_unreported ON usage_events (org_id, feature)
  WHERE reported_at IS NULL AND billed = true;

ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY usage_org_read ON usage_events
  USING (org_id = current_setting('app.current_org_id', true)::uuid);
```

### 5.6 `stripe_events` (not RLS, billing schema)

```sql
CREATE SCHEMA IF NOT EXISTS billing_raw;

CREATE TABLE billing_raw.stripe_events (
  id            text PRIMARY KEY,
  event_type    text NOT NULL,
  received_at   timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz,
  payload       jsonb NOT NULL,
  processing_error text
);

CREATE INDEX idx_stripe_events_unprocessed
  ON billing_raw.stripe_events (received_at)
  WHERE processed_at IS NULL;
```

## 6. Stripe Configuration

### Products and prices (create once, manually, record IDs in `.env.production`)

| Product | Price ID (env var) | Type | Amount | Interval |
|---|---|---|---|---|
| Trader | `STRIPE_PRICE_TRADER_MONTHLY` | recurring | $49.00 | month |
| Trader | `STRIPE_PRICE_TRADER_ANNUAL` | recurring | $468.00 | year |
| Pro | `STRIPE_PRICE_PRO_MONTHLY` | recurring | $199.00 | month |
| Pro | `STRIPE_PRICE_PRO_ANNUAL` | recurring | $1,908.00 | year |
| Team | `STRIPE_PRICE_TEAM_MONTHLY` | recurring | $799.00 | month |
| Team | `STRIPE_PRICE_TEAM_ANNUAL` | recurring | $7,668.00 | year |
| Team seats | `STRIPE_PRICE_TEAM_SEAT` | recurring, licensed | $99.00 | month |
| AI signal overage (Trader) | `STRIPE_PRICE_SIGNAL_OVERAGE_TRADER` | metered, sum | $0.04 / unit | month |
| AI signal overage (Pro) | `STRIPE_PRICE_SIGNAL_OVERAGE_PRO` | metered, sum | $0.025 / unit | month |
| AI signal overage (Team) | `STRIPE_PRICE_SIGNAL_OVERAGE_TEAM` | metered, sum | $0.015 / unit | month |
| Signals 1k pack | `STRIPE_PRICE_SIGNALS_1K` | one-time | $25.00 | — |
| Backtest pack 10k | `STRIPE_PRICE_BACKTESTS_10K` | one-time | $49.00 | — |

### Coupons

| Code | Discount | Duration |
|---|---|---|
| `BETA100` | 100% off | 3 months (then normal pricing) |
| `ANNUAL20` | 20% off | forever (annual only) |

### Webhook events to subscribe to

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.paid`
- `invoice.payment_failed`

## 7. API Endpoints

### 7.1 `POST /api/billing/checkout`

Creates a Stripe Checkout session and returns the URL.

**Request:**
```json
{
  "price_id": "price_1P...",
  "coupon": "BETA100",
  "success_url": "https://app.proxialpha.com/billing/success",
  "cancel_url": "https://app.proxialpha.com/pricing"
}
```

**Response 200:**
```json
{
  "checkout_url": "https://checkout.stripe.com/c/pay/cs_..."
}
```

**Errors:**
- `400` invalid price_id or coupon
- `401` not authenticated
- `409` org already has an active subscription — point them at the Customer Portal instead

### 7.2 `POST /api/billing/portal`

Creates a Stripe Customer Portal session.

**Request:** empty
**Response 200:** `{"portal_url": "https://billing.stripe.com/..."}`
**Errors:** `404` if org has no `stripe_customer_id`

### 7.3 `POST /api/billing/webhook` (Stripe → us)

Receives all Stripe webhook events. Validates signature using `STRIPE_WEBHOOK_SECRET`.

**Flow:**
1. Verify signature; reject `400` on failure.
2. `INSERT INTO billing_raw.stripe_events (id, event_type, payload) VALUES (...) ON CONFLICT (id) DO NOTHING RETURNING id`.
3. If RETURNING returned no row, the event is a replay — return `200` immediately.
4. Otherwise, dispatch to the handler for the event type.
5. On success: `UPDATE billing_raw.stripe_events SET processed_at = now() WHERE id = $1`.
6. On failure: record error, return `500` so Stripe retries.

Return `200` in <3 seconds (Stripe timeout).

### 7.4 `GET /api/entitlements`

Returns the current org's entitlements for the current period.

**Response 200:**
```json
{
  "tier": "pro",
  "period_start": "2026-04-01T00:00:00Z",
  "period_end": "2026-05-01T00:00:00Z",
  "features": {
    "signals": {"included": 5000, "remaining": 4387, "overage_enabled": true},
    "backtests": {"included": 2000, "remaining": 1998},
    "tickers": {"max": 200},
    "strategy_slots": {"max": 20},
    "live_trading": true,
    "live_perps": true,
    "custom_strategies": true,
    "api_access": "read_write"
  }
}
```

### 7.5 Entitlement decorator (used by other routes)

```python
from fastapi import HTTPException

def requires_entitlement(feature: str, consume: int = 0):
    def decorator(fn):
        async def wrapper(*args, request: Request, **kwargs):
            org_id = request.state.org_id
            allowed = await entitlements.try_consume(org_id, feature, consume)
            if not allowed:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "quota_exhausted",
                        "feature": feature,
                        "upgrade_url": f"{settings.app_url}/pricing",
                    },
                )
            return await fn(*args, request=request, **kwargs)
        return wrapper
    return decorator
```

`entitlements.try_consume` is an atomic UPDATE:

```sql
UPDATE entitlements
SET remaining = remaining - $consume,
    updated_at = now()
WHERE org_id = $org_id
  AND feature = $feature
  AND period_end > now()
  AND (remaining >= $consume OR overage_enabled = true)
RETURNING remaining, overage_enabled;
```

If `remaining` goes negative and `overage_enabled = true`, we record a billed `usage_events` row for the overage.

## 8. Webhook Handler Logic

See `docs/prompts/02-stripe-webhook-handler.md` for the full implementation spec. Summary of what each event type does:

| Event | Effect |
|---|---|
| `checkout.session.completed` | Create/link `stripe_customer_id` on org, set tier from price, insert subscription row, seed entitlements |
| `customer.subscription.created` | Idempotent form of the above — if session.completed already fired, do nothing |
| `customer.subscription.updated` | Update subscription row (status, seats, cancel_at_period_end), reseed entitlements if tier changed |
| `customer.subscription.deleted` | Downgrade org to Free, zero entitlements to Free values |
| `invoice.paid` | For metered invoices, mark the corresponding `usage_events` rows as reported. Reset entitlements to tier defaults for the new period. |
| `invoice.payment_failed` | Set subscription status to `past_due`. Do not touch entitlements — user paid for this period. |

## 9. Metering Job

A cron job (`jobs/meter_usage.py`) runs hourly:

```
SELECT org_id, feature, sum(quantity) AS qty
FROM usage_events
WHERE billed = true
  AND reported_at IS NULL
  AND occurred_at < now() - interval '5 minutes'  -- avoid racing inserts
GROUP BY org_id, feature
```

For each row, find the relevant subscription item ID in Stripe (cached in `subscriptions.metered_item_ids` as JSONB) and POST a usage record with an idempotency key of `{org_id}:{feature}:{hour_bucket}`. On success, mark all contributing `usage_events` rows with `reported_at = now()` and `stripe_usage_record_id`.

If the job fails mid-batch, the next run picks up the un-reported rows naturally — Stripe dedupes on the idempotency key.

## 10. Tier Definitions (source of truth in `config/tiers.yaml`)

```yaml
tiers:
  free:
    signals_included: 20
    backtests_included: 10
    tickers_max: 5
    strategy_slots_max: 2
    live_trading: false
    live_perps: false
    custom_strategies: false
    api_access: none
    diary_retention_days: 7
  trader:
    signals_included: 500
    backtests_included: 200
    tickers_max: 25
    strategy_slots_max: 5
    live_trading: true
    live_perps: false
    custom_strategies: false
    api_access: read
    diary_retention_days: 90
    stripe_prices:
      monthly: STRIPE_PRICE_TRADER_MONTHLY
      annual: STRIPE_PRICE_TRADER_ANNUAL
    overage_price: STRIPE_PRICE_SIGNAL_OVERAGE_TRADER
  pro:
    signals_included: 5000
    backtests_included: 2000
    tickers_max: 200
    strategy_slots_max: 20
    live_trading: true
    live_perps: true
    custom_strategies: true
    api_access: read_write
    diary_retention_days: 365
    stripe_prices:
      monthly: STRIPE_PRICE_PRO_MONTHLY
      annual: STRIPE_PRICE_PRO_ANNUAL
    overage_price: STRIPE_PRICE_SIGNAL_OVERAGE_PRO
  team:
    signals_included: 25000
    backtests_included: 100000
    tickers_max: 10000
    strategy_slots_max: 500
    live_trading: true
    live_perps: true
    custom_strategies: true
    api_access: full
    diary_retention_days: 2555  # 7 years
    seats_included: 5
    stripe_prices:
      monthly: STRIPE_PRICE_TEAM_MONTHLY
      annual: STRIPE_PRICE_TEAM_ANNUAL
    overage_price: STRIPE_PRICE_SIGNAL_OVERAGE_TEAM
    seat_price: STRIPE_PRICE_TEAM_SEAT
```

The `seed_entitlements(org_id, tier)` function reads this file and inserts the right rows.

## 11. Test Plan

### Unit tests
- `test_tiers_yaml_parses_cleanly`
- `test_seed_entitlements_pro_tier_correct_values`
- `test_try_consume_decrements_atomically`
- `test_try_consume_rejects_when_exhausted_and_no_overage`
- `test_try_consume_allows_when_exhausted_and_overage_enabled`

### Integration tests (with test Postgres and Stripe test mode)
- `test_checkout_flow_creates_subscription_and_entitlements`
- `test_webhook_subscription_created_is_idempotent` (fire the same event 5 times, assert single effect)
- `test_webhook_out_of_order_delivery` (subscription_updated before subscription_created — dedupe correctly)
- `test_upgrade_trader_to_pro_reseeds_entitlements`
- `test_cancel_at_period_end_keeps_access_until_period_ends`
- `test_payment_failed_to_paid_recovers_cleanly`
- `test_metering_job_posts_idempotent_usage_record`
- `test_tenant_isolation_rls_blocks_cross_org_reads` (from ADR-005)

### Smoke test add-on
- `scripts/test_billing.py` — runs against a test Stripe account, creates a customer, subscribes to Trader, fires a synthetic webhook, asserts entitlements appear. Runs as step 9 of `smoke.sh` if `STRIPE_SECRET_KEY` is set, skips otherwise.

## 12. Security Review Checklist

- [ ] Webhook signature verification uses `stripe.Webhook.construct_event` with `STRIPE_WEBHOOK_SECRET`. Never accept unsigned webhooks in production.
- [ ] `STRIPE_SECRET_KEY` is only in the control plane env, never in the agent, never in client code.
- [ ] No price IDs are user-input. Always validated against `tiers.yaml`.
- [ ] Customer Portal sessions are scoped to the authenticated user's org_id — impossible to open another org's portal.
- [ ] RLS on every table per ADR-005. CI gate verifies.
- [ ] Stripe Tax is enabled (Dashboard → Tax → Activate).
- [ ] PCI scope: we never touch card data. Verify by absence of any card fields in our schema.

## 13. Rollout Plan

1. Deploy schema migrations to staging.
2. Create Stripe products and prices in **test mode**.
3. Point staging `.env` at test Stripe keys.
4. Run `scripts/test_billing.py` in staging; must pass.
5. Invite 5 friends to sign up in staging with Stripe test cards; collect feedback for 1 week.
6. Create Stripe products and prices in **live mode**.
7. Flip production feature flag `billing_enabled = true`.
8. Cohort-1 opens to the private beta list with `BETA100` coupon.

## 14. Open Questions

- **Annual to annual upgrade mid-cycle:** Stripe handles proration; UX copy TBD in Phase 4.
- **Team-tier seat ownership:** When a seat is removed, the user loses dashboard access but their historical data remains tied to the org. Confirm legal stance before Phase 5.
- **Chargeback handling:** Out of scope for Phase 1. We handle disputes manually via Stripe dashboard for the first year.
