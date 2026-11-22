"""Tests for @kanode / K8sNode and K8sPlugin in ADK 2.0 Workflows."""

from __future__ import annotations

import types as py_types
from typing import Any

from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.adk.tools.function_tool import FunctionTool
from google.adk.workflow import BaseNode
from google.adk.workflow import JoinNode
from google.adk.workflow import Workflow
from google.genai import types
from pydantic import BaseModel
import pytest

from adk_kanode import encode_event_line
from adk_kanode import InProcessPodExecutor
from adk_kanode import K8sConfig
from adk_kanode import K8sNode
from adk_kanode import K8sPlugin
from adk_kanode import K8sPodExecutionError
from adk_kanode import K8sPodExecutor
from adk_kanode import kanode
from adk_kanode import kanote
from adk_kanode import k8s_node
from adk_kanode import SubprocessPodExecutor


class ItemPayload(BaseModel):
    value: int
    label: str


@kanode(
    image="us-docker.pkg.dev/my-gke-proj/adk/worker:latest",
    namespace="adk-workers",
    cpu_request="250m",
    memory_limit="512Mi",
)
def task_C_node(node_input: str):
    """A FunctionNode to execute node C."""
    return Event(output=f"Task C completed: {node_input}")


@kanote(namespace="custom-ns")
def routed_classifier_node(node_input: str):
    """Routes based on input content and updates workflow state."""
    if "urgent" in node_input.lower():
        return Event(
            output=node_input.upper(),
            route="urgent",
            state={"priority": "high"},
        )
    return Event(
        output=node_input,
        route="normal",
        state={"priority": "normal"},
    )


@k8s_node
async def async_enrichment_node(node_input: str, priority: str = "unknown") -> dict[str, str]:
    """An async node that reads 'priority' from workflow state."""
    return {"message": node_input, "priority": priority}


def compute_square(x: int) -> dict[str, int]:
    """Computes the square of an integer."""
    return {"squared": x * x}


class CustomMultiplierNode(BaseNode):
    factor: int = 3

    async def _run_impl(self, *, ctx: Any, node_input: Any):
        yield Event(output=int(node_input) * self.factor)


def test_kanode_is_valid_adk_base_node():
    """Verifies @kanode returns a K8sNode (BaseNode subclass) accepted by ADK Workflow."""
    assert isinstance(task_C_node, K8sNode)
    assert isinstance(task_C_node, BaseNode)
    assert task_C_node.name == "task_C_node"
    assert task_C_node.description == "A FunctionNode to execute node C."
    assert task_C_node.k8s_config.namespace == "adk-workers"
    assert task_C_node.k8s_config.image == "us-docker.pkg.dev/my-gke-proj/adk/worker:latest"
    assert task_C_node.extracted.kind == "function"
    assert task_C_node.extracted.func_name == "task_C_node"
    assert "@kanode" not in (task_C_node.extracted.source_code or "")
    assert "Task C completed" in (task_C_node.extracted.source_code or "")


@pytest.mark.asyncio
async def test_workflow_with_subprocess_pod_executor():
    """Tests full end-to-end extraction and execution in an isolated Python subprocess."""
    plugin = K8sPlugin(
        namespace="gke-prod",
        executor=SubprocessPodExecutor(),
    )

    workflow = Workflow(
        name="k8s_workflow",
        edges=[
            ("START", routed_classifier_node),
            (
                routed_classifier_node,
                {
                    "urgent": async_enrichment_node,
                    "normal": task_C_node,
                },
            ),
        ],
    )

    app = App(name="test_app", root_agent=workflow, plugins=[plugin])
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="test_app", user_id="user_1"
    )

    outputs: list[Any] = []
    async for event in runner.run_async(
        user_id="user_1",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="urgent deployment")]
        ),
    ):
        if event.output is not None:
            outputs.append(event.output)

    assert outputs[-1] == {"message": "URGENT DEPLOYMENT", "priority": "high"}
    assert "routed_classifier_node" in plugin.executed_nodes
    assert "async_enrichment_node" in plugin.executed_nodes


@pytest.mark.asyncio
async def test_kanode_wraps_tools_and_custom_nodes_in_workflow():
    """Verifies kanode() accepts BaseTool (FunctionTool) and custom BaseNode subclasses."""
    tool_node = kanode(
        FunctionTool(func=compute_square),
        name="square_tool_k8s",
        executor=SubprocessPodExecutor(),
    )
    custom_k8s = kanode(
        CustomMultiplierNode(name="triple_node", factor=3),
        executor=SubprocessPodExecutor(),
    )

    def extract_squared(node_input: dict[str, int]) -> int:
        return node_input["squared"]

    workflow = Workflow(
        name="poly_workflow",
        edges=[
            ("START", tool_node, extract_squared, custom_k8s),
        ],
    )

    app = App(name="poly_app", root_agent=workflow)
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="poly_app", user_id="user_1"
    )

    final_output = None
    async for event in runner.run_async(
        user_id="user_1",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text='{"x": 4}')]
        ),
    ):
        if event.output is not None:
            final_output = event.output

    assert final_output == 48


@pytest.mark.asyncio
async def test_kanode_parallel_worker_and_join_node():
    """Verifies @kanode supports parallel_worker=True and JoinNode fan-in."""

    @kanode(parallel_worker=True, max_parallel_workers=2, executor=InProcessPodExecutor())
    def double_item(node_input: int) -> int:
        return node_input * 2

    def produce_list(node_input: str) -> list[int]:
        return [1, 2, 3]

    join = JoinNode(name="join_results")

    workflow = Workflow(
        name="parallel_k8s_workflow",
        edges=[
            ("START", produce_list),
            (produce_list, double_item),
            (double_item, join),
        ],
    )

    app = App(name="parallel_app", root_agent=workflow)
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="parallel_app", user_id="user_1"
    )

    final_output = None
    async for event in runner.run_async(
        user_id="user_1",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text="go")]),
    ):
        if event.output is not None:
            final_output = event.output

    assert final_output == {"double_item": [2, 4, 6]}


@pytest.mark.asyncio
async def test_k8s_pod_executor_spawns_and_cleans_up_gke_pod():
    """Verifies K8sPodExecutor creates a V1Pod in GKE, reads logs, emits Event, and deletes the pod."""
    created_pods: list[tuple[str, Any]] = []
    deleted_pods: list[tuple[str, str]] = []

    class FakeCoreV1Api:
        def create_namespaced_pod(self, *, namespace: str, body: Any) -> Any:
            created_pods.append((namespace, body))
            return body

        def read_namespaced_pod_status(self, *, name: str, namespace: str) -> Any:
            return py_types.SimpleNamespace(
                status=py_types.SimpleNamespace(phase="Succeeded")
            )

        def read_namespaced_pod_log(self, *, name: str, namespace: str) -> str:
            ev = Event(output="Task C completed")
            return f"Starting pod execution...\n{encode_event_line(ev)}\nDone.\n"

        def delete_namespaced_pod(self, *, name: str, namespace: str) -> None:
            deleted_pods.append((namespace, name))

    fake_api = FakeCoreV1Api()
    k8s_executor = K8sPodExecutor(core_v1_api=fake_api)
    plugin = K8sPlugin(
        config=K8sConfig(
            namespace="gke-agents",
            service_account_name="adk-workload-sa",
            env={"CLUSTER_ENV": "production"},
            secret_env={"API_KEY": ("agent-secrets", "api-key")},
            poll_interval=0.01,
        ),
        executor=k8s_executor,
    )

    workflow = Workflow(
        name="gke_pod_workflow",
        edges=[("START", task_C_node)],
    )
    app = App(name="gke_app", root_agent=workflow, plugins=[plugin])
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="gke_app", user_id="user_1"
    )

    outputs: list[Any] = []
    async for event in runner.run_async(
        user_id="user_1",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="run on gke")]
        ),
    ):
        if event.output is not None:
            outputs.append(event.output)

    assert outputs[-1] == "Task C completed"
    assert len(created_pods) == 1
    namespace, pod = created_pods[0]
    # Node-level namespace ("adk-workers") overrides plugin default ("gke-agents")
    assert namespace == "adk-workers"
    assert pod.spec.service_account_name == "adk-workload-sa"
    container = pod.spec.containers[0]
    assert container.image == "us-docker.pkg.dev/my-gke-proj/adk/worker:latest"
    assert container.resources.requests["cpu"] == "250m"
    assert container.resources.limits["memory"] == "512Mi"
    assert container.security_context.allow_privilege_escalation is False
    env_names = {e.name for e in container.env}
    assert {"ADK_K8S_PAYLOAD", "PYTHONUNBUFFERED", "CLUSTER_ENV", "API_KEY"} <= env_names
    assert len(deleted_pods) == 1
    assert deleted_pods[0][0] == "adk-workers"


@pytest.mark.asyncio
async def test_k8s_pod_executor_raises_on_failed_pod():
    """Verifies K8sPodExecutor raises K8sPodExecutionError and still cleans up the failed pod."""
    deleted_pods: list[tuple[str, str]] = []

    class FailingCoreV1Api:
        def create_namespaced_pod(self, *, namespace: str, body: Any) -> Any:
            return body

        def read_namespaced_pod_status(self, *, name: str, namespace: str) -> Any:
            return py_types.SimpleNamespace(
                status=py_types.SimpleNamespace(phase="Failed")
            )

        def read_namespaced_pod_log(self, *, name: str, namespace: str) -> str:
            return "Traceback: RuntimeError in pod"

        def delete_namespaced_pod(self, *, name: str, namespace: str) -> None:
            deleted_pods.append((namespace, name))

    executor = K8sPodExecutor(core_v1_api=FailingCoreV1Api())

    @kanode(namespace="fail-ns", executor=executor, poll_interval=0.01)
    def failing_node(node_input: str) -> str:
        return node_input

    workflow = Workflow(name="fail_wf", edges=[("START", failing_node)])
    app = App(name="fail_app", root_agent=workflow)
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="fail_app", user_id="user_1"
    )

    error_events = []
    with pytest.raises(K8sPodExecutionError) as exc_info:
        async for event in runner.run_async(
            user_id="user_1",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text="hi")]
            ),
        ):
            if event.error_code or event.error_message:
                error_events.append(event)

    assert exc_info.value.logs == "Traceback: RuntimeError in pod"
    assert len(error_events) == 1
    assert error_events[0].error_code == K8sPodExecutionError.__name__
    assert len(deleted_pods) == 1


def test_k8s_config_from_environment_variables(monkeypatch: pytest.MonkeyPatch):
    """Verifies K8sConfig and K8sPlugin automatically read settings from environment variables."""
    monkeypatch.setenv("ADK_K8S_IMAGE", "us-docker.pkg.dev/env-proj/adk/env-worker:v2")
    monkeypatch.setenv("ADK_K8S_NAMESPACE", "env-namespace")
    monkeypatch.setenv("ADK_K8S_SERVICE_ACCOUNT", "env-sa")
    monkeypatch.setenv("ADK_K8S_RUNTIME_CLASS_NAME", "gvisor")
    monkeypatch.setenv("ADK_K8S_CPU_REQUEST", "500m")
    monkeypatch.setenv("ADK_K8S_MEMORY_LIMIT", "1Gi")
    monkeypatch.setenv("ADK_K8S_CLEANUP_POD", "false")

    plugin = K8sPlugin()
    assert plugin.config.image == "us-docker.pkg.dev/env-proj/adk/env-worker:v2"
    assert plugin.config.namespace == "env-namespace"
    assert plugin.config.service_account_name == "env-sa"
    assert plugin.config.runtime_class_name == "gvisor"
    assert plugin.config.cpu_request == "500m"
    assert plugin.config.memory_limit == "1Gi"
    assert plugin.config.cleanup_pod is False


def test_k8s_pod_manifest_sets_runtime_class_name_and_validates():
    """Verifies runtime_class_name is validated, merged, and written to V1PodSpec."""
    from adk_kanode import PodExecutionPayload

    @kanode(
        mode="pod",
        runtime_class_name="gvisor",
        node_selector={"cloud.google.com/compute-class": "sandbox-gvisor-class"},
    )
    def sandboxed_node(node_input: str) -> str:
        return node_input

    plugin = K8sPlugin(runtime_class_name="kata")
    merged = plugin.config.merge_with(sandboxed_node.k8s_config)
    assert merged.runtime_class_name == "gvisor"
    assert merged.node_selector == {
        "cloud.google.com/compute-class": "sandbox-gvisor-class"
    }

    payload = PodExecutionPayload(
        node_name=sandboxed_node.name,
        node_input="hi",
        extracted=sandboxed_node.extracted,
    )
    pod = K8sPodExecutor.build_pod_manifest(payload, merged)
    assert pod.spec.runtime_class_name == "gvisor"
    assert pod.spec.node_selector == {
        "cloud.google.com/compute-class": "sandbox-gvisor-class"
    }

    # Inheriting runtime_class_name from plugin when not overridden on the node
    inherited = plugin.config.merge_with(task_C_node.k8s_config)
    assert inherited.runtime_class_name == "kata"

    with pytest.raises(ValueError, match="runtime_class_name"):
        K8sConfig(runtime_class_name="INVALID_UPPER")


