"""GKE / Kubernetes Pod executors for ADK Workflow nodes."""

from __future__ import annotations

import abc
import asyncio
import base64
import logging
import os
import sys
import time
import uuid
from typing import Any

from google.adk.events.event import Event

from .config import K8sConfig
from .config import sanitize_k8s_name
from .pod_runner import build_pod_bootstrap_script
from .pod_runner import decode_events_from_logs
from .pod_runner import execute_payload_async
from .pod_runner import PodExecutionPayload

logger = logging.getLogger("google_adk.adk_graph_k8s.executor")


class K8sPodExecutionError(RuntimeError):
    """Raised when a dedicated GKE / Kubernetes pod fails or times out."""

    def __init__(
        self,
        message: str,
        *,
        pod_name: str | None = None,
        namespace: str | None = None,
        logs: str | None = None,
    ) -> None:
        super().__init__(message)
        self.pod_name = pod_name
        self.namespace = namespace
        self.logs = logs


def load_kube_configuration(config: K8sConfig) -> None:
    """Loads in-cluster or kubeconfig credentials according to ``config``."""
    from kubernetes import config as k8s_config

    if config.in_cluster is True:
        k8s_config.load_incluster_config()
    elif config.in_cluster is False:
        k8s_config.load_kube_config(
            config_file=config.kubeconfig_path,
            context=config.context,
        )
    else:
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config(
                config_file=config.kubeconfig_path,
                context=config.context,
            )


class PodExecutor(abc.ABC):
    """Abstract base class for executing a ``PodExecutionPayload``."""

    @abc.abstractmethod
    async def execute(
        self,
        payload: PodExecutionPayload,
        config: K8sConfig,
    ) -> list[Event]:
        """Executes the node payload and returns the emitted ADK ``Event``s."""

    async def cleanup_all(self) -> None:
        """Cleans up any active resources managed by this executor."""

    async def close(self) -> None:
        """Releases all resources when the runner shuts down."""
        await self.cleanup_all()


class K8sPodExecutor(PodExecutor):
    """Spawns a dedicated GKE / Kubernetes Pod per node execution using the ``kubernetes`` SDK."""

    def __init__(self, core_v1_api: Any | None = None) -> None:
        self._core_v1_api = core_v1_api
        self._active_pods: set[tuple[str, str]] = set()

    def _get_core_v1_api(self, config: K8sConfig) -> Any:
        if self._core_v1_api is not None:
            return self._core_v1_api

        from kubernetes import client

        load_kube_configuration(config)
        return client.CoreV1Api()

    @staticmethod
    def generate_pod_name(payload: PodExecutionPayload) -> str:
        """Generates a unique, RFC-1123 compliant Kubernetes pod name."""
        safe_node = sanitize_k8s_name(payload.node_name, max_length=28)
        safe_run = sanitize_k8s_name(str(payload.run_id or "1"), max_length=6)
        short_uid = uuid.uuid4().hex[:8]
        return f"{safe_node}-{safe_run}-{short_uid}"

    @classmethod
    def build_pod_manifest(
        cls,
        payload: PodExecutionPayload,
        config: K8sConfig,
        *,
        pod_name: str | None = None,
    ) -> Any:
        """Builds a ``kubernetes.client.V1Pod`` manifest for executing the ADK node."""
        from kubernetes import client

        effective_pod_name = pod_name or cls.generate_pod_name(payload)
        payload_json = payload.model_dump_json()
        payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")

        env_vars = [
            client.V1EnvVar(name="ADK_K8S_PAYLOAD", value=payload_b64),
            client.V1EnvVar(name="PYTHONUNBUFFERED", value="1"),
            # Marks this process as a node worker so K8sPlugin.prewarm() is a no-op in it.
            client.V1EnvVar(name="ADK_K8S_WORKER", value="1"),
        ]
        for k, v in config.env.items():
            env_vars.append(client.V1EnvVar(name=k, value=str(v)))

        for env_name, (secret_name, secret_key) in config.secret_env.items():
            env_vars.append(
                client.V1EnvVar(
                    name=env_name,
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=secret_name,
                            key=secret_key,
                        )
                    ),
                )
            )

        requests: dict[str, str] = {}
        limits: dict[str, str] = {}
        if config.cpu_request:
            requests["cpu"] = config.cpu_request
        if config.memory_request:
            requests["memory"] = config.memory_request
        if config.cpu_limit:
            limits["cpu"] = config.cpu_limit
        if config.memory_limit:
            limits["memory"] = config.memory_limit

        resources = (
            client.V1ResourceRequirements(
                requests=requests or None,
                limits=limits or None,
            )
            if (requests or limits)
            else None
        )

        security_context = client.V1SecurityContext(
            allow_privilege_escalation=False,
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        )

        container = client.V1Container(
            name="adk-node",
            image=config.image,
            image_pull_policy=config.image_pull_policy,
            command=["python3", "-u", "-c", build_pod_bootstrap_script()],
            env=env_vars,
            volume_mounts=config.volume_mounts or None,
            resources=resources,
            security_context=security_context,
        )

        image_pull_secrets = (
            [
                client.V1LocalObjectReference(name=secret_name)
                for secret_name in config.image_pull_secrets
            ]
            if config.image_pull_secrets
            else None
        )

        pod_labels = {
            "app.kubernetes.io/managed-by": "adk-kanode",
            "adk.google.com/node-name": sanitize_k8s_name(payload.node_name, max_length=63),
            "adk.google.com/run-id": sanitize_k8s_name(str(payload.run_id or "1"), max_length=63),
        }
        pod_labels.update(config.labels)

        pod_spec = client.V1PodSpec(
            containers=[container],
            restart_policy="Never",
            service_account_name=config.service_account_name,
            runtime_class_name=config.runtime_class_name,
            node_selector=config.node_selector or None,
            volumes=config.volumes or None,
            image_pull_secrets=image_pull_secrets,
        )

        metadata = client.V1ObjectMeta(
            name=effective_pod_name,
            namespace=config.namespace,
            labels=pod_labels,
            annotations=config.annotations or None,
        )

        return client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=metadata,
            spec=pod_spec,
        )

    async def execute(
        self,
        payload: PodExecutionPayload,
        config: K8sConfig,
    ) -> list[Event]:
        """Creates the GKE pod, waits for completion, parses ADK events, and cleans up."""
        api = await asyncio.to_thread(self._get_core_v1_api, config)
        pod_name = self.generate_pod_name(payload)
        pod_manifest = self.build_pod_manifest(payload, config, pod_name=pod_name)
        namespace = config.namespace

        logger.info(
            "Creating GKE pod %s in namespace %s for node %s",
            pod_name,
            namespace,
            payload.node_name,
        )
        await asyncio.to_thread(
            api.create_namespaced_pod,
            namespace=namespace,
            body=pod_manifest,
        )
        self._active_pods.add((namespace, pod_name))

        try:
            deadline = time.monotonic() + config.pod_startup_timeout
            phase = "Pending"
            while True:
                pod_status_obj = await asyncio.to_thread(
                    api.read_namespaced_pod_status,
                    name=pod_name,
                    namespace=namespace,
                )
                phase = getattr(pod_status_obj.status, "phase", None) or "Pending"
                if phase in ("Succeeded", "Failed"):
                    break
                if time.monotonic() >= deadline:
                    raise K8sPodExecutionError(
                        f"Timed out waiting for GKE pod {pod_name!r} in namespace {namespace!r} "
                        f"(last phase: {phase}).",
                        pod_name=pod_name,
                        namespace=namespace,
                    )
                await asyncio.sleep(config.poll_interval)

            logs: str = await asyncio.to_thread(
                api.read_namespaced_pod_log,
                name=pod_name,
                namespace=namespace,
            )

            if phase == "Failed":
                raise K8sPodExecutionError(
                    f"GKE pod {pod_name!r} for node {payload.node_name!r} failed.",
                    pod_name=pod_name,
                    namespace=namespace,
                    logs=logs,
                )

            return decode_events_from_logs(logs)
        finally:
            self._active_pods.discard((namespace, pod_name))
            if config.cleanup_pod:
                await self._delete_pod_safe(api, namespace=namespace, pod_name=pod_name)

    async def _delete_pod_safe(
        self, api: Any, *, namespace: str, pod_name: str
    ) -> None:
        try:
            await asyncio.to_thread(
                api.delete_namespaced_pod,
                name=pod_name,
                namespace=namespace,
            )
        except Exception as exc:
            logger.debug(
                "Ignoring error while deleting pod %s/%s: %s",
                namespace,
                pod_name,
                exc,
            )

    async def cleanup_all(self) -> None:
        """Deletes any remaining active pods tracked by this executor."""
        if not self._active_pods or self._core_v1_api is None:
            self._active_pods.clear()
            return
        pods = list(self._active_pods)
        self._active_pods.clear()
        for namespace, pod_name in pods:
            await self._delete_pod_safe(
                self._core_v1_api, namespace=namespace, pod_name=pod_name
            )


class SubprocessPodExecutor(PodExecutor):
    """Executes the exact GKE pod bootstrap script in an isolated local subprocess.

    Useful for local development and integration testing of the full source
    extraction + bootstrap script + event serialization pipeline without a
    live Kubernetes cluster.
    """

    async def execute(
        self,
        payload: PodExecutionPayload,
        config: K8sConfig,
    ) -> list[Event]:
        payload_json = payload.model_dump_json()
        payload_b64 = base64.b64encode(payload_json.encode("utf-8")).decode("ascii")
        script = build_pod_bootstrap_script()

        env = dict(os.environ)
        env.update(config.env)
        env["ADK_K8S_PAYLOAD"] = payload_b64
        env["PYTHONUNBUFFERED"] = "1"

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=config.pod_startup_timeout
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise K8sPodExecutionError(
                f"Subprocess pod execution timed out for node {payload.node_name!r}."
            ) from exc

        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise K8sPodExecutionError(
                f"Subprocess pod execution failed for node {payload.node_name!r} "
                f"(exit code {proc.returncode}): {stderr_str.strip()}",
                logs=f"{stdout_str}\n{stderr_str}".strip(),
            )

        return decode_events_from_logs(stdout_str)


class InProcessPodExecutor(PodExecutor):
    """Executes the extracted node payload in-process for fast unit tests."""

    async def execute(
        self,
        payload: PodExecutionPayload,
        config: K8sConfig,
    ) -> list[Event]:
        return await execute_payload_async(payload)
