"""Runs the swarm example's graph locally, with the Gemini agents stubbed out."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types
import pytest

from adk_kanode import K8sPlugin
from adk_kanode import kanode
from adk_kanode import SubprocessPodExecutor

_AGENT_PATH = Path(__file__).parents[1] / "examples" / "swarm" / "agent.py"


def _load_example() -> Any:
    name = "swarm_example_agent"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _AGENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@kanode
def stub_security(node_input: str) -> str:
    return f"security report <{node_input.splitlines()[-1]}>"


@kanode
def stub_cost(node_input: str) -> str:
    return f"cost report <{node_input.splitlines()[-1]}>"


def test_every_roster_entry_is_a_kanode_agent():
    example = _load_example()
    assert set(example.SPECIALISTS) == set(example.ROSTER)
    for name, specialist in example.SPECIALISTS.items():
        assert specialist.name == name
        assert specialist.k8s_config.mode == "deployment"
    for name in example.ROSTER:
        assert name in example.planner.extracted.node_config["instruction"]


@pytest.mark.asyncio
async def test_dispatch_runs_only_the_planned_specialists(monkeypatch):
    """The plan decides which specialists run; unknown names are dropped."""
    example = _load_example()
    monkeypatch.setattr(
        example, "SPECIALISTS", {"security": stub_security, "cost": stub_cost}
    )

    def planner(node_input: str) -> dict[str, Any]:
        return {
            "assignments": [
                {"specialist": "security", "task": "check the auth flow"},
                {"specialist": "cost", "task": "size the cluster"},
                {"specialist": "security", "task": "check the data store"},
                {"specialist": "intruder", "task": "should be dropped"},
            ]
        }

    def lead(node_input: str) -> str:
        return node_input

    workflow = Workflow(
        name="swarm_test",
        edges=[("START", example.capture_request, planner, example.dispatch, lead)],
    )
    plugin = K8sPlugin(executor=SubprocessPodExecutor())
    app = App(name="swarm_app", root_agent=workflow, plugins=[plugin])
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(app_name="swarm_app", user_id="u")

    outputs: list[Any] = []
    texts: list[str] = []
    async for event in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="Move login to passkeys")]
        ),
    ):
        if event.output is not None:
            outputs.append(event.output)
        if event.content and event.content.parts:
            texts.extend(p.text for p in event.content.parts if p.text)

    final = outputs[-1]
    assert final.startswith("Request:\nMove login to passkeys")
    # Reports come back in plan order, one per assignment, including the repeated specialist.
    assert final.index("security report <check the auth flow>") < final.index(
        "cost report <size the cluster>"
    ) < final.index("security report <check the data store>")
    assert "intruder" not in final
    assert "Swarm of 3: security, cost, security" in texts
    assert sorted(plugin.executed_nodes) == ["stub_cost", "stub_security", "stub_security"]
