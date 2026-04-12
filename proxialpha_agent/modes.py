"""Agent mode state machine primitives.

The :class:`Mode` enum defines the six states an agent can be in at
any moment. The *transitions* themselves are owned by
:class:`proxialpha_agent.supervisor.Supervisor` — we deliberately
keep them off the enum so the state machine logic can hold richer
context (clocks, grace deadlines, diary handles) without polluting
the data type.

Mode semantics
--------------

``BOOTING``
    Initial state on process start. License has been loaded/verified
    but the first heartbeat has not yet completed. The engine MUST
    NOT send orders in this mode — brokers are not connected.

``RUNNING``
    Steady-state. Heartbeats succeed on the 60-second cadence. All
    engines (paper, live, backtesting) are allowed to operate per
    their respective feature flags and entitlements.

``OFFLINE_GRACE``
    Heartbeat has failed recently (network, 503, 429) and ``now <
    grace_until``. The engine continues running on the local cached
    license — this is the whole point of the offline grace window.
    When ``now >= grace_until`` the supervisor moves us to
    ``DEGRADED``.

``DEGRADED``
    Restricted operation. Either billing is past due (402 from the
    control plane) or the grace window has expired. Paper trading
    and backtests keep working; live trading is blocked. The
    supervisor reports this to the engine via a feature flag the
    brokers check on every order.

``REVOKED``
    The control plane returned 403 or the license signature is
    invalid. The supervisor initiates a hard stop: flush diary,
    terminate engines, exit with a non-zero code. No transition
    exits ``REVOKED`` — it is a terminal state.

``STOPPED``
    Graceful shutdown complete. The process is about to exit with
    code 0. Terminal state.

These six values exactly match the Phase 1 schema's
``agents.mode`` CHECK constraint (migration 0001) so the same
string flows unchanged from the agent to the control-plane's
``agents`` row.
"""
from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    """All legal agent modes.

    Inherits from :class:`str` so the value serialises directly
    in JSON/YAML logs, diary entries, and HTTP bodies without
    needing a custom encoder. ``Mode.RUNNING == "running"`` is
    ``True`` which keeps comparison cheap at call sites that
    receive a mode from the network.
    """

    BOOTING = "booting"
    RUNNING = "running"
    OFFLINE_GRACE = "offline_grace"
    DEGRADED = "degraded"
    REVOKED = "revoked"
    STOPPED = "stopped"

    @property
    def allows_live_trading(self) -> bool:
        """Return True iff this mode permits live order submission.

        The engine's live broker adapters call this on every order.
        Paper trading and backtests ignore the mode entirely — they
        never touch a customer's funds and so don't need the gate.
        """
        return self is Mode.RUNNING

    @property
    def is_terminal(self) -> bool:
        """Return True for modes from which the supervisor cannot recover."""
        return self in (Mode.REVOKED, Mode.STOPPED)


__all__ = ["Mode"]
