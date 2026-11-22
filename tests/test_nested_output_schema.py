"""An agent's output schema may reference other models from the same module."""

from __future__ import annotations

from typing import Any

from google.adk.agents import Agent
from pydantic import BaseModel
import pytest

from adk_kanode import build_worker_bootstrap_script
from adk_kanode import kanode
from adk_kanode.pod_runner import reconstruct_node


class Assignment(BaseModel):
    specialist: str
    task: str


class Plan(BaseModel):
    assignments: list[Assignment]


@kanode
def planner() -> Agent:
    return Agent(name="planner", model="gemini-2.5-flash", output_schema=Plan, instruction="Plan.")


def _reconstruct_in_worker(extracted: dict[str, Any]) -> Any:
    namespace: dict[str, Any] = {"__name__": "adk_kanode_worker"}
    exec(build_worker_bootstrap_script(), namespace)  # noqa: S102 - our own script
    return namespace["_reconstruct"](extracted)


@pytest.mark.parametrize("where", ["worker_script", "pod_runner"])
def test_nested_output_schema_is_usable_in_the_worker(where: str):
    if where == "worker_script":
        node = _reconstruct_in_worker(planner.extracted.model_dump(mode="json"))
    else:
        node = reconstruct_node(planner.extracted)

    schema = node.output_schema
    assert schema is not Plan  # rebuilt from source, not imported from this test module
    parsed = schema.model_validate({"assignments": [{"specialist": "cost", "task": "size it"}]})
    assert parsed.assignments[0].specialist == "cost"
    assert "Assignment" in str(schema.model_json_schema())
