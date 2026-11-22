"""Runs the GKE Sandbox example's graph locally, with the Gemini agents stubbed out."""

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
from adk_kanode import SubprocessPodExecutor

_AGENT_PATH = (
    Path(__file__).parents[1] / "examples" / "gke-sandbox" / "code_interpreter" / "agent.py"
)


def _load_example() -> Any:
    name = "code_interpreter_example_agent"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _AGENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_sandbox_node_is_configured_for_gke_sandbox():
    example = _load_example()
    config = example.run_in_sandbox.k8s_config
    assert config.mode == "pod"
    assert config.runtime_class_name == "gvisor"
    assert config.service_account_name == "adk-sandbox-sa"
    assert config.labels == {"trust-zone": "sandbox"}
    # The Gemini agents stay on warm Deployments.
    assert example.code_writer.k8s_config.mode == "deployment"
    assert example.code_writer.k8s_config.runtime_class_name is None


@pytest.mark.asyncio
async def test_failed_script_is_retried_then_answered(monkeypatch: pytest.MonkeyPatch):
    """A failing script goes back to the writer; the fixed one reaches the answer node."""
    monkeypatch.setenv("HOST_ONLY_SECRET", "s3cr3t")
    example = _load_example()
    scripts = [
        "import sys\nsys.exit('boom')",
        "import os\nprint(6 * 7, os.environ.get('HOST_ONLY_SECRET'), 'PATH' in os.environ)",
    ]
    prompts: list[str] = []

    def code_writer(node_input: str) -> Event:
        prompts.append(node_input)
        generated = {"code": scripts[len(prompts) - 1]}
        return Event(output=generated, state={"generated_code": generated})

    def answer_writer(node_input: str) -> str:
        return node_input

    workflow = Workflow(
        name="code_interpreter_test",
        edges=[
            (
                "START",
                example.capture_question,
                code_writer,
                example.run_in_sandbox,
                example.review_result,
            ),
            (example.review_result, {"retry": code_writer, "done": answer_writer}),
        ],
    )
    app = App(
        name="ci_app",
        root_agent=workflow,
        plugins=[K8sPlugin(executor=SubprocessPodExecutor())],
    )
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(app_name="ci_app", user_id="u")

    outputs: list[Any] = []
    async for event in runner.run_async(
        user_id="u",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="what is 6 times 7?")]
        ),
    ):
        if event.output is not None:
            outputs.append(event.output)

    assert len(prompts) == 2
    assert "boom" in prompts[1] and "sys.exit('boom')" in prompts[1]
    final = outputs[-1]
    assert "what is 6 times 7?" in final
    assert "Exit code: 0" in final
    # The script ran with a scrubbed environment: the harness's variables did not reach it.
    assert "42 None True" in final
    assert "s3cr3t" not in final
