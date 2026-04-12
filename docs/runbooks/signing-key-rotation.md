# Agent signing key rotation runbook

The control plane signs every agent license JWT with an RS256 private
key. The corresponding public key is bundled into the customer agent
binary AND published at `/.well-known/jwks.json`. Rotating the
private key without breaking the field fleet relies on a brief
overlap window where both the new and old public keys appear in the
JWKS set.

## When to rotate

Rotate on a schedule (recommended: every 12 months) AND immediately
in any of these scenarios:

- A node that held the key file is decommissioned and you cannot
  prove the file was destroyed.
- The signing key was committed to source control by accident.
- A control-plane host was compromised at the OS level.
- An ops engineer with key access leaves the company.

## Pre-flight

1. Generate a new keypair OFFLINE on a trusted workstation:
   ```
   openssl genrsa -out agent_signing_key.new.pem 2048
   openssl rsa -in agent_signing_key.new.pem -pubout -out agent_signing_key.new.pub
   ```
2. Compute the new key's fingerprint (the value that will become
   the JWT header `kid`):
   ```
   python -c "
   import hashlib
   from cryptography.hazmat.primitives import serialization
   from cryptography.hazmat.primitives.asymmetric import rsa
   pub = serialization.load_pem_public_key(open('agent_signing_key.new.pub','rb').read())
   pem = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
   print(hashlib.sha256(pem).hexdigest()[:12])
   "
   ```
3. Push the new private key into your secret manager.
4. Push the new PUBLIC key into the agent repo for the next agent
   release. (The current fleet does NOT need this — they use the
   JWKS endpoint as a fallback.)

## The overlap window

The trick is to publish BOTH keys in the JWKS set during a window
that exceeds the agent's JWKS cache TTL (10 min by default) plus a
margin. We do this by setting two env vars on the API:

- `AGENT_SIGNING_KEY_PATH` — points at the NEW key. Used for signing.
- `AGENT_PREVIOUS_SIGNING_KEY_PATH` — points at the OLD key. Used
  for verification only, published in JWKS.

## Sequence

1. **Push the new key into secrets** but DO NOT change `AGENT_SIGNING_KEY_PATH` yet.
2. **Set `AGENT_PREVIOUS_SIGNING_KEY_PATH`** to the OLD key. This is
   a no-op for signing (the active key is still the old one) but
   pre-warms the JWKS publish path. Restart the API.
3. **Verify the JWKS now lists both keys**:
   ```
   curl -fsS https://api.proxiant.io/.well-known/jwks.json | jq '.keys[].kid'
   ```
   You should see two `kid` values.
4. **Wait 20 minutes**. Every agent in the fleet refreshes its JWKS
   cache within 10 min (cache TTL) + a margin for clock skew.
5. **Swap `AGENT_SIGNING_KEY_PATH` to the NEW key**. Keep
   `AGENT_PREVIOUS_SIGNING_KEY_PATH` pointed at the OLD key. Restart
   the API. Agents now receive tokens with the NEW `kid`; their JWKS
   cache already has the new key, so verification succeeds.
6. **Wait 24 hours**. Every old token in the wild has expired by
   now (license TTL is 24h).
7. **Drop `AGENT_PREVIOUS_SIGNING_KEY_PATH`** and restart the API.
   The JWKS endpoint now publishes only the new key. Old tokens
   minted before step 5 cannot be replayed.
8. **Securely destroy the old key** in your secret manager.

## Failure modes and recovery

| Symptom | Cause | Fix |
|---------|-------|-----|
| Agents start hitting `invalid_token/signature` after step 5 | JWKS cache didn't refresh in time | Roll back to the old key (step 5 reversed) and increase the wait in step 4 |
| `/.well-known/jwks.json` returns one key after step 2 | Previous key env var wrong | Double-check `AGENT_PREVIOUS_SIGNING_KEY_PATH` value and file permissions |
| Heartbeats start failing during the wait | Unrelated incident | Roll back to the previous deploy; rotation is safe to retry |

## KMS migration (future work)

The current procedure assumes file-based keys. The
`SIGNING_KEY_PROVIDER` setting (added in P1-5) reserves the slot for
`aws-kms`, `gcp-kms`, and `vault` providers. When those land:

- The KMS provider exposes the same `load_active` /
  `load_previous` shape via `core/key_providers.py`.
- Rotation becomes "create new KMS key version, mark new version
  active, wait, retire old version" — the JWKS overlap rules still
  apply because the agent side has no idea where the key comes from.
- Set `SIGNING_KEY_PROVIDER=aws-kms` (or equivalent) on the API and
  remove the file env vars in the same deploy.

Until then, attempting to use any provider other than `file` raises
a clear `NotImplementedError` at startup pointing the operator back
to this runbook.
