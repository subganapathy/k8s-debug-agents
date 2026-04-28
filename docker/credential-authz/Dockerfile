# Build credential-authz as a static binary in a multi-stage build.
#
# Stage 1: golang builder. Uses GOOS/GOARCH for cross-compilation; CGO disabled
# so the binary has no libc dependencies and can run on distroless.
#
# Stage 2: distroless static base. ~2MB base image. Non-root by default
# (UID 65532). No shell, no package manager, no /bin/sh — minimal attack
# surface.
#
# Build context: invoke from REPO ROOT, not from this directory.
# (Makefile target `make build-credential-authz-image` does this for you.)
#
#   docker build -f docker/credential-authz/Dockerfile -t k8s-debug-agents/credential-authz:dev .

ARG GO_VERSION=1.23
ARG ALPINE_VERSION=3.20

# ─── Stage 1: builder ──────────────────────────────────────────────────────────
FROM golang:${GO_VERSION}-alpine${ALPINE_VERSION} AS builder

WORKDIR /src

# Copy go.mod / go.sum first to populate the module cache layer.
# Subsequent code changes don't bust the dependency layer unless deps change.
COPY credential-authz/go.mod credential-authz/go.sum ./
RUN go mod download

COPY credential-authz/ .

# Build flags:
#   CGO_ENABLED=0    pure-Go binary, runs on distroless static
#   -trimpath        reproducible builds: strip absolute paths from binary
#   -ldflags "-s -w" strip symbol table + DWARF debug info → smaller binary
#                    (we still get crash stack traces from Go's runtime)
ARG TARGETOS=linux
ARG TARGETARCH=amd64

ENV CGO_ENABLED=0 \
    GOOS=${TARGETOS} \
    GOARCH=${TARGETARCH}

RUN go build \
      -trimpath \
      -ldflags="-s -w" \
      -o /out/credential-authz \
      ./cmd/credential-authz/

# ─── Stage 2: runtime ──────────────────────────────────────────────────────────
# distroless/static-debian12: nonroot user (UID 65532), no shell, no libc.
# This image has CA certificates bundled (used for outbound TLS — though we
# don't make outbound calls; the K8s API client wouldn't be present yet either).
FROM gcr.io/distroless/static-debian12:nonroot

# OCI labels — show up in `docker inspect`, registry UIs.
LABEL org.opencontainers.image.title="credential-authz"
LABEL org.opencontainers.image.description="Envoy ext_authz service that injects the Anthropic API key into outbound requests"
LABEL org.opencontainers.image.source="https://github.com/subganapathy/k8s-debug-agents"
LABEL org.opencontainers.image.licenses="MIT"

COPY --from=builder /out/credential-authz /credential-authz

# Already non-root (UID 65532) by virtue of the distroless:nonroot base.
USER 65532:65532

EXPOSE 9001 9002

ENTRYPOINT ["/credential-authz"]
