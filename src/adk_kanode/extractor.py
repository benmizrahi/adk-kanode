"""Automatic extraction of ADK node implementations for remote GKE Pod execution."""

from __future__ import annotations

import ast
from collections.abc import Callable
import inspect
import textwrap
from typing import Any
from typing import Literal

from google.adk.tools.base_tool import BaseTool
from google.adk.workflow._base_node import BaseNode
from google.adk.workflow._function_node import FunctionNode
from google.adk.workflow._tool_node import _ToolNode
from pydantic import BaseModel
from pydantic import Field

_DECORATOR_NAMES_TO_STRIP = frozenset({
    "kanode",
    "k8s_node",
    "kanote",
    "node",
})


class ExtractedImplementation(BaseModel):
    """Serializable representation of an ADK node's internal implementation."""

    kind: Literal["function", "llm_agent", "tool", "custom_node"] = Field(
        description="The underlying ADK node category."
    )
    name: str = Field(description="The ADK node name.")
    module_name: str | None = Field(
        default=None,
        description="Original Python module where the implementation was defined.",
    )
    qualname: str | None = Field(
        default=None,
        description="Qualified name of the function, class, or object.",
    )
    func_name: str | None = Field(
        default=None,
        description="Function or class name inside the extracted source code.",
    )
    source_code: str | None = Field(
        default=None,
        description="AST-cleaned Python source code for the function or class.",
    )
    module_preamble: str = Field(
        default="",
        description="Extracted top-level imports and schema classes from the source module.",
    )
    parameter_binding: Literal["state", "node_input"] = Field(
        default="state",
        description="Parameter binding mode for FunctionNode.",
    )
    node_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Serialized configuration for LlmAgent or BaseNode subclasses.",
    )


def _get_decorator_root_name(dec: ast.expr) -> str | None:
    """Returns the terminal identifier of a decorator expression."""
    if isinstance(dec, ast.Call):
        dec = dec.func
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return dec.attr
    return None


def _strip_node_decorators_from_source(source: str) -> tuple[str, str | None]:
    """Parses function/class source, strips @kanode/@k8s_node/@node decorators, and unparses."""
    dedented = textwrap.dedent(source)
    tree = ast.parse(dedented)
    target_name: str | None = None
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if target_name is None:
                target_name = stmt.name
            stmt.decorator_list = [
                dec
                for dec in stmt.decorator_list
                if _get_decorator_root_name(dec) not in _DECORATOR_NAMES_TO_STRIP
            ]
    return ast.unparse(tree), target_name


def _extract_module_preamble(obj: Any, exclude_name: str | None = None) -> str:
    """Extracts top-level imports and Pydantic BaseModel definitions from the object's module."""
    try:
        source_file = inspect.getsourcefile(obj)
        if not source_file:
            return ""
        module = inspect.getmodule(obj)
        if not module:
            return ""
        module_source = inspect.getsource(module)
        tree = ast.parse(module_source)
    except (OSError, TypeError, SyntaxError):
        return ""

    kept_nodes: list[ast.stmt] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            names = [
                alias
                for alias in stmt.names
                if not alias.name.startswith(("adk_kanode", "adk_graph_k8s"))
            ]
            if names:
                kept_nodes.append(ast.Import(names=names))
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.module and stmt.module.startswith(("adk_kanode", "adk_graph_k8s")):
                continue
            if stmt.level and stmt.level > 0:
                continue
            kept_nodes.append(stmt)
        elif isinstance(stmt, ast.ClassDef):
            if stmt.name == exclude_name:
                continue
            # Include helper/schema classes that are not decorated with @kanode
            has_node_dec = any(
                _get_decorator_root_name(dec) in _DECORATOR_NAMES_TO_STRIP
                for dec in stmt.decorator_list
            )
            if not has_node_dec:
                kept_nodes.append(stmt)

    if not kept_nodes:
        return ""

    preamble_mod = ast.Module(body=kept_nodes, type_ignores=[])
    return ast.unparse(preamble_mod)


def _extract_callable_impl(
    func: Callable[..., Any],
    *,
    node_name: str,
    parameter_binding: Literal["state", "node_input"] = "state",
) -> ExtractedImplementation:
    """Extracts source and module metadata from a Python callable."""
    unwrapped = inspect.unwrap(func)
    module_name = getattr(unwrapped, "__module__", None)
    qualname = getattr(unwrapped, "__qualname__", None)
    func_name = getattr(unwrapped, "__name__", None) or node_name

    cleaned_source: str | None = None
    try:
        raw_source = inspect.getsource(unwrapped)
        cleaned_source, parsed_name = _strip_node_decorators_from_source(raw_source)
        if parsed_name:
            func_name = parsed_name
    except (OSError, TypeError, SyntaxError):
        cleaned_source = None

    preamble = _extract_module_preamble(unwrapped, exclude_name=func_name)

    return ExtractedImplementation(
        kind="function",
        name=node_name,
        module_name=module_name,
        qualname=qualname,
        func_name=func_name,
        source_code=cleaned_source,
        module_preamble=preamble,
        parameter_binding=parameter_binding,
    )


def extract_node_implementation(
    node_like: Any,
    built_node: BaseNode,
    *,
    parameter_binding: Literal["state", "node_input"] = "state",
) -> ExtractedImplementation:
    """Extracts the internal implementation of any ADK NodeLike for GKE pod execution.

    Supports:
    - Python functions / callables / ``FunctionNode``
    - ``LlmAgent``
    - ``BaseTool`` / ``FunctionTool`` / ``_ToolNode``
    - Custom ``BaseNode`` / ``BaseAgent`` subclasses
    """
    from google.adk.agents.llm_agent import LlmAgent

    # 1. FunctionNode or direct callable
    if isinstance(built_node, FunctionNode):
        target_func = getattr(built_node, "_unwrapped_func", None) or getattr(
            built_node, "_func", None
        )
        if callable(target_func):
            return _extract_callable_impl(
                target_func,
                node_name=built_node.name,
                parameter_binding=built_node.parameter_binding,
            )
    if callable(node_like) and not isinstance(node_like, (BaseNode, BaseTool)):
        return _extract_callable_impl(
            node_like,
            node_name=built_node.name,
            parameter_binding=parameter_binding,
        )

    # 2. _ToolNode or BaseTool
    if isinstance(built_node, _ToolNode) or isinstance(node_like, BaseTool):
        tool_obj = built_node.tool if isinstance(built_node, _ToolNode) else node_like
        tool_func = getattr(tool_obj, "func", None)
        if callable(tool_func):
            impl = _extract_callable_impl(
                tool_func,
                node_name=built_node.name,
                parameter_binding="node_input",
            )
            return impl.model_copy(update={"kind": "tool"})

        cls = type(tool_obj)
        source_code: str | None = None
        try:
            raw_source = inspect.getsource(cls)
            source_code, _ = _strip_node_decorators_from_source(raw_source)
        except (OSError, TypeError, SyntaxError):
            source_code = None

        return ExtractedImplementation(
            kind="tool",
            name=built_node.name,
            module_name=cls.__module__,
            qualname=cls.__qualname__,
            func_name=cls.__name__,
            source_code=source_code,
            module_preamble=_extract_module_preamble(cls, exclude_name=cls.__name__),
        )

    # 3. LlmAgent
    if isinstance(built_node, LlmAgent):
        config: dict[str, Any] = {
            "name": built_node.name,
            "description": built_node.description,
            "instruction": (
                built_node.instruction
                if isinstance(built_node.instruction, str)
                else ""
            ),
            "model": (
                built_node.model if isinstance(built_node.model, str) else "gemini-2.5-flash"
            ),
            "output_key": built_node.output_key,
            "include_contents": built_node.include_contents,
            "mode": built_node.mode,
            "disallow_transfer_to_parent": built_node.disallow_transfer_to_parent,
            "disallow_transfer_to_peers": built_node.disallow_transfer_to_peers,
        }
        preamble = ""
        if built_node.output_schema is not None and inspect.isclass(
            built_node.output_schema
        ):
            config["output_schema_name"] = built_node.output_schema.__name__
            preamble = _extract_module_preamble(built_node.output_schema)
            try:
                schema_src = textwrap.dedent(
                    inspect.getsource(built_node.output_schema)
                )
                if schema_src not in preamble:
                    preamble = f"{preamble}\n\n{schema_src}".strip()
            except (OSError, TypeError):
                pass

        return ExtractedImplementation(
            kind="llm_agent",
            name=built_node.name,
            module_name=type(built_node).__module__,
            qualname=type(built_node).__qualname__,
            node_config=config,
            module_preamble=preamble,
        )

    # 4. Custom BaseNode / BaseAgent subclass
    cls = type(built_node)
    cleaned_cls_source: str | None = None
    cls_name = cls.__name__
    try:
        raw_cls_source = inspect.getsource(cls)
        cleaned_cls_source, parsed_cls_name = _strip_node_decorators_from_source(
            raw_cls_source
        )
        if parsed_cls_name:
            cls_name = parsed_cls_name
    except (OSError, TypeError, SyntaxError):
        cleaned_cls_source = None

    node_config: dict[str, Any] = {}
    try:
        node_config = built_node.model_dump(mode="json")
    except Exception:
        node_config = {"name": built_node.name, "description": built_node.description}

    return ExtractedImplementation(
        kind="custom_node",
        name=built_node.name,
        module_name=cls.__module__,
        qualname=cls.__qualname__,
        func_name=cls_name,
        source_code=cleaned_cls_source,
        module_preamble=_extract_module_preamble(cls, exclude_name=cls_name),
        node_config=node_config,
    )
