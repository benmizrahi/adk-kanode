"""Tests for warm Deployment mode (K8sDeploymentExecutor + HTTP worker)."""

from __future__ import annotations

import base64
import types as py_types
from typing import Any

from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types
import httpx
from kubernetes.client.exceptions import ApiException
import pytest

from adk_kanode import build_worker_bootstrap_script
from adk_kanode import encode_event_line
from adk_kanode import K8sDeploymentExecutor
from adk_kanode import K8sPlugin
from adk_kanode import K8sPodExecutionError
from adk_kanode import kanode


@kanode(namespace="adk-workers", replicas=2)
def shout_node(node_input: str):
    """Upper-cases the input on a warm worker."""
    return Event(output=node_input.upper(), state={"shouted": True})


@kanode(namespace="adk-workers", mode="pod")
def one_shot_node(node_input: str):
    """Runs in a fresh single-use pod."""
    return Event(output=node_input)


class FakeCluster:
    """In-memory stand-in for CoreV1Api + AppsV1Api."""

    def __init__(self) -> None:
        self.deployments: dict[str, Any] = {}
        self.services: dict[str, Any] = {}
        self.secrets: dict[str, Any] = {}
        self.pods: list[Any] = []
        self.writes: list[str] = []

    @staticmethod
    def _get(store: dict[str, Any], name: str) -> Any:
        if name not in store:
            raise ApiException(status=404)
        return store[name]

    def read_namespaced_deployment(self, *, name: str, namespace: str) -> Any:
        return self._get(self.deployments, name)

    def create_namespaced_deployment(self, *, namespace: str, body: Any) -> None:
        self.writes.append("create_deployment")
        self.deployments[body.metadata.name] = body

    def replace_namespaced_deployment(self, *, name: str, namespace: str, body: Any) -> None:
        self.writes.append("replace_deployment")
        self.deployments[name] = body

    def read_namespaced_service(self, *, name: str, namespace: str) -> Any:
        return self._get(self.services, name)

    def create_namespaced_service(self, *, namespace: str, body: Any) -> None:
        self.services[body.metadata.name] = body

    def patch_namespaced_service(self, *, name: str, namespace: str, body: Any) -> None:
        self.services[name] = body

    def read_namespaced_secret(self, *, name: str, namespace: str) -> Any:
        return self._get(self.secrets, name)

    def create_namespaced_secret(self, *, namespace: str, body: Any) -> None:
        token = body.string_data["token"].encode("utf-8")
        self.secrets[body.metadata.name] = py_types.SimpleNamespace(
            data={"token": base64.b64encode(token).decode("ascii")}
        )

    def create_namespaced_pod(self, *, namespace: str, body: Any) -> Any:
        self.pods.append(body)
        return body

    def read_namespaced_pod_status(self, *, name: str, namespace: str) -> Any:
        return py_types.SimpleNamespace(status=py_types.SimpleNamespace(phase="Succeeded"))

    def read_namespaced_pod_log(self, *, name: str, namespace: str) -> str:
        return encode_event_line(Event(output="from one-shot pod"))

    def delete_namespaced_pod(self, *, name: str, namespace: str) -> None:
        pass

    def token(self) -> str:
        raw = self.secrets["adk-kanode-worker-token"].data["token"]
        return base64.b64decode(raw).decode("utf-8")


def build_worker_app(cluster: FakeCluster, name: str) -> Any:
    """Boots the real worker script for a deployment, exactly as the pod would."""
    container = cluster.deployments[name].spec.template.spec.containers[0]
    env = {e.name: e.value for e in container.env}
    namespace: dict[str, Any] = {"__name__": "adk_kanode_worker"}
    exec(build_worker_bootstrap_script(), namespace)  # noqa: S102 - our own script
    return namespace["build_app"](
        env["ADK_K8S_NODE_SPEC"], env["ADK_K8S_SPEC_HASH"], cluster.token()
    )


class ClusterTransport(httpx.AsyncBaseTransport):
    """Routes http://<service>.<ns>.svc:8080 to the worker app of that deployment."""

    def __init__(self, cluster: FakeCluster) -> None:
        self.cluster = cluster
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        name = request.url.host.split(".")[0]
        app = build_worker_app(self.cluster, name)
        return await httpx.ASGITransport(app=app).handle_async_request(request)


async def run_workflow(node: Any, plugin: K8sPlugin, text: str) -> list[Any]:
    workflow = Workflow(name="wf", edges=[("START", node)])
    app = App(name="deploy_app", root_agent=workflow, plugins=[plugin])
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="deploy_app", user_id="user_1"
    )
    outputs: list[Any] = []
    async for event in runner.run_async(
        user_id="user_1",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=text)]),
    ):
        if event.output is not None:
            outputs.append(event.output)
    return outputs


def make_plugin(cluster: FakeCluster) -> tuple[K8sPlugin, ClusterTransport]:
    transport = ClusterTransport(cluster)
    executor = K8sDeploymentExecutor(
        core_v1_api=cluster, apps_v1_api=cluster, transport=transport
    )
    return K8sPlugin(executor=executor, poll_interval=0.01), transport


@pytest.mark.asyncio
async def test_deployment_mode_reuses_one_warm_worker():
    """A node gets one Deployment + Service, reused across calls, with no pod per call."""
    cluster = FakeCluster()
    plugin, transport = make_plugin(cluster)

    assert (await run_workflow(shout_node, plugin, "hello"))[-1] == "HELLO"
    assert (await run_workflow(shout_node, plugin, "again"))[-1] == "AGAIN"

    assert transport.requests == 2
    assert cluster.writes == ["create_deployment"]
    assert cluster.pods == []
    deployment = cluster.deployments["shout-node"]
    assert deployment.metadata.namespace == "adk-workers"
    assert deployment.spec.replicas == 2
    assert "shout-node" in cluster.services
    env = {e.name: e for e in deployment.spec.template.spec.containers[0].env}
    # The token is referenced from a Secret, never inlined in the Deployment.
    assert env["ADK_K8S_WORKER_TOKEN"].value is None
    assert env["ADK_K8S_WORKER_TOKEN"].value_from.secret_key_ref.name == "adk-kanode-worker-token"


@pytest.mark.asyncio
async def test_config_change_rolls_the_deployment():
    """Changing the node's config updates the existing Deployment instead of adding one."""
    cluster = FakeCluster()
    plugin, _ = make_plugin(cluster)
    await run_workflow(shout_node, plugin, "hello")

    plugin.config = plugin.config.model_copy(update={"env": {"NEW_FLAG": "1"}})
    assert (await run_workflow(shout_node, plugin, "hello"))[-1] == "HELLO"
    assert cluster.writes == ["create_deployment", "replace_deployment"]
    assert list(cluster.deployments) == ["shout-node"]


@pytest.mark.asyncio
async def test_pod_mode_node_still_uses_one_shot_pod():
    """mode="pod" opts a node out of the warm Deployment."""
    cluster = FakeCluster()
    plugin, transport = make_plugin(cluster)

    assert (await run_workflow(one_shot_node, plugin, "hi"))[-1] == "from one-shot pod"
    assert len(cluster.pods) == 1
    assert cluster.deployments == {}
    assert transport.requests == 0


@pytest.mark.asyncio
async def test_never_overwrites_a_deployment_it_does_not_own():
    """A node whose name matches an unrelated Deployment fails instead of replacing it."""
    cluster = FakeCluster()
    cluster.deployments["shout-node"] = py_types.SimpleNamespace(
        metadata=py_types.SimpleNamespace(labels={"app": "something-else"}, annotations=None)
    )
    plugin, _ = make_plugin(cluster)
    with pytest.raises(K8sPodExecutionError, match="not managed by"):
        await run_workflow(shout_node, plugin, "hello")
    assert cluster.writes == []


def test_prewarm_creates_deployments_before_first_request():
    """plugin.prewarm() provisions deployment-mode nodes only."""
    cluster = FakeCluster()
    plugin, _ = make_plugin(cluster)
    workflow = Workflow(name="wf", edges=[("START", shout_node, one_shot_node)])

    thread = plugin.prewarm(workflow)
    assert thread is not None
    thread.join(timeout=10)
    assert list(cluster.deployments) == ["shout-node"]


def test_prewarm_is_a_noop_inside_a_worker(monkeypatch: pytest.MonkeyPatch):
    """Workers import the agent module too and must not provision anything."""
    monkeypatch.setenv("ADK_K8S_WORKER", "1")
    cluster = FakeCluster()
    plugin, _ = make_plugin(cluster)
    workflow = Workflow(name="wf", edges=[("START", shout_node)])
    assert plugin.prewarm(workflow) is None
    assert cluster.deployments == {}


@pytest.mark.asyncio
async def test_worker_rejects_bad_token_and_stale_revision():
    """The worker fails closed on a wrong token and refuses calls for another revision."""
    cluster = FakeCluster()
    plugin, _ = make_plugin(cluster)
    await run_workflow(shout_node, plugin, "hello")
    app = build_worker_app(cluster, "shout-node")
    good = {"Authorization": f"Bearer {cluster.token()}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as http:
        assert (await http.get("/healthz")).status_code == 200
        body = {"node_name": "shout_node", "node_input": "x", "spec_hash": "wrong"}
        assert (await http.post("/execute", json=body)).status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert (await http.post("/execute", json=body, headers=bad)).status_code == 401
        assert (await http.post("/execute", json=body, headers=good)).status_code == 409


def test_worker_refuses_to_start_without_token():
    namespace: dict[str, Any] = {"__name__": "adk_kanode_worker"}
    exec(build_worker_bootstrap_script(), namespace)  # noqa: S102 - our own script
    with pytest.raises(RuntimeError):
        namespace["build_app"]("e30=", "hash", "")
