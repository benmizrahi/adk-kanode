"""Runs the quickstart example's workflow locally, with the Gemini agent stubbed out."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types
import pytest

from adk_kanode import K8sPlugin
from adk_kanode import SubprocessPodExecutor

_AGENT_PATH = Path(__file__).parents[1] / "examples" / "quickstart" / "agent.py"


def _load_example() -> Any:
    name = "quickstart_example_agent"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _AGENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_quickstart_has_a_function_node_and_an_agent_node():
    example = _load_example()
    assert example.count_levels.extracted.kind == "function"
    assert example.advisor.extracted.kind == "llm_agent"
    # No per-node options beyond the Gemini settings forwarded to the agent's worker.
    assert example.count_levels.k8s_config.model_fields_set == set()
    assert example.advisor.k8s_config.model_fields_set <= {"env"}
    assert [n.name for n in example.root_agent.graph.nodes if n.name != "__START__"] == [
        "count_levels",
        "advisor",
    ]


@pytest.mark.asyncio
async def test_quickstart_counts_log_levels_and_feeds_the_agent():
    example = _load_example()
    seen: list[str] = []

    def advisor(node_input: str) -> str:
        seen.append(node_input)
        return "advice"

    workflow = Workflow(
        name="quickstart_test",
        edges=[("START", example.count_levels, advisor)],
    )
    app = App(
        name="quickstart_app",
        root_agent=workflow,
        plugins=[K8sPlugin(executor=SubprocessPodExecutor())],
    )
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="quickstart_app", user_id="u"
    )

    logs = "INFO started\nWARN slow request\nERROR db timeout\n\nERROR disk full"
    texts: list[str] = []
    outputs: list[Any] = []
    async for event in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=logs)]),
    ):
        if event.output is not None:
            outputs.append(event.output)
        if event.content and event.content.parts:
            texts.extend(p.text for p in event.content.parts if p.text)

    assert seen == [
        "4 lines: 2 errors, 1 warnings, 1 info\nError lines:\nERROR db timeout\nERROR disk full"
    ]
    assert any(t.startswith("4 lines: 2 errors, 1 warnings, 1 info (counted in pod ") for t in texts)
    assert outputs[-1] == "advice"
