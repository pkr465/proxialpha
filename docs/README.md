# ProxiAlpha — Product Documentation

This folder contains the strategy, architecture, and implementation docs for turning ProxiAlpha into a subscription service. It is organized for two audiences:

1. **Humans making decisions** — read `adr/` and `specs/`.
2. **Coding agents and engineers executing tickets** — read `prompts/`.

---

## 1. Reading order for a new contributor

1. [`../SUBSCRIPTION_STRATEGY.md`](../SUBSCRIPTION_STRATEGY.md) — why we're building this, pricing, delivery model, phased plan.
2. [`adr/`](./adr/) — the five load-bearing architecture decisions, in order.
3. [`specs/phase1-entitlements-and-billing.md`](./specs/phase1-entitlements-and-billing.md) — the first shipping unit.
4. [`specs/phase2-customer-agent.md`](./specs/phase2-customer-agent.md) — the second shipping unit.
5. [`prompts/`](./prompts/) — one file per ticket, ready to hand to Claude Code or an engineer.

---

## 2. Architecture Decision Records (`adr/`)

ADRs capture the "why" behind load-bearing choices so they don't get re-litigated every sprint. Each ADR is one document, status-tracked, with the options considered and the consequences of the decision.

| # | ADR | Decision |
|---|---|---|
| [001](./adr/ADR-001-hybrid-topology.md) | Hybrid topology | Control plane + customer agent binary, three deployment topologies from one image |
| [002](./adr/ADR-002-billing-and-entitlements.md) | Billing + entitlements | Stripe Billing as SoR, local Postgres `entitlements` table for the hot path |
| [003](./adr/ADR-003-license-token-and-heartbeat.md) | License token + heartbeat | Short-lived JWT (RS256, 24h), entitlement snapshot embedded, 7-day grace window |
| [004](./adr/ADR-004-llm-gateway.md) | LLM gateway | LiteLLM in front of `LLMAdapter` in the control plane; per-topology routing |
| [005](./adr/ADR-005-multitenancy.md) | Multitenancy | Shared Postgres, RLS on every tenant table, `app.current_org_id` session variable |

---

## 3. Product Requirements Documents (`specs/`)

PRDs turn strategy into buildable tickets. Each PRD contains problem statement, goals, non-goals, user stories with acceptance criteria, data model, API contracts, test plan, and rollout plan.

| Phase | Spec | What it delivers |
|---|---|---|
| 1 | [Entitlements & Billing](./specs/phase1-entitlements-and-billing.md) | Signup → pay → entitled. Metered overage. Plan changes via Stripe Customer Portal. |
| 2 | [Customer Agent](./specs/phase2-customer-agent.md) | `docker run proxialpha/agent` → running live/paper/backtest engine with local entitlement enforcement and diary replication. |

Phases 3–6 (LLM gateway, dashboard, compliance, launch) do not have PRDs yet. Write them only when their predecessor is shipping, not before.

---

## 4. Agent-ready task prompts (`prompts/`)

One file per ticket. Each prompt is designed to be handed to a coding agent (Claude Code, Cursor, etc.) or a human engineer without additional context. The format:

- **Objective** — one sentence
- **Context** — where to look
- **Exact files to create or modify** — no ambiguity
- **Acceptance criteria** — testable, not aspirational
- **Do not** — guardrails
- **Hints and gotchas** — the sharp edges
- **Test command** — how to verify done

| # | Task | Phase | Depends on |
|---|---|---|---|
| [01](./prompts/01-db-schema-and-migrations.md) | Database schema + Alembic | 1 | — |
| [02](./prompts/02-stripe-webhook-handler.md) | Stripe webhook handler | 1 | 01 |
| [03](./prompts/03-checkout-and-portal-endpoints.md) | Checkout + Portal endpoints | 1 | 01, 02 |
| [04](./prompts/04-entitlement-decorator-and-consume.md) | Entitlement decorator + atomic consume | 1 | 01, 02 |
| [05](./prompts/05-metering-job.md) | Hourly metering job (Stripe usage records) | 1 | 01–04 |
| [06](./prompts/06-agent-heartbeat-endpoint.md) | Agent heartbeat endpoint (control plane side) | 2 | 01–04 |
| [07](./prompts/07-agent-boot-and-license-client.md) | Agent boot + license client + mode machine | 2 | 06 |
| [08](./prompts/08-agent-docker-image-and-ci.md) | Docker image + CI pipeline + doctor bundle | 2 | 07 |

### Task ordering

Strictly: 01 → 02 → 03 → 04 → 05 → 06 → 07 → 08.

Tasks 02 and 03 can run in parallel if two agents are working simultaneously, but each must be completed before Task 04 starts. Task 06 can begin once Task 04 is done, in parallel with Task 05.

---

## 5. How to hand a task to a coding agent

```
Read the following files in order, then execute the task:

1. docs/README.md
2. docs/adr/ADR-001-hybrid-topology.md
3. docs/adr/ADR-002-billing-and-entitlements.md
4. docs/specs/phase1-entitlements-and-billing.md
5. docs/prompts/01-db-schema-and-migrations.md   ← THIS IS YOUR TASK

Do not proceed past the acceptance criteria in the prompt. If any acceptance
criterion is unclear or the spec is ambiguous, stop and ask before writing
code. When done, run the test command and paste the output.
```

Each prompt is deliberately self-contained with pointers into the specs. An agent that has read the prompt, the referenced ADR, and the referenced PRD section has everything it needs.

---

## 6. What is NOT in this folder

- **Operational runbooks** (oncall, incident response, postmortems) — they will live in `operations/` once we have customers.
- **Marketing and sales copy** — lives elsewhere; separate workflow.
- **Compliance docs** (SOC 2 policies, DPA templates) — Phase 5 deliverable, not started.
- **Financial model** (unit economics, burn, runway) — separate spreadsheet.

---

## 7. Maintenance

- **ADRs are immutable once Accepted.** To change a decision, write a new ADR that supersedes the old one. Never edit an Accepted ADR except to fix a typo or mark it Superseded.
- **PRDs evolve during their phase.** Track changes in the file's top metadata. Once a phase ships, the PRD is frozen as a historical record.
- **Prompts can be updated freely.** They are working documents. If a prompt is wrong, fix it and re-run the task.

---

## 8. Quick links

- [Subscription strategy memo](../SUBSCRIPTION_STRATEGY.md) — the executive summary
- [Testing guide](../TESTING.md) — how to run the existing framework tests
- [Hyperliquid integration analysis](../HYPERLIQUID_INTEGRATION_ANALYSIS.md) — context on perps support
