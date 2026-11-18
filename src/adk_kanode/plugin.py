"""ADK BasePlugin for managing GKE / Kubernetes workflow node execution."""

from __future__ import annotations

import contextvars
import logging
import os
import threading
from typing import Any
from typing import TYPE_CHECKING

from google.adk.plugins.base_plugin import BasePlugin

from .config import K8sConfig
from .deployment_executor import K8sDeploymentExecutor
from .executor import PodExecutor

if TYPE_CHECKING:
    from google.adk.agents.context import Context
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event

logger = logging.getLogger("google_adk.adk_graph_k8s.plugin")

_ACTIVE_K8S_PLUGIN: contextvars.ContextVar[K8sPlugin | None] = (
    contextvars.ContextVar("_ACTIVE_K8S_PLUGIN", default=None)
)


class K8sPlugin(BasePlugin):
    """ADK Plugin that provides cluster-level configuration and lifecycle management for ``@kanode`` / ``K8sNode``.

    Registering ``K8sPlugin`` on an ADK ``App`` or ``Runner`` provides:
    - Default ``K8sConfig`` (GKE image, namespace, service account, env vars, etc.)
      automatically inherited by all ``@kanode`` / ``K8sNode`` nodes in the workflow.
    - Shared ``PodExecutor`` (``K8sDeploymentExecutor`` by default, or e.g.
      ``K8sPodExecutor`` / ``SubprocessPodExecutor``).
    - Automatic cleanup of any in-flight GKE pods when an invocation finishes,
      errors, or is closed.
    """

    def __init__(
        self,
        *,
        name: str = "k8s_plugin",
        config: K8sConfig | None = None,
        executor: PodExecutor | None = None,
        **config_kwargs: Any,
    ) -> None:
        super().__init__(name=name)
        if config is not None and config_kwargs:
            self.config = config.merge_with(K8sConfig(**config_kwargs))
        elif config is not None:
            self.config = config
        elif config_kwargs:
            self.config = K8sConfig(**config_kwargs)
        else:
            self.config = K8sConfig()

        self.executor: PodExecutor = executor or K8sDeploymentExecutor()
        self.executed_nodes: list[str] = []

    def prewarm(self, root: Any) -> threading.Thread | None:
        """Creates the warm worker Deployments for every ``@kanode`` under ``root`` in the background.

        Call it once at import time (``plugin.prewarm(root_agent)``) so workers are
        already running before the first request. Without it, each Deployment is
        created lazily on the node's first call. Failures are logged, never raised.
        """
        executor = self.executor
        # Workers import the agent module too; they must not provision anything.
        if os.environ.get("ADK_K8S_WORKER") or not isinstance(executor, K8sDeploymentExecutor):
            return None

        nodes: list[Any] = []
        seen: set[int] = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            if hasattr(node, "extracted") and hasattr(node, "k8s_config"):
                if getattr(node, "executor", None) is None:
                    nodes.append(node)
                continue
            stack.extend(getattr(getattr(node, "graph", None), "nodes", None) or [])

        def _run() -> None:
            for node in nodes:
                try:
                    executor.prewarm(node.extracted, self.config.merge_with(node.k8s_config))
                except Exception:
                    logger.warning("Could not prewarm node %s", node.name, exc_info=True)

        thread = threading.Thread(target=_run, name="adk-kanode-prewarm", daemon=True)
        thread.start()
        return thread

    async def before_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        """Binds this plugin as the active K8sPlugin for the invocation."""
        _ACTIVE_K8S_PLUGIN.set(self)
        return None

    async def on_event_callback(
        self, *, invocation_context: InvocationContext, event: Event
    ) -> Event | None:
        """Observes events emitted during the workflow run."""
        return None

    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        """Cleans up any lingering GKE pods after the workflow run completes."""
        await self.executor.cleanup_all()

    async def on_run_error_callback(
        self,
        *,
        invocation_context: InvocationContext,
        error: Exception,
    ) -> None:
        """Cleans up any in-flight GKE pods when an unhandled error occurs."""
        await self.executor.cleanup_all()

    async def close(self) -> None:
        """Cleans up any resources when the runner closes."""
        await self.executor.close()


def resolve_k8s_plugin(ctx: Context | None) -> K8sPlugin | None:
    """Finds the registered ``K8sPlugin`` from the current ``Context`` or ``ContextVar``."""
    if ctx is not None:
        ic = getattr(ctx, "_invocation_context", None)
        pm = getattr(ic, "plugin_manager", None)
        plugins = getattr(pm, "plugins", None)
        if isinstance(plugins, list):
            for plugin in plugins:
                if isinstance(plugin, K8sPlugin):
                    return plugin
    return _ACTIVE_K8S_PLUGIN.get()
