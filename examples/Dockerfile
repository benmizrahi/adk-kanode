# Build from repository root (use --network=host so uv can reach local airlock-proxy):
#   docker build --network=host -f examples/Dockerfile -t "$ADK_K8S_IMAGE" .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Create a non-root user for GKE runtime execution
RUN adduser --disabled-password --gecos "" --uid 10001 appuser

# Copy dependency manifests and plugin source code
COPY --chown=appuser:appuser pyproject.toml uv.lock README.md /app/
COPY --chown=appuser:appuser src/ /app/src/

# Run uv sync as root during build so airlock-proxy (127.0.0.1:999) authorizes the local socket UID.
# --compile-bytecode pre-builds .pyc files: appuser cannot write them at runtime, so without it
# every worker start would recompile google-adk from source.
# Ensure /app/src is world-readable regardless of host umask (e.g. 0027 -> 0750/0640).
RUN uv sync --frozen --no-dev --compile-bytecode \
    && chmod -R a+rX /app/src

# Copy one example into /app/agents/<name> so `adk web /app/agents` discovers it.
# Defaults to the quickstart; override both args to package another example, e.g.
#   --build-arg AGENT_SRC=examples/gke-sandbox/code_interpreter --build-arg AGENT_NAME=code_interpreter
ARG AGENT_SRC=examples/quickstart
ARG AGENT_NAME=quickstart
COPY --chown=appuser:appuser ${AGENT_SRC}/ /app/agents/${AGENT_NAME}/
RUN chmod -R a+rX /app/agents

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
