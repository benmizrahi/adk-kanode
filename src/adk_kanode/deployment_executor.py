"""Warm per-node Deployment executor (``mode="deployment"``).

Each ``@kanode`` gets one long-lived ``Deployment`` + ``ClusterIP Service``
running the HTTP worker from ``_bootstrap.py``. Calls become a single in-cluster
HTTP request to an already-warm process instead of a cold pod per call.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from typing import Any

from google.adk.events.event import Event
import httpx

from .config import K8sConfig
from .config import sanitize_k8s_name
from .executor import K8sPodExecutionError
from .executor import K8sPodExecutor
from .executor import load_kube_configuration
from .executor import PodExecutor
from .extractor import ExtractedImplementation
from .pod_runner import build_worker_bootstrap_script
from .pod_runner import PodExecutionPayload

logger = logging.getLogger("google_adk.adk_graph_k8s.deployment_executor")

WORKER_PORT = 8080
WORKER_LABEL = "adk.google.com/worker"
HASH_ANNOTATION = "adk.google.com/spec-hash"
TOKEN_SECRET_NAME = "adk-kanode-worker-token"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY = "adk-kanode"

# Config fields that only affect the workflow host, not the worker Deployment.
_HOST_ONLY_FIELDS = {
    "mode",
    "in_cluster",
    "kubeconfig_path",
    "context",
    "poll_interval",
    "pod_startup_timeout",
    "cleanup_pod",
}
_RESERVED_ENV = {
    "ADK_K8S_NODE_SPEC",
    "ADK_K8S_SPEC_HASH",
    "ADK_K8S_WORKER",
    "ADK_K8S_WORKER_TOKEN",
}


def worker_name(extracted: ExtractedImplementation) -> str:
    """Deployment / Service name for a node: the node name itself (``my_node`` -> ``my-node``)."""
    name = sanitize_k8s_name(extracted.name, max_length=63)
    # Service names must start with a letter.
    return name if name[0].isalpha() else f"node-{name}"[:63].rstrip("-")


def compute_spec_hash(extracted: ExtractedImplementation, config: K8sConfig) -> str:
    """Hash of everything that defines a worker; a change triggers a rolling update."""
    blob = json.dumps(
        {
            "extracted": extracted.model_dump(mode="json"),
            "config": config.model_dump(mode="json", exclude=_HOST_ONLY_FIELDS),
            "script": build_worker_bootstrap_script(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _apply(read: Any, create: Any, update: Any, *, name: str, namespace: str, body: Any, spec_hash: str) -> None:
    """Creates the object, or updates it when its spec hash changed."""
    from kubernetes.client.exceptions import ApiException

    try:
        existing = read(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status != 404:
            raise
        try:
            create(namespace=namespace, body=body)
        except ApiException as create_exc:
            if create_exc.status != 409:  # 409: another host replica created it first
                raise
        return
    if (existing.metadata.labels or {}).get(MANAGED_BY_LABEL) != MANAGED_BY:
        # Never overwrite something we did not create (e.g. the workflow host's own Deployment).
        raise K8sPodExecutionError(
            f"{name!r} already exists in namespace {namespace!r} and is not managed by "
            "adk-kanode. Rename the node.",
            namespace=namespace,
        )
    if (existing.metadata.annotations or {}).get(HASH_ANNOTATION) != spec_hash:
        update(name=name, namespace=namespace, body=body)


class K8sDeploymentExecutor(PodExecutor):
    """Runs each node on a warm ``Deployment``; falls back to one-shot pods for ``mode="pod"``."""

    def __init__(
        self,
        core_v1_api: Any | None = None,
        apps_v1_api: Any | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._core = core_v1_api
        self._apps = apps_v1_api
        self._transport = transport  # Custom transport (tests); skips the in-cluster check.
        self._pod_executor = K8sPodExecutor(core_v1_api)
        self._lock = threading.Lock()
        self._applied: dict[tuple[str, str], str] = {}
        self._tokens: dict[str, str] = {}

    def _uses_deployment(self, config: K8sConfig) -> bool:
        if config.mode != "deployment":
            return False
        if self._transport is not None or config.in_cluster is True:
            return True
        # Worker Services are only resolvable from inside the cluster.
        return config.in_cluster is None and bool(os.environ.get("KUBERNETES_SERVICE_HOST"))

    def _ensure_token(self, core: Any, namespace: str) -> None:
        """Loads (or creates once) the shared bearer token Secret for this namespace."""
        from kubernetes import client
        from kubernetes.client.exceptions import ApiException

        if namespace in self._tokens:
            return
        try:
            secret = core.read_namespaced_secret(name=TOKEN_SECRET_NAME, namespace=namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            try:
                core.create_namespaced_secret(
                    namespace=namespace,
                    body=client.V1Secret(
                        metadata=client.V1ObjectMeta(name=TOKEN_SECRET_NAME, namespace=namespace),
                        string_data={"token": secrets.token_urlsafe(32)},
                    ),
                )
            except ApiException as create_exc:
                if create_exc.status != 409:
                    raise
            secret = core.read_namespaced_secret(name=TOKEN_SECRET_NAME, namespace=namespace)
        self._tokens[namespace] = base64.b64decode(secret.data["token"]).decode("utf-8")

    @staticmethod
    def build_manifests(
        extracted: ExtractedImplementation, config: K8sConfig, *, name: str, spec_hash: str
    ) -> tuple[Any, Any]:
        """Builds the worker ``V1Deployment`` and ``V1Service`` for a node."""
        from kubernetes import client

        spec_b64 = base64.b64encode(extracted.model_dump_json().encode("utf-8")).decode("ascii")
        env = [
            client.V1EnvVar(name="ADK_K8S_NODE_SPEC", value=spec_b64),
            client.V1EnvVar(name="ADK_K8S_SPEC_HASH", value=spec_hash),
            client.V1EnvVar(name="ADK_K8S_WORKER", value="1"),
            client.V1EnvVar(name="PYTHONUNBUFFERED", value="1"),
            client.V1EnvVar(
                name="ADK_K8S_WORKER_TOKEN",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name=TOKEN_SECRET_NAME, key="token")
                ),
            ),
        ]
        env += [
            client.V1EnvVar(name=k, value=str(v))
            for k, v in config.env.items()
            if k not in _RESERVED_ENV
        ]
        env += [
            client.V1EnvVar(
                name=k,
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(name=secret_name, key=secret_key)
                ),
            )
            for k, (secret_name, secret_key) in config.secret_env.items()
            if k not in _RESERVED_ENV
        ]

        requests = {"cpu": config.cpu_request, "memory": config.memory_request}
        limits = {"cpu": config.cpu_limit, "memory": config.memory_limit}
        container = client.V1Container(
            name="adk-node",
            image=config.image,
            image_pull_policy=config.image_pull_policy,
            command=["python3", "-u", "-c", build_worker_bootstrap_script()],
            env=env,
            ports=[client.V1ContainerPort(container_port=WORKER_PORT, name="http")],
            readiness_probe=client.V1Probe(
                http_get=client.V1HTTPGetAction(path="/healthz", port=WORKER_PORT),
                period_seconds=1,
                timeout_seconds=2,
            ),
            resources=client.V1ResourceRequirements(
                requests={k: v for k, v in requests.items() if v} or None,
                limits={k: v for k, v in limits.items() if v} or None,
            ),
            security_context=client.V1SecurityContext(
                allow_privilege_escalation=False,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
            ),
        )

        selector = {WORKER_LABEL: name}
        labels = {**config.labels, MANAGED_BY_LABEL: MANAGED_BY, **selector}
        metadata = client.V1ObjectMeta(
            name=name,
            namespace=config.namespace,
            labels=labels,
            annotations={HASH_ANNOTATION: spec_hash},
        )
        deployment = client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=metadata,
            spec=client.V1DeploymentSpec(
                replicas=config.replicas,
                selector=client.V1LabelSelector(match_labels=selector),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=labels, annotations=config.annotations or None
                    ),
                    spec=client.V1PodSpec(
                        containers=[container],
                        service_account_name=config.service_account_name,
                        runtime_class_name=config.runtime_class_name,
                        node_selector=config.node_selector or None,
                        image_pull_secrets=[
                            client.V1LocalObjectReference(name=s)
                            for s in config.image_pull_secrets
                        ]
                        or None,
                    ),
                ),
            ),
        )
        service = client.V1Service(
            api_version="v1",
            kind="Service",
            metadata=metadata,
            spec=client.V1ServiceSpec(
                type="ClusterIP",
                selector=selector,
                ports=[client.V1ServicePort(name="http", port=WORKER_PORT, target_port=WORKER_PORT)],
            ),
        )
        return deployment, service

    def ensure_worker(
        self, extracted: ExtractedImplementation, config: K8sConfig
    ) -> tuple[str, str, str]:
        """Creates / updates the node's Deployment + Service. Returns ``(url, token, spec_hash)``."""
        name = worker_name(extracted)
        namespace = config.namespace
        spec_hash = compute_spec_hash(extracted, config)
        with self._lock:
            if self._applied.get((namespace, name)) != spec_hash:
                if self._core is None or self._apps is None:
                    from kubernetes import client

                    load_kube_configuration(config)
                    self._core = self._core or client.CoreV1Api()
                    self._apps = self._apps or client.AppsV1Api()
                self._ensure_token(self._core, namespace)
                deployment, service = self.build_manifests(
                    extracted, config, name=name, spec_hash=spec_hash
                )
                _apply(
                    self._apps.read_namespaced_deployment,
                    self._apps.create_namespaced_deployment,
                    self._apps.replace_namespaced_deployment,
                    name=name, namespace=namespace, body=deployment, spec_hash=spec_hash,
                )
                _apply(
                    self._core.read_namespaced_service,
                    self._core.create_namespaced_service,
                    self._core.patch_namespaced_service,
                    name=name, namespace=namespace, body=service, spec_hash=spec_hash,
                )
                self._applied[(namespace, name)] = spec_hash
                logger.info("Worker deployment %s/%s is at revision %s", namespace, name, spec_hash)
            # TODO(security): Plain HTTP inside the cluster; use a service mesh (mTLS) if
            # in-cluster traffic must be encrypted.
            url = f"http://{name}.{namespace}.svc:{WORKER_PORT}/execute"
            return url, self._tokens[namespace], spec_hash

    def prewarm(self, extracted: ExtractedImplementation, config: K8sConfig) -> None:
        """Provisions the node's worker ahead of the first request."""
        if self._uses_deployment(config):
            self.ensure_worker(extracted, config)

    async def execute(self, payload: PodExecutionPayload, config: K8sConfig) -> list[Event]:
        if not self._uses_deployment(config):
            if config.mode == "deployment":
                logger.warning(
                    "Deployment mode needs the workflow host to run inside the cluster; "
                    "running node %s as a one-shot pod instead.",
                    payload.node_name,
                )
            return await self._pod_executor.execute(payload, config)

        url, token, spec_hash = await asyncio.to_thread(
            self.ensure_worker, payload.extracted, config
        )
        body = payload.model_dump(mode="json", exclude={"extracted"})
        body["spec_hash"] = spec_hash
        deadline = time.monotonic() + config.pod_startup_timeout
        async with httpx.AsyncClient(
            transport=self._transport, timeout=httpx.Timeout(None, connect=5.0)
        ) as http:
            while True:
                # Only retry when the node certainly did not run: the worker is not
                # reachable yet, or it is still the previous revision (409).
                try:
                    response = await http.post(
                        url, json=body, headers={"Authorization": f"Bearer {token}"}
                    )
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    response = None
                except httpx.HTTPError as exc:
                    raise K8sPodExecutionError(
                        f"Request to worker for node {payload.node_name!r} failed: {exc!r}",
                        namespace=config.namespace,
                    ) from exc
                if response is not None and response.status_code == 200:
                    return [Event.model_validate(e) for e in response.json()["events"]]
                if response is not None and response.status_code not in (409, 503):
                    if response.status_code == 401:
                        self._tokens.pop(config.namespace, None)  # Re-read the token next call.
                    raise K8sPodExecutionError(
                        f"Worker for node {payload.node_name!r} failed "
                        f"(HTTP {response.status_code}): {response.text[:2000]}",
                        namespace=config.namespace,
                        logs=response.text,
                    )
                if time.monotonic() >= deadline:
                    raise K8sPodExecutionError(
                        f"Timed out waiting for worker of node {payload.node_name!r} "
                        f"in namespace {config.namespace!r} to become ready.",
                        namespace=config.namespace,
                    )
                await asyncio.sleep(min(config.poll_interval, 0.5))

    async def cleanup_all(self) -> None:
        """Deletes in-flight one-shot pods; warm Deployments are kept on purpose."""
        await self._pod_executor.cleanup_all()


_default_executor: K8sDeploymentExecutor | None = None


def get_default_executor() -> K8sDeploymentExecutor:
    """Shared executor used by ``@kanode`` nodes when no ``K8sPlugin`` is registered."""
    global _default_executor
    if _default_executor is None:
        _default_executor = K8sDeploymentExecutor()
    return _default_executor
