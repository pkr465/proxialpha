#!/usr/bin/env bash
#
# Build the ProxiAlpha customer agent image for linux/amd64 and
# linux/arm64 using docker buildx. This script is called by the
# release workflow and by developers who want to smoke-test a
# local build before pushing.
#
# Usage:
#   ./scripts/build_agent.sh <version>
#   ./scripts/build_agent.sh 1.0.0-rc.1
#
# Arguments:
#   version   The tag to apply (e.g. 1.0.0-rc.1). Must match the
#             ``__version__`` string in ``proxialpha_agent/version.py``
#             — the verify_image.sh script cross-checks this.
#
# Environment:
#   REGISTRY        Defaults to ``ghcr.io/proxialpha``. Override
#                   when pushing to a local registry during testing.
#   PLATFORMS       Defaults to ``linux/amd64,linux/arm64``. Set
#                   to ``linux/amd64`` for quick single-arch local
#                   builds.
#   PUSH            If ``1``, push after building. Default ``0``
#                   (load into local daemon only — only works for
#                   single-arch builds).
#   BUILDER         The buildx builder name to use. Defaults to
#                   ``proxialpha-builder``; created on first run.
#
# Exit codes:
#   0   Build + tag succeeded.
#   1   Usage error or docker/buildx error.
#   2   The passed version does not match the version embedded
#       in proxialpha_agent/version.py. This is a guardrail — the
#       image tag must always agree with the code's __version__.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <version>" >&2
    exit 1
fi

VERSION="$1"
REGISTRY="${REGISTRY:-ghcr.io/proxialpha}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
PUSH="${PUSH:-0}"
BUILDER="${BUILDER:-proxialpha-builder}"

IMAGE="${REGISTRY}/agent:${VERSION}"

# Locate the repo root relative to this script so the build
# works from any cwd (CI checks out in a weird path).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# ---- guardrail: version must match __version__ ----------------------
EMBEDDED_VERSION="$(
    python -c 'from proxialpha_agent.version import __version__; print(__version__)'
)"
if [[ "${EMBEDDED_VERSION}" != "${VERSION}" ]]; then
    echo "error: version mismatch" >&2
    echo "  argument:     ${VERSION}" >&2
    echo "  __version__:  ${EMBEDDED_VERSION}" >&2
    echo "  Bump proxialpha_agent/version.py before building." >&2
    exit 2
fi

# ---- ensure buildx is ready -----------------------------------------
if ! docker buildx inspect "${BUILDER}" >/dev/null 2>&1; then
    echo "creating buildx builder: ${BUILDER}"
    docker buildx create --name "${BUILDER}" --driver docker-container --use
else
    docker buildx use "${BUILDER}"
fi
docker buildx inspect --bootstrap >/dev/null

# ---- build ----------------------------------------------------------
BUILDX_ARGS=(
    buildx build
    --file Dockerfile.agent
    --platform "${PLATFORMS}"
    --tag "${IMAGE}"
    --label "org.opencontainers.image.version=${VERSION}"
    --label "org.opencontainers.image.revision=${GITHUB_SHA:-unknown}"
    --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    --progress plain
)

if [[ "${PUSH}" == "1" ]]; then
    BUILDX_ARGS+=(--push)
else
    # ``--load`` only works with a single platform — fall back to
    # linux/amd64 when running locally without PUSH=1.
    if [[ "${PLATFORMS}" == *","* ]]; then
        echo "note: PUSH=0 forces single-arch build (--load is single-platform only)" >&2
        PLATFORMS="linux/amd64"
        BUILDX_ARGS=(
            buildx build
            --file Dockerfile.agent
            --platform "${PLATFORMS}"
            --tag "${IMAGE}"
            --label "org.opencontainers.image.version=${VERSION}"
            --label "org.opencontainers.image.revision=${GITHUB_SHA:-unknown}"
            --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            --progress plain
        )
    fi
    BUILDX_ARGS+=(--load)
fi

BUILDX_ARGS+=(.)

echo "building ${IMAGE}"
echo "  platforms: ${PLATFORMS}"
echo "  push:      ${PUSH}"
echo "  builder:   ${BUILDER}"

docker "${BUILDX_ARGS[@]}"

echo
echo "build complete: ${IMAGE}"
if [[ "${PUSH}" == "1" ]]; then
    echo "image pushed to ${REGISTRY}"
else
    echo "image loaded into local docker daemon (single-arch)"
    echo "run ./scripts/verify_image.sh ${IMAGE} to validate"
fi
