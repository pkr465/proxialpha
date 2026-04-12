"""Dashboard endpoint for issuing one-shot install tokens.

This is the **issuer** side of the install-token flow. The
**consumer** side lives at :mod:`api.agent.enroll` and the underlying
primitives at :mod:`api.agent.install_tokens`.

Endpoint
--------

``POST /api/orgs/{org_id}/install-tokens``

Request body (optional, both fields default sensibly)::

    {
        "label": "qa-laptop-2026-04",      // free-form admin note
        "ttl_minutes": 10                   // 1..60, default 10
    }

Response (the plaintext is shown EXACTLY ONCE — never persisted)::

    {
        "token": "x9f3...long-random-string",
        "token_id": "uuid-of-the-row",
        "expires_at": "2026-04-11T15:30:00+00:00",
        "label": "qa-laptop-2026-04"
    }

After this call returns, the row exists in ``install_tokens`` with
only the SHA-256 hash of the plaintext. We never store the plaintext
itself, never log it, and the dashboard UI is responsible for
copy-to-clipboard + warning the user it cannot be retrieved later.

Auth model
----------

The route requires an authed Clerk user with an org context. The org
in the URL path MUST match the authed org — we never let an admin
issue an install token for a different tenant. This is the same
defense pattern :mod:`api.billing.endpoints` uses for Checkout.

Why a separate file from :mod:`api.billing.endpoints`?
------------------------------------------------------

* ``endpoints.py`` is Stripe-heavy and imports the ``stripe`` SDK.
* This file has zero Stripe surface — it only touches the
  ``install_tokens`` table.

Splitting them keeps the import graph clean (the agent enroll path,
which imports the validate-and-consume primitive, would otherwise
transitively pull in the entire Stripe SDK at import time).
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.agent.install_tokens import (
    DEFAULT_INSTALL_TOKEN_TTL,
    issue_install_token,
)
from api.billing.endpoints import _get_billing_session
from api.middleware.auth_stub import require_authed_org

log = logging.getLogger(__name__)

router = APIRouter()


#: Hard ceiling on install-token TTL. We don't let an admin generate a
#: token that lives longer than an hour, even by mistake. The whole
#: point of an install token is "use it now or throw it away".
_MAX_TTL_MINUTES = 60

#: Floor on install-token TTL. Anything below 1 minute is almost
#: certainly a typo and would race the dashboard's "you have N seconds
#: left to copy this" countdown UI.
_MIN_TTL_MINUTES = 1


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class IssueInstallTokenRequest(BaseModel):
    """Optional body for ``POST /api/orgs/{org_id}/install-tokens``.

    Both fields are optional. The dashboard typically calls with no
    body at all and the defaults kick in (10-minute TTL, no label).
    """

    label: Optional[str] = Field(
        default=None,
        max_length=120,
        description=(
            "Free-form admin label so the dashboard can show a "
            "human-meaningful name in the install-tokens list."
        ),
    )
    ttl_minutes: Optional[int] = Field(
        default=None,
        ge=_MIN_TTL_MINUTES,
        le=_MAX_TTL_MINUTES,
        description=(
            "Override the default 10-minute lifetime. Clamped to "
            f"[{_MIN_TTL_MINUTES}, {_MAX_TTL_MINUTES}]."
        ),
    )


class IssueInstallTokenResponse(BaseModel):
    """Successful issuance — what the dashboard receives.

    ``token`` is the plaintext bearer string. **THIS IS THE ONLY TIME
    IT IS EVER RETURNED.** The dashboard must show it once with a
    copy-to-clipboard button and a clear "you cannot retrieve this
    again" warning.
    """

    token: str = Field(..., description="One-shot install-token plaintext.")
    token_id: str = Field(..., description="UUID of the install_tokens row.")
    expires_at: str = Field(
        ..., description="ISO-8601 timestamp when the token stops working."
    )
    label: Optional[str] = Field(default=None, description="Admin label echo.")


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post(
    "/orgs/{org_id}/install-tokens",
    response_model=IssueInstallTokenResponse,
    status_code=201,
)
async def issue_install_token_endpoint(
    request: Request,
    body: Optional[IssueInstallTokenRequest] = None,
    org_id: uuid.UUID = Path(..., description="The org to issue a token for."),
    session: AsyncSession = Depends(_get_billing_session),
) -> IssueInstallTokenResponse:
    """Issue a one-shot install token for ``org_id``.

    See the module docstring for the full contract. Notable rules:

    * The path ``org_id`` MUST match the authed user's org. Mismatch
      → 403 (not 404 — we don't want to leak whether the org exists).
    * The plaintext is generated server-side via
      :func:`api.agent.install_tokens.issue_install_token` and
      returned in the response body. We never echo it to the access
      log and never write it to any other table.
    """
    user, authed_org = require_authed_org(request)

    # Tenant binding: the URL org and the JWT org must match. We
    # deliberately use 403 (not 404) so an admin who fat-fingers
    # somebody else's org_id sees a clean "not your org" error
    # instead of leaking whether the target org exists at all.
    if authed_org != org_id:
        log.warning(
            "install_tokens: user=%s tried to issue for foreign org=%s "
            "(authed=%s)",
            user.email,
            org_id,
            authed_org,
        )
        raise HTTPException(
            status_code=403, detail="Cannot issue tokens for another org"
        )

    # Resolve TTL with the documented bounds. The pydantic ge/le on
    # the field already enforces the [1, 60] bound when the field is
    # set; this branch covers the "no body or no ttl_minutes" case.
    if body is not None and body.ttl_minutes is not None:
        ttl = timedelta(minutes=body.ttl_minutes)
    else:
        ttl = DEFAULT_INSTALL_TOKEN_TTL

    label = body.label if body is not None else None

    # Look up the internal user_id for the audit trail. The auth_stub
    # only carries an email, but the issuer wants a UUID for the
    # ``created_by`` foreign key. We resolve here so the primitive
    # stays DB-shape-agnostic.
    created_by = await _resolve_user_uuid(session, user.email)

    issued = await issue_install_token(
        session,
        org_id=org_id,
        created_by=created_by,
        label=label,
        ttl=ttl,
    )

    # Commit the row before returning. If the commit fails the
    # plaintext we generated is thrown away and the admin retries —
    # no orphan row, no leaked secret.
    await session.commit()

    log.info(
        "install_tokens: issued org=%s token_id=%s label=%r ttl=%ss",
        org_id,
        issued.token_id,
        label,
        int(ttl.total_seconds()),
    )

    return IssueInstallTokenResponse(
        token=issued.plaintext,
        token_id=str(issued.token_id),
        expires_at=issued.expires_at.isoformat(),
        label=label,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_user_uuid(
    session: AsyncSession, email: str
) -> Optional[uuid.UUID]:
    """Look up our internal ``users.id`` by email for the audit trail.

    Returns ``None`` if the user isn't in our table — in which case
    the install_tokens row's ``created_by`` is left null. The Clerk
    JIT-provisioning path (P0-2) will populate this for real users;
    test fixtures that don't seed a user row will land in the
    null branch and that's fine.
    """
    from sqlalchemy import text

    result = await session.execute(
        text("SELECT id FROM users WHERE email = :e LIMIT 1"),
        {"e": email},
    )
    row = result.fetchone()
    if row is None:
        return None
    return uuid.UUID(str(row[0]))


__all__ = [
    "IssueInstallTokenRequest",
    "IssueInstallTokenResponse",
    "issue_install_token_endpoint",
    "router",
]
