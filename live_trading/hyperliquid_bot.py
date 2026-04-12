"""
Hyperliquid broker adapter for ProxiAlpha.

Implements ``BrokerProtocol`` so ``StrategyManager`` / the AI decision maker
can route perp + HIP-3 tradfi trades through the same interface as Alpaca
equities.

Design notes
------------
* **Optional dependency** — ``hyperliquid-python-sdk`` and ``eth-account``
  are imported lazily inside ``_init_api``. Users who only trade equities
  shouldn't have to install them.
* **Sync facade over async SDK** — The original Hyperliquid agent is
  async-first; ProxiAlpha is currently sync. We keep both worlds happy by
  running the async calls through ``asyncio.run`` in the sync methods. If/
  when ProxiAlpha goes async-first, callers can reach into the ``_async``
  helpers directly.
* **Agent wallet pattern** — signer key trades, vault holds funds. See
  ``HYPERLIQUID_INTEGRATION_ANALYSIS.md`` section 1.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_hl_config() -> dict:
    """Pull Hyperliquid settings from env first, then config_trading.yaml."""
    cfg = {
        "private_key": os.getenv("HL_AGENT_KEY") or os.getenv("HYPERLIQUID_PRIVATE_KEY"),
        "vault_address": os.getenv("HL_VAULT") or os.getenv("HYPERLIQUID_VAULT_ADDRESS"),
        "network": os.getenv("HYPERLIQUID_NETWORK", "mainnet"),
        "base_url": os.getenv("HYPERLIQUID_BASE_URL"),
    }
    try:
        import yaml  # type: ignore
        from pathlib import Path

        path = Path("config_trading.yaml")
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
            hl = (data.get("brokers", {}) or {}).get("hyperliquid", {}) or {}
            for k, v in hl.items():
                if v and not cfg.get(k):
                    cfg[k] = v
    except Exception as e:
        logger.debug("Skipping yaml config load: %s", e)
    return cfg


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class HyperliquidLiveTrader:
    """
    Hyperliquid broker adapter.

    Implements ``BrokerProtocol`` — see ``live_trading/broker_protocol.py``.
    """

    name = "hyperliquid"
    asset_classes = ["perp", "hip3"]

    # Map canonical ProxiAlpha intervals to Hyperliquid's candle intervals.
    _INTERVAL_MAP = {
        "1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h",
        "4h": "4h", "1d": "1d", "1wk": "1w",
    }

    def __init__(
        self,
        private_key: str | None = None,
        vault_address: str | None = None,
        network: str = "mainnet",
        base_url: str | None = None,
        max_daily_trades: int = 20,
    ) -> None:
        cfg = _load_hl_config()
        self.private_key = private_key or cfg.get("private_key")
        self.vault_address = vault_address or cfg.get("vault_address")
        self.network = (network or cfg.get("network") or "mainnet").lower()
        self.base_url = base_url or cfg.get("base_url")
        self.max_daily_trades = max_daily_trades
        self.daily_trade_count = 0
        self.trade_log: list[dict] = []

        self._api = None
        self._meta_cache: Any = None
        self._hip3_meta_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lazy init
    # ------------------------------------------------------------------

    def _init_api(self) -> bool:
        if self._api is not None:
            return True
        try:
            from hyperliquid.exchange import Exchange  # type: ignore
            from hyperliquid.info import Info  # type: ignore
            from hyperliquid.utils import constants  # type: ignore
            from eth_account import Account  # type: ignore
        except ImportError:
            logger.warning(
                "hyperliquid-python-sdk not installed; "
                "install with `pip install hyperliquid-python-sdk eth-account` "
                "to use HyperliquidLiveTrader."
            )
            return False

        if not self.private_key:
            logger.error("HyperliquidLiveTrader: HL_AGENT_KEY / private_key not set")
            return False

        wallet = Account.from_key(self.private_key)

        if not self.base_url:
            if self.network == "testnet":
                self.base_url = getattr(
                    constants, "TESTNET_API_URL", constants.MAINNET_API_URL
                )
            else:
                self.base_url = constants.MAINNET_API_URL

        self._info = Info(self.base_url)
        self._exchange = Exchange(
            wallet, self.base_url, account_address=self.vault_address
        )
        self._query_address = self.vault_address or wallet.address
        self._api = {"info": self._info, "exchange": self._exchange}
        return True

    # ------------------------------------------------------------------
    # Async retry helper (identical semantics to the original HL agent)
    # ------------------------------------------------------------------

    async def _retry(
        self,
        fn,
        *args,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        to_thread: bool = True,
        **kwargs,
    ):
        last_err: Exception | None = None
        for attempt in range(max_attempts):
            try:
                if to_thread:
                    return await asyncio.to_thread(fn, *args, **kwargs)
                return await fn(*args, **kwargs)
            except Exception as e:
                last_err = e
                logger.warning(
                    "HL call failed (attempt %s/%s): %s", attempt + 1, max_attempts, e
                )
                await asyncio.sleep(backoff_base * (2 ** attempt))
        if last_err:
            raise last_err
        raise RuntimeError("HL retry: unknown error")

    def _run_async(self, coro):
        """Run an async coroutine from a sync caller."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Inside a running loop (e.g. FastAPI) — schedule on a fresh loop.
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
        except RuntimeError:
            pass
        return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Metadata / rounding
    # ------------------------------------------------------------------

    def _round_size(self, asset: str, amount: float) -> float:
        meta = self._meta_cache[0] if self._meta_cache else None
        if meta:
            universe = meta.get("universe", [])
            info = next((u for u in universe if u.get("name") == asset), None)
            if info:
                return round(amount, info.get("szDecimals", 8))
        if ":" in asset:
            dex = asset.split(":")[0]
            dex_data = self._hip3_meta_cache.get(dex)
            if dex_data and isinstance(dex_data, list) and dex_data:
                universe = dex_data[0].get("universe", [])
                info = next((u for u in universe if u.get("name") == asset), None)
                if info:
                    return round(amount, info.get("szDecimals", 8))
        return round(amount, 8)

    async def _ensure_meta(self, dex: str | None = None) -> None:
        if dex:
            if dex not in self._hip3_meta_cache:
                resp = await self._retry(
                    lambda: self._info.post(
                        "/info", {"type": "metaAndAssetCtxs", "dex": dex}
                    )
                )
                if isinstance(resp, list):
                    self._hip3_meta_cache[dex] = resp
            return
        if not self._meta_cache:
            self._meta_cache = await self._retry(self._info.meta_and_asset_ctxs)

    # ------------------------------------------------------------------
    # BrokerProtocol: account / positions
    # ------------------------------------------------------------------

    async def _get_account_async(self) -> dict:
        state = await self._retry(
            lambda: self._info.user_state(self._query_address)
        )
        balance = float(state.get("withdrawable", 0.0))
        equity = float(state.get("accountValue", balance) or balance)
        return {
            "equity": equity,
            "cash": balance,
            "buying_power": balance,
            "portfolio_value": equity,
            "currency": "USD",
            "broker": self.name,
            "status": "ACTIVE",
            "raw": state,
        }

    def get_account(self) -> dict:
        if not self._init_api():
            return {"error": "hyperliquid SDK not available", "broker": self.name}
        try:
            return self._run_async(self._get_account_async())
        except Exception as e:
            logger.error("HL get_account failed: %s", e)
            return {"error": str(e), "broker": self.name}

    async def _get_positions_async(self) -> list[dict]:
        state = await self._retry(
            lambda: self._info.user_state(self._query_address)
        )
        out: list[dict] = []
        for wrap in state.get("assetPositions", []):
            pos = wrap.get("position", {})
            size = float(pos.get("szi", 0) or 0)
            if size == 0:
                continue
            entry = float(pos.get("entryPx", 0) or 0)
            coin = pos.get("coin", "")
            try:
                current = await self._get_current_price_async(coin)
            except Exception:
                current = entry
            side_long = size > 0
            pnl = (current - entry) * abs(size) if side_long else (entry - current) * abs(size)
            pnl_pct = (pnl / (abs(size) * entry) * 100) if entry and size else 0.0
            out.append({
                "ticker": coin,
                "shares": size,
                "avg_cost": entry,
                "current_price": current,
                "market_value": abs(size) * current,
                "unrealized_pnl": pnl,
                "unrealized_pnl_pct": pnl_pct,
                "leverage": float(pos.get("leverage", {}).get("value", 1)) if isinstance(pos.get("leverage"), dict) else 1.0,
                "broker": self.name,
            })
        return out

    def get_positions(self) -> list[dict]:
        if not self._init_api():
            return []
        try:
            return self._run_async(self._get_positions_async())
        except Exception as e:
            logger.error("HL get_positions failed: %s", e)
            return []

    async def _get_current_price_async(self, asset: str) -> float:
        if ":" in asset:
            dex = asset.split(":")[0]
            mids = await self._retry(
                lambda: self._info.post("/info", {"type": "allMids", "dex": dex})
            )
        else:
            mids = await self._retry(self._info.all_mids)
        return float(mids.get(asset, 0.0))

    # ------------------------------------------------------------------
    # BrokerProtocol: orders
    # ------------------------------------------------------------------

    async def _submit_order_async(
        self,
        ticker: str,
        qty: float,
        side: str,
        order_type: str,
        limit_price: float | None,
        slippage: float = 0.01,
    ) -> dict:
        dex = ticker.split(":")[0] if ":" in ticker else None
        await self._ensure_meta(dex)
        qty = self._round_size(ticker, abs(qty))
        is_buy = side.lower() == "buy"
        if order_type == "market":
            result = await self._retry(
                lambda: self._exchange.market_open(ticker, is_buy, qty, None, slippage)
            )
        elif order_type == "limit":
            if limit_price is None:
                raise ValueError("limit_price required for limit order")
            ot = {"limit": {"tif": "Gtc"}}
            result = await self._retry(
                lambda: self._exchange.order(ticker, is_buy, qty, limit_price, ot)
            )
        else:
            raise ValueError(f"Unsupported order_type: {order_type}")
        return result

    def submit_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str = "day",
        **kwargs,
    ) -> dict:
        if not self._init_api():
            return {"error": "hyperliquid SDK not available", "broker": self.name}
        if self.daily_trade_count >= self.max_daily_trades:
            return {"error": f"Daily trade limit reached ({self.max_daily_trades})", "broker": self.name}
        try:
            raw = self._run_async(
                self._submit_order_async(ticker, qty, side, order_type, limit_price)
            )
            self.daily_trade_count += 1
            oids = self._extract_oids(raw)
            result = {
                "order_id": oids[0] if oids else "",
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "type": order_type,
                "status": raw.get("status", "submitted") if isinstance(raw, dict) else "submitted",
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "broker": self.name,
                "raw": raw,
            }
            self.trade_log.append(result)
            return result
        except Exception as e:
            logger.error("HL submit_order failed: %s", e)
            return {"error": str(e), "broker": self.name}

    @staticmethod
    def _extract_oids(result: Any) -> list[int]:
        oids: list[int] = []
        try:
            statuses = result["response"]["data"]["statuses"]
            for st in statuses:
                if "resting" in st and "oid" in st["resting"]:
                    oids.append(st["resting"]["oid"])
                if "filled" in st and "oid" in st["filled"]:
                    oids.append(st["filled"]["oid"])
        except (KeyError, TypeError, ValueError):
            pass
        return oids

    async def _close_position_async(self, ticker: str) -> dict:
        positions = await self._get_positions_async()
        match = next((p for p in positions if p["ticker"] == ticker), None)
        if not match:
            return {"status": "no_position", "ticker": ticker}
        qty = abs(match["shares"])
        is_long = match["shares"] > 0
        dex = ticker.split(":")[0] if ":" in ticker else None
        await self._ensure_meta(dex)
        qty = self._round_size(ticker, qty)
        # Market-close on the opposite side
        return await self._retry(
            lambda: self._exchange.market_open(ticker, not is_long, qty, None, 0.01)
        )

    def close_position(self, ticker: str) -> dict:
        if not self._init_api():
            return {"error": "hyperliquid SDK not available", "broker": self.name}
        try:
            raw = self._run_async(self._close_position_async(ticker))
            self.daily_trade_count += 1
            return {
                "ticker": ticker,
                "side": "close",
                "status": "submitted",
                "broker": self.name,
                "raw": raw,
            }
        except Exception as e:
            logger.error("HL close_position failed: %s", e)
            return {"error": str(e), "broker": self.name}

    # ------------------------------------------------------------------
    # BrokerProtocol: candles
    # ------------------------------------------------------------------

    async def _get_candles_async(
        self, ticker: str, interval: str, limit: int
    ) -> list[dict]:
        hl_interval = self._INTERVAL_MAP.get(interval, interval)
        interval_ms_map = {
            "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
            "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000, "1w": 604_800_000,
        }
        interval_ms = interval_ms_map.get(hl_interval, 300_000)
        end_time = int(time.time() * 1000)
        start_time = end_time - (limit * interval_ms)

        if ":" in ticker:
            raw = await self._retry(
                lambda: self._info.post("/info", {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": ticker, "interval": hl_interval,
                        "startTime": start_time, "endTime": end_time,
                    },
                })
            )
        else:
            raw = await self._retry(
                lambda: self._info.candles_snapshot(ticker, hl_interval, start_time, end_time)
            )
        return [
            {
                "t": c.get("t"),
                "open": float(c.get("o", 0)),
                "high": float(c.get("h", 0)),
                "low": float(c.get("l", 0)),
                "close": float(c.get("c", 0)),
                "volume": float(c.get("v", 0)),
            }
            for c in (raw or [])
        ]

    def get_candles(
        self, ticker: str, interval: str = "1d", limit: int = 200
    ) -> list[dict]:
        if not self._init_api():
            return []
        try:
            return self._run_async(self._get_candles_async(ticker, interval, limit))
        except Exception as e:
            logger.error("HL get_candles failed for %s: %s", ticker, e)
            return []

    # ------------------------------------------------------------------
    # Convenience: plug this broker into AIDecisionMaker as a CandleProvider
    # ------------------------------------------------------------------

    def as_candle_provider(self):
        """Return a (ticker, interval, limit) -> candles callable."""
        def _provider(ticker: str, interval: str = "1d", limit: int = 200):
            return self.get_candles(ticker, interval, limit)

        return _provider
