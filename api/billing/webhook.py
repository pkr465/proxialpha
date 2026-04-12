"""FastAPI router for Stripe webhooks.

This module exposes ``POST /webhook`` (mounted at ``/api/billing/webhook``
by :mod:`api.server`) and is the control plane's single entry point for
Stripe → us traffic.

Flow per request (Phase 1 PRD §7.3):

1. Read the raw request body (Stripe verifies the exact bytes you
   received — any JSON re-serialization breaks the signature).
2. Call ``stripe.Webhook.construct_event(payload, sig_header, secret)``
   which raises on malformed or bad-signature input. Return 400.
3. ``INSERT ... ON CONFLICT (id) DO NOTHING RETURNING id`` into
   ``billing_raw.stripe_events`` to atomically dedupe by event ID.
4. If the RETURNING produced no row, the event is a replay — return 200
   without running the handler.
5. Otherwise, dispatch to the type-specific handler from
   :data:`api.billing.handlers.EVENT_HANDLERS`. On success, update
   ``processed_at``. On failure, roll back and return 500 so Stripe
   retries (our idempotency key keeps the retry from double-processing).

The whole request runs inside **one** DB transaction per event. Either
everything (dedupe row + handler mutations + processed_at update) lands
atomically, or nothing does.

Engine acquisition
------------------

The webhook handler does NOT run under a tenant (``app.current_org_id``
is never set). It needs direct engine access with BYPASSRLS privileges.
We expose :func:`_get_webhook_session` as an async generator that tests
override via ``app.dependency_overrides`` in conftest.py; in production
it opens a session against the main engine, assuming the bg_worker DB
role has ``BYPASSRLS`` set (which ADR-005 requires).

Logging
-------

Per Task 02 "Do not": **never** log the full payload at INFO — it
contains customer emails and dollar amounts. We log
``event_id``, ``event_type``, and ``outcome`` only. Full payloads go
through the standard ``logging`` DEBUG handler.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.billing.handlers import EVENT_HANDLERS
from core.settings import get_settings

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Engine / session for webhook context
# ---------------------------------------------------------------------------

# Separate engine singleton so this path doesn't need a tenant context.
# Tests override the dependency to inject a SQLite-backed engine.
_webhook_engine = None
_webhook_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def _get_or_create_webhook_engine():
    """Lazily build the webhook-context engine from settings."""
    global _webhook_engine, _webhook_sessionmaker
    if _webhook_engine is None:
        settings = get_settings()
        _webhook_engine = create_async_engine(
            settings.database_url, pool_pre_ping=True, pool_size=5
        )
        _webhook_sessionmaker = async_sessionmaker(
            bind=_webhook_engine, expire_on_commit=False, class_=AsyncSession
        )
    return _webhook_sessionmaker


async def _get_webhook_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a webhook-context session.

    One *transaction* per yielded session. Caller is responsible for
    committing/rolling back — in practice the router wraps its work in
    ``async with session.begin():`` so we just hand over a raw
    sessionmaker product.

    Tests override this dependency at the app level to swap in a
    SQLite-backed fixture session.
    """
    maker = _get_or_create_webhook_engine()
    assert maker is not None
    session = maker()
    try:
        yield session
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _verify_signature(
    payload: bytes, sig_header: Optional[str], secret: str
) -> Dict[str, Any]:
    """Verify the Stripe signature and return the parsed event.

    Raises ``HTTPException(400)`` on any failure. We deliberately return
    a short message — leaking "expected whsec_..." would be an
    information disclosure.
    """
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except ValueError as exc:
        log.warning("stripe webhook malformed payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid payload") from exc
    except stripe.SignatureVerificationError as exc:
        log.warning("stripe webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid signature") from exc
    return event


# ---------------------------------------------------------------------------
# Dedupe + dispatch
# ---------------------------------------------------------------------------


# Table prefix is configurable so tests using SQLite (which has no
# schemas) can set it to ``""``. Production uses ``billing_raw.``.
STRIPE_EVENTS_TABLE = "billing_raw.stripe_events"


async def _insert_event_if_new(
    session: AsyncSession,
    event_id: str,
    event_type: str,
    payload_json: str,
) -> bool:
    """Insert the raw event and return True if this is a first-seen event.

    Uses ``ON CONFLICT (id) DO NOTHING`` so a second insert with the same
    event ID is a no-op. We then SELECT to find out whether the caller
    should dispatch to a handler.
    """
    # First, try to insert. The RETURNING clause tells us whether a row
    # was actually inserted (new event) vs. hit the DO NOTHING path
    # (duplicate).
    result = await session.execute(
        text(
            f"""
            INSERT INTO {STRIPE_EVENTS_TABLE} (id, event_type, received_at, payload)
            VALUES (:id, :type, :now, :payload)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """.replace("now()", "now()")
        ),
        {
            "id": event_id,
            "type": event_type,
            "now": __import__("datetime").datetime.utcnow(),
            "payload": payload_json,
        },
    )
    row = result.fetchone()
    return row is not None


async def _mark_event_processed(session: AsyncSession, event_id: str) -> None:
    """Set ``processed_at = now()`` on the raw event row."""
    await session.execute(
        text(
            f"UPDATE {STRIPE_EVENTS_TABLE} "
            f"SET processed_at = :now "
            f"WHERE id = :id"
        ),
        {
            "id": event_id,
            "now": __import__("datetime").datetime.utcnow(),
        },
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: Optional[str] = Header(default=None, alias="Stripe-Signature"),
    session: AsyncSession = Depends(_get_webhook_session),
) -> JSONResponse:
    """Stripe webhook entry point.

    Always returns 200 on idempotent replays and on successful dispatch.
    Returns 400 on signature failure (no DB write). Returns 500 on
    handler exception so Stripe retries.
    """
    # 1. Read raw body. Must be the exact bytes Stripe sent.
    payload = await request.body()

    # 2. Verify signature.
    secret = get_settings().stripe_webhook_secret
    event = _verify_signature(payload, stripe_signature, secret)

    # ``stripe.Webhook.construct_event`` returns a ``stripe.Event``
    # object that supports subscript access but NOT ``.get()``. Convert
    # to a plain recursive dict once here so the handlers can rely on
    # standard collection operations (``.get``, membership tests, etc).
    if hasattr(event, "to_dict"):
        event = event.to_dict()
    if not isinstance(event, dict):
        # Defensive — if a future Stripe SDK ever returns something
        # unexpected we want a clean 500 rather than a silent attribute
        # error further down.
        raise HTTPException(
            status_code=500, detail="Unexpected event shape from Stripe SDK"
        )

    event_id = str(event.get("id"))
    event_type = str(event.get("type"))
    # The dumped form is our audit-log payload.
    payload_text = json.dumps(event, default=str)

    log.info("stripe webhook received: id=%s type=%s", event_id, event_type)

    # 3–5. Dedupe, dispatch, mark processed — all in one transaction.
    try:
        async with session.begin():
            is_new = await _insert_event_if_new(
                session, event_id, event_type, payload_text
            )
            if not is_new:
                log.info(
                    "stripe webhook replay: id=%s type=%s (no-op)",
                    event_id,
                    event_type,
                )
                return JSONResponse({"status": "replay"})

            handler = EVENT_HANDLERS.get(event_type)
            if handler is not None:
                await handler(session, event)
                await _mark_event_processed(session, event_id)
                log.info(
                    "stripe webhook processed: id=%s type=%s outcome=ok",
                    event_id,
                    event_type,
                )
            else:
                # Unknown event type — still mark processed so we don't
                # retry it forever. The raw payload is in stripe_events
                # if we ever want to add a handler later.
                await _mark_event_processed(session, event_id)
                log.info(
                    "stripe webhook processed: id=%s type=%s outcome=unhandled",
                    event_id,
                    event_type,
                )
    except HTTPException:
        raise
    except Exception as exc:
        # Rollback happens automatically when ``async with session.begin()``
        # exits on exception. Return 500 so Stripe retries.
        log.exception(
            "stripe webhook FAILED: id=%s type=%s error=%s",
            event_id,
            event_type,
            exc,
        )
        raise HTTPException(status_code=500, detail="Webhook handler failed") from exc

    return JSONResponse({"status": "ok"})
