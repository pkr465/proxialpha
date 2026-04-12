# ProxiAlpha — Subscription Strategy

**Audience:** Active retail traders + small funds (RIAs, prop shops, family offices)
**Regulatory posture:** Software + AI signals, no advice
**ARPU target:** $50–$2,000 / month
**Date:** 2026-04-10

---

## TL;DR

1. **Delivery model:** Hybrid — hosted control plane + customer-side execution agent. Best fit for ProxiAlpha's existing architecture, the strongest legal moat for "no-advice" posture, and the only model that lets a small fund keep their broker keys, their LLM, and their DGX in-house.
2. **Pricing model:** 4 tiers (Free → Trader → Pro → Team), seat-priced where it matters, with **bundled LLM quota + BYO-key + metered overage** so no single design choice traps you.
3. **Build path:** 6 phases over ~16 weeks. Don't build a marketplace on day one; build entitlements + metering + the customer agent first. Everything else is sugar.

---

## 1. Delivery Architecture (Recommended: Hybrid)

You have four real options. Here's the trade-off matrix:

| Model | Margin | Ops burden | Legal risk | Fit for ProxiAlpha |
|---|---|---|---|---|
| Pure SaaS (you hold broker keys + run engine) | Highest | Highest | **Very high** — looks like custody/advice | Bad |
| Self-hosted license | Lowest | Lowest | Low | Bad — small funds won't pay $2k/mo for a zip file |
| Managed cloud + BYO broker keys | High | High | Medium | OK |
| **Hybrid: hosted control plane + customer agent** | High | Medium | **Low** | **Best** |

### Why hybrid wins for you specifically

- **Your code is already split this way.** `api/server.py` is a thin FastAPI control plane. `live_trading/`, `paper_trading/`, `backtesting/`, and the broker adapters are an execution layer that doesn't need to live on your servers.
- **Broker keys never leave the customer.** Alpaca/Hyperliquid private keys stay on the customer's box. You never custody, you never see secrets, and your insurance/SOC story is dramatically simpler.
- **DGX/Ollama story works out of the box.** A small fund with their own DGX runs Ollama locally and the agent talks to it on `localhost:11434`. You bill them for the *control plane*, not for inference they're already paying for in capex.
- **You can still run a hosted execution pool** for retail users who don't want to install anything — the same agent binary, just running in your cluster on their behalf.

### What lives where

```
┌──────────────────────────────────────────────────────────────┐
│  HOSTED CONTROL PLANE (you run this)                         │
│  ─ Auth, billing, entitlements                               │
│  ─ Strategy + config registry (versioned YAML)               │
│  ─ Backtest runner pool (CPU-bound, easy to scale)           │
│  ─ AI orchestration (Claude/OpenAI proxy + metering)         │
│  ─ Diary aggregator (read-only mirror of customer diaries)   │
│  ─ Dashboard (React/Next.js)                                 │
│  ─ Webhooks: alerts, signals, fills                          │
└──────────────────────────────────────────────────────────────┘
                          ▲   ▲
                  HTTPS   │   │  WebSocket (signals, controls)
                          │   │
┌──────────────────────────────────────────────────────────────┐
│  CUSTOMER-SIDE AGENT (Docker container, one binary)          │
│  ─ live_trading/, paper_trading/ engines                     │
│  ─ Broker adapters (Alpaca, Hyperliquid, future Drift/IBKR)  │
│  ─ Optional local Ollama / vLLM for AI signals               │
│  ─ Local JSONL diary, replicated to control plane            │
│  ─ License token + heartbeat                                 │
└──────────────────────────────────────────────────────────────┘
```

### Three deployment topologies you sell

| Topology | Who runs the agent | Who runs LLM | Best for |
|---|---|---|---|
| **A. Hosted-Hosted** | You (multitenant pod per customer) | You (Claude/OpenAI proxy) | Retail Free/Trader tiers |
| **B. Hosted-BYO LLM** | You | Customer's API key, proxied | Pro tier — power users |
| **C. Self-hosted-Self-hosted** | Customer (their box, their DGX) | Customer's local Ollama | Team tier — small funds |

One codebase. One agent binary. Three SKUs.

---

## 2. Pricing Model

### Tiers

| | **Free** | **Trader** | **Pro** | **Team** |
|---|---|---|---|---|
| **Price** | $0 | **$49 / mo** | **$199 / mo** | **$799 / mo** + $99/seat |
| **Annual** | — | $39 / mo ($468/yr) | $159 / mo ($1,908/yr) | $639 / mo ($7,668/yr) |
| **Tickers tracked** | 5 | 25 | 200 | Unlimited |
| **Backtests / mo** | 10 | 200 | 2,000 | Unlimited |
| **Paper trading** | ✓ | ✓ | ✓ | ✓ |
| **Live trading (Alpaca)** | — | ✓ | ✓ | ✓ |
| **Live trading (Hyperliquid + perps)** | — | — | ✓ | ✓ |
| **AI signals / mo (bundled)** | 20 | 500 | 5,000 | 25,000 |
| **AI signals overage** | n/a | $0.04 each | $0.025 each | $0.015 each |
| **BYO LLM key** | — | — | ✓ | ✓ |
| **BYO Ollama / DGX** | — | — | ✓ | ✓ |
| **Strategy slots** | 2 | 5 | 20 | Unlimited |
| **Custom strategies (Python)** | — | — | ✓ | ✓ |
| **Diary retention** | 7 days | 90 days | 1 year | 7 years (audit-grade) |
| **Risk manager guards** | 4 | 8 | 8 + custom | 8 + custom |
| **API access** | — | Read-only | Read + write | Full + webhooks |
| **Seats** | 1 | 1 | 3 | 5 included, +$99/seat |
| **White-label dashboard** | — | — | — | ✓ |
| **SSO / SAML** | — | — | — | ✓ |
| **Audit log export** | — | — | ✓ | ✓ (signed) |
| **Support** | Community | Email 48h | Email 24h | Slack channel + 8h |
| **SLA** | — | — | 99.5% | 99.9% |

### Why these numbers

- **$49 Trader** is the well-trodden retail price point (TradingView Pro, TrendSpider entry, Composer). Below it you sell to non-buyers.
- **$199 Pro** is the "I take this seriously" line. It must unlock something the $49 tier doesn't — here it's perps, BYO-LLM, custom Python strategies, and 10× the AI quota. Also the cheapest tier where you can profitably pay for support.
- **$799 Team** opens the small-fund door. Anything under ~$500/mo gets routed to a junior; $800–$1,500 lands in an analyst's discretionary budget without a procurement cycle. Above $2k you need a real sales motion.
- **Annual = ~20% off.** Standard. Improves retention math and gives you cash up front.
- **Free tier exists to feed the funnel and seed strategy ideas.** Hard-cap it at 5 tickers and 20 AI signals/mo so it's a real product, not a trojan horse.

### Add-ons (any tier)

| Add-on | Price | Notes |
|---|---|---|
| Extra AI signals (1k pack) | $25 | Cheaper than overage; reduces bill shock |
| Extra strategy slot | $19 / mo | Power users want more |
| Backtest compute pack (10k runs) | $49 | For walk-forward / hyperparam sweeps |
| Premium data feed (e.g. Polygon) | passthrough + 15% | You don't want to be a data vendor; you want margin on convenience |
| Dedicated execution pod | $499 / mo | Hosted topology, isolated resources |
| Onboarding / strategy review | $1,500 one-time | High-margin services for Team tier |

### LLM cost handling — the only honest answer

You said "maybe all of them" for who pays. That's actually correct, and the way to do it is **bundled quota + BYO-key + metered overage**, all three at once:

1. **Every paid tier gets a quota** (500 / 5,000 / 25,000 signals). Most users never hit it. This is your headline simplicity.
2. **Pro+ can plug in their own Anthropic / OpenAI / Ollama key.** When they do, those calls don't count against quota. This is how you peel margin pressure off heavy users without raising prices on light ones.
3. **Overage is metered in cents per signal** (not per token — users can't reason about tokens). You proxy through your LLM gateway and bill via Stripe metered prices.
4. **Hard cap is on by default** (user must opt in to overage). This is the single most important UX call — it eliminates bill-shock support tickets, which are the #1 churn driver in usage-priced AI products.

#### Unit economics sanity check

Assume Claude Sonnet at ~$3/M input + $15/M output. A single ProxiAlpha AI signal (your existing tool-calling loop in `core/ai_decision_maker.py`) is ~3,000 input tokens + ~500 output tokens = **~$0.017 per signal**.

| Tier | Signals included | Your LLM cost | Charged | Gross margin on LLM |
|---|---|---|---|---|
| Trader | 500 | $8.50 | $49 | $40.50 (83%) |
| Pro | 5,000 | $85 | $199 | $114 (57%) |
| Team | 25,000 | $425 | $799 | $374 (47%) |

Margin compresses on the higher tiers because that's where heavy users live — but Pro and Team users are the ones most likely to **bring their own key**, which immediately restores margin. That's why offering BYO-key is a feature, not a concession.

---

## 3. Implementation Plan

### Stack recommendation

| Layer | Choice | Why |
|---|---|---|
| Auth | **Clerk** or **WorkOS** | SSO ready for Team tier, social login for retail, 2 weeks of work avoided |
| Billing | **Stripe Billing** + **Stripe Metered** | Subscriptions, usage events, dunning, tax, all solved |
| Entitlements | **Stripe Entitlements** or self-hosted in Postgres | Stripe Entitlements is new and good enough |
| Metering | **OpenMeter** or **Lago** (self-hosted) | Don't write a meter from scratch |
| Control plane API | **Keep your FastAPI** | Already exists, works |
| Dashboard | **Next.js + shadcn/ui** | Standard, ships fast |
| DB | **Postgres** (Supabase or Neon to start) | RLS for multitenancy |
| Queue | **Redis** + **RQ** or **Celery** for backtests | Backtests are CPU-bound, queue them |
| Customer agent | **Docker container** built from existing repo | Ship a single image: `proxialpha/agent:latest` |
| Agent ↔ control plane | **mTLS + signed JWT** over HTTPS, optional WebSocket | License token doubles as auth |
| Observability | **Sentry** + **Posthog** + **Grafana Cloud** | Errors, product analytics, infra |
| LLM gateway | **LiteLLM** in front of your `LLMAdapter` | Already multi-provider; LiteLLM gives you metering hooks |

### Phases

#### Phase 0 — Foundations (Week 1–2)
- [ ] Pick legal entity and ToS (use Common Paper or Termly templates, then a 2-hour lawyer review for the AI signals disclaimer language)
- [ ] Set up Stripe account, configure tax (Stripe Tax handles 99% of it)
- [ ] Wire Clerk/WorkOS into FastAPI (`api/server.py` middleware)
- [ ] Postgres schema: `users`, `organizations`, `subscriptions`, `entitlements`, `agents`, `usage_events`
- [ ] Decide on subdomain strategy: `app.proxialpha.com`, `api.proxialpha.com`, `agent.proxialpha.com`

#### Phase 1 — Entitlements + Billing (Week 3–5)
- [ ] Stripe products + prices for all tiers (recurring + metered)
- [ ] Webhook handler: `customer.subscription.*` → flip `entitlements` table
- [ ] Entitlement decorator on FastAPI routes:
  ```python
  @router.get("/api/backtest/run")
  @requires_entitlement("backtests", consume=1)
  async def run_backtest(...): ...
  ```
- [ ] Quota tracking middleware reads/writes `entitlements.remaining` per period
- [ ] Ship a paywall on `dashboard/index.html` and a "Manage subscription" Stripe Customer Portal link

#### Phase 2 — Customer Agent (Week 5–8)
- [ ] Refactor `live_trading/`, `paper_trading/`, `backtesting/` into a thin `proxialpha-agent` package that boots from a config bundle pulled from the control plane
- [ ] License token: signed JWT (RS256) issued by control plane on subscription activation, includes `tier`, `org_id`, `expires_at`, `topology` (A/B/C)
- [ ] Agent boot sequence:
  1. Load license token from `~/.proxialpha/license`
  2. POST `/agent/heartbeat` → control plane responds with current entitlements + config bundle
  3. Initialize broker adapters from local secrets
  4. Start the engine
- [ ] Diary streaming: agent appends locally, batches every 30s to `/agent/diary` (already JSONL, easy)
- [ ] Docker image: `proxialpha/agent:1.0`, multi-arch (linux/amd64, linux/arm64)
- [ ] `proxialpha-cli` wrapper: `proxialpha login`, `proxialpha start`, `proxialpha logs`
- [ ] **Critical:** the agent must work fully offline once licensed for 7 days (heartbeat grace period). Don't make a fund's trading depend on your uptime.

#### Phase 3 — LLM Gateway + Metering (Week 7–9, parallel to Phase 2)
- [ ] LiteLLM (or similar) deployed in front of `core/llm_adapter.py`
- [ ] Every AI signal request emits a `signal.generated` usage event with `org_id`, `provider`, `tokens_in`, `tokens_out`, `cost_usd`
- [ ] OpenMeter aggregates events → pushes to Stripe metered price
- [ ] BYO-key path: when `org.byo_llm_key` is set, gateway uses customer key, **does not bill**, but still records the event for analytics
- [ ] Local Ollama path (Topology C): agent talks to localhost, posts a `signal.generated` event with `provider=ollama` and `cost_usd=0` for analytics only
- [ ] Hard-cap enforcement: when `entitlements.signals.remaining <= 0` and overage not enabled, return 402 with a "raise cap" CTA

#### Phase 4 — Dashboard (Week 8–11)
- [ ] Next.js app with: portfolio overview, live diary, backtest runner, strategy editor, AI signal log, billing
- [ ] Real-time view powered by control-plane WebSocket (agent → control plane → browser)
- [ ] Strategy editor: YAML editor with schema validation against your existing `config_strategies.yaml` shape
- [ ] Backtest runner: fire-and-forget, results cached, shareable links
- [ ] **Don't build the strategy marketplace yet.** Wait until you have 200+ paid users.

#### Phase 5 — Trust + Compliance (Week 10–12)
- [ ] ToS, Privacy Policy, AI Disclaimer page, "Not investment advice" banner everywhere AI output renders
- [ ] SOC 2 Type 1 prep using **Vanta** or **Drata** (~$10k/yr, 8 weeks). Don't skip — Team-tier customers will ask.
- [ ] Status page (`statuspage.io` or `betterstack`)
- [ ] Backup + restore drill on Postgres
- [ ] Incident response runbook
- [ ] Bug bounty program (HackerOne or Intigriti) — small budget, big trust signal

#### Phase 6 — Launch + Iterate (Week 13–16)
- [ ] Private beta: 20 hand-picked traders from your network, free Pro for 60 days, weekly feedback calls
- [ ] Public launch: Hacker News + r/algotrading + a Substack post + 1 podcast
- [ ] Pricing experiments: A/B test the $49 vs $59 vs $79 Trader price using Stripe coupon codes (first 100 signups get the price you want to test long-term)
- [ ] Cohort retention dashboard from day one. **Net revenue retention is the metric that matters.**

---

## 4. The Things That Will Bite You

These are the predictable failure modes for a SaaS in this category. Plan for them now, fix them never.

1. **Bill shock from AI overage.** Hard-cap by default, period. Send "you're at 80% of quota" emails. Show a live counter on the dashboard. The day you let one user get a $4,000 surprise bill, you lose the case in chargeback and the screenshots end up on Reddit.
2. **The advice vs. tool line.** Your output strings matter. "Buy AAPL at $150" is advice. "AAPL signal: bullish, confidence 0.72, generated by AIStrategy" is software output. Audit every UI string and every email. Run them past a securities attorney once, in a single 1-hour call, and save the redlines as a style guide.
3. **Broker key custody.** Even with the hybrid model, you'll be tempted to "make it easier" by storing API keys in your DB. Don't. Once you do, you're a custodian, your insurance changes, your SOC scope explodes, and a single breach is existential. Customers paste keys into their local agent. Period.
4. **Your support inbox at month 3.** You will not have time to answer "why did my backtest return 0 trades" 40 times a day. Build the diagnostic into the dashboard now. Every backtest result page shows: tickers loaded, signals generated, signals filtered by risk, signals executed. Self-service is the only scalable support.
5. **Strategy IP leakage.** Pro + Team users will write custom Python strategies. Where does that code run? In their agent, on their box, never uploaded to you. If you ever need to debug it, ask them to send you the file. Don't accept code into your control plane — you'll inherit liability and supply-chain risk.
6. **The AI is wrong sometimes.** It will be. The diary system you already have is a competitive moat — every signal is logged with full reasoning. When a user complains, you can show them exactly what the model saw and why it decided. Lean into this in your marketing.

---

## 5. What to Build First (if you only do one thing this week)

The highest-leverage move is **shipping the customer agent as a Docker image** with a fake license token, so a friend can `docker run` it against their own Alpaca paper account in under 10 minutes. That single artifact:

- Validates the hybrid topology end-to-end
- Becomes your sales demo
- Forces the entitlement + heartbeat protocol to exist
- Costs nothing in cloud bills

Everything else (Stripe, dashboards, marketplace) is downstream of that working.

---

## 6. Open Questions for You

1. **Brand:** Is "ProxiAlpha" the customer-facing name or is there a parent brand?
2. **Geography:** US-only at launch, or international? Affects payment processing (Stripe is fine either way) and the disclaimer language.
3. **Existing audience:** Do you have a list, a Discord, a Substack — anywhere you can launch into? If not, the first 90 days post-launch are the ones to plan most carefully.
4. **Funding posture:** Bootstrapped or VC? Bootstrapped means Trader tier carries the company. VC-backed means you can subsidize Free + Trader and bet on Team-tier expansion.
5. **Co-founder / team:** Who runs ops + support when this lands? Solo founders should not run a hosted topology in year one — start with self-hosted Pro/Team and add hosted later.
