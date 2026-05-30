# syntax=docker/dockerfile:1.7
#
# agent-task — the variant Job container.
#
# Multi-stage build:
#   Stage 1 (builder): install pip dependencies into a venv. Cached
#                      independently from source changes because pyproject.toml
#                      is copied first.
#   Stage 2 (runtime): slim Python base, copy the venv + source, register the
#                      `agent` + `agent-runner` console scripts on PATH.
#
# Two console scripts ship in this image:
#   `agent`        — Job-spawning user CLI (used outside the cluster too;
#                    included in the image so the same image can act as a
#                    `kubectl run`-style debug tool if needed)
#   `agent-runner` — in-Pod entrypoint that runs the agent loop in-process
#                    and writes to HandoffRequest.status. THIS is what
#                    Job.spec.containers[0].command points at.
#
# PSA `restricted` compliance:
#   - Runs as non-root (UID 65532, the `nonroot` convention)
#   - readOnlyRootFilesystem-compatible: nothing writes to / at runtime
#     (Python's __pycache__ goes to /tmp, mounted as emptyDir by job_runner.py)
#   - allowPrivilegeEscalation=false safe (no setuid binaries)
#   - Drops all caps (none needed)
#   - Compatible with seccompProfile=RuntimeDefault
#
# Build context: invoke from REPO ROOT, not from this directory.
#   docker build -f docker/agent-task/Dockerfile -t agent-task:dev .

ARG PYTHON_VERSION=3.13

# ─── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

# uv would be much faster than pip but we keep pip for portability (matches
# what `make agent-setup` does locally; one fewer tool for newcomers to learn).
# Tradeoff: ~30s slower builds.

WORKDIR /build

# Copy ONLY the dependency-declaration file first; this layer caches as long
# as deps don't change, even when source files change. Massive speedup for
# the inner dev loop (edit .py → rebuild = ~5s; edit pyproject.toml → ~30s).
COPY agent-task/pyproject.toml ./
COPY agent-task/README.md ./

# Create the venv where the runtime will look for it (/opt/venv).
# --no-cache-dir avoids baking pip's HTTP cache into the layer (we don't
# reuse it across builds; the BuildKit cache mount below handles that).
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip

# Mount pip's cache as a BuildKit cache so subsequent builds reuse downloads
# even when this layer is invalidated. Sharing=locked because pip's cache
# isn't always parallel-safe.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    /opt/venv/bin/pip install --no-deps -e . || true && \
    /opt/venv/bin/pip install \
      "anthropic>=0.40.0" \
      "kubernetes>=29.0.0" \
      "pydantic>=2.0"

# Now copy the source. This layer rebuilds on every .py edit, but it's tiny
# and fast because the pip-install layer above is cached.
COPY agent-task/src/ ./src/

# Install the package itself (not its deps — they're already in /opt/venv).
# This registers the `agent` + `agent-runner` console scripts.
RUN /opt/venv/bin/pip install --no-deps .

# ─── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.title="agent-task"
LABEL org.opencontainers.image.description="Diagnostic agent variant Job — runs the agent loop, writes findings to HandoffRequest.status"
LABEL org.opencontainers.image.source="https://github.com/subganapathy/k8s-debug-agents"
LABEL org.opencontainers.image.licenses="MIT"

# Create the non-root user. UID 65532 matches distroless/nonroot convention
# so the same UID can be referenced across our images.
RUN groupadd --system --gid 65532 nonroot && \
    useradd --system --uid 65532 --gid 65532 --shell /usr/sbin/nologin nonroot

# Copy the prebuilt venv from the builder. The `agent` + `agent-runner`
# console scripts live under /opt/venv/bin and need to be on PATH.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Verify the console scripts are wired up at build time — fails the build
# if pyproject.toml's [project.scripts] is malformed or the import path
# changed. Cheap to run, catches a class of accidental breakage.
RUN agent --help >/dev/null && agent-runner --help >/dev/null

USER 65532:65532

# No ENTRYPOINT — Job.spec.containers[0].command sets the command explicitly
# (`["agent-runner", "<variant>", ...]`). Allows the same image to serve as
# CLI host, runner, or future tooling without baking in one assumption.
