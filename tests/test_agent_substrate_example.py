"""Checks the Agent Substrate example's workflow and durable-memory handoff."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import threading
from typing import Any
from http.client import HTTPConnection

import pytest

from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types

_EXAMPLE_DIR = Path(__file__).parents[1] / "examples" / "agent-substrate"


def _load_module(name: str, path: Path) -> Any:
    module_name = f"agent_substrate_example_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_memory_actor_keeps_bounded_user_and_assistant_history(
    monkeypatch: pytest.MonkeyPatch,
):
    actor = _load_module("memory_actor", _EXAMPLE_DIR / "memory_actor.py")
    monkeypatch.setattr(actor, "_turns", [])

    actor.append_turn("user", "Remember this.")
    turns = actor.append_turn("assistant", "I will.")

    assert turns == [
        {"role": "user", "text": "Remember this."},
        {"role": "assistant", "text": "I will."},
    ]
    with pytest.raises(ValueError):
        actor.append_turn("system", "Do not accept arbitrary roles.")


def test_memory_actor_accepts_utf8_http_turns():
    actor = _load_module("memory_actor", _EXAMPLE_DIR / "memory_actor.py")
    actor._turns.clear()
    server = actor.ThreadingHTTPServer(("127.0.0.1", 0), actor.MemoryHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        payload = json.dumps({"text": "こんにちは"}, ensure_ascii=True)
        connection.request(
            "POST",
            "/turn",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["turns"] == [
            {"role": "user", "text": "こんにちは"}
        ]
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_workflow_uses_kanode_workers_for_memory_and_answer(
    monkeypatch: pytest.MonkeyPatch,
):
    agent = _load_module("agent", _EXAMPLE_DIR / "agent.py")
    calls: list[tuple[str, str]] = []

    def fake_call_actor(
        _client: Any, path: str, text: str
    ) -> dict[str, Any]:
        calls.append((path, text))
        turns = [{"role": "user", "text": text}]
        if path == "/turn":
            turns.insert(0, {"role": "assistant", "text": "Earlier answer."})
        return {"turns": turns}

    monkeypatch.setattr(agent.MemoryActorClient, "call", fake_call_actor)
    load_memory = agent.load_memory.inner_node._unwrapped_func
    save_answer = agent.save_answer.inner_node._unwrapped_func
    loaded_event = load_memory("A follow-up question")
    saved_event = save_answer("The follow-up answer")

    assert "ASSISTANT: Earlier answer." in loaded_event.output
    assert "USER: A follow-up question" in loaded_event.output
    assert saved_event.output == "The follow-up answer"
    assert calls == [
        ("/turn", "A follow-up question"),
        ("/reply", "The follow-up answer"),
    ]
    assert "class MemoryActorClient" in agent.load_memory.extracted.module_preamble
    assert "class MemoryActorClient" in agent.save_answer.extracted.module_preamble
    effective_config = agent.K8sPlugin().config.merge_with(
        agent.load_memory.k8s_config
    )
    assert effective_config.volumes[0].name == "substrate-identity"
    assert effective_config.volume_mounts[0].mount_path == "/var/run/ate"

    worker_calls: list[str] = []

    class StubExecutor:
        async def execute(self, payload: Any, config: Any) -> list[Event]:
            worker_calls.append(payload.node_name)
            outputs = {
                agent.load_memory.name: "Conversation history and latest user turn.",
                agent.answer_agent.name: "The follow-up answer",
                agent.save_answer.name: "The follow-up answer",
            }
            return [Event(output=outputs[payload.node_name])]

    executor = StubExecutor()
    for node in (agent.load_memory, agent.answer_agent, agent.save_answer):
        monkeypatch.setattr(node, "executor", executor)

    workflow = Workflow(
        name="agent_substrate_memory_test",
        edges=[("START", agent.load_memory, agent.answer_agent, agent.save_answer)],
    )
    runner = InMemoryRunner(
        app=App(name="agent_substrate_memory_test", root_agent=workflow)
    )
    session = await runner.session_service.create_session(
        app_name="agent_substrate_memory_test",
        user_id="test-user",
    )
    outputs: list[Any] = []
    async for event in runner.run_async(
        user_id="test-user",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="A follow-up question")]
        ),
    ):
        if event.output is not None:
            outputs.append(event.output)

    assert worker_calls == [
        "load_memory",
        "answer_agent",
        "save_answer",
    ]
    assert outputs[-1] == "The follow-up answer"
    assert agent.answer_agent.extracted.kind == "llm_agent"
    assert agent.load_memory.k8s_config.volumes[0].name == "substrate-identity"
