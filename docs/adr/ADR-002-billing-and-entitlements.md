# ADR-002: Stripe Billing + Self-Hosted Entitlements

**Status:** Accepted
**Date:** 2026-04-11
**Deciders:** Pavan
**Depends on:** ADR-001 (hybrid topology)

---

## Context

ProxiAlpha needs to sell four tiers (Free, Trader $49, Pro $199, Team $799 + seats), with annual and monthly billing, bundled quotas (backtests, AI signals, tickers, strategy slots), metered overage (AI signals beyond quota), add-ons (strategy slots, backtest compute packs, data feeds), and coupon support for private-beta pricing.

The system must:
- Reflect subscription state changes (upgrade, downgrade, cancellation, failed payment, reactivation) inside seconds, because the customer agent consults entitlements on every risk-gated action.
- Survive webhook replay, out-of-order delivery, and transient downtime without double-counting or losing events.
- Gate API routes and agent actions on entitlements cheaply — in-process, with no network call on the hot path.
- Support BYO-LLM (where the user supplies their own Anthropic/OpenAI/Ollama key) as a first-class path that bypasses metered billing.
- Be auditable — every state change must be traceable to a Stripe event ID.

## Decision

**We will use Stripe Billing as the subscription and payment system of record, and maintain a local `entitlements` table in our Postgres database as the hot-path source of truth for the running system.**

- Stripe holds: customers, subscriptions, products, prices, invoices, metered usage, coupons, tax, and dunning.
- Postgres holds: `organizations`, `users`, `entitlements`, `usage_events`, `agents`, and a `stripe_events` table for webhook idempotency.
- Stripe webhooks are the only writer to entitlement rows — no admin panel can directly mutate them. This means Stripe event replay is always the correct recovery path.
- Entitlements are consulted on every gated action via a synchronous in-process read (indexed on `org_id`), with a write only on consumption (atomic `UPDATE ... RETURNING`).
- Metered usage is recorded as an append-only row in `usage_events`, aggregated by a daily job, and posted to Stripe as metered usage records keyed by `(subscription_item_id, timestamp_bucket)` for idempotency.

## Options Considered

### Option 1 (Chosen): Stripe + local entitlements table

- **Pros:**
  - Stripe handles the hard parts: SCA, tax, dunning, retries, proration.
  - Local entitlements are fast (single-digit ms), no cross-network call on the hot path.
  - Single writer (webhook handler) means idempotency is containable.
  - Works offline — agent caches its entitlements inside the license token (ADR-003), so agent can operate through a brief control-plane outage.
- **Cons:**
  - Two sources of truth that can drift if webhooks are lost. Mitigation: nightly reconciliation job that pulls Stripe subscriptions and diffs against local entitlements.
  - Requires webhook idempotency discipline.

### Option 2: Stripe Entitlements (the new Stripe-managed product)

- **Pros:** Fewer tables to maintain; Stripe holds the entitlement state itself.
- **Cons:**
  - Forces a network call on the gated hot path unless we re-cache locally — at which point we're back to Option 1 with extra latency.
  - The product is new (2024); some features (complex quotas, per-period consumption) are still thin.
  - Lock-in: harder to migrate off Stripe later.
- **Verdict:** Revisit in 18 months. Not worth the coupling today.

### Option 3: Build our own billing system

- **Pros:** Full control.
- **Cons:** SCA, VAT MOSS, dunning, chargebacks, proration, mid-cycle upgrades, failed-payment retries. Every one of these is a multi-week project staffed by someone who has done it before. You have not done it before and neither have I.
- **Verdict:** Rejected. This is the single most expensive mistake a new SaaS can make.

### Option 4: Lemon Squeezy or Paddle (Merchant-of-Record)

- **Pros:** They handle global tax compliance including VAT MOSS as the merchant of record; simpler accounting.
- **Cons:**
  - Higher take rate (5%+ vs Stripe's ~3%).
  - Weaker usage-billing primitives than Stripe Metered.
  - Smaller ecosystem of integrations (Zapier, Segment, analytics tools).
- **Verdict:** Reconsider only if selling internationally at scale becomes a tax headache. For US launch, Stripe is faster.

## Consequences

### Positive

- Launch-ready subscriptions in ~2 weeks of focused work instead of ~2 months.
- Stripe tax + VAT MOSS handled by Stripe Tax (a single toggle).
- A full audit trail lives in Stripe independently of our database, which is itself a backup for the billing state.
- Coupons, trial periods, and private-beta pricing work out of the box.

### Negative

- We must write idempotent webhook handlers. This is the single highest-risk piece of Phase 1 code — see `docs/prompts/02-stripe-webhook-handler.md` for the implementation spec.
- Stripe is a vendor dependency. If Stripe goes down, new signups break, but existing subscribers continue to work because entitlements are cached locally.
- Refunds and manual adjustments must flow through Stripe first, then propagate to us via webhook. No local override UI in v1.

### Schema summary (detailed in Phase 1 PRD)

```
organizations (id, name, stripe_customer_id, tier, created_at)
users (id, email, org_id, role, created_at)
subscriptions (id, org_id, stripe_subscription_id, status, current_period_end, tier, seats)
entitlements (org_id, feature, period_start, period_end, included, remaining, overage_enabled, UNIQUE (org_id, feature, period_start))
usage_events (id, org_id, feature, quantity, cost_usd, idempotency_key, stripe_usage_record_id NULL, occurred_at)
stripe_events (id PRIMARY KEY, event_type, received_at, processed_at NULL)
agents (id, org_id, hostname, topology, last_heartbeat_at, version, status)
```

### Idempotency strategy

- **Webhooks:** `INSERT ... ON CONFLICT (id) DO NOTHING RETURNING id` on `stripe_events`. If the insert returns a row, process the event; otherwise, it's a replay — acknowledge and return 200 without doing work.
- **Usage records:** `idempotency_key` is a deterministic hash of `(org_id, feature, minute_bucket, request_id)`. Upstream consumers can safely retry.
- **Stripe metered push:** Key on the bucket timestamp + subscription item ID; Stripe dedupes on the server side.

## Open Questions

1. **Annual prepay and mid-cycle upgrades:** Stripe handles proration, but the UX for "I'm on Trader annual and want to upgrade to Pro" needs a design pass. Deferred to Phase 4.
2. **Refund policy:** Full refund within 14 days, pro-rata thereafter. Must be written into ToS and mirrored in the Stripe dashboard settings.
3. **Seat billing for Team tier:** Handled as a Stripe "licensed" quantity item. Seat changes are immediate and prorated — confirmed in Stripe docs.
