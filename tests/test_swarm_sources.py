"""The swarm example lists the pages Google Search gave each investigator.

Kept apart from test_swarm_example.py: that module's stub nodes run in
subprocesses, which rebuild every class defined next to them.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.models.registry import LLMRegistry
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types
import pytest

from adk_kanode import execute_payload_async
from adk_kanode import K8sPlugin
from adk_kanode import kanode
from adk_kanode import PodExecutor

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


class _SearchingLlm(BaseLlm):
    """Stands in for Gemini with Google Search: an answer plus the pages it used."""

    @classmethod
    def supported_models(cls) -> list[str]:
        return [r"fake-searching-.*"]

    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="- Released in 2018.")]
            ),
            grounding_metadata=types.GroundingMetadata(
                grounding_chunks=[
                    types.GroundingChunk(
                        web=types.GroundingChunkWeb(uri="https://example.com/a", title="example.com")
                    ),
                    types.GroundingChunk(
                        web=types.GroundingChunkWeb(uri="https://example.org/b", title="example.org")
                    ),
                    types.GroundingChunk(
                        web=types.GroundingChunkWeb(uri="https://example.com/a", title="example.com")
                    ),
                ]
            ),
        )


class _SerializingExecutor(PodExecutor):
    """Runs the node in-process but passes its events through JSON, as a worker does."""

    async def execute(self, payload: Any, config: Any) -> list[Event]:
        events = await execute_payload_async(payload)
        return [
            Event.model_validate(json.loads(json.dumps(e.model_dump(mode="json", exclude_none=True))))
            for e in events
        ]


@pytest.mark.asyncio
async def test_sources_from_google_search_end_up_in_the_report(monkeypatch):
    """The pages an investigator was grounded on are listed under its findings."""
    example = _load_example()
    LLMRegistry.register(_SearchingLlm)
    background = kanode(
        Agent(name="background", model="fake-searching-model", instruction="Investigate.")
    )
    monkeypatch.setattr(example, "INVESTIGATORS", {"background": background})

    def planner(node_input: str) -> dict[str, Any]:
        return {"assignments": [{"investigator": "background", "task": "when was it released"}]}

    def lead(node_input: str) -> str:
        return "It was released in 2018."

    workflow = Workflow(
        name="swarm_sources_test",
        edges=[
            ("START", example.capture_request, planner, example.dispatch, lead, example.publish)
        ],
    )
    plugin = K8sPlugin(executor=_SerializingExecutor())
    app = App(name="swarm_sources_app", root_agent=workflow, plugins=[plugin])
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name="swarm_sources_app", user_id="u"
    )
    async for _ in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text="What is gVisor?")]),
    ):
        pass

    artifact = await runner.artifact_service.load_artifact(
        app_name="swarm_sources_app",
        user_id="u",
        session_id=session.id,
        filename=example.REPORT_FILENAME,
    )
    assert artifact is not None and artifact.inline_data is not None
    report = artifact.inline_data.data.decode("utf-8")
    assert "| 1 | background | when was it released | 2 |" in report
    assert (
        "- Released in 2018.\n\nSources:\n\n"
        "- [example.com](https://example.com/a)\n"
        "- [example.org](https://example.org/b)\n"
    ) in report
