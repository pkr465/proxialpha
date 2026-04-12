# ADR-001: Hybrid Control Plane + Customer Agent Topology

**Status:** Accepted
**Date:** 2026-04-11
**Deciders:** Pavan
**Supersedes:** —
**Superseded by:** —

---

## Context

ProxiAlpha is a Python framework for backtesting, paper trading, and live trading across equities (Alpaca) and perps (Hyperliquid), with an AI decision-making layer that uses Claude/OpenAI/Ollama through a provider-agnostic `LLMAdapter`. We are converting it into a commercial subscription service targeting active retail traders and small funds (RIAs, prop shops, family offices).

The regulatory posture is **software + AI signals, not advice**. Customers must remain in full control of:
- Broker API keys and private keys (Alpaca, Hyperliquid wallets)
- Order execution decisions (the tool proposes; the user or their own system accepts)
- LLM provider credentials, if they choose to bring their own

The commercial target is $50–$2,000/month ARPU across four tiers (Free, Trader, Pro, Team). Team tier must work on customer-owned infrastructure including DGX servers running local Ollama or vLLM.

We need to pick a delivery topology that supports all three customer profiles (retail self-hosted, retail hosted, small-fund fully-on-prem) from a single codebase.

## Decision

**We will ship ProxiAlpha as a hybrid system: a hosted control plane that we operate, and a customer-side agent binary that we ship as a Docker image.**

The split is:

| Concern | Control plane (we run) | Customer agent (they run) |
|---|---|---|
| Authentication & billing | ✓ | — |
| Entitlement enforcement | ✓ (source of truth) | ✓ (cached + enforced locally) |
| Strategy + config registry | ✓ | — |
| Backtest scheduling | ✓ | ✓ (execution) |
| AI orchestration (proxy) | ✓ (optional path) | ✓ (alternative path) |
| Broker API keys | **Never** | ✓ (local secrets only) |
| Order placement | — | ✓ |
| Local LLM (Ollama/vLLM) | — | ✓ |
| Diary (JSONL) — write | — | ✓ |
| Diary — read-only mirror | ✓ | — |
| Dashboard UI | ✓ | — |

The agent supports three deployment topologies from one binary, selected by a flag in the license token:

- **Topology A (Hosted-Hosted):** We run the agent in our cluster in a per-tenant pod. Customer uses it via the dashboard. Used for Free and Trader tiers.
- **Topology B (Hosted agent + BYO LLM):** Agent runs in our cluster, but LLM calls go to the customer's API key. Used for Pro tier.
- **Topology C (Self-hosted agent + local LLM):** Customer runs the agent on their own box (including DGX). All broker keys, all LLM inference, all order flow stays on their network. Used for Team tier.

## Options Considered

### Option 1: Pure SaaS — we run everything, customers upload keys

- **Pros:** Simplest UX, highest gross margin, single operations surface.
- **Cons:** We would custody broker API keys (and Hyperliquid private keys, which are bearer credentials). This creates:
  - Regulatory exposure — holding keys that can place trades looks a lot like custody and advice, depending on jurisdiction.
  - Security surface — one breach is existential; SOC 2 scope and insurance costs balloon.
  - Hyperliquid specifically — private keys are wallets; losing them is losing funds, which is a non-recoverable failure mode.
- **Verdict:** Rejected. The legal and security risk is disproportionate to the margin benefit for a pre-revenue product.

### Option 2: Self-hosted license — ship a zip file, license server stamps keys

- **Pros:** Lowest ops burden, no cloud costs, simplest legal story.
- **Cons:**
  - Small funds will not pay $800/month for a zip file — the expectation at that price is a dashboard, a support inbox, and an audit trail we operate.
  - Enforcing entitlements is hard; piracy is trivial.
  - No way to ship strategy updates or new models without a manual step.
  - Loses the observability story that makes the AI diary a competitive moat.
- **Verdict:** Rejected. Acceptable for enthusiasts, wrong fit for the target ARPU.

### Option 3: Managed cloud + BYO broker keys (we run the agent, user pastes keys into our UI)

- **Pros:** High margin, real ops control, dashboard is first-class.
- **Cons:** Still a custodial posture from a regulatory perspective — we are the ones holding the credentials that move money. Only marginally better than Option 1 on the legal front.
- **Verdict:** Rejected as the *only* topology, but retained as a sub-option inside the hybrid (Topology A/B, on explicit customer consent and with a clear ToS carve-out).

### Option 4 (Chosen): Hybrid control plane + customer agent

- **Pros:**
  - Cleanest legal story — for Team tier, no keys or order flow ever touch our infrastructure.
  - One codebase, three deployment topologies, selected by license flag.
  - Reuses the existing architectural split in the ProxiAlpha repo — `api/` is already the control plane and `live_trading/`, `paper_trading/`, `backtesting/` are already the execution layer.
  - DGX / local Ollama story works naturally — agent talks to localhost.
  - Diary is local-first, replicated to control plane for dashboard and analytics.
- **Cons:**
  - More complex than any single topology — we must build and maintain both sides.
  - Heartbeat + grace-period protocol adds protocol surface and a class of bugs.
  - Supporting Topology C means we need remote diagnostics (logs, status) without being able to SSH into the box.
  - Strategy updates to Topology C require pull semantics (agent fetches config bundle on heartbeat) rather than push.

## Consequences

### Positive

- We can sell to a customer who says "no code or keys leave our network" without changing our product.
- The agent binary becomes our sales demo — `docker run proxialpha/agent:latest` with a trial license token is a 10-minute onboarding flow.
- Every topology shares the same diary schema, the same LLM adapter, and the same risk manager, which keeps test surface manageable.
- The control plane is small and boring — FastAPI + Postgres + Stripe webhooks — which is cheap to run and easy to harden.

### Negative

- Must design a heartbeat protocol with offline grace (see ADR-003). Non-trivial.
- Must ship a Docker image as a first-class artifact, with semver, release notes, and an upgrade path. Cannot be an afterthought.
- Must solve remote diagnostics for Topology C: structured logs streamed to control plane, redacted for secrets, opt-in per customer.
- We cannot debug a Topology-C customer's issue by looking at our own logs — we need a `proxialpha doctor` CLI command that bundles safe diagnostic output for the support inbox.

### Neutral / follow-up ADRs required

- ADR-002 covers billing + entitlement source-of-truth.
- ADR-003 covers license token format and heartbeat protocol.
- ADR-004 covers the LLM gateway strategy.
- ADR-005 covers multitenancy inside the control plane.
- A future ADR will cover the upgrade / rollout strategy for the agent binary (channels: stable / beta, auto-update off by default for Team tier).

## Compliance Notes

The "software + signals, not advice" posture is load-bearing for this decision. It requires:

1. All UI strings and API responses describe AI output as "signals," "research," or "analysis," never as "recommendations," "advice," or "suggestions to trade."
2. The customer-side agent is the only component that places orders. The control plane never calls `broker.submit_order`. This boundary must be enforced in code review and in automated tests.
3. The ToS must say explicitly that ProxiAlpha is a software tool, the customer is solely responsible for all trading decisions and outcomes, and the AI output is informational research.

Any future decision that moves order placement into the control plane implicitly changes our regulatory posture and must be escalated to counsel before shipping.
