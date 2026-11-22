"""Quickstart: a function node and a Gemini agent, each in its own Kubernetes worker.

Paste a few log lines into `adk web`. The function node counts them by level,
then the agent reads the summary and says what to check first.
"""

from __future__ import annotations

import os
import socket

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.workflow import Workflow
from google.genai import types

from adk_kanode import K8sPlugin
from adk_kanode import kanode


# A function node: add @kanode and it runs in its own worker.
@kanode
def count_levels(node_input: str) -> Event:
    """Counts log lines by level and collects the error lines."""
    counts = {"ERROR": 0, "WARN": 0, "INFO": 0}
    errors = []
    for line in node_input.splitlines():
        if not line.strip():
            continue
        upper = line.upper()
        if "ERROR" in upper:
            counts["ERROR"] += 1
            errors.append(line.strip())
        elif "WARN" in upper:
            counts["WARN"] += 1
        else:
            counts["INFO"] += 1

    summary = (
        f"{sum(counts.values())} lines: {counts['ERROR']} errors, "
        f"{counts['WARN']} warnings, {counts['INFO']} info"
    )
    return Event(
        # Shown in the chat, with the pod this node ran in.
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"{summary} (counted in pod {socket.gethostname()})")],
        ),
        # Passed to the next node.
        output=summary + "\nError lines:\n" + "\n".join(errors[:5]),
    )


# Gemini settings the agent's worker needs, copied from this process's environment.
_GENAI_ENV = {
    key: os.environ[key]
    for key in (
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    )
    if os.environ.get(key)
}

# An agent node: wrap the agent with kanode() and it runs in a second worker.
advisor = kanode(
    Agent(
        name="advisor",
        model="gemini-2.5-flash",
        instruction=(
            "You are an on-call engineer. You get a summary of a log sample and its "
            "error lines. Reply with two short bullet points: what looks wrong, and "
            "what to check first."
        ),
    ),
    env=_GENAI_ENV,
)


root_agent = Workflow(
    name="quickstart_workflow",
    edges=[("START", count_levels, advisor)],
)

app = App(
    name="quickstart",
    root_agent=root_agent,
    plugins=[K8sPlugin()],
)
