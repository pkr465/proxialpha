"""Bundled key material for the ProxiAlpha agent.

``dev_pub.pem`` is a dev-only RSA public key bundled with the
agent distribution so a fresh install can verify dev-signed
license tokens without needing a separate config step. Production
deployments override this by setting ``PROXIALPHA_PUBLIC_KEY_PATH``
to a real control-plane public key (or eventually using
``PROXIALPHA_JWKS_URL``).

Do not commit a production private key anywhere near this
directory. The agent has no need for a private key — signing is
the control plane's job.
"""
