"""Pod-side execution engine and bootstrap script generator for GKE pods."""

from __future__ import annotations

import ast
import base64
import importlib
import inspect
import json
import textwrap
from typing import Any

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
from pydantic import BaseModel
from pydantic import Field

from ._bootstrap import build_pod_bootstrap_script
from ._bootstrap import build_worker_bootstrap_script
from .extractor import ExtractedImplementation

EVENT_LINE_PREFIX = "__ADK_EVENT__:"


class PodExecutionPayload(BaseModel):
    """JSON-serializable execution payload sent to the dedicated GKE pod."""

    node_name: str
    node_path: str = ""
    run_id: str = "1"
    invocation_id: str = "k8s-invocation"
    session_id: str = "k8s-session"
    node_input: Any = None
    node_input_is_content: bool = False
    state: dict[str, Any] = Field(default_factory=dict)
    resume_inputs: dict[str, Any] = Field(default_factory=dict)
    extracted: ExtractedImplementation


def serialize_node_input(node_input: Any) -> tuple[Any, bool]:
    """Serializes ``node_input`` into a JSON-compatible value and flags ``types.Content``."""
    if isinstance(node_input, types.Content):
        return node_input.model_dump(mode="json", exclude_none=True), True
    if isinstance(node_input, BaseModel):
        return node_input.model_dump(mode="json"), False
    if isinstance(node_input, list):
        return [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in node_input
        ], False
    if isinstance(node_input, dict):
        return {
            k: (v.model_dump(mode="json") if isinstance(v, BaseModel) else v)
            for k, v in node_input.items()
        }, False
    return node_input, False


def deserialize_node_input(raw_input: Any, is_content: bool) -> Any:
    """Reconstructs ``node_input`` inside the GKE pod."""
    if is_content and isinstance(raw_input, dict):
        return types.Content.model_validate(raw_input)
    return raw_input


def _try_import_symbol(module_name: str | None, qualname: str | None) -> Any | None:
    """Attempts to import an object by module and qualname if installed in the pod image."""
    if not module_name or not qualname or module_name == "__main__":
        return None
    if "<locals>" in qualname:
        return None
    try:
        mod = importlib.import_module(module_name)
        obj: Any = mod
        for part in qualname.split("."):
            if not part.isidentifier():
                return None
            obj = getattr(obj, part)
        return obj
    except Exception:
        return None


def _compile_from_extracted_source(extracted: ExtractedImplementation) -> Any:
    """Compiles the AST-verified extracted source code in an isolated namespace."""
    from google.adk.agents import Agent
    from google.adk.agents import LlmAgent

    namespace: dict[str, Any] = {
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

    full_source_parts: list[str] = []
    if extracted.module_preamble:
        full_source_parts.append(extracted.module_preamble)
    if extracted.source_code:
        full_source_parts.append(extracted.source_code)

    full_source = "\n\n".join(full_source_parts).strip()
    if not full_source:
        raise RuntimeError(
            f"Cannot reconstruct node {extracted.name!r}: no source code or importable module found."
        )

    # Parse into AST first to validate syntax before compiling
    parsed_ast = ast.parse(full_source, filename=f"<k8s_node:{extracted.name}>")
    code_obj = compile(parsed_ast, filename=f"<k8s_node:{extracted.name}>", mode="exec")
    exec(code_obj, namespace)  # noqa: S102 - Executes AST-verified node source extracted by the host workflow

    if extracted.func_name and extracted.func_name in namespace:
        return namespace[extracted.func_name]
    return namespace


def reconstruct_node(extracted: ExtractedImplementation) -> BaseNode:
    """Reconstructs the underlying ADK ``BaseNode`` inside the GKE pod."""
    from google.adk.agents.llm_agent import LlmAgent

    # 1. Try direct module import if available in the container image
    imported = _try_import_symbol(extracted.module_name, extracted.qualname)
    if imported is not None:
        inner = getattr(imported, "inner_node", None)
        if isinstance(inner, BaseNode):
            return inner
        if isinstance(imported, BaseNode):
            return imported
        if callable(imported) and not inspect.isclass(imported):
            if extracted.kind == "tool":
                return build_node(FunctionTool(func=imported), name=extracted.name)
            return build_node(
                imported,
                name=extracted.name,
                parameter_binding=extracted.parameter_binding,
            )

    # 2. Reconstruct LlmAgent from config + preamble schemas
    if extracted.kind == "llm_agent":
        cfg = dict(extracted.node_config)
        schema_name = cfg.pop("output_schema_name", None)
        output_schema = None
        if schema_name and extracted.module_preamble:
            ns = _compile_from_extracted_source(extracted)
            if isinstance(ns, dict) and schema_name in ns:
                output_schema = ns[schema_name]
        agent = LlmAgent(
            name=cfg.get("name", extracted.name),
            model=cfg.get("model", "gemini-2.5-flash"),
            instruction=cfg.get("instruction", ""),
            description=cfg.get("description", ""),
            output_key=cfg.get("output_key"),
            output_schema=output_schema,
            include_contents=cfg.get("include_contents", "default"),
            mode=cfg.get("mode"),
            disallow_transfer_to_parent=cfg.get(
                "disallow_transfer_to_parent", False
            ),
            disallow_transfer_to_peers=cfg.get(
                "disallow_transfer_to_peers", False
            ),
        )
        return build_node(agent, name=extracted.name)

    # 3. Reconstruct function, tool, or custom BaseNode from extracted source
    target = _compile_from_extracted_source(extracted)
    if extracted.kind == "tool":
        if inspect.isclass(target):
            tool_instance = target()
            return build_node(tool_instance, name=extracted.name)
        if callable(target):
            return build_node(FunctionTool(func=target), name=extracted.name)

    if extracted.kind == "custom_node" and inspect.isclass(target):
        node_cfg = dict(extracted.node_config)
        node_cfg.setdefault("name", extracted.name)
        return target(**node_cfg)

    if callable(target):
        return build_node(
            target,
            name=extracted.name,
            parameter_binding=extracted.parameter_binding,
        )

    raise RuntimeError(f"Failed to reconstruct ADK node {extracted.name!r}.")


async def execute_payload_async(payload: PodExecutionPayload) -> list[Event]:
    """Executes ``PodExecutionPayload`` and returns the resulting ADK ``Event`` list."""
    target_node = reconstruct_node(payload.extracted)
    node_input = deserialize_node_input(
        payload.node_input, payload.node_input_is_content
    )

    session_service = InMemorySessionService()
    session = Session(
        id=payload.session_id,
        app_name="adk_k8s_pod",
        user_id="adk_k8s_user",
        state=dict(payload.state),
    )
    ic = InvocationContext(
        session_service=session_service,
        invocation_id=payload.invocation_id,
        session=session,
        run_config=RunConfig(),  # Required by LLM-based nodes.
    )
    ctx = Context(
        ic,
        node=target_node,
        node_path=payload.node_path or target_node.name,
        run_id=payload.run_id,
        resume_inputs=dict(payload.resume_inputs),
    )

    emitted_events: list[Event] = []
    async for event in target_node.run(ctx=ctx, node_input=node_input):
        if event.output is not None:
            ctx._output_emitted = True
        if event.actions and event.actions.route is not None:
            ctx._route_emitted = True
        if ctx.actions.state_delta:
            event.actions.state_delta.update(ctx.actions.state_delta)
            ctx.actions.state_delta.clear()
        emitted_events.append(event)

    # Flush any deferred ctx.output, ctx.route, or ctx.state mutations
    has_deferred_output = (
        ctx._output_value is not None
        and not ctx._output_emitted
        and not ctx._output_delegated
    )
    has_unflushed_route = (
        ctx._route_value is not None and not ctx._route_emitted
    )
    has_state_delta = bool(ctx.actions.state_delta)

    if has_deferred_output or has_unflushed_route or has_state_delta:
        flush_event = Event(
            output=ctx._output_value if has_deferred_output else None,
            route=ctx._route_value if has_unflushed_route else None,
            state=dict(ctx.actions.state_delta) if has_state_delta else None,
        )
        ctx.actions.state_delta.clear()
        emitted_events.append(flush_event)

    return emitted_events


def encode_event_line(event: Event) -> str:
    """Encodes an ADK ``Event`` into a single framed log line."""
    raw_json = json.dumps(event.model_dump(mode="json", exclude_none=True))
    b64 = base64.b64encode(raw_json.encode("utf-8")).decode("ascii")
    return f"{EVENT_LINE_PREFIX}{b64}"


def decode_events_from_logs(log_output: str) -> list[Event]:
    """Parses framed ``__ADK_EVENT__:`` lines from GKE pod stdout logs."""
    events: list[Event] = []
    for line in log_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(EVENT_LINE_PREFIX):
            continue
        b64_part = stripped[len(EVENT_LINE_PREFIX) :].strip()
        raw_json = base64.b64decode(b64_part.encode("ascii")).decode("utf-8")
        data = json.loads(raw_json)
        events.append(Event.model_validate(data))
    return events
