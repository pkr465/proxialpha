# Task 08 — Agent Docker Image and CI Pipeline

**Phase:** 2 (Customer Agent)
**Est. effort:** 3–4 hours
**Prerequisites:** Task 07. Engine integration (diary, risk manager, strategies) can be stubbed — the goal of this task is packaging, not runtime completeness.

## Objective

Produce a reproducible, signed, multi-arch Docker image `proxialpha/agent:1.0.0-rc.1` and a CI workflow that builds it on every push, runs the agent test suite, and publishes the image on tagged releases.

## Context

- Image spec in `docs/specs/phase2-customer-agent.md` §13.
- User stories US-2.1 (first-run experience) and US-2.5 (`doctor` command) set the bar for what "shippable" means.
- We are **not** yet ready for public release. This task ships to a private registry and gates the `latest` tag behind manual approval.

## Exact files to create or modify

1. `Dockerfile.agent` — **new file** at repo root. (Separate from any existing `Dockerfile` so they don't interfere.)
2. `.dockerignore` — **new file** or extended. Exclude `venv`, `.git`, `tests/`, `docs/`, `data/`, local config files.
3. `.github/workflows/agent-build.yml` — **new file**. CI workflow.
4. `scripts/build_agent.sh` — **new file**. Local build helper.
5. `scripts/verify_image.sh` — **new file**. Runs `docker history` to confirm no secrets in layers, runs the built image with a test license, curls `/health`, tears down.
6. `proxialpha_agent/version.py` — **new file**. `__version__ = "1.0.0-rc.1"`. Read by `proxialpha version` CLI.
7. `pyproject.toml` — ensure the agent package is installable on its own; add optional `[tool.uv]` or equivalent lock file.

## Acceptance criteria

`Dockerfile.agent`:
- Multi-stage build: builder stage installs dependencies into a venv, runtime stage copies only the venv and source.
- Base: `python:3.11-slim` for both stages. No Alpine. No `latest` tag on the base image.
- Runs as non-root user `proxialpha` (UID 1000).
- `WORKDIR /app`.
- Installs only production deps (no pytest, no dev tools).
- Declares `VOLUME ["/var/lib/proxialpha"]`.
- Exposes port `9877` (health endpoint).
- Has a `HEALTHCHECK` that curls `http://localhost:9877/health`.
- Final image size ≤ 500 MB compressed.
- Build passes `docker build --no-cache` reproducibly.

`.github/workflows/agent-build.yml`:
- Triggers on push to `main` and on tags matching `agent-v*`.
- Jobs:
  - `test` — runs `pytest tests/test_agent_*.py` on Python 3.11. Must pass before build.
  - `build` — runs `docker buildx build --platform linux/amd64,linux/arm64` and pushes to `ghcr.io/proxiant/proxialpha-agent` with the tag from the git ref.
  - `scan` — runs `trivy image` against the built image; fails on HIGH or CRITICAL vulnerabilities.
  - `sign` — uses `cosign` to sign the image with a keyless OIDC signature.
  - `verify` — runs `scripts/verify_image.sh` against the pushed image.
- Image is only tagged `latest` when the workflow is triggered with `workflow_dispatch` **and** the caller has approval — do not tag `latest` on every main push.

`scripts/verify_image.sh`:
1. Pulls the image.
2. Runs `docker history --no-trunc <image>` and greps for suspicious strings (`API_KEY`, `SECRET`, `PRIVATE_KEY`, `sk_`, `password`). Fails if any found.
3. Starts the container with a test license token from env.
4. Waits up to 30 seconds for `curl http://localhost:9877/health` to return 200.
5. Runs `docker exec <container> proxialpha version` and asserts the output contains `1.0.0-rc.1`.
6. Runs `docker exec <container> proxialpha doctor --output /tmp/bundle.tar.gz` and asserts the bundle exists and is under 5 MB.
7. Extracts the bundle and greps for the same secret patterns. Fails if any found.
8. Tears down the container.
9. Exits 0 on all success.

Tests already exist from Task 07; this task adds:
- `tests/test_doctor_bundle.py` — unit test that calls `proxialpha_agent.doctor.build_bundle()` with fixture data and asserts no secrets appear in the output, bundle is gzipped, under 5 MB, and contains the expected file list.

## Do not

- Do not include the dev signing private key in the image. Only the public key.
- Do not pin the base image to `python:3.11-slim` without a digest in the final release — for `rc.1` a floating tag is fine, but before `1.0.0` we pin by sha256.
- Do not build the image locally and push manually as part of this task. Everything goes through CI. Local builds are for development only.
- Do not enable `latest` tag pushing in CI. That's a manual approval step.
- Do not include `.git` in the image.

## Hints and gotchas

- Docker BuildKit is required for multi-arch. Enable with `DOCKER_BUILDKIT=1` or use `docker buildx`.
- `docker history --no-trunc` is your friend for debugging layer bloat and accidental secret inclusion.
- Trivy tends to flag transitive CVEs that come from the base image. If `trivy image python:3.11-slim` shows HIGH findings, either accept them with a `.trivyignore` with justification or bump to a newer base image. Don't suppress silently.
- Cosign keyless signing uses the OIDC token from GitHub Actions, so no key management is needed. Verification on the customer side is `cosign verify --certificate-identity=... --certificate-oidc-issuer=https://token.actions.githubusercontent.com ghcr.io/...`.
- The `doctor` command is security-critical. Write the redaction pass as a denylist of regexes: `(sk|pk)_[a-zA-Z0-9]{20,}`, `0x[a-fA-F0-9]{64}`, `-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----`, etc. Run the redacted output through the same regexes to catch bugs in the redactor itself.

## Test command

```bash
# Local development build
./scripts/build_agent.sh 1.0.0-rc.1

# Verify the locally-built image
./scripts/verify_image.sh proxialpha/agent:1.0.0-rc.1

# Unit test
pytest tests/test_doctor_bundle.py -v
```

All must pass.

## Definition of done

- `docker run --rm -v /tmp/proxialpha:/var/lib/proxialpha -e PROXIALPHA_INSTALL_TOKEN=<fake> ghcr.io/proxiant/proxialpha-agent:1.0.0-rc.1` boots to BOOTING mode, logs the expected "no control plane" error for a fake token, and exits cleanly.
- The CI workflow runs green end-to-end on a fresh PR.
- A `cosign verify` command succeeds against the pushed image in the private registry.
- The `verify_image.sh` script passes against the published image.
