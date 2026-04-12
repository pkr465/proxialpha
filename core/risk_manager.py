"""
Centralized risk management (ported from hyperliquid-trading-agent/src/risk_manager.py).

This module provides a single authoritative gate every trade must pass through.
It replaces the previously-scattered risk rules that lived across YAML +
backtester + paper trader + alpaca bot.

Asset-class agnostic: works for equities (Alpaca), crypto perps (Hyperliquid),
and paper/backtest simulations. Feed it a generic `ProposedTrade` +
`AccountState` and it returns `(allowed, reason, adjusted_trade)`.

Hard-coded guards (LLM or strategy cannot override):
  1. check_daily_drawdown     — daily loss circuit breaker
  2. check_balance_reserve    — preserve initial capital floor
  3. check_position_size      — max % of account per single position
  4. check_total_exposure     — sum of notionals cap
  5. check_leverage           — effective leverage cap (perps / margin)
  6. check_concurrent_positions
  7. enforce_stop_loss        — auto-set if missing
  8. check_losing_positions   — force-close at max loss %

All checks return (ok: bool, reason: str). `validate_trade()` composes them.

Config is loaded from `config_trading.yaml` if present, else from sensible
defaults that match the existing ProxiAlpha behavior.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults — these mirror config_trading.yaml but are safe fallbacks if YAML
# is missing or a key is not configured.
# ---------------------------------------------------------------------------

DEFAULTS = {
    "max_position_pct": 10.0,              # single position <= 10% of account
    "max_loss_per_position_pct": 20.0,     # force-close at 20% loss
    "max_leverage": 10.0,                  # effective leverage cap (perps)
    "max_total_exposure_pct": 80.0,        # sum of notionals cap
    "daily_loss_circuit_breaker_pct": 15.0,
    "mandatory_sl_pct": 8.0,               # ProxiAlpha default SL
    "max_concurrent_positions": 10,
    "min_balance_reserve_pct": 10.0,
    "min_order_usd": 100.0,                # Alpaca minimum (Hyperliquid: 10)
}


def _load_risk_config(config_path: str | Path | None = None) -> dict:
    """Load risk params from config_trading.yaml with graceful fallback."""
    if not _HAS_YAML:
        return dict(DEFAULTS)

    if config_path is None:
        # Look for config_trading.yaml two directories up from this file
        here = Path(__file__).resolve().parent.parent
        config_path = here / "config_trading.yaml"

    config_path = Path(config_path)
    if not config_path.exists():
        return dict(DEFAULTS)

    try:
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Failed to parse %s: %s — using defaults", config_path, e)
        return dict(DEFAULTS)

    merged = dict(DEFAULTS)

    # Map YAML paths into flat risk keys
    position = cfg.get("position_sizing", {}) or {}
    if "max_position_pct" in position:
        merged["max_position_pct"] = float(position["max_position_pct"]) * 100 if position["max_position_pct"] <= 1 else float(position["max_position_pct"])

    risk = cfg.get("risk", {}) or {}
    if "max_leverage" in risk:
        merged["max_leverage"] = float(risk["max_leverage"])
    if "max_portfolio_drawdown_pct" in risk:
        merged["daily_loss_circuit_breaker_pct"] = float(risk["max_portfolio_drawdown_pct"])
    if "max_concurrent_positions" in risk:
        merged["max_concurrent_positions"] = int(risk["max_concurrent_positions"])
    if "trailing_stop_pct" in risk:
        merged["mandatory_sl_pct"] = float(risk["trailing_stop_pct"]) * 100 if risk["trailing_stop_pct"] <= 1 else float(risk["trailing_stop_pct"])

    portfolio = cfg.get("portfolio", {}) or {}
    if "cash_reserve_pct" in portfolio:
        merged["min_balance_reserve_pct"] = float(portfolio["cash_reserve_pct"]) * 100 if portfolio["cash_reserve_pct"] <= 1 else float(portfolio["cash_reserve_pct"])

    # Explicit risk_manager section takes highest precedence — these keys
    # match the DEFAULTS dict exactly and override everything above.
    rm_section = cfg.get("risk_manager", {}) or {}
    for key in DEFAULTS.keys():
        if key in rm_section:
            merged[key] = rm_section[key]

    return merged


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """Enforces risk limits on every trade before execution.

    Usage:
        rm = RiskManager()
        ok, reason, adjusted = rm.validate_trade(trade, account_state, initial_balance)
        if not ok:
            logger.warning("Trade rejected: %s", reason)
            return
        execute(adjusted)

    The LLM/strategy cannot override these limits — they are hard-coded checks
    applied before every trade execution.
    """

    def __init__(self, config: dict | None = None, config_path: str | Path | None = None):
        if config is None:
            config = _load_risk_config(config_path)

        self.max_position_pct = float(config.get("max_position_pct", DEFAULTS["max_position_pct"]))
        self.max_loss_per_position_pct = float(config.get("max_loss_per_position_pct", DEFAULTS["max_loss_per_position_pct"]))
        self.max_leverage = float(config.get("max_leverage", DEFAULTS["max_leverage"]))
        self.max_total_exposure_pct = float(config.get("max_total_exposure_pct", DEFAULTS["max_total_exposure_pct"]))
        self.daily_loss_circuit_breaker_pct = float(config.get("daily_loss_circuit_breaker_pct", DEFAULTS["daily_loss_circuit_breaker_pct"]))
        self.mandatory_sl_pct = float(config.get("mandatory_sl_pct", DEFAULTS["mandatory_sl_pct"]))
        self.max_concurrent_positions = int(config.get("max_concurrent_positions", DEFAULTS["max_concurrent_positions"]))
        self.min_balance_reserve_pct = float(config.get("min_balance_reserve_pct", DEFAULTS["min_balance_reserve_pct"]))
        self.min_order_usd = float(config.get("min_order_usd", DEFAULTS["min_order_usd"]))

        # Daily tracking (circuit breaker)
        self.daily_high_value: float | None = None
        self.daily_high_date = None
        self.circuit_breaker_active = False
        self.circuit_breaker_date = None

    # ------------------------------------------------------------------
    # Daily high-watermark tracking
    # ------------------------------------------------------------------

    def _reset_daily_if_needed(self, account_value: float) -> None:
        """Reset daily high watermark at UTC day boundary."""
        today = datetime.now(timezone.utc).date()
        if self.daily_high_date != today:
            self.daily_high_value = account_value
            self.daily_high_date = today
            self.circuit_breaker_active = False
            self.circuit_breaker_date = None
        elif self.daily_high_value is None or account_value > self.daily_high_value:
            self.daily_high_value = account_value

    # ------------------------------------------------------------------
    # Individual checks — each returns (allowed, reason)
    # ------------------------------------------------------------------

    def check_position_size(self, alloc_usd: float, account_value: float) -> tuple[bool, str]:
        """Single position cannot exceed max_position_pct of account."""
        if account_value <= 0:
            return False, "Account value is zero or negative"
        max_alloc = account_value * (self.max_position_pct / 100.0)
        if alloc_usd > max_alloc:
            return False, (
                f"Allocation ${alloc_usd:.2f} exceeds {self.max_position_pct}% "
                f"of account (${max_alloc:.2f})"
            )
        return True, ""

    def check_total_exposure(self, positions: list[dict], new_alloc: float,
                             account_value: float) -> tuple[bool, str]:
        """Sum of all position notionals + new allocation cannot exceed cap."""
        current_exposure = 0.0
        for pos in positions:
            # Normalize: accept Hyperliquid szi/entryPx or Alpaca shares/avg_cost
            qty = abs(float(
                pos.get("quantity")
                or pos.get("shares")
                or pos.get("szi")
                or 0
            ))
            entry = float(
                pos.get("entry_price")
                or pos.get("avg_cost")
                or pos.get("entryPx")
                or 0
            )
            current_exposure += qty * entry
        total = current_exposure + new_alloc
        max_exposure = account_value * (self.max_total_exposure_pct / 100.0)
        if total > max_exposure:
            return False, (
                f"Total exposure ${total:.2f} would exceed "
                f"{self.max_total_exposure_pct}% of account (${max_exposure:.2f})"
            )
        return True, ""

    def check_leverage(self, alloc_usd: float, balance: float) -> tuple[bool, str]:
        """Effective leverage of new trade cannot exceed max_leverage."""
        if balance <= 0:
            return False, "Balance is zero or negative"
        effective_lev = alloc_usd / balance
        if effective_lev > self.max_leverage:
            return False, (
                f"Effective leverage {effective_lev:.1f}x exceeds max "
                f"{self.max_leverage}x"
            )
        return True, ""

    def check_daily_drawdown(self, account_value: float) -> tuple[bool, str]:
        """Activate circuit breaker if account drops max % from daily high."""
        self._reset_daily_if_needed(account_value)
        if self.circuit_breaker_active:
            return False, (
                "Daily loss circuit breaker is active — "
                "no new trades until tomorrow (UTC)"
            )
        if self.daily_high_value and self.daily_high_value > 0:
            drawdown_pct = ((self.daily_high_value - account_value) / self.daily_high_value) * 100
            if drawdown_pct >= self.daily_loss_circuit_breaker_pct:
                self.circuit_breaker_active = True
                self.circuit_breaker_date = datetime.now(timezone.utc).date()
                return False, (
                    f"Daily drawdown {drawdown_pct:.2f}% exceeds circuit "
                    f"breaker threshold of {self.daily_loss_circuit_breaker_pct}%"
                )
        return True, ""

    def check_concurrent_positions(self, current_count: int) -> tuple[bool, str]:
        """Limit number of simultaneous open positions."""
        if current_count >= self.max_concurrent_positions:
            return False, (
                f"Already at max concurrent positions "
                f"({self.max_concurrent_positions})"
            )
        return True, ""

    def check_balance_reserve(self, balance: float,
                              initial_balance: float) -> tuple[bool, str]:
        """Don't trade if balance falls below reserve threshold."""
        if initial_balance <= 0:
            return True, ""
        min_balance = initial_balance * (self.min_balance_reserve_pct / 100.0)
        if balance < min_balance:
            return False, (
                f"Balance ${balance:.2f} below minimum reserve "
                f"${min_balance:.2f} ({self.min_balance_reserve_pct}% of initial)"
            )
        return True, ""

    # ------------------------------------------------------------------
    # Stop-loss enforcement
    # ------------------------------------------------------------------

    def enforce_stop_loss(self, sl_price: float | None, entry_price: float,
                          is_buy: bool) -> float:
        """Ensure every trade has a stop-loss. Auto-set if missing."""
        if sl_price is not None and sl_price > 0:
            return float(sl_price)
        sl_distance = entry_price * (self.mandatory_sl_pct / 100.0)
        if is_buy:
            return round(entry_price - sl_distance, 4)
        return round(entry_price + sl_distance, 4)

    # ------------------------------------------------------------------
    # Force-close losing positions
    # ------------------------------------------------------------------

    def check_losing_positions(self, positions: list[dict]) -> list[dict]:
        """Return positions that should be force-closed due to excessive loss.

        Accepts both Hyperliquid-shaped and Alpaca-shaped position dicts.
        """
        to_close = []
        for pos in positions:
            symbol = (
                pos.get("ticker")
                or pos.get("symbol")
                or pos.get("coin")
            )
            entry_px = float(
                pos.get("avg_cost")
                or pos.get("entry_price")
                or pos.get("entryPx")
                or 0
            )
            size = float(
                pos.get("shares")
                or pos.get("quantity")
                or pos.get("szi")
                or 0
            )
            pnl = float(
                pos.get("unrealized_pnl")
                or pos.get("pnl")
                or 0
            )

            if entry_px == 0 or size == 0:
                continue

            notional = abs(size) * entry_px
            if notional == 0:
                continue

            loss_pct = abs(pnl / notional) * 100 if pnl < 0 else 0

            if loss_pct >= self.max_loss_per_position_pct:
                logger.warning(
                    "RISK: force-closing %s — loss %.2f%% exceeds max %.2f%%",
                    symbol, loss_pct, self.max_loss_per_position_pct,
                )
                to_close.append({
                    "symbol": symbol,
                    "size": abs(size),
                    "is_long": size > 0,
                    "loss_pct": round(loss_pct, 2),
                    "pnl": round(pnl, 2),
                })
        return to_close

    # ------------------------------------------------------------------
    # Composite validation — run all checks before a trade
    # ------------------------------------------------------------------

    def validate_trade(self, trade: dict, account_state: dict,
                       initial_balance: float) -> tuple[bool, str, dict]:
        """Run all safety checks on a proposed trade.

        Args:
            trade: Dict with keys:
                symbol/ticker/asset, action (buy/sell/hold), allocation_usd,
                current_price, tp_price, sl_price
            account_state: Dict with keys:
                balance, total_value, positions
            initial_balance: Starting balance for reserve check

        Returns:
            (allowed, reason, adjusted_trade)
            adjusted_trade may have modified sl_price (auto-set) or
            allocation_usd (capped to max_position_pct).
        """
        action = str(trade.get("action", "hold")).lower()
        if action == "hold":
            return True, "", trade

        alloc_usd = float(trade.get("allocation_usd", 0) or 0)
        if alloc_usd <= 0:
            return False, "Zero or negative allocation", trade

        # Enforce minimum order size (Alpaca = $100, Hyperliquid = $10)
        if alloc_usd < self.min_order_usd:
            alloc_usd = self.min_order_usd
            trade = {**trade, "allocation_usd": alloc_usd}
            logger.info(
                "RISK: bumped allocation to $%.2f (min order size)",
                self.min_order_usd,
            )

        account_value = float(account_state.get("total_value") or account_state.get("equity") or 0)
        balance = float(account_state.get("balance") or account_state.get("cash") or 0)
        positions = account_state.get("positions", [])
        if isinstance(positions, dict):
            positions = list(positions.values())
        is_buy = action == "buy"

        # 1. Daily drawdown circuit breaker
        ok, reason = self.check_daily_drawdown(account_value)
        if not ok:
            return False, reason, trade

        # 2. Balance reserve
        ok, reason = self.check_balance_reserve(balance, initial_balance)
        if not ok:
            return False, reason, trade

        # 3. Position size limit — CAP instead of reject
        ok, reason = self.check_position_size(alloc_usd, account_value)
        if not ok:
            max_alloc = account_value * (self.max_position_pct / 100.0)
            if max_alloc < self.min_order_usd:
                max_alloc = self.min_order_usd
            logger.warning(
                "RISK: capping allocation from $%.2f to $%.2f",
                alloc_usd, max_alloc,
            )
            alloc_usd = max_alloc
            trade = {**trade, "allocation_usd": alloc_usd}

        # 4. Total exposure
        ok, reason = self.check_total_exposure(positions, alloc_usd, account_value)
        if not ok:
            return False, reason, trade

        # 5. Leverage check (only relevant for margin/perps, but still gated)
        ok, reason = self.check_leverage(alloc_usd, balance)
        if not ok:
            return False, reason, trade

        # 6. Concurrent positions
        active_count = sum(
            1 for p in positions
            if abs(float(
                p.get("shares") or p.get("quantity") or p.get("szi") or 0
            )) > 0
        )
        ok, reason = self.check_concurrent_positions(active_count)
        if not ok:
            return False, reason, trade

        # 7. Enforce mandatory stop-loss
        current_price = float(trade.get("current_price", 0) or 0)
        entry_price = current_price if current_price > 0 else 1.0
        sl_price = trade.get("sl_price")
        enforced_sl = self.enforce_stop_loss(sl_price, entry_price, is_buy)
        if sl_price is None or sl_price == 0:
            logger.info(
                "RISK: auto-setting SL at %.4f (%.1f%% from entry)",
                enforced_sl, self.mandatory_sl_pct,
            )
        trade = {**trade, "sl_price": enforced_sl}

        return True, "", trade

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_risk_summary(self) -> dict[str, Any]:
        """Return current risk parameters (for inclusion in LLM context)."""
        return {
            "max_position_pct": self.max_position_pct,
            "max_loss_per_position_pct": self.max_loss_per_position_pct,
            "max_leverage": self.max_leverage,
            "max_total_exposure_pct": self.max_total_exposure_pct,
            "daily_loss_circuit_breaker_pct": self.daily_loss_circuit_breaker_pct,
            "mandatory_sl_pct": self.mandatory_sl_pct,
            "max_concurrent_positions": self.max_concurrent_positions,
            "min_balance_reserve_pct": self.min_balance_reserve_pct,
            "min_order_usd": self.min_order_usd,
            "circuit_breaker_active": self.circuit_breaker_active,
        }
