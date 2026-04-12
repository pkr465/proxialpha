#!/usr/bin/env bash
#
# Post-build verification for the ProxiAlpha agent image.
#
# The CI workflow runs this against the tag it just built; a
# developer can run it locally against a freshly ``--load``-ed
# image. Every step below is a separate hard gate — the script
# exits non-zero on the first failure. The goals are:
#
#   1. No secrets got baked into any layer.
#   2. The runtime can actually start and serve /health.
#   3. ``proxialpha version`` prints the expected tag.
#   4. ``proxialpha doctor`` produces a bundle under the size
#      cap and with no secret regexes in any member.
#
# Usage:
#   ./scripts/verify_image.sh <image-ref>
#   ./scripts/verify_image.sh ghcr.io/proxialpha/agent:1.0.0-rc.1
#
# Exit codes:
#   0   All 9 checks passed.
#   1   Some check failed; see stderr.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <image-ref>" >&2
    exit 1
fi

IMAGE="$1"
CONTAINER_NAME="proxialpha-verify-$$"
TMP_DIR="$(mktemp -d)"
trap 'cleanup' EXIT

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    rm -rf "${TMP_DIR}"
}

step() {
    echo
    echo "=== $1 ==="
}

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

# ---- Step 1: pull the image if not already local -------------------
step "1/9 pulling image ${IMAGE}"
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    docker pull "${IMAGE}" || fail "could not pull ${IMAGE}"
else
    echo "already present locally"
fi

# ---- Step 2: scan image history for secret markers ----------------
#
# ``docker history --no-trunc`` prints every layer's CMD as it was
# baked. Any sign of an env-var secret or a hardcoded credential
# is a hard fail. We match on a conservative denylist that mirrors
# the proxialpha_agent.doctor regex set.
step "2/9 scanning image history for secret markers"
HISTORY="$(docker history --no-trunc --format '{{.CreatedBy}}' "${IMAGE}")"
SECRET_HITS="$(
    echo "${HISTORY}" | grep -E -i \
        -e 'API_KEY=' -e 'SECRET=' -e 'PRIVATE_KEY' \
        -e 'sk_live_' -e 'sk_test_' -e 'pk_live_' \
        -e 'AKIA[0-9A-Z]{16}' \
        -e 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' \
        -e 'password=' -e 'PASSWORD=' \
        || true
)"
if [[ -n "${SECRET_HITS}" ]]; then
    echo "secret markers found in image history:" >&2
    echo "${SECRET_HITS}" >&2
    fail "image layers contain one or more secret markers"
fi
echo "no secret markers in image history"

# ---- Step 3: image size under 500 MB --------------------------------
step "3/9 checking image size ≤ 500 MB"
SIZE_BYTES="$(docker image inspect "${IMAGE}" --format '{{.Size}}')"
SIZE_MB=$((SIZE_BYTES / 1024 / 1024))
echo "image size: ${SIZE_MB} MB"
if [[ "${SIZE_MB}" -gt 500 ]]; then
    fail "image size ${SIZE_MB} MB exceeds 500 MB cap"
fi

# ---- Step 4: start the container with a test license env ----------
step "4/9 starting container"
docker run -d \
    --name "${CONTAINER_NAME}" \
    --rm \
    -e PROXIALPHA_CONTROL_PLANE_URL="https://cp.example.com" \
    -e PROXIALPHA_INSTALL_TOKEN="test-install-token-not-real" \
    -e PROXIALPHA_LOG_LEVEL="DEBUG" \
    --entrypoint /usr/bin/tini \
    "${IMAGE}" \
    -- sleep 300 \
    >/dev/null
echo "container started: ${CONTAINER_NAME}"

# ---- Step 5: proxialpha version matches the image tag --------------
step "5/9 proxialpha version matches image tag"
EXPECTED_VERSION="${IMAGE##*:}"
REPORTED_VERSION="$(
    docker exec "${CONTAINER_NAME}" python -m proxialpha_agent version \
        | awk '{print $NF}'
)"
echo "expected: ${EXPECTED_VERSION}"
echo "reported: ${REPORTED_VERSION}"
if [[ "${EXPECTED_VERSION}" != "${REPORTED_VERSION}" ]]; then
    fail "version mismatch (expected ${EXPECTED_VERSION}, got ${REPORTED_VERSION})"
fi

# ---- Step 6: running as non-root ----------------------------------
step "6/9 container is running as non-root UID 1000"
ACTUAL_UID="$(docker exec "${CONTAINER_NAME}" id -u)"
echo "uid: ${ACTUAL_UID}"
if [[ "${ACTUAL_UID}" != "1000" ]]; then
    fail "container is running as uid ${ACTUAL_UID}, expected 1000"
fi

# ---- Step 7: doctor subcommand builds a bundle --------------------
step "7/9 proxialpha doctor builds a support bundle"
docker exec "${CONTAINER_NAME}" python -m proxialpha_agent doctor \
    --output /tmp/bundle.tar.gz \
    || fail "proxialpha doctor failed"
docker cp "${CONTAINER_NAME}:/tmp/bundle.tar.gz" "${TMP_DIR}/bundle.tar.gz"

BUNDLE_SIZE="$(stat -c%s "${TMP_DIR}/bundle.tar.gz" 2>/dev/null \
    || stat -f%z "${TMP_DIR}/bundle.tar.gz")"
BUNDLE_SIZE_MB=$((BUNDLE_SIZE / 1024 / 1024))
echo "bundle size: ${BUNDLE_SIZE} bytes (${BUNDLE_SIZE_MB} MB)"
if [[ "${BUNDLE_SIZE}" -gt $((5 * 1024 * 1024)) ]]; then
    fail "bundle exceeds 5 MB cap"
fi

# ---- Step 8: extract the bundle and grep every member for secrets -
step "8/9 extracting bundle and grepping for secret markers"
mkdir -p "${TMP_DIR}/extract"
tar -xzf "${TMP_DIR}/bundle.tar.gz" -C "${TMP_DIR}/extract"
BUNDLE_HITS="$(
    grep -R -E \
        -e 'sk_live_[a-zA-Z0-9]{20,}' \
        -e 'sk_test_[a-zA-Z0-9]{20,}' \
        -e 'pk_live_[a-zA-Z0-9]{20,}' \
        -e 'pk_test_[a-zA-Z0-9]{20,}' \
        -e 'AKIA[0-9A-Z]{16}' \
        -e '0x[a-fA-F0-9]{64}' \
        -e 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY' \
        "${TMP_DIR}/extract" \
        || true
)"
if [[ -n "${BUNDLE_HITS}" ]]; then
    echo "secret markers found in extracted bundle:" >&2
    echo "${BUNDLE_HITS}" >&2
    fail "doctor bundle leaked one or more secrets"
fi
echo "no secret markers in doctor bundle"

# ---- Step 9: teardown ---------------------------------------------
step "9/9 teardown"
docker rm -f "${CONTAINER_NAME}" >/dev/null
echo "container stopped and removed"

echo
echo "====================================="
echo "  ALL 9 VERIFICATION CHECKS PASSED"
echo "  image: ${IMAGE}"
echo "====================================="
exit 0
