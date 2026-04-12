# Task 03 — Checkout and Customer Portal Endpoints

**Phase:** 1 (Entitlements + Billing)
**Est. effort:** 3–4 hours
**Prerequisites:** Task 01 (schema), Task 02 (webhook handler); Stripe test-mode account.

## Objective

Implement two public API endpoints:
- `POST /api/billing/checkout` — creates a Stripe Checkout session for the signed-in user's org.
- `POST /api/billing/portal` — creates a Stripe Customer Portal session for plan management.

These are the two routes the React dashboard calls to move users in and out of paid tiers.

## Context

- Full spec in `docs/specs/phase1-entitlements-and-billing.md` §7.1 and §7.2.
- User stories US-1.1 and US-1.6 in the same file.
- These endpoints require authenticated users; assume `request.state.user` and `request.state.org_id` are set by the auth middleware (not yet implemented — stub it for now; Task 04 wires up real Clerk/WorkOS auth).

## Exact files to create or modify

1. `api/billing/endpoints.py` — **new file**. The two POST routes.
2. `api/billing/schemas.py` — **new file**. Pydantic request/response models.
3. `api/middleware/auth_stub.py` — **new file**. Temporary middleware that reads `X-Stub-User-Email` and `X-Stub-Org-Id` headers in non-production and sets `request.state`. Clearly labeled as stub with a `TODO(phase1-task4)` comment.
4. `api/server.py` — **modify**. Install the stub middleware before the billing router.
5. `tests/test_billing_endpoints.py` — **new file**.

## Acceptance criteria

`POST /api/billing/checkout`:
- Accepts `{"price_id": "...", "coupon": "BETA100" (optional), "success_url": "...", "cancel_url": "..."}`.
- Validates `price_id` against the set of price IDs in `config/tiers.yaml` (via env). Unknown price → 400.
- Validates `coupon` if provided (non-empty string, <=32 chars). Invalid Stripe coupons surface as 400 from the Stripe SDK error.
- If the org already has an active subscription (`subscriptions.status IN ('active', 'trialing', 'past_due')`), returns 409 with `{"error": "active_subscription_exists", "portal_url": "..."}` where `portal_url` is generated via the portal path for convenience.
- Otherwise, calls `stripe.checkout.Session.create(...)` with:
  - `mode="subscription"`
  - `line_items=[{"price": price_id, "quantity": 1}]`
  - `success_url`, `cancel_url` from request
  - `client_reference_id=str(request.state.org_id)` **← load-bearing, don't skip**
  - `customer=org.stripe_customer_id` if it exists, else `customer_email=user.email`
  - `allow_promotion_codes=true`
  - `discounts=[{"coupon": coupon}]` if coupon is provided
- Returns `{"checkout_url": session.url}` with status 200.

`POST /api/billing/portal`:
- Looks up `organizations.stripe_customer_id` for the current org. Missing → 404 with `{"error": "no_customer"}`.
- Calls `stripe.billing_portal.Session.create(customer=stripe_customer_id, return_url=settings.app_url + "/dashboard")`.
- Returns `{"portal_url": session.url}`.

Tests:
- `test_checkout_creates_session_with_valid_price` — mock the Stripe SDK, assert the right args.
- `test_checkout_rejects_unknown_price_id` — returns 400.
- `test_checkout_rejects_if_active_subscription_exists` — fixture inserts a subscription row, expect 409 with portal URL in body.
- `test_checkout_sets_client_reference_id` — mock Stripe, assert the argument equals the org's UUID as a string.
- `test_portal_returns_404_without_stripe_customer` — expect 404.
- `test_portal_returns_url_when_customer_exists` — happy path.

## Do not

- Do not create the Stripe products or prices from code. They're created manually once; the env carries their IDs. The endpoint reads env via `core/settings.py`.
- Do not write a full auth system. The stub middleware is good enough for this task; Clerk/WorkOS integration is Task 04.
- Do not cache Stripe sessions. Checkout sessions are short-lived and cheap to create; caching introduces correctness risk for no real savings.
- Do not allow the user to specify an arbitrary `customer_id` in the request. Always look it up from the authenticated org.

## Hints and gotchas

- The `client_reference_id` is the **single most important line in this task**. If you don't set it, the webhook handler in Task 02 can't correlate the Checkout session back to the org, and new subscriptions fail silently. Test for it explicitly.
- The Stripe SDK is synchronous. FastAPI is fine with sync calls inside `async def` routes as long as they're short; Stripe's API typically responds in 100–300ms, which is fine. If it ever becomes a problem, wrap with `run_in_threadpool`.
- `allow_promotion_codes=true` enables Stripe's native promo code UI in Checkout. Useful for ad-hoc discounts.
- The difference between `coupon` and `promotion_code`: `coupon` is the base object, `promotion_code` is the user-facing code. If a user pastes `BETA100`, that's a promotion code; you can either (a) look it up first and pass the coupon ID, or (b) pass it through `allow_promotion_codes=true` and let Stripe handle it. Pick (b) for simplicity.

## Test command

```bash
pytest tests/test_billing_endpoints.py -v
```

All 6 tests must pass. Then integration-check by running the dev server and curling the stub endpoint:

```bash
uvicorn api.server:app --reload &
curl -X POST http://localhost:8000/api/billing/checkout \
  -H "X-Stub-User-Email: test@example.com" \
  -H "X-Stub-Org-Id: 00000000-0000-0000-0000-000000000001" \
  -H "Content-Type: application/json" \
  -d '{"price_id": "<trader-monthly-id>", "success_url": "https://example.com/s", "cancel_url": "https://example.com/c"}'
```

Expected: `{"checkout_url": "https://checkout.stripe.com/..."}`
