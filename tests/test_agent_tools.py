"""An agent's tools are rebuilt in its worker."""

from __future__ import annotations

from typing import Any

from google.adk.agents import Agent
from google.adk.tools import google_search
import pytest

from adk_kanode import build_worker_bootstrap_script
from adk_kanode import kanode
from adk_kanode.pod_runner import reconstruct_node


def lookup_weather(city: str) -> str:
    """Returns the weather for a city."""
    return f"Sunny in {city}"


def _reconstruct_in_worker(extracted: dict[str, Any]) -> Any:
    namespace: dict[str, Any] = {"__name__": "adk_kanode_worker"}
    exec(build_worker_bootstrap_script(), namespace)  # noqa: S102 - our own script
    return namespace["_reconstruct"](extracted)


@pytest.mark.parametrize("where", ["worker_script", "pod_runner"])
def test_agent_tools_reach_the_worker(where: str):
    searcher = kanode(
        Agent(
            name="searcher",
            model="gemini-2.5-flash",
            instruction="Search.",
            tools=[google_search, lookup_weather],
        )
    )
    assert searcher.extracted.node_config["tools"] == [
        {"module": "google.adk.tools.google_search_tool", "name": "google_search"},
        {"module": __name__, "name": "lookup_weather"},
    ]

    if where == "worker_script":
        node = _reconstruct_in_worker(searcher.extracted.model_dump(mode="json"))
    else:
        node = reconstruct_node(searcher.extracted)

    assert node.tools == [google_search, lookup_weather]


def test_agent_without_tools_has_no_tools_entry():
    plain = kanode(Agent(name="plain", model="gemini-2.5-flash", instruction="Answer."))
    assert "tools" not in plain.extracted.node_config


def test_tool_the_worker_cannot_import_is_rejected():
    def local_tool(city: str) -> str:
        return city

    with pytest.raises(ValueError, match="cannot import"):
        kanode(
            Agent(name="broken", model="gemini-2.5-flash", instruction="x", tools=[local_tool])
        )
