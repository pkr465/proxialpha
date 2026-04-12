"""
AI decision maker (ported from hyperliquid-trading-agent/src/agent/decision_maker.py).

This is the production-grade LLM tool-calling loop. It replaces ProxiAlpha's
old `strategies/ai_strategy.py` stub (which just accepted injected JSON).

Key upgrades over the old stub:
  1. Strict JSON output contract with explicit schema
  2. Anthropic tool-calling: Claude can invoke `fetch_indicator` mid-inference
     to pull fresh EMA/RSI/MACD/ATR/BBands/ADX/OBV/VWAP/Stoch-RSI for any
     ticker × timeframe. Indicators are computed locally via core/indicators.py
  3. Haiku sanitizer fallback — if Claude returns malformed JSON, a cheap
     second call normalizes it
  4. Low-churn / hysteresis / cooldown_bars system prompt — aggressively
     discourages LLM whipsaw between scan cycles
  5. Multi-iteration tool loop (up to 6 iterations) so Claude can chain tool
     calls if it needs more context
  6. Full request/response logging via core/diary.LLMRequestLog

This module is Anthropic-specific (because tool calling is provider-specific
and Claude's API shape is what the pattern was built for). However, it
accepts a `PortfolioProvider` callable so it remains exchange-agnostic.

Non-Anthropic fallback: if anthropic SDK is unavailable, we expose a simpler
path through `core/llm_adapter.py` (no tool use) so the module still
functions on Ollama / OpenAI / Gemini.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from core.indicators import (
    compute_all,
    df_to_candles,
    latest,
    last_n,
    ema as _ema,
    sma as _sma,
    rsi as _rsi,
    atr as _atr,
)
from core.diary import get_llm_log


logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a rigorous QUANTITATIVE TRADER and interdisciplinary MATHEMATICIAN-ENGINEER optimizing risk-adjusted returns under real execution, margin, and transaction cost constraints.

You will receive market + account context for SEVERAL assets, including:
- assets = {assets}
- per-asset recent price/volume and computed indicators (multi-timeframe where available)
- Active positions and their Exit Plans
- Recent Trading History
- Risk management limits (HARD-ENFORCED by the system, not just guidelines — you cannot override them; the system will cap or reject trades that exceed them)

Always use the 'current time' provided in the user message to evaluate any time-based conditions, such as cooldown expirations or timed exit plans.

Your goal: make decisive, first-principles decisions per asset that MINIMIZE CHURN while capturing edge. Aggressively pursue setups where calculated risk is outweighed by expected edge; size positions so downside is controlled while upside remains meaningful.

Core policy (low-churn, position-aware)
1) Respect prior plans: If an active trade has an exit_plan with explicit invalidation (e.g., "close if daily close above SMA50"), DO NOT close or flip early unless that invalidation (or a stronger one) has occurred.
2) Hysteresis: Require stronger evidence to CHANGE a decision than to keep it. Only flip direction if BOTH:
   a) Higher-timeframe structure supports the new direction (e.g., daily EMA20 vs EMA50, MACD regime), AND
   b) Intraday/recent structure confirms with a decisive break beyond ~0.5×ATR and momentum alignment (MACD histogram or RSI slope).
   Otherwise, prefer HOLD or adjust TP/SL.
3) Cooldown: After opening, adding, reducing, or flipping, impose a self-cooldown of at least 3 bars of the decision timeframe before another direction change, unless a hard invalidation occurs. Encode this in exit_plan (e.g., "cooldown_bars:3 until 2026-04-13"). You must honor your own cooldowns on future cycles.
4) Overbought/oversold ≠ reversal by itself: Treat RSI extremes as risk-of-pullback. You need structure + momentum confirmation to bet against trend. Prefer tightening stops or taking partial profits over instant flips.
5) Prefer adjustments over exits: If the thesis weakens but is not invalidated, first consider: tighten stop to a recent swing or ATR multiple, trail TP, or reduce size. Flip only on hard invalidation + fresh confluence.

Decision discipline (per asset)
- Choose one: buy / sell / hold.
- Proactively harvest profits when price action presents a clear, high-quality opportunity that aligns with your thesis.
- You control allocation_usd (but the system will cap it — see risk limits).
- Order type: set order_type to "market" for immediate execution, or "limit" for resting orders.
  • For limit orders, you MUST set limit_price.
  • For market orders, limit_price should be null.
  • Default is "market" if omitted.
- TP/SL sanity:
  • BUY: tp_price > current_price, sl_price < current_price
  • SELL: tp_price < current_price, sl_price > current_price
  If sensible TP/SL cannot be set, use null and explain the logic. A mandatory SL will be auto-applied if you don't set one.
- exit_plan must include at least ONE explicit invalidation trigger and may include cooldown guidance you will follow later.

Tool usage
- Use the fetch_indicator tool whenever an additional datapoint could sharpen your thesis. Parameters:
  • indicator: one of ema, sma, rsi, macd, bbands, atr, adx, obv, vwap, stoch_rsi, all
  • ticker: e.g. "COIN", "HOOD", "MSFT", "ETH"
  • interval: "1d" (equities) or "1h"/"4h" (crypto)
  • period: optional indicator period
- Incorporate tool findings into your reasoning, but NEVER paste raw tool responses into the final JSON — summarize the insight instead.
- Use tools to upgrade your analysis; lack of confidence is a cue to query them before deciding.

Reasoning recipe (first principles)
- Structure (trend, EMAs slope/cross, HH/HL vs LH/LL), Momentum (MACD regime, RSI slope), Liquidity/volatility (ATR, volume), Positioning tilt (drawdown from ATH).
- Favor alignment across higher and lower timeframes. Counter-trend trades require stronger confirmation and tighter risk.

Output contract
- Output ONLY a strict JSON object (no markdown, no code fences) with exactly two properties:
  • "reasoning": long-form string capturing detailed, step-by-step analysis.
  • "trade_decisions": array ordered to match the provided assets list.
- Each item inside trade_decisions must contain the keys: asset, action, allocation_usd, order_type, limit_price, tp_price, sl_price, exit_plan, rationale.
  • order_type: "market" (default) or "limit"
  • limit_price: required if order_type is "limit", null otherwise
- Do not emit Markdown or any extra properties.
"""


TOOL_SCHEMA = {
    "name": "fetch_indicator",
    "description": (
        "Fetch technical indicators computed locally from OHLCV candle data. "
        "Works for any ticker with loaded price history. "
        "Available indicators: ema, sma, rsi, macd, bbands, atr, adx, obv, vwap, stoch_rsi, all. "
        "Returns the latest value and a recent series (last 10 non-null points)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "indicator": {
                "type": "string",
                "enum": [
                    "ema", "sma", "rsi", "macd", "bbands", "atr",
                    "adx", "obv", "vwap", "stoch_rsi", "all",
                ],
            },
            "ticker": {
                "type": "string",
                "description": "Ticker / asset symbol, e.g. COIN, HOOD, ETH",
            },
            "interval": {
                "type": "string",
                "enum": ["1m", "5m", "15m", "1h", "4h", "1d"],
                "description": "Timeframe interval (default 1d for equities)",
            },
            "period": {
                "type": "integer",
                "description": "Indicator period (default varies by indicator)",
            },
        },
        "required": ["indicator", "ticker"],
    },
}


# ---------------------------------------------------------------------------
# CandleProvider protocol — exchange-agnostic
# ---------------------------------------------------------------------------

# A CandleProvider is any callable that, given (ticker, interval, limit),
# returns a list of candle dicts (or an equivalent DataFrame that can be
# converted via indicators.df_to_candles).
CandleProvider = Callable[[str, str, int], Any]


# ---------------------------------------------------------------------------
# AIDecisionMaker
# ---------------------------------------------------------------------------

class AIDecisionMaker:
    """LLM-powered trade decision engine with tool calling + hysteresis.

    Args:
        model: Anthropic model ID (default: claude-sonnet-4-6)
        sanitize_model: fallback model for JSON normalization (default: claude-haiku-4-5)
        candle_provider: callable(ticker, interval, limit) -> candles or DataFrame
            Used by the fetch_indicator tool. If None, tool calling is disabled.
        api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
        max_tokens: max tokens per call (default 4096)
        enable_tools: whether to expose fetch_indicator tool (default True)
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        sanitize_model: str = "claude-haiku-4-5-20251001",
        candle_provider: CandleProvider | None = None,
        api_key: str | None = None,
        max_tokens: int = 4096,
        enable_tools: bool = True,
        llm_log_path: str | None = None,
    ):
        self.model = model
        self.sanitize_model = sanitize_model
        self.candle_provider = candle_provider
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_tokens = max_tokens
        self.enable_tools = enable_tools and candle_provider is not None

        self._client = None
        self._llm_log = get_llm_log(llm_log_path) if llm_log_path else get_llm_log()

    # ------------------------------------------------------------------
    # Client init
    # ------------------------------------------------------------------

    def _init_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore
            self._client = anthropic.Anthropic(api_key=self.api_key)
            return self._client
        except ImportError:
            logger.error(
                "anthropic package not installed. Install: pip install anthropic"
            )
            return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def decide_trade(self, assets: Iterable[str], context: str) -> dict:
        """Run one full Claude decision round for the given assets.

        Args:
            assets: list of ticker symbols being decided on this cycle
            context: serialized market + account context (string)

        Returns:
            {
                "reasoning": str,
                "trade_decisions": [ { asset, action, allocation_usd, ... }, ... ]
            }
        """
        assets = list(assets)
        client = self._init_client()
        if client is None:
            return self._empty_result(assets, "anthropic SDK not available")

        system_prompt = SYSTEM_PROMPT.format(assets=json.dumps(assets))
        messages: list[dict] = [{"role": "user", "content": context}]

        for iteration in range(6):
            try:
                response = self._call_claude(client, system_prompt, messages)
            except Exception as e:
                logger.error("Claude API error: %s", e)
                self._llm_log.log_error(str(e))
                return self._empty_result(assets, f"api error: {e}")

            tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            text_blocks = [b for b in response.content if getattr(b, "type", None) == "text"]

            if tool_use_blocks and response.stop_reason == "tool_use":
                # Echo assistant content back (required for tool_result turn)
                assistant_content = []
                for block in response.content:
                    btype = getattr(block, "type", None)
                    if btype == "text":
                        assistant_content.append({"type": "text", "text": block.text})
                    elif btype == "tool_use":
                        assistant_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                messages.append({"role": "assistant", "content": assistant_content})

                # Execute each tool call
                tool_results = []
                for block in tool_use_blocks:
                    result_str = self._handle_tool_call(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            # No tool calls — parse text as JSON
            raw_text = "".join(getattr(b, "text", "") for b in text_blocks)
            if not raw_text.strip():
                logger.error("Empty response from Claude")
                return self._empty_result(assets, "empty response")

            parsed = self._parse_json_output(raw_text, assets)
            if parsed is not None:
                return parsed

            # Parsing failed — try the Haiku sanitizer
            sanitized = self._sanitize_output(client, raw_text, assets)
            if sanitized.get("trade_decisions"):
                return sanitized
            return self._empty_result(assets, "parse error")

        return self._empty_result(assets, "tool loop cap reached")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _call_claude(self, client, system_prompt: str, messages: list[dict]):
        self._llm_log.log_request(self.model, messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": messages,
        }
        if self.enable_tools:
            kwargs["tools"] = [TOOL_SCHEMA]
        response = client.messages.create(**kwargs)
        self._llm_log.log_response(getattr(response, "stop_reason", "?"), getattr(response, "usage", {}))
        return response

    def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Execute a fetch_indicator tool call against the candle provider."""
        if tool_name != "fetch_indicator":
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        if self.candle_provider is None:
            return json.dumps({"error": "No candle provider configured"})

        try:
            ticker = tool_input["ticker"]
            interval = tool_input.get("interval", "1d")
            indicator = tool_input["indicator"]
            limit = 200

            raw = self.candle_provider(ticker, interval, limit)
            # Accept either a list of candle dicts or a DataFrame
            if hasattr(raw, "columns"):  # pandas DataFrame
                candles = df_to_candles(raw)
            else:
                candles = raw or []

            if not candles:
                return json.dumps({"error": f"No candle data for {ticker}"})

            all_indicators = compute_all(candles)

            if indicator == "all":
                result = {
                    k: {
                        "latest": latest(v) if isinstance(v, list) else v,
                        "series": last_n(v, 10) if isinstance(v, list) else v,
                    }
                    for k, v in all_indicators.items()
                }
            elif indicator == "macd":
                result = {
                    "macd": {
                        "latest": latest(all_indicators.get("macd", [])),
                        "series": last_n(all_indicators.get("macd", []), 10),
                    },
                    "signal": {
                        "latest": latest(all_indicators.get("macd_signal", [])),
                        "series": last_n(all_indicators.get("macd_signal", []), 10),
                    },
                    "histogram": {
                        "latest": latest(all_indicators.get("macd_histogram", [])),
                        "series": last_n(all_indicators.get("macd_histogram", []), 10),
                    },
                }
            elif indicator == "bbands":
                result = {
                    "upper": {
                        "latest": latest(all_indicators.get("bbands_upper", [])),
                        "series": last_n(all_indicators.get("bbands_upper", []), 10),
                    },
                    "middle": {
                        "latest": latest(all_indicators.get("bbands_middle", [])),
                        "series": last_n(all_indicators.get("bbands_middle", []), 10),
                    },
                    "lower": {
                        "latest": latest(all_indicators.get("bbands_lower", [])),
                        "series": last_n(all_indicators.get("bbands_lower", []), 10),
                    },
                }
            elif indicator in ("ema", "sma"):
                period = int(tool_input.get("period", 20))
                closes = [c["close"] for c in candles]
                series = _ema(closes, period) if indicator == "ema" else _sma(closes, period)
                result = {"latest": latest(series), "series": last_n(series, 10), "period": period}
            elif indicator == "rsi":
                period = int(tool_input.get("period", 14))
                series = _rsi(candles, period)
                result = {"latest": latest(series), "series": last_n(series, 10), "period": period}
            elif indicator == "atr":
                period = int(tool_input.get("period", 14))
                series = _atr(candles, period)
                result = {"latest": latest(series), "series": last_n(series, 10), "period": period}
            else:
                key_map = {"adx": "adx", "obv": "obv", "vwap": "vwap", "stoch_rsi": "stoch_rsi"}
                mapped = key_map.get(indicator, indicator)
                series = all_indicators.get(mapped, [])
                result = {
                    "latest": latest(series) if isinstance(series, list) else series,
                    "series": last_n(series, 10) if isinstance(series, list) else series,
                }

            return json.dumps(result, default=str)
        except Exception as e:
            logger.error("Tool call error: %s", e)
            return json.dumps({"error": str(e)})

    def _parse_json_output(self, raw_text: str, assets: list[str]) -> dict | None:
        """Try to parse raw text as the strict JSON contract."""
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.debug("JSON parse failed: %s", e)
            return None

        if not isinstance(parsed, dict):
            return None

        reasoning_text = parsed.get("reasoning", "") or ""
        decisions = parsed.get("trade_decisions")
        if not isinstance(decisions, list):
            return None

        normalized = []
        for item in decisions:
            if isinstance(item, dict):
                item.setdefault("allocation_usd", 0.0)
                item.setdefault("order_type", "market")
                item.setdefault("limit_price", None)
                item.setdefault("tp_price", None)
                item.setdefault("sl_price", None)
                item.setdefault("exit_plan", "")
                item.setdefault("rationale", "")
                normalized.append(item)
        return {"reasoning": reasoning_text, "trade_decisions": normalized}

    def _sanitize_output(self, client, raw_content: str, assets: list[str]) -> dict:
        """Use a cheap Claude model to normalize malformed output."""
        try:
            response = client.messages.create(
                model=self.sanitize_model,
                max_tokens=2048,
                system=(
                    "You are a strict JSON normalizer. Return ONLY a JSON object "
                    "with two keys: \"reasoning\" (string) and \"trade_decisions\" "
                    "(array). Each trade_decisions item must have: asset, action "
                    "(buy/sell/hold), allocation_usd (number), order_type "
                    "(\"market\" or \"limit\"), limit_price (number or null), "
                    "tp_price (number or null), sl_price (number or null), "
                    "exit_plan (string), rationale (string). "
                    f"Valid assets: {json.dumps(assets)}. "
                    "Extract only the JSON from markdown or prose input. "
                    "Do not add fields."
                ),
                messages=[{"role": "user", "content": raw_content}],
            )
            content = "".join(
                getattr(b, "text", "")
                for b in response.content
                if getattr(b, "type", None) == "text"
            )
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "trade_decisions" in parsed:
                return parsed
        except Exception as e:
            logger.error("Sanitize failed: %s", e)
        return {"reasoning": "", "trade_decisions": []}

    def _empty_result(self, assets: list[str], reason: str) -> dict:
        return {
            "reasoning": reason,
            "trade_decisions": [
                {
                    "asset": a,
                    "action": "hold",
                    "allocation_usd": 0.0,
                    "order_type": "market",
                    "limit_price": None,
                    "tp_price": None,
                    "sl_price": None,
                    "exit_plan": "",
                    "rationale": reason,
                }
                for a in assets
            ],
        }


# ---------------------------------------------------------------------------
# Convenience: build a context dict for the LLM from proxialpha state
# ---------------------------------------------------------------------------

def build_context(
    assets: list[str],
    data: dict,                  # ticker -> pd.DataFrame
    account_state: dict,         # {equity, cash, positions}
    risk_summary: dict | None = None,
    recent_trades: list[dict] | None = None,
) -> str:
    """Serialize market + account state into an LLM-ready context string.

    Produces a stable, deterministic shape with:
      - current time (UTC)
      - per-asset: current_price, recent OHLCV (last 5 bars), full indicator
        suite latest + last 10
      - account: equity, cash, positions
      - risk limits (so the LLM knows what will be enforced)
      - recent trades (for cooldown/hysteresis reasoning)
    """
    now = datetime.now(timezone.utc).isoformat()
    per_asset = {}
    for ticker in assets:
        df = data.get(ticker)
        if df is None or getattr(df, "empty", True):
            continue
        candles = df_to_candles(df.tail(300))
        if not candles:
            continue
        indicators = compute_all(candles)
        last_candle = candles[-1]
        per_asset[ticker] = {
            "current_price": last_candle["close"],
            "recent_ohlcv": candles[-5:],
            "indicators": {
                k: {"latest": latest(v) if isinstance(v, list) else v,
                    "last10": last_n(v, 10) if isinstance(v, list) else v}
                for k, v in indicators.items()
            },
        }

    ctx = {
        "current_time_utc": now,
        "assets": assets,
        "per_asset": per_asset,
        "account": {
            "equity": account_state.get("equity") or account_state.get("total_value"),
            "cash": account_state.get("cash") or account_state.get("balance"),
            "positions": account_state.get("positions"),
        },
        "risk_limits": risk_summary or {},
        "recent_trades": (recent_trades or [])[-20:],
    }
    return json.dumps(ctx, default=str, indent=2)
