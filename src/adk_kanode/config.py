"""Configuration models for GKE / Kubernetes pod execution."""

from __future__ import annotations

import os
import re
from typing import Literal

from kubernetes.client import V1Volume
from kubernetes.client import V1VolumeMount
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS_SUBDOMAIN_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_first(*names: str, default: str | None = None) -> str | None:
    """Returns the first non-empty environment variable value from ``names``."""
    for name in names:
        val = os.environ.get(name)
        if val is not None and val.strip() != "":
            return val.strip()
    return default


def _env_bool(*names: str, default: bool | None = None) -> bool | None:
    """Parses a boolean environment variable."""
    raw = _env_first(*names)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(*names: str, default: float) -> float:
    """Parses a float environment variable."""
    raw = _env_first(*names)
    if raw is None:
        return default
    return float(raw)


def sanitize_k8s_name(raw: str, max_length: int = 45) -> str:
    """Converts an arbitrary string into a valid RFC-1123 DNS label prefix."""
    cleaned = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    if not cleaned:
        cleaned = "node"
    cleaned = cleaned[:max_length].strip("-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = f"n-{cleaned}".strip("-")
    return cleaned


class K8sConfig(BaseModel):
    """Configuration for running an ADK Workflow node inside a dedicated GKE Pod.

    All fields automatically fall back to environment variables (`ADK_K8S_*` or
    `K8S_*`) when not explicitly passed in code.

    Note:
        Secrets should be injected via ``secret_env`` (referencing Kubernetes
        Secrets in the cluster) rather than hardcoded in ``env``.
        # TODO(security): Integrate with GKE Workload Identity / GCP Secret Manager CSI driver if secret mounting is needed.
    """

    model_config = ConfigDict(extra="forbid")

    image: str = Field(
        default_factory=lambda: (
            _env_first("ADK_K8S_IMAGE", "K8S_IMAGE", default="python:3.12-slim")
            or "python:3.12-slim"
        ),
        description=(
            "Container image used to run the node in GKE "
            "(env: ADK_K8S_IMAGE or K8S_IMAGE)."
        ),
    )
    namespace: str = Field(
        default_factory=lambda: (
            _env_first("ADK_K8S_NAMESPACE", "K8S_NAMESPACE", default="default")
            or "default"
        ),
        description=(
            "Kubernetes namespace where the dedicated pod will be spawned "
            "(env: ADK_K8S_NAMESPACE or K8S_NAMESPACE)."
        ),
    )
    service_account_name: str | None = Field(
        default_factory=lambda: _env_first(
            "ADK_K8S_SERVICE_ACCOUNT", "K8S_SERVICE_ACCOUNT"
        ),
        description=(
            "Kubernetes ServiceAccount name "
            "(env: ADK_K8S_SERVICE_ACCOUNT or K8S_SERVICE_ACCOUNT)."
        ),
    )
    runtime_class_name: str | None = Field(
        default_factory=lambda: _env_first(
            "ADK_K8S_RUNTIME_CLASS_NAME", "K8S_RUNTIME_CLASS_NAME"
        ),
        description=(
            "Kubernetes RuntimeClass name, e.g. 'gvisor' for GKE Sandbox "
            "(env: ADK_K8S_RUNTIME_CLASS_NAME or K8S_RUNTIME_CLASS_NAME)."
        ),
    )
    cpu_request: str | None = Field(
        default_factory=lambda: _env_first("ADK_K8S_CPU_REQUEST", "K8S_CPU_REQUEST"),
        description="CPU request for the pod container (env: ADK_K8S_CPU_REQUEST or K8S_CPU_REQUEST).",
    )
    cpu_limit: str | None = Field(
        default_factory=lambda: _env_first("ADK_K8S_CPU_LIMIT", "K8S_CPU_LIMIT"),
        description="CPU limit for the pod container (env: ADK_K8S_CPU_LIMIT or K8S_CPU_LIMIT).",
    )
    memory_request: str | None = Field(
        default_factory=lambda: _env_first(
            "ADK_K8S_MEMORY_REQUEST", "K8S_MEMORY_REQUEST"
        ),
        description=(
            "Memory request for the pod container "
            "(env: ADK_K8S_MEMORY_REQUEST or K8S_MEMORY_REQUEST)."
        ),
    )
    memory_limit: str | None = Field(
        default_factory=lambda: _env_first("ADK_K8S_MEMORY_LIMIT", "K8S_MEMORY_LIMIT"),
        description=(
            "Memory limit for the pod container "
            "(env: ADK_K8S_MEMORY_LIMIT or K8S_MEMORY_LIMIT)."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Non-sensitive environment variables passed to the pod.",
    )
    secret_env: dict[str, tuple[str, str]] = Field(
        default_factory=dict,
        description=(
            "Mapping of env var name to (k8s_secret_name, secret_key) for "
            "injecting sensitive values from Kubernetes Secrets."
        ),
    )
    volumes: list[V1Volume] = Field(
        default_factory=list,
        description="Additional Kubernetes volumes mounted into the worker pod.",
    )
    volume_mounts: list[V1VolumeMount] = Field(
        default_factory=list,
        description="Additional volume mounts for the worker container.",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description="Additional Kubernetes labels to attach to the pod.",
    )
    annotations: dict[str, str] = Field(
        default_factory=dict,
        description="Additional Kubernetes annotations to attach to the pod.",
    )
    image_pull_policy: Literal["Always", "IfNotPresent", "Never"] = Field(
        default_factory=lambda: _env_first(  # type: ignore[return-value]
            "ADK_K8S_IMAGE_PULL_POLICY",
            "K8S_IMAGE_PULL_POLICY",
            default="IfNotPresent",
        )
        or "IfNotPresent",
        description=(
            "Container image pull policy "
            "(env: ADK_K8S_IMAGE_PULL_POLICY or K8S_IMAGE_PULL_POLICY)."
        ),
    )
    image_pull_secrets: list[str] = Field(
        default_factory=lambda: [
            s.strip()
            for s in (
                _env_first("ADK_K8S_IMAGE_PULL_SECRETS", "K8S_IMAGE_PULL_SECRETS")
                or ""
            ).split(",")
            if s.strip()
        ],
        description=(
            "Names of imagePullSecrets in the target namespace "
            "(comma-separated env: ADK_K8S_IMAGE_PULL_SECRETS or K8S_IMAGE_PULL_SECRETS)."
        ),
    )
    node_selector: dict[str, str] = Field(
        default_factory=dict,
        description="Node selector labels for scheduling the pod on specific GKE node pools.",
    )
    cleanup_pod: bool = Field(
        default_factory=lambda: (
            _env_bool("ADK_K8S_CLEANUP_POD", "K8S_CLEANUP_POD", default=True)
            is not False
        ),
        description=(
            "Whether to delete the pod automatically after execution completes "
            "(env: ADK_K8S_CLEANUP_POD or K8S_CLEANUP_POD)."
        ),
    )
    mode: Literal["deployment", "pod"] = Field(
        default_factory=lambda: _env_first(  # type: ignore[return-value]
            "ADK_K8S_MODE", "K8S_MODE", default="deployment"
        )
        or "deployment",
        description=(
            "Execution mode. 'deployment' keeps a warm Deployment + ClusterIP "
            "Service per node (fast). 'pod' spawns a fresh single-use pod per "
            "call (strict isolation). (env: ADK_K8S_MODE or K8S_MODE)."
        ),
    )
    replicas: int = Field(
        default_factory=lambda: int(
            _env_float("ADK_K8S_REPLICAS", "K8S_REPLICAS", default=1)
        ),
        ge=1,
        description=(
            "Number of warm worker replicas in deployment mode "
            "(env: ADK_K8S_REPLICAS or K8S_REPLICAS)."
        ),
    )
    poll_interval: float = Field(
        default_factory=lambda: _env_float(
            "ADK_K8S_POLL_INTERVAL", "K8S_POLL_INTERVAL", default=1.0
        ),
        gt=0.0,
        description="Seconds between pod status polls (env: ADK_K8S_POLL_INTERVAL or K8S_POLL_INTERVAL).",
    )
    pod_startup_timeout: float = Field(
        default_factory=lambda: _env_float(
            "ADK_K8S_POD_STARTUP_TIMEOUT",
            "K8S_POD_STARTUP_TIMEOUT",
            default=300.0,
        ),
        gt=0.0,
        description=(
            "Maximum time in seconds to wait for the pod to complete (pod mode) "
            "or for a warm worker to become ready (deployment mode) "
            "(env: ADK_K8S_POD_STARTUP_TIMEOUT or K8S_POD_STARTUP_TIMEOUT)."
        ),
    )
    in_cluster: bool | None = Field(
        default_factory=lambda: _env_bool(
            "ADK_K8S_IN_CLUSTER", "K8S_IN_CLUSTER", default=None
        ),
        description=(
            "If True, use in-cluster Kubernetes config. If False, use kubeconfig. "
            "If None, auto-detect (env: ADK_K8S_IN_CLUSTER or K8S_IN_CLUSTER)."
        ),
    )
    kubeconfig_path: str | None = Field(
        default_factory=lambda: _env_first("ADK_K8S_KUBECONFIG", "KUBECONFIG"),
        description="Optional path to a kubeconfig file (env: ADK_K8S_KUBECONFIG or KUBECONFIG).",
    )
    context: str | None = Field(
        default_factory=lambda: _env_first("ADK_K8S_CONTEXT", "K8S_CONTEXT"),
        description="Optional kubeconfig context name (env: ADK_K8S_CONTEXT or K8S_CONTEXT).",
    )

    @field_validator("namespace")
    @classmethod
    def _validate_namespace(cls, v: str) -> str:
        if not v or len(v) > 63 or not _DNS_LABEL_RE.match(v):
            raise ValueError(
                f"Invalid Kubernetes namespace {v!r}: must be a valid RFC-1123 DNS label."
            )
        return v

    @field_validator("service_account_name")
    @classmethod
    def _validate_service_account(cls, v: str | None) -> str | None:
        if v is not None and (not v or len(v) > 253):
            raise ValueError(f"Invalid Kubernetes service_account_name {v!r}.")
        return v

    @field_validator("runtime_class_name")
    @classmethod
    def _validate_runtime_class_name(cls, v: str | None) -> str | None:
        if v is not None and (
            not v or len(v) > 253 or not _DNS_SUBDOMAIN_RE.match(v)
        ):
            raise ValueError(
                f"Invalid Kubernetes runtime_class_name {v!r}: "
                "must be a valid RFC-1123 DNS subdomain."
            )
        return v

    @field_validator("env")
    @classmethod
    def _validate_env_keys(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not _ENV_NAME_RE.match(key):
                raise ValueError(f"Invalid environment variable name {key!r}.")
        return v

    @field_validator("secret_env")
    @classmethod
    def _validate_secret_env(
        cls, v: dict[str, tuple[str, str]]
    ) -> dict[str, tuple[str, str]]:
        for env_key, (secret_name, secret_key) in v.items():
            if not _ENV_NAME_RE.match(env_key):
                raise ValueError(f"Invalid environment variable name {env_key!r}.")
            if not secret_name or not secret_key:
                raise ValueError(
                    f"secret_env entry for {env_key!r} must specify non-empty (secret_name, secret_key)."
                )
        return v

    def merge_with(self, override: K8sConfig | None) -> K8sConfig:
        """Merges this base config with a node-specific override config."""
        if override is None:
            return self.model_copy(deep=True)

        merged_data = self.model_dump()
        override_data = override.model_dump()

        for field_name in override.model_fields_set:
            if field_name in ("env", "secret_env", "labels", "annotations", "node_selector"):
                combined = dict(merged_data.get(field_name) or {})
                combined.update(override_data.get(field_name) or {})
                merged_data[field_name] = combined
            elif field_name == "image_pull_secrets":
                existing = list(merged_data.get("image_pull_secrets") or [])
                for secret in override_data.get("image_pull_secrets") or []:
                    if secret not in existing:
                        existing.append(secret)
                merged_data["image_pull_secrets"] = existing
            else:
                merged_data[field_name] = override_data[field_name]

        return K8sConfig.model_validate(merged_data)
