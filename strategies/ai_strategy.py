"""
AIStrategy — thin wrapper around `core/ai_decision_maker.AIDecisionMaker`.

This is the ProxiAlpha-facing adapter that:
  1. Implements the ``BaseStrategy`` ABC so it slots into ``StrategyManager``.
  2. Delegates all the hard work (tool-calling, JSON parsing, hysteresis
     prompting, sanitizer fallback) to ``AIDecisionMaker``.
  3. Translates the decision maker's ``trade_decisions[]`` output into
     ``StrategySignal`` objects so the consensus engine can weight it
     alongside the 13 existing rule-based strategies.
  4. Retains the legacy ``inject_signal`` / ``inject_signals_from_json``
     hooks so the FastAPI layer and test harness keep working.

The previous stub that directly hand-rolled its own Claude call has been
removed — it's now handled by ``core/ai_decision_maker.py``.
"""
from __future__ import annotations

import json
import logging
import pandas as pd
from typing import Any

from strategies.base import BaseStrategy, StrategySignal, SignalType
from core.indicators import df_to_candles

logger = logging.getLogger(__name__)


# Map string signals from the decision maker to the SignalType enum.
_SIGNAL_MAP = {
    "BUY": SignalType.BUY,
    "SELL": SignalType.SELL,
    "HOLD": SignalType.HOLD,
    "STRONG_BUY": SignalType.STRONG_BUY,
    "STRONG_SELL": SignalType.STRONG_SELL,
    # Handle the hyperliquid-style action verbs the decision maker uses.
    "LONG": SignalType.BUY,
    "SHORT": SignalType.SELL,
    "OPEN_LONG": SignalType.STRONG_BUY,
    "OPEN_SHORT": SignalType.STRONG_SELL,
    "CLOSE": SignalType.HOLD,
    "NOTHING": SignalType.HOLD,
}


def _coerce_signal(action: str, confidence: float | None = None) -> SignalType:
    """Coerce a free-form LLM action string into a SignalType."""
    if not action:
        return SignalType.HOLD
    key = action.strip().upper().replace(" ", "_")
    st = _SIGNAL_MAP.get(key, SignalType.HOLD)
    # If the model said BUY/SELL with very high confidence, upgrade to STRONG_*
    if confidence is not None and confidence >= 0.8:
        if st == SignalType.BUY:
            return SignalType.STRONG_BUY
        if st == SignalType.SELL:
            return SignalType.STRONG_SELL
    return st


class AIStrategy(BaseStrategy):
    """
    Strategy that delegates to ``AIDecisionMaker`` for Claude-powered signals.

    Params
    ------
    model : str
        Anthropic model ID. Defaults to ``claude-sonnet-4-6``.
    sanitize_model : str
        Cheaper model used only when the primary response is malformed JSON.
    api_key : str | None
        If unset, falls back to ``ANTHROPIC_API_KEY`` env var.
    use_api : bool
        If False, the strategy only returns manually injected signals (test mode).
    max_tokens : int
        Max tokens for the primary call.
    enable_tools : bool
        Whether to enable the ``fetch_indicator`` tool. Turn off for pure
        zero-tool-use providers.
    """

    def __init__(self, weight: float = 1.0, params: dict | None = None):
        defaults = {
            "model": "claude-sonnet-4-6",
            "sanitize_model": "claude-haiku-4-5-20251001",
            "api_key": None,
            "use_api": False,
            "max_tokens": 4096,
            "enable_tools": True,
        }
        if params:
            defaults.update(params)
        super().__init__("AI_Claude", weight, defaults)
        self._pending_signals: list[StrategySignal] = []
        self._decision_maker = None
        # Keep a small cache of the last DataFrame per ticker so the
        # CandleProvider tool can serve fetch_indicator requests mid-inference.
        self._candle_cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # Legacy compatibility: manual signal injection (tests, webhook layer)
    # ------------------------------------------------------------------

    def inject_signal(self, signal: StrategySignal) -> None:
        self._pending_signals.append(signal)

    def inject_signals_from_json(self, json_signals: list[dict]) -> None:
        for s in json_signals:
            self._pending_signals.append(StrategySignal(
                ticker=s["ticker"],
                signal=_SIGNAL_MAP.get(s.get("signal", "HOLD"), SignalType.HOLD),
                confidence=s.get("confidence", 0.5),
                strategy_name=self.name,
                price=s.get("price", 0),
                target_price=s.get("target_price"),
                stop_loss=s.get("stop_loss"),
                position_size_pct=s.get("position_size_pct", 0.05),
                reasoning=s.get("reasoning", "AI generated signal"),
            ))

    # ------------------------------------------------------------------
    # Decision-maker wiring
    # ------------------------------------------------------------------

    def _init_decision_maker(self):
        if self._decision_maker is not None:
            return self._decision_maker
        try:
            from core.ai_decision_maker import AIDecisionMaker
        except ImportError as e:
            logger.error("Failed to import AIDecisionMaker: %s", e)
            return None

        def candle_provider(ticker: str, interval: str = "1d", limit: int = 200):
            # Serve from cache first; callers can prime the cache via
            # `self._candle_cache[ticker] = df` before invoking generate_signals.
            df = self._candle_cache.get(ticker)
            if df is None or df.empty:
                return []
            return df_to_candles(df.tail(limit))

        self._decision_maker = AIDecisionMaker(
            model=self.params["model"],
            sanitize_model=self.params["sanitize_model"],
            candle_provider=candle_provider,
            api_key=self.params.get("api_key"),
            max_tokens=self.params.get("max_tokens", 4096),
            enable_tools=self.params.get("enable_tools", True),
        )
        return self._decision_maker

    def _decision_to_signal(self, ticker: str, decision: dict, price: float) -> StrategySignal:
        """Convert one ``trade_decision`` dict from AIDecisionMaker to a StrategySignal."""
        action = decision.get("action") or decision.get("signal") or "HOLD"
        confidence = float(decision.get("confidence", 0.5) or 0.5)
        signal = _coerce_signal(action, confidence)

        size_pct = decision.get("position_size_pct")
        if size_pct is None:
            # Hyperliquid-style decisions use "size" as USD notional; normalize
            size_pct = 0.05
        size_pct = max(0.01, min(float(size_pct), 0.25))

        exit_plan = decision.get("exit_plan") or {}
        tp = decision.get("take_profit") or decision.get("target_price") or exit_plan.get("take_profit")
        sl = decision.get("stop_loss") or exit_plan.get("stop_loss")

        reasoning = decision.get("rationale") or decision.get("reasoning") or "AI generated signal"
        # Include exit_plan details in metadata so the execution layer can honor cooldowns.
        metadata = {
            "exit_plan": exit_plan,
            "model": self.params.get("model"),
            "raw": decision,
        }

        return StrategySignal(
            ticker=ticker,
            signal=signal,
            confidence=confidence,
            strategy_name=self.name,
            price=price,
            target_price=float(tp) if tp else None,
            stop_loss=float(sl) if sl else None,
            position_size_pct=size_pct,
            reasoning=str(reasoning)[:500],
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # BaseStrategy implementation
    # ------------------------------------------------------------------

    def generate_signals(
        self, ticker: str, df: pd.DataFrame, portfolio: dict | None = None
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []

        # 1) Drain any manually-injected signals for this ticker.
        ticker_signals = [s for s in self._pending_signals if s.ticker == ticker]
        signals.extend(ticker_signals)
        self._pending_signals = [s for s in self._pending_signals if s.ticker != ticker]

        # 2) Live LLM call (if enabled).
        if not self.params.get("use_api"):
            return signals

        dm = self._init_decision_maker()
        if dm is None:
            return signals

        # Prime the candle cache so fetch_indicator tool calls resolve.
        self._candle_cache[ticker] = df

        try:
            current_price = float(df["Close"].iloc[-1])
        except Exception:
            current_price = 0.0

        # Build a compact context string for the decision maker.
        from core.ai_decision_maker import build_context
        context = build_context(
            assets=[ticker],
            data={ticker: df_to_candles(df.tail(200))},
            account_state=portfolio or {},
            risk_summary=None,
            recent_trades=None,
        )

        try:
            result = dm.decide_trade(assets=[ticker], context=context)
        except Exception as e:
            logger.error("AIDecisionMaker.decide_trade failed for %s: %s", ticker, e)
            return signals

        for decision in result.get("trade_decisions", []):
            # Only accept decisions that match this ticker (the decision maker
            # may batch multiple assets; the BaseStrategy contract is per-ticker).
            tk = decision.get("ticker") or decision.get("asset") or ticker
            if tk != ticker:
                continue
            signals.append(self._decision_to_signal(ticker, decision, current_price))

        return signals


# ---------------------------------------------------------------------------
# Webhook adapter — kept for backwards compatibility with api/server.py
# ---------------------------------------------------------------------------


class AIStrategyWebhook:
    """
    Webhook handler for receiving AI signals from an external scheduler.

    Example FastAPI endpoint:

        @app.post("/ai/signals")
        async def receive_signals(payload: dict):
            webhook = AIStrategyWebhook(ai_strategy)
            return webhook.process_payload(payload)
    """

    def __init__(self, ai_strategy: AIStrategy):
        self.strategy = ai_strategy

    def process_payload(self, payload: dict) -> dict:
        if "signals" in payload:
            self.strategy.inject_signals_from_json(payload["signals"])

        if "param_updates" in payload:
            self.strategy.update_params(payload["param_updates"])

        rule_updates = payload.get("rule_updates")
        return {
            "status": "ok",
            "signals_queued": len(payload.get("signals", [])),
            "rule_updates": rule_updates,
        }
