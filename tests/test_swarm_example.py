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
    assert set(example.INVESTIGATORS) == set(example.ROSTER)
    for name, investigator in example.INVESTIGATORS.items():
        assert investigator.name == name
        assert investigator.k8s_config.mode == "deployment"
        # Every investigator takes Google Search with it into its worker.
        assert investigator.extracted.node_config["tools"] == [
            {"module": "google.adk.tools.google_search_tool", "name": "google_search"}
        ]
    for name in example.ROSTER:
        assert name in example.planner.extracted.node_config["instruction"]


@pytest.mark.asyncio
async def test_dispatch_runs_only_the_planned_investigators(monkeypatch):
    """The plan decides which investigators run; unknown names are dropped."""
    example = _load_example()
    monkeypatch.setattr(
        example, "INVESTIGATORS", {"security": stub_security, "cost": stub_cost}
    )

    def planner(node_input: str) -> dict[str, Any]:
        return {
            "assignments": [
                {"investigator": "security", "task": "check the auth flow"},
                {"investigator": "cost", "task": "size the cluster"},
                {"investigator": "security", "task": "check the data store"},
                {"investigator": "intruder", "task": "should be dropped"},
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
    assert final.startswith("Question:\nMove login to passkeys")
    # Findings come back in plan order, one per assignment, including the repeated investigator.
    assert final.index("security report <check the auth flow>") < final.index(
        "cost report <size the cluster>"
    ) < final.index("security report <check the data store>")
    assert "intruder" not in final
    assert "Swarm of 3: security, cost, security" in texts
    assert sorted(plugin.executed_nodes) == ["stub_cost", "stub_security", "stub_security"]


@pytest.mark.asyncio
async def test_publish_saves_the_report_as_an_artifact(monkeypatch):
    """The last node writes the brief, the timings and the findings to one file."""
    example = _load_example()
    monkeypatch.setattr(
        example, "INVESTIGATORS", {"security": stub_security, "cost": stub_cost}
    )

    def planner(node_input: str) -> dict[str, Any]:
        return {
            "assignments": [
                {"investigator": "security", "task": "check the auth | flow"},
                {"investigator": "cost", "task": "size the cluster"},
            ]
        }

    def lead(node_input: str) -> str:
        return "Ship it."

    workflow = Workflow(
        name="swarm_publish_test",
        edges=[
            ("START", example.capture_request, planner, example.dispatch, lead, example.publish)
        ],
    )
    plugin = K8sPlugin(executor=SubprocessPodExecutor())
    app = App(name="swarm_publish_app", root_agent=workflow, plugins=[plugin])
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="swarm_publish_app", user_id="u"
    )

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

    assert outputs[-1] == "Ship it."
    assert "Saved the full report as artifact swarm-report.md (version 0)." in texts

    artifact = await runner.artifact_service.load_artifact(
        app_name="swarm_publish_app",
        user_id="u",
        session_id=session.id,
        filename=example.REPORT_FILENAME,
    )
    assert artifact is not None and artifact.inline_data is not None
    assert artifact.inline_data.mime_type == "text/markdown"
    report = artifact.inline_data.data.decode("utf-8")
    assert "**Question:** Move login to passkeys" in report
    assert "## Brief\n\nShip it." in report
    assert "2 assignments ran in parallel" in report
    assert "| 1 | security | check the auth \\| flow | 0 |" in report
    assert "| 2 | cost | size the cluster | 0 |" in report
    assert "security report <check the auth | flow>" in report
    assert "cost report <size the cluster>" in report
