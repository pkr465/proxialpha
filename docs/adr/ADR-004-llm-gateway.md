# ADR-004: LLM Gateway Strategy

**Status:** Accepted
**Date:** 2026-04-11
**Deciders:** Pavan
**Depends on:** ADR-001 (hybrid topology), ADR-002 (entitlements)

---

## Context

ProxiAlpha already has a working multi-provider `LLMAdapter` in `core/llm_adapter.py` that speaks to Claude, OpenAI, Ollama, Gemini, and a generic "custom" provider. The commercial product needs three things on top of that:

1. **Metering** — every signal generated must be recorded with token counts and cost, so we can bill metered overage (ADR-002).
2. **BYO-key routing** — when a Pro/Team customer provides their own API key, the call uses their key and does not count against quota.
3. **Rate limiting and abuse protection** — a runaway bug in a customer strategy must not spike our Claude bill by $5k overnight.

We also need to preserve two existing properties:
- The agent's hot path (`live_trading/ai_decision_maker.py`) must remain low-latency. We are not going to double the signal latency for the sake of observability.
- The Topology-C path (agent talks to localhost Ollama) must still work when the control plane is unreachable.

## Decision

**We will place LiteLLM as a thin gateway in front of `LLMAdapter` inside the control plane (for Topology A/B), and keep `LLMAdapter` as-is inside the agent (for Topology C and for BYO-key calls in Topology B).**

- **Control plane path (Topology A/B with bundled LLM):**
  - Agent calls `POST https://control.proxialpha.com/llm/generate` with the prompt and provider hint.
  - Control plane runs LiteLLM → Anthropic/OpenAI/Gemini → records `usage_events` row → returns response.
  - Metering and cost attribution happen at the gateway edge, where we have reliable token counts.
- **BYO-key path (Topology B with customer key):**
  - Agent receives the customer API key from its license bundle at startup (encrypted in the config_bundle payload).
  - Agent calls the provider directly via `LLMAdapter`. Zero control-plane round trip.
  - Agent still emits a `signal.generated` event back to the control plane with `provider_cost_usd=null, billed_to=customer_key`, purely for analytics and quota tracking.
- **Local LLM path (Topology C):**
  - Agent calls Ollama or vLLM on localhost via `LLMAdapter`.
  - No control-plane call on the hot path.
  - Signal event posted asynchronously in a batch with the next heartbeat.

## Options Considered

### Option 1: Every LLM call goes through the control plane

- **Pros:** Metering is trivial; one place to rate-limit; single audit log.
- **Cons:**
  - Latency penalty: agent → control plane → provider → control plane → agent adds ~100-300ms minimum, which is noticeable on live trading.
  - Destroys the DGX story entirely — if the customer's whole point is "my DGX, my Ollama, no egress," we cannot round-trip through our cloud.
  - Control plane becomes a hard dependency for every trade decision, which violates ADR-001's operate-through-outage goal.
- **Verdict:** Rejected as the only path. Retained as the default path for bundled-LLM customers.

### Option 2: Every LLM call happens in the agent, control plane only meters post-hoc

- **Pros:** Lowest latency; simplest agent code.
- **Cons:**
  - Post-hoc metering is always behind real usage. A runaway agent can burn through $5k before the control plane sees the events.
  - We need to trust agent-reported token counts for billing, which is an attack surface. A malicious customer could fake low counts.
  - Bundled-LLM calls need the provider API key — we'd have to ship our Anthropic key to every agent, which is an immediate security disaster.
- **Verdict:** Rejected. The bundled-LLM path cannot ship provider keys to untrusted agents.

### Option 3 (Chosen): Per-topology routing

- **Pros:**
  - Bundled-LLM path stays in our control plane, where our Anthropic key lives and metering is authoritative.
  - BYO-key and local-LLM paths bypass the control plane entirely, preserving latency and the Topology-C story.
  - The agent knows which path to use from a single field in its license bundle: `llm_routing: gateway | byo_key | local`.
- **Cons:**
  - Three code paths instead of one; three sets of failure modes.
  - Analytics for BYO-key and local paths are event-based and async, so there's a lag between signal generation and dashboard visibility.
- **Mitigation:** Compact interface. `LLMAdapter` already abstracts the provider; we add one `ControlPlaneAdapter` that conforms to the same interface. Agent swaps adapters at startup based on `llm_routing`.

### Option 4: Build our own gateway instead of LiteLLM

- **Pros:** No dependency.
- **Cons:** LiteLLM already handles provider retries, timeout tuning, cost tables, streaming, and SDK differences across ~100 providers. Writing this is weeks of wasted effort.
- **Verdict:** Rejected. Use LiteLLM. Wrap it. Pin the version.

## Consequences

### Positive

- Each topology pays only the cost it needs to pay: Topology C has zero control-plane latency, bundled-LLM customers have single-point metering, BYO-key customers have zero billing surprise.
- LiteLLM gives us free upgrades to new providers and new models via dependency bumps.
- The agent's `LLMAdapter` contract stays stable — the control-plane path is "just another provider" as far as the agent is concerned.

### Negative

- Three paths to test. Smoke tests must cover all three (`test_ollama.py` already covers local; we need `test_control_plane_llm.py` and `test_byo_key.py` in the same style).
- LiteLLM is a dependency we don't control. Pin it, track its changelog, have a plan B (direct Anthropic SDK) if it ever becomes a problem.
- BYO-key and local paths report usage asynchronously, so "AI signals remaining" in the dashboard can lag by up to one heartbeat cycle (60 seconds). Communicate this in the UI.

### Control-plane gateway endpoint

```
POST /llm/generate
Authorization: Bearer <license token>
Content-Type: application/json

{
  "model": "claude-sonnet-4-6",
  "messages": [...],
  "max_tokens": 4096,
  "temperature": 0.3,
  "system_prompt": "...",
  "tools": [...],
  "purpose": "ai_signal",
  "signal_id": "sig_01HXYZ..."
}

→ 200:
{
  "response": "...",
  "usage": {"prompt_tokens": 3102, "completion_tokens": 487, "cost_usd": 0.01665},
  "provider": "anthropic",
  "model": "claude-sonnet-4-6",
  "latency_ms": 1842
}

→ 402: quota exhausted, overage disabled
→ 429: org-level rate limit hit
→ 503: upstream provider unavailable, retry with backoff
```

### Rate limiting

- **Per-org:** 10 concurrent signals in flight, 1000 signals/hour hard cap. Configurable per tier; Team tier gets 100 concurrent.
- **Global circuit breaker:** If our total Anthropic spend in the last hour exceeds 2× the rolling 7-day average, the gateway returns 503 for bundled-LLM calls and alerts on-call. This is the last line of defense against a bug in a released agent.

## Open Questions

1. **Streaming responses:** The current `AIDecisionMaker` uses tool-calling, which benefits from streaming. LiteLLM supports it. We enable streaming in Phase 3, not Phase 1.
2. **Provider failover:** If Anthropic 500s, do we automatically fall back to OpenAI? Tempting, but quality differences will confuse users. Default: no automatic failover. The agent surfaces the error, the user retries or switches manually.
3. **Fine-tuned models:** A future Team-tier feature may be "fine-tuned strategy model." LiteLLM supports this out of the box for OpenAI and Anthropic. Revisit when a customer asks.
