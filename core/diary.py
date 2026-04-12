"""
Trade & decision diary (ported from hyperliquid-trading-agent).

Append-only JSONL log of every trading-relevant event:
  - trade submitted / executed
  - risk manager rejection
  - force-close due to losing position
  - LLM decision (reasoning + decisions)
  - reconciliation (stale active trade removed)

The diary is the "exchange-as-source-of-truth" debugging loop. When the AI
strategy is turned on, it becomes indispensable for explaining why a trade
was made, rejected, or unwound.

Also logs raw LLM request/response traffic to a separate file
(`llm_requests.log`) so you can debug Claude's tool calls without spelunking
through Python logs.

Both files live in `data/` by default and are served via the API at
`/api/diary` and `/api/llm-logs`.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger(__name__)


DEFAULT_DIARY_PATH = "data/diary.jsonl"
DEFAULT_LLM_LOG_PATH = "data/llm_requests.log"


class Diary:
    """Append-only JSONL diary of all trading events."""

    def __init__(self, path: str | Path = DEFAULT_DIARY_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def log(self, event_type: str, **payload: Any) -> dict:
        """Append an event. Returns the serialized entry."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **payload,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("Failed to write diary entry: %s", e)
        return entry

    # Convenience helpers for common events -----------------------------

    def log_decision(self, reasoning: str, decisions: list[dict], source: str = "ai") -> dict:
        return self.log(
            "decision",
            source=source,
            reasoning=reasoning,
            decisions=decisions,
        )

    def log_trade_submitted(self, broker: str, trade: dict) -> dict:
        return self.log("trade_submitted", broker=broker, trade=trade)

    def log_trade_executed(self, broker: str, trade: dict, result: dict) -> dict:
        return self.log("trade_executed", broker=broker, trade=trade, result=result)

    def log_trade_rejected(self, trade: dict, reason: str) -> dict:
        return self.log("trade_rejected", trade=trade, reason=reason)

    def log_force_close(self, position: dict, reason: str) -> dict:
        return self.log("force_close", position=position, reason=reason)

    def log_reconciliation(self, symbol: str, detail: str) -> dict:
        return self.log("reconciliation", symbol=symbol, detail=detail)

    def log_risk_event(self, event: str, detail: str, **extra: Any) -> dict:
        return self.log("risk_event", risk_event=event, detail=detail, **extra)

    # Readers -----------------------------------------------------------

    def read(self, limit: int = 200, event_filter: str | None = None) -> list[dict]:
        """Read the most recent ``limit`` entries, newest first.

        Args:
            limit: max entries to return
            event_filter: optional event_type to filter on
        """
        if not self.path.exists():
            return []
        out: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            logger.error("Failed to read diary: %s", e)
            return []

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_filter and entry.get("event") != event_filter:
                continue
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def clear(self) -> None:
        """Truncate the diary (mostly for tests)."""
        self.path.write_text("")


class LLMRequestLog:
    """Append-only plain-text log of LLM request/response traffic.

    Kept separate from the structured JSONL diary so you can tail it easily.
    """

    def __init__(self, path: str | Path = DEFAULT_LLM_LOG_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def log_request(self, model: str, messages: list[dict]) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"\n\n=== {datetime.now(timezone.utc).isoformat()} ===\n")
                f.write(f"Model: {model}\n")
                f.write(f"Messages count: {len(messages)}\n")
                if messages:
                    last = messages[-1]
                    content_str = str(last.get("content", ""))[:800]
                    f.write(f"Last role: {last.get('role')}\n")
                    f.write(f"Last content (truncated): {content_str}\n")
        except Exception as e:
            logger.error("Failed to write llm request log: %s", e)

    def log_response(self, stop_reason: str, usage: dict | Any) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"Response stop_reason: {stop_reason}\n")
                try:
                    f.write(
                        f"Usage: input={getattr(usage, 'input_tokens', None)}, "
                        f"output={getattr(usage, 'output_tokens', None)}\n"
                    )
                except Exception:
                    f.write(f"Usage: {usage}\n")
        except Exception as e:
            logger.error("Failed to write llm response log: %s", e)

    def log_error(self, message: str) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"ERROR: {message}\n")
        except Exception as e:
            logger.error("Failed to write llm error: %s", e)

    def tail(self, n_bytes: int = 20000) -> str:
        """Return the last ``n_bytes`` of the log as a string."""
        if not self.path.exists():
            return ""
        try:
            size = self.path.stat().st_size
            with open(self.path, "rb") as f:
                if size > n_bytes:
                    f.seek(size - n_bytes)
                data = f.read()
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            logger.error("Failed to tail llm log: %s", e)
            return ""


# ---------------------------------------------------------------------------
# Module-level singletons (convenient for quick imports)
# ---------------------------------------------------------------------------

_diary: Diary | None = None
_llm_log: LLMRequestLog | None = None


def get_diary(path: str | Path = DEFAULT_DIARY_PATH) -> Diary:
    global _diary
    if _diary is None or str(_diary.path) != str(path):
        _diary = Diary(path)
    return _diary


def get_llm_log(path: str | Path = DEFAULT_LLM_LOG_PATH) -> LLMRequestLog:
    global _llm_log
    if _llm_log is None or str(_llm_log.path) != str(path):
        _llm_log = LLMRequestLog(path)
    return _llm_log
