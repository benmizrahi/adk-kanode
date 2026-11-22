"""Dynamic agent swarm: a planner picks specialists at runtime, each runs in its own worker.

The graph is fixed and small. What changes per request is inside `dispatch`:
it reads the planner's assignments and starts one specialist agent per
assignment with `ctx.run_node(...)`, all in parallel. Every specialist is a
`@kanode` agent, so each one does its part in its own Kubernetes worker.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.workflow import node
from google.adk.workflow import Workflow
from google.genai import types
from pydantic import BaseModel
from pydantic import Field

from adk_kanode import K8sPlugin
from adk_kanode import kanode

MODEL = "gemini-2.5-flash"
MAX_ASSIGNMENTS = 6

# Gemini settings the agent workers need, copied from this process's environment.
_GENAI_ENV = {
    key: os.environ[key]
    for key in (
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    )
    if os.environ.get(key)
}


# --- The swarm ----------------------------------------------------------------

# One entry per specialist: its name and the part it plays.
ROSTER = {
    "architect": "system design: components, data flow, and the simplest design that works",
    "security": "security: threats, data exposure, and the controls that are needed",
    "reliability": "operations: failure modes, rollout, monitoring and rollback",
    "cost": "cost: what drives the bill, rough sizing, and cheaper alternatives",
    "product": "users: who benefits, what they lose, and how to measure success",
}

# Each specialist is its own @kanode agent, so each gets its own warm worker.
SPECIALISTS = {
    name: kanode(
        Agent(
            name=name,
            model=MODEL,
            instruction=(
                f"You are the {name} specialist on a review board. Your part is {part}. "
                "You get a request and one assignment. Do only your assignment. "
                "Reply with at most five short bullet points."
            ),
        ),
        env=_GENAI_ENV,
    )
    for name, part in ROSTER.items()
}


class Assignment(BaseModel):
    """One piece of work for one specialist."""

    specialist: str = Field(description="Name of a specialist from the roster.")
    task: str = Field(description="What this specialist should work out.")


class Plan(BaseModel):
    """Structured output of the planner agent."""

    assignments: list[Assignment]


planner = kanode(
    Agent(
        name="planner",
        model=MODEL,
        output_schema=Plan,
        instruction=(
            "You lead a review board. Split the user's request into assignments for "
            "the specialists that are relevant to it. Skip specialists that have "
            "nothing useful to add. Give each assignment a concrete question.\n"
            "Roster:\n" + "\n".join(f"- {name}: {part}" for name, part in ROSTER.items())
        ),
    ),
    env=_GENAI_ENV,
)

lead = kanode(
    Agent(
        name="lead",
        model=MODEL,
        instruction=(
            "You are given a request and reports from the specialists who reviewed it. "
            "Write a short recommendation: a one-line verdict, the main reasons, and "
            "the open risks. Use only what the reports say."
        ),
    ),
    env=_GENAI_ENV,
)


# --- Host process -------------------------------------------------------------


def capture_request(node_input: str) -> Event:
    """Stores the user's request so `dispatch` can hand it to each specialist."""
    request = node_input.strip()
    return Event(output=request, state={"request": request})


@node(rerun_on_resume=True)  # required for any node that calls ctx.run_node()
async def dispatch(ctx: Context, node_input: dict[str, Any], request: str = "") -> Event:
    """Starts one specialist per assignment, in parallel, and collects the reports."""
    plan = Plan.model_validate(node_input)
    # The plan comes from a model: keep only known specialists, and cap the fan-out.
    assignments = [a for a in plan.assignments if a.specialist in SPECIALISTS]
    assignments = assignments[:MAX_ASSIGNMENTS]

    reports = await asyncio.gather(
        *(
            ctx.run_node(
                SPECIALISTS[a.specialist],
                f"Request:\n{request}\n\nYour assignment:\n{a.task}",
                use_sub_branch=True,  # keep each specialist's events on its own branch
            )
            for a in assignments
        )
    )

    sections = [
        f"## {a.specialist}\nAssignment: {a.task}\nReport:\n{report}"
        for a, report in zip(assignments, reports)
    ]
    names = ", ".join(a.specialist for a in assignments) or "none"
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"Swarm of {len(assignments)}: {names}")],
        ),
        output=f"Request:\n{request}\n\n" + ("\n\n".join(sections) or "No reports."),
    )


root_agent = Workflow(
    name="swarm_workflow",
    edges=[("START", capture_request, planner, dispatch, lead)],
)

k8s_plugin = K8sPlugin()
# Start the warm workers now. Specialists are not in the graph, so prewarm each one
# explicitly; otherwise a specialist's worker starts the first time it is picked.
for member in (root_agent, *SPECIALISTS.values()):
    k8s_plugin.prewarm(member)

app = App(
    name="swarm",
    root_agent=root_agent,
    plugins=[k8s_plugin],
)
