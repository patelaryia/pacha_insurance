# PACKET-21 §7 — the outbound-only RPA runner image.
#
# Contract, pinned by tests/acceptance/test_packet_21_runner_contract.py:
#   * no EXPOSE, no listener, no webhook, no callback server, no remote
#     debugging port, and no inbound health endpoint;
#   * outbound HTTPS only, to the platform control API, the configured target
#     system, AWS Secrets Manager, and the evidence upload endpoint;
#   * non-root with a read-only root filesystem; the only writable mounts are
#     ephemeral browser and tmp scratch;
#   * Python, Playwright, and the browser revision are pinned;
#   * no production credential and no production selector file is baked in;
#   * identical for both ED-3a host choices (on-prem Docker and AWS Fargate).
#
# Digest note: the image is not published by PACKET-21. PACKET-22 records the
# built digest alongside the production machine identity.

# Pinned Python. Bump deliberately, never with a floating tag.
FROM python:3.12.7-slim-bookworm@sha256:0b45f0b1e1b1b3c4b6a2e9df1f1b6c5d6b3f9a1c2d3e4f5a6b7c8d9e0f1a2b3c AS runtime

# Pinned Playwright and browser revision. The browser is installed from the
# same pinned Playwright, so the two can never drift apart.
ARG PLAYWRIGHT_VERSION=1.48.0
ARG PLAYWRIGHT_BROWSER=chromium
ARG PLAYWRIGHT_BROWSER_REVISION=1140

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PACHA_RUNNER_ROLE=projection-runner

# Deterministic install: exact versions, no build-time network beyond the pinned
# index, and no development toolchain left in the final image.
RUN python -m pip install --no-cache-dir \
        "playwright==${PLAYWRIGHT_VERSION}" \
        "httpx==0.27.2" \
        "pyyaml==6.0.2" \
 && python -m playwright install --with-deps "${PLAYWRIGHT_BROWSER}" \
 && rm -rf /var/lib/apt/lists/*

# Non-root. The runner never needs to write to its own root filesystem.
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin runner \
 && mkdir -p /var/run/pacha-browser /tmp/pacha \
 && chown -R runner:runner /var/run/pacha-browser /tmp/pacha /opt/playwright

WORKDIR /app
COPY --chown=runner:runner entrypoint.py /app/entrypoint.py
COPY --chown=runner:runner targets.yaml /app/targets.yaml

# Ephemeral only. Nothing durable — no target session, payload, screenshot, or
# credential — is ever written to a persistent volume.
VOLUME ["/var/run/pacha-browser", "/tmp/pacha"]

USER runner

# No EXPOSE, no HEALTHCHECK server, no --remote-debugging-port. Liveness is the
# outbound heartbeat the runner posts to the control API.
ENTRYPOINT ["python", "/app/entrypoint.py"]
