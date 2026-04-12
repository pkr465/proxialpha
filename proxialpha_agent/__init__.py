"""ProxiAlpha on-prem Customer Agent — Phase 2.

This package is the supervisor that wraps the existing ProxiAlpha
trading engine (``live_trading/``, ``paper_trading/``,
``backtesting/``) on a customer's machine. It is responsible for:

* Reading or fetching an agent license JWT from the control plane.
* Verifying the license signature and claims against a bundled
  dev public key (or a JWKS URL in production).
* Running the hourly heartbeat loop against ``/agent/heartbeat``
  on the control plane.
* Managing a small mode state machine
  (``BOOTING → RUNNING → OFFLINE_GRACE/DEGRADED → REVOKED/STOPPED``)
  that the engine adapters use to decide what's allowed to run.
* Exposing a localhost-only ``/health`` and ``/metrics`` endpoint
  for Prometheus scraping and docker healthchecks.

This is deliberately a separate package from :mod:`core` and
:mod:`api`: the agent runs on customer hardware, ships with a small
dependency surface, and must never import the control-plane DB
layer or anything that touches Stripe. The only shared code is the
JWT verification logic, which is intentionally re-implemented here
rather than imported from :mod:`core.jwt_keys` — that module
performs **signing**, which agents must never be able to do.

Module layout
-------------

* :mod:`proxialpha_agent.modes` — the ``Mode`` enum.
* :mod:`proxialpha_agent.settings` — env-driven configuration.
* :mod:`proxialpha_agent.license` — :class:`LicenseClient`,
  :class:`License`, :class:`LicenseError`.
* :mod:`proxialpha_agent.heartbeat` — :class:`HeartbeatClient`.
* :mod:`proxialpha_agent.supervisor` — :class:`Supervisor`,
  the orchestrator that wires everything together.
* :mod:`proxialpha_agent.health` — the localhost HTTP server.
* :mod:`proxialpha_agent.cli` — CLI subcommand router (used by
  the ``proxialpha`` console script defined in ``pyproject.toml``).
* :mod:`proxialpha_agent.__main__` — ``python -m proxialpha_agent``
  entry point.
* :mod:`proxialpha_agent.version` — single source of truth for
  ``__version__``. Imported here as a re-export so callers can
  keep using ``proxialpha_agent.__version__``.
* :mod:`proxialpha_agent.doctor` — builds redacted support bundles
  for the ``proxialpha doctor`` CLI subcommand.
"""
from __future__ import annotations

from .version import __version__

__all__ = ["__version__"]
