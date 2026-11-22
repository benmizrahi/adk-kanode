"""K8sNode and @kanode / @k8s_node decorator for ADK Workflow graphs."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from collections.abc import Callable
from typing import Any
from typing import Literal
from typing import overload
from typing import TYPE_CHECKING
from typing import TypeVar

from google.adk.workflow._base_node import BaseNode
from google.adk.workflow._errors import WorkflowConfigurationError
from google.adk.workflow._function_node import FunctionNode
from google.adk.workflow._graph import NodeLike
from google.adk.workflow._node import Node
from google.adk.workflow._retry_config import RetryConfig
from google.adk.workflow.utils._workflow_graph_utils import build_node
from google.adk.workflow.utils._workflow_hitl_utils import apply_confirmation_resume
from google.adk.workflow.utils._workflow_hitl_utils import auth_interrupt_id
from google.adk.workflow.utils._workflow_hitl_utils import create_auth_request_event
from google.adk.workflow.utils._workflow_hitl_utils import create_confirmation_request_input
from google.adk.workflow.utils._workflow_hitl_utils import has_auth_credential
from google.adk.workflow.utils._workflow_hitl_utils import process_auth_resume
from pydantic import BaseModel
from pydantic import Field
from pydantic import TypeAdapter
from typing_extensions import override

from .config import K8sConfig
from .deployment_executor import get_default_executor
from .executor import PodExecutor
from .extractor import extract_node_implementation
from .extractor import ExtractedImplementation
from .plugin import resolve_k8s_plugin
from .pod_runner import PodExecutionPayload
from .pod_runner import serialize_node_input

if TYPE_CHECKING:
    from google.adk.agents.context import Context
    from google.adk.auth.auth_tool import AuthConfig

T = TypeVar("T", bound=Callable[..., Any])
_ANY_TYPE_ADAPTER: TypeAdapter[Any] = TypeAdapter(Any)


class K8sNode(Node):
    """A valid ADK Workflow ``Node`` (`BaseNode` subclass) that executes its inner node in a dedicated GKE pod.

    Inherits from ``google.adk.workflow.Node``, so it is accepted directly in
    ``Workflow(edges=[...])``, routing maps, fan-out tuples, ``JoinNode``s,
    ``parallel_worker=True``, and ``ctx.run_node(...)``.
    """

    inner_node: BaseNode = Field(
        description="The wrapped ADK BaseNode (FunctionNode, LlmAgent, _ToolNode, etc.)."
    )
    k8s_config: K8sConfig = Field(
        default_factory=K8sConfig,
        description="Node-level GKE / Kubernetes pod configuration.",
    )
    extracted: ExtractedImplementation = Field(
        description="Automatically extracted internal implementation for GKE pod execution."
    )
    executor: PodExecutor | None = Field(
        default=None,
        exclude=True,
        description="Optional per-node PodExecutor override.",
    )

    @override
    def _validate_input_data(self, data: Any) -> Any:
        return self.inner_node._validate_input_data(data)

    @override
    def _validate_output_data(self, data: Any) -> Any:
        return self.inner_node._validate_output_data(data)

    @override
    async def run_node_impl(
        self,
        *,
        ctx: Context,
        node_input: Any,
    ) -> AsyncGenerator[Any, None]:
        """Executes the wrapped ADK node inside a dedicated GKE pod and yields its Events."""
        from google.adk.events.event import Event

        # 1. Handle FunctionNode confirmation and auth gates on the host before spinning up a pod
        resumed_confirmation = False
        if isinstance(self.inner_node, FunctionNode):
            kwargs = self.inner_node._bind_parameters(ctx, node_input)
            if self.inner_node.require_confirmation:
                resumed_confirmation = apply_confirmation_resume(ctx)
                if ctx.tool_confirmation is not None:
                    if not ctx.tool_confirmation.confirmed:
                        yield Event(output={"error": "This tool call is rejected."})
                        return
                else:
                    bound_args = {
                        k: v
                        for k, v in kwargs.items()
                        if k != self.inner_node._context_param_name
                    }
                    if await self.inner_node._check_require_confirmation(
                        ctx, node_input=node_input, bound_args=bound_args
                    ):
                        yield create_confirmation_request_input(
                            node_path=ctx.node_path,
                            tool_name=self.name,
                            args=_ANY_TYPE_ADAPTER.dump_python(bound_args),
                        )
                        return

            if self.inner_node.auth_config:
                interrupt_id = auth_interrupt_id(ctx.node_path)
                auth_response = ctx.resume_inputs.get(interrupt_id)
                if auth_response is not None:
                    await process_auth_resume(
                        auth_response,
                        self.inner_node.auth_config,
                        ctx.state,
                        interrupt_id,
                    )
                elif not has_auth_credential(self.inner_node.auth_config, ctx.state):
                    yield create_auth_request_event(
                        self.inner_node.auth_config, interrupt_id, ctx.state
                    )
                    return

        # 2. Resolve effective K8sConfig and PodExecutor (merging with K8sPlugin if active)
        plugin = resolve_k8s_plugin(ctx)
        if plugin is not None:
            effective_config = plugin.config.merge_with(self.k8s_config)
            effective_executor = self.executor or plugin.executor
            plugin.executed_nodes.append(self.name)
        else:
            effective_config = self.k8s_config
            effective_executor = self.executor or get_default_executor()

        # 3. Build serializable execution payload for the dedicated GKE pod
        raw_input = (
            node_input.source
            if hasattr(node_input, "source") and hasattr(node_input, "bound")
            else node_input
        )
        serialized_input, is_content = serialize_node_input(raw_input)
        ic = getattr(ctx, "_invocation_context", None)
        session = getattr(ic, "session", None)

        payload = PodExecutionPayload(
            node_name=self.name,
            node_path=ctx.node_path or self.name,
            run_id=str(ctx.run_id or "1"),
            invocation_id=getattr(ic, "invocation_id", None) or "k8s-invocation",
            session_id=getattr(session, "id", None) or "k8s-session",
            node_input=serialized_input,
            node_input_is_content=is_content,
            state=ctx.state.to_dict(),
            resume_inputs=dict(ctx.resume_inputs or {}),
            extracted=self.extracted,
        )

        # 4. Execute in dedicated GKE pod and stream back resulting ADK Events
        events = await effective_executor.execute(payload, effective_config)
        emitted = False
        for event in events:
            emitted = True
            # Ensure author matches this node so NodeRunner recognizes routes/outputs as native
            event.author = self.name
            if event.actions and event.actions.state_delta:
                ctx.state.update(dict(event.actions.state_delta))
            yield event

        if not emitted and resumed_confirmation:
            yield Event()


KaNode = K8sNode


@overload
def k8s_node(
    *,
    name: str | None = None,
    rerun_on_resume: bool | None = None,
    retry_config: RetryConfig | None = None,
    timeout: float | None = None,
    parallel_worker: bool = False,
    max_parallel_workers: int | None = None,
    auth_config: AuthConfig | None = None,
    require_confirmation: bool | Callable[..., bool] | None = None,
    parameter_binding: Literal["state", "node_input"] = "state",
    state_schema: type[BaseModel] | None = None,
    k8s_config: K8sConfig | None = None,
    executor: PodExecutor | None = None,
    image: str | None = None,
    namespace: str | None = None,
    service_account_name: str | None = None,
    runtime_class_name: str | None = None,
    cpu_request: str | None = None,
    cpu_limit: str | None = None,
    memory_request: str | None = None,
    memory_limit: str | None = None,
    env: dict[str, str] | None = None,
    secret_env: dict[str, tuple[str, str]] | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] | None = None,
    image_pull_secrets: list[str] | None = None,
    node_selector: dict[str, str] | None = None,
    cleanup_pod: bool | None = None,
    poll_interval: float | None = None,
    pod_startup_timeout: float | None = None,
    in_cluster: bool | None = None,
    kubeconfig_path: str | None = None,
    context: str | None = None,
    mode: Literal["deployment", "pod"] | None = None,
    replicas: int | None = None,
) -> Callable[[T], K8sNode]:
    ...


@overload
def k8s_node(
    node_like: NodeLike,
    *,
    name: str | None = None,
    rerun_on_resume: bool | None = None,
    retry_config: RetryConfig | None = None,
    timeout: float | None = None,
    parallel_worker: bool = False,
    max_parallel_workers: int | None = None,
    auth_config: AuthConfig | None = None,
    require_confirmation: bool | Callable[..., bool] | None = None,
    parameter_binding: Literal["state", "node_input"] = "state",
    state_schema: type[BaseModel] | None = None,
    k8s_config: K8sConfig | None = None,
    executor: PodExecutor | None = None,
    image: str | None = None,
    namespace: str | None = None,
    service_account_name: str | None = None,
    runtime_class_name: str | None = None,
    cpu_request: str | None = None,
    cpu_limit: str | None = None,
    memory_request: str | None = None,
    memory_limit: str | None = None,
    env: dict[str, str] | None = None,
    secret_env: dict[str, tuple[str, str]] | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] | None = None,
    image_pull_secrets: list[str] | None = None,
    node_selector: dict[str, str] | None = None,
    cleanup_pod: bool | None = None,
    poll_interval: float | None = None,
    pod_startup_timeout: float | None = None,
    in_cluster: bool | None = None,
    kubeconfig_path: str | None = None,
    context: str | None = None,
    mode: Literal["deployment", "pod"] | None = None,
    replicas: int | None = None,
) -> K8sNode:
    ...


def k8s_node(
    node_like: NodeLike | None = None,
    *,
    name: str | None = None,
    rerun_on_resume: bool | None = None,
    retry_config: RetryConfig | None = None,
    timeout: float | None = None,
    parallel_worker: bool = False,
    max_parallel_workers: int | None = None,
    auth_config: AuthConfig | None = None,
    require_confirmation: bool | Callable[..., bool] | None = None,
    parameter_binding: Literal["state", "node_input"] = "state",
    state_schema: type[BaseModel] | None = None,
    k8s_config: K8sConfig | None = None,
    executor: PodExecutor | None = None,
    image: str | None = None,
    namespace: str | None = None,
    service_account_name: str | None = None,
    runtime_class_name: str | None = None,
    cpu_request: str | None = None,
    cpu_limit: str | None = None,
    memory_request: str | None = None,
    memory_limit: str | None = None,
    env: dict[str, str] | None = None,
    secret_env: dict[str, tuple[str, str]] | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] | None = None,
    image_pull_secrets: list[str] | None = None,
    node_selector: dict[str, str] | None = None,
    cleanup_pod: bool | None = None,
    poll_interval: float | None = None,
    pod_startup_timeout: float | None = None,
    in_cluster: bool | None = None,
    kubeconfig_path: str | None = None,
    context: str | None = None,
    mode: Literal["deployment", "pod"] | None = None,
    replicas: int | None = None,
) -> Any:
    """Decorator or wrapper function to run any ADK ``NodeLike`` in a dedicated GKE pod.

    Accepts everything that ``google.adk.workflow.node`` accepts (functions,
    ``LlmAgent``, ``BaseAgent``, ``BaseTool``, ``BaseNode``) plus GKE pod options.

    Examples:
        >>> @kanode(image="us-docker.pkg.dev/my-proj/adk/worker:latest", namespace="agents")
        ... def task_C_node(node_input: str):
        ...     \"\"\"A FunctionNode to execute node C.\"\"\"
        ...     return Event(output="Task C completed")
    """
    if max_parallel_workers is not None:
        if not parallel_worker:
            raise WorkflowConfigurationError(
                "max_parallel_workers can only be set when parallel_worker is True."
            )
        if max_parallel_workers < 1:
            raise WorkflowConfigurationError(
                "max_parallel_workers must be greater than or equal to 1."
            )

    if node_like is None:
        return lambda target: k8s_node(
            target,
            name=name,
            rerun_on_resume=rerun_on_resume,
            retry_config=retry_config,
            timeout=timeout,
            parallel_worker=parallel_worker,
            max_parallel_workers=max_parallel_workers,
            auth_config=auth_config,
            require_confirmation=require_confirmation,
            parameter_binding=parameter_binding,
            state_schema=state_schema,
            k8s_config=k8s_config,
            executor=executor,
            image=image,
            namespace=namespace,
            service_account_name=service_account_name,
            runtime_class_name=runtime_class_name,
            cpu_request=cpu_request,
            cpu_limit=cpu_limit,
            memory_request=memory_request,
            memory_limit=memory_limit,
            env=env,
            secret_env=secret_env,
            labels=labels,
            annotations=annotations,
            image_pull_policy=image_pull_policy,
            image_pull_secrets=image_pull_secrets,
            node_selector=node_selector,
            cleanup_pod=cleanup_pod,
            poll_interval=poll_interval,
            pod_startup_timeout=pod_startup_timeout,
            in_cluster=in_cluster,
            kubeconfig_path=kubeconfig_path,
            context=context,
            mode=mode,
            replicas=replicas,
        )

    if node_like == "START":
        raise WorkflowConfigurationError(
            "START sentinel node cannot be wrapped in a K8sNode."
        )

    # Build the underlying ADK BaseNode using ADK's standard build_node
    built_inner = build_node(
        node_like,
        name=name,
        rerun_on_resume=rerun_on_resume,
        retry_config=retry_config,
        timeout=timeout,
        auth_config=auth_config,
        require_confirmation=require_confirmation,
        parameter_binding=parameter_binding,
    )
    if state_schema is not None:
        built_inner.state_schema = state_schema

    # Build node-level K8sConfig
    k8s_kwargs: dict[str, Any] = {
        k: v
        for k, v in {
            "image": image,
            "namespace": namespace,
            "service_account_name": service_account_name,
            "runtime_class_name": runtime_class_name,
            "cpu_request": cpu_request,
            "cpu_limit": cpu_limit,
            "memory_request": memory_request,
            "memory_limit": memory_limit,
            "env": env,
            "secret_env": secret_env,
            "labels": labels,
            "annotations": annotations,
            "image_pull_policy": image_pull_policy,
            "image_pull_secrets": image_pull_secrets,
            "node_selector": node_selector,
            "cleanup_pod": cleanup_pod,
            "poll_interval": poll_interval,
            "pod_startup_timeout": pod_startup_timeout,
            "in_cluster": in_cluster,
            "kubeconfig_path": kubeconfig_path,
            "context": context,
            "mode": mode,
            "replicas": replicas,
        }.items()
        if v is not None
    }
    override_cfg = K8sConfig(**k8s_kwargs) if k8s_kwargs else None
    if k8s_config is not None and override_cfg is not None:
        effective_k8s_config = k8s_config.merge_with(override_cfg)
    elif k8s_config is not None:
        effective_k8s_config = k8s_config
    elif override_cfg is not None:
        effective_k8s_config = override_cfg
    else:
        effective_k8s_config = K8sConfig()

    extracted = extract_node_implementation(
        node_like,
        built_inner,
        parameter_binding=parameter_binding,
    )

    return K8sNode(
        name=built_inner.name,
        description=built_inner.description,
        rerun_on_resume=built_inner.rerun_on_resume,
        wait_for_output=built_inner.wait_for_output,
        retry_config=built_inner.retry_config,
        timeout=built_inner.timeout,
        input_schema=built_inner.input_schema,
        output_schema=built_inner.output_schema,
        state_schema=built_inner.state_schema,
        parallel_worker=parallel_worker,
        max_parallel_workers=max_parallel_workers,
        inner_node=built_inner,
        k8s_config=effective_k8s_config,
        extracted=extracted,
        executor=executor,
    )


kanode = k8s_node
kanote = k8s_node
