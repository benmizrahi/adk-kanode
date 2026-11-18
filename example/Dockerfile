# Build from repository root (use --network=host so uv can reach local airlock-proxy):
#   docker build --network=host -f example/Dockerfile -t "$ADK_K8S_IMAGE" .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Create a non-root user for GKE runtime execution
RUN adduser --disabled-password --gecos "" --uid 10001 appuser

# Copy dependency manifests and plugin source code
COPY pyproject.toml uv.lock README.md /app/
COPY src/ /app/src/

# Run uv sync as root during build so airlock-proxy (127.0.0.1:999) authorizes the local socket UID.
# --compile-bytecode pre-builds .pyc files: appuser cannot write them at runtime, so without it
# every worker start would recompile google-adk from source.
RUN uv sync --frozen --no-dev --compile-bytecode

# Copy the example agent into /app/agents/example so `adk web /app/agents` discovers it
COPY --chown=appuser:appuser example/ /app/agents/example/

USER appuser

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/agents:/app/src"
ENV PYTHONUNBUFFERED="1"

# Default runtime configuration (override via GKE Deployment env vars).
# No CPU limit on purpose: a tight limit throttles the Python import at worker startup.
ENV ADK_K8S_NAMESPACE="default"
ENV ADK_K8S_SERVICE_ACCOUNT="adk-worker-sa"
ENV ADK_K8S_CPU_REQUEST="250m"
ENV ADK_K8S_MEMORY_REQUEST="256Mi"
ENV ADK_K8S_MEMORY_LIMIT="512Mi"
ENV ADK_K8S_CLEANUP_POD="true"

EXPOSE 8080

# Expose the ADK Web UI on port 8080 (worker pods spawned by @kanode override this command automatically)
CMD ["adk", "web", "--host=0.0.0.0", "--port=8080", "/app/agents"]
