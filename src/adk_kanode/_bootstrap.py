"""Self-contained Python scripts executed via ``python3 -u -c`` inside GKE pods.

Two entrypoints share one core (imports, node reconstruction, node execution):

- **one-shot** (``mode="pod"``): reads ``ADK_K8S_PAYLOAD``, runs the node once,
  prints framed ``__ADK_EVENT__:`` lines and exits.
- **worker** (``mode="deployment"``): reconstructs the node once at startup from
  ``ADK_K8S_NODE_SPEC`` and serves executions over HTTP so every call hits a
  warm process.

The scripts only require ``google-adk`` (which ships ``starlette`` and
``uvicorn``) in the container image; ``adk_kanode`` does not need to be
installed in the pod.
"""

from __future__ import annotations

_BOOTSTRAP_CORE = r'''
import asyncio
import ast
import base64
import importlib
import inspect
import json
import os
import sys
from typing import Any

from google.adk.agents import Agent, LlmAgent
from google.adk.agents.context import Context
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.run_config import RunConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.sessions.session import Session
from google.adk.tools.function_tool import FunctionTool
from google.adk.workflow._base_node import BaseNode
from google.adk.workflow._node import Node
from google.adk.workflow.utils._workflow_graph_utils import build_node
from google.genai import types
from pydantic import BaseModel, Field

EVENT_LINE_PREFIX = "__ADK_EVENT__:"

def _try_import(mod_name, qualname):
    if not mod_name or not qualname or mod_name == "__main__" or "<locals>" in qualname:
        return None
    try:
        obj = importlib.import_module(mod_name)
        for part in qualname.split("."):
            if not part.isidentifier():
                return None
            obj = getattr(obj, part)
        return obj
    except Exception:
        return None

def _compile_extracted(extracted):
    ns = {
        "Any": Any,
        "BaseModel": BaseModel,
        "Field": Field,
        "Event": Event,
        "RequestInput": RequestInput,
        "Context": Context,
        "types": types,
        "Agent": Agent,
        "LlmAgent": LlmAgent,
        "BaseNode": BaseNode,
        "Node": Node,
        "FunctionTool": FunctionTool,
    }
    parts = []
    if extracted.get("module_preamble"):
        parts.append(extracted["module_preamble"])
    if extracted.get("source_code"):
        parts.append(extracted["source_code"])
    src = "\n\n".join(parts).strip()
    tree = ast.parse(src, filename="<k8s_node>")
    exec(compile(tree, filename="<k8s_node>", mode="exec"), ns)
    # The source module uses postponed annotations, and these classes do not live in a
    # real module, so a model that refers to another one (list[Assignment]) cannot
    # resolve it on its own. Resolve those references against this namespace.
    for value in list(ns.values()):
        if (
            inspect.isclass(value)
            and issubclass(value, BaseModel)
            and value is not BaseModel
            and not value.__pydantic_complete__
        ):
            value.model_rebuild(raise_errors=False, _types_namespace=ns)
    fn = extracted.get("func_name")
    return ns[fn] if fn and fn in ns else ns

def _reconstruct(extracted):
    name = extracted["name"]
    kind = extracted["kind"]
    binding = extracted.get("parameter_binding", "state")
    imported = _try_import(extracted.get("module_name"), extracted.get("qualname"))
    if imported is not None:
        inner = getattr(imported, "inner_node", None)
        if isinstance(inner, BaseNode):
            return inner
        if isinstance(imported, BaseNode):
            return imported
        if callable(imported) and not inspect.isclass(imported):
            if kind == "tool":
                return build_node(FunctionTool(func=imported), name=name)
            return build_node(imported, name=name, parameter_binding=binding)
    if kind == "llm_agent":
        cfg = dict(extracted.get("node_config") or {})
        schema_name = cfg.pop("output_schema_name", None)
        output_schema = None
        if schema_name and extracted.get("module_preamble"):
            ns = _compile_extracted(extracted)
            if isinstance(ns, dict) and schema_name in ns:
                output_schema = ns[schema_name]
        tools = []
        for ref in cfg.get("tools") or []:
            tool = _try_import(ref["module"], ref["name"])
            if tool is None:
                raise RuntimeError(
                    "Cannot import tool %s.%s for agent %r in the worker image."
                    % (ref["module"], ref["name"], name)
                )
            tools.append(tool)
        agent = LlmAgent(
            name=cfg.get("name", name),
            model=cfg.get("model", "gemini-2.5-flash"),
            instruction=cfg.get("instruction", ""),
            description=cfg.get("description", ""),
            output_key=cfg.get("output_key"),
            output_schema=output_schema,
            tools=tools,
            include_contents=cfg.get("include_contents", "default"),
            mode=cfg.get("mode"),
            disallow_transfer_to_parent=cfg.get("disallow_transfer_to_parent", False),
            disallow_transfer_to_peers=cfg.get("disallow_transfer_to_peers", False),
        )
        return build_node(agent, name=name)
    target = _compile_extracted(extracted)
    if kind == "tool":
        if inspect.isclass(target):
            return build_node(target(), name=name)
        return build_node(FunctionTool(func=target), name=name)
    if kind == "custom_node" and inspect.isclass(target):
        cfg = dict(extracted.get("node_config") or {})
        cfg.setdefault("name", name)
        return target(**cfg)
    return build_node(target, name=name, parameter_binding=binding)

async def _run_payload(target_node, payload):
    """Runs ``target_node`` for one payload and returns the emitted ADK Events."""
    node_input = payload.get("node_input")
    if payload.get("node_input_is_content") and isinstance(node_input, dict):
        node_input = types.Content.model_validate(node_input)
    session = Session(
        id=payload.get("session_id", "k8s-session"),
        app_name="adk_k8s_pod",
        user_id="adk_k8s_user",
        state=dict(payload.get("state") or {}),
    )
    ic = InvocationContext(
        session_service=InMemorySessionService(),
        invocation_id=payload.get("invocation_id", "k8s-invocation"),
        session=session,
        run_config=RunConfig(),  # Required by LLM-based nodes.
    )
    ctx = Context(
        ic,
        node=target_node,
        node_path=payload.get("node_path") or target_node.name,
        run_id=payload.get("run_id", "1"),
        resume_inputs=dict(payload.get("resume_inputs") or {}),
    )
    events = []
    async for event in target_node.run(ctx=ctx, node_input=node_input):
        if event.output is not None:
            ctx._output_emitted = True
        if event.actions and event.actions.route is not None:
            ctx._route_emitted = True
        if ctx.actions.state_delta:
            event.actions.state_delta.update(ctx.actions.state_delta)
            ctx.actions.state_delta.clear()
        events.append(event)
    has_def_out = ctx._output_value is not None and not ctx._output_emitted and not ctx._output_delegated
    has_def_route = ctx._route_value is not None and not ctx._route_emitted
    has_delta = bool(ctx.actions.state_delta)
    if has_def_out or has_def_route or has_delta:
        events.append(Event(
            output=ctx._output_value if has_def_out else None,
            route=ctx._route_value if has_def_route else None,
            state=dict(ctx.actions.state_delta) if has_delta else None,
        ))
        ctx.actions.state_delta.clear()
    return events
'''

_ONESHOT_MAIN = r'''
async def _main():
    raw_b64 = os.environ["ADK_K8S_PAYLOAD"]
    payload = json.loads(base64.b64decode(raw_b64.encode("ascii")).decode("utf-8"))
    target_node = _reconstruct(payload["extracted"])
    for ev in await _run_payload(target_node, payload):
        raw = json.dumps(ev.model_dump(mode="json", exclude_none=True))
        b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        print(f"{EVENT_LINE_PREFIX}{b64}", flush=True)

if __name__ == "__main__":
    asyncio.run(_main())
'''

_WORKER_MAIN = r'''
import hmac
import logging

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

_worker_logger = logging.getLogger("adk_kanode.worker")
MAX_BODY_BYTES = int(os.environ.get("ADK_K8S_WORKER_MAX_BODY_BYTES", str(32 * 1024 * 1024)))

def build_app(spec_b64, spec_hash, token):
    """Builds the ASGI app serving exactly one pre-provisioned ADK node.

    The node implementation comes only from the Deployment spec (``spec_b64``);
    requests can supply inputs and state but never code.
    """
    if not token:
        # Fail closed: never serve executions without authentication.
        raise RuntimeError("ADK_K8S_WORKER_TOKEN is required to start the worker.")
    extracted = json.loads(base64.b64decode(spec_b64.encode("ascii")).decode("utf-8"))
    target_node = _reconstruct(extracted)
    expected_auth = ("Bearer " + token).encode("utf-8")

    async def healthz(request):
        return JSONResponse({"status": "ok", "spec_hash": spec_hash})

    async def execute(request):
        supplied = request.headers.get("authorization", "").encode("utf-8")
        if not hmac.compare_digest(supplied, expected_auth):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        declared = request.headers.get("content-length", "")
        if not declared.isdigit() or int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "invalid_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid_payload"}, status_code=400)
        if payload.get("spec_hash") != spec_hash:
            # The caller expects a different node revision (rollout in progress).
            return JSONResponse({"error": "stale_worker", "spec_hash": spec_hash}, status_code=409)
        try:
            events = await _run_payload(target_node, payload)
        except Exception as exc:
            _worker_logger.exception("Node %r failed", extracted.get("name"))
            # The caller is the authenticated workflow host, so it gets the
            # error type and message; the traceback stays in the pod logs.
            return JSONResponse(
                {"error": type(exc).__name__, "message": str(exc)}, status_code=500
            )
        return JSONResponse(
            {"events": [ev.model_dump(mode="json", exclude_none=True) for ev in events]}
        )

    # TODO(security): No per-client rate limiting; the endpoint is only reachable
    # in-cluster, requires the bearer token, and is bounded by MAX_BODY_BYTES.
    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/execute", execute, methods=["POST"]),
        ]
    )

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    _app = build_app(
        os.environ["ADK_K8S_NODE_SPEC"],
        os.environ.get("ADK_K8S_SPEC_HASH", ""),
        os.environ.get("ADK_K8S_WORKER_TOKEN", ""),
    )
    # Binds all interfaces so the ClusterIP Service can reach the pod.
    uvicorn.run(
        _app,
        host=os.environ.get("ADK_K8S_WORKER_HOST", "0.0.0.0"),
        port=int(os.environ.get("ADK_K8S_WORKER_PORT", "8080")),
        log_level="info",
        access_log=False,
    )
'''


def build_pod_bootstrap_script() -> str:
    """Returns a self-contained Python script executed via ``python3 -u -c`` in the GKE pod.

    The script works even if ``adk_graph_k8s`` is not installed inside the pod
    container image, requiring only ``google-adk`` in the container environment.
    """
    return _BOOTSTRAP_CORE + _ONESHOT_MAIN


def build_worker_bootstrap_script() -> str:
    """Returns the long-lived HTTP worker script used by ``mode="deployment"``.

    The worker reconstructs its node once at startup, then serves
    ``POST /execute`` (bearer-token authenticated) and ``GET /healthz``.
    """
    return _BOOTSTRAP_CORE + _WORKER_MAIN
