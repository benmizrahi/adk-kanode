"""Dynamic agent swarm: a planner picks investigators at runtime, each runs in its own worker.

The graph is fixed and small. What changes per request is inside `dispatch`:
it reads the planner's assignments and starts one investigator agent per
assignment with `ctx.run_node(...)`, all in parallel. Every investigator is a
`@kanode` agent with Google Search, so each one does its part in its own
Kubernetes worker. The last node saves the brief, the findings, their sources
and the timings as an artifact.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from google.adk.agents import Agent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.tools import google_search
from google.adk.workflow import node
from google.adk.workflow import Workflow
from google.genai import types
from pydantic import BaseModel
from pydantic import Field

from adk_kanode import K8sPlugin
from adk_kanode import kanode

MODEL = "gemini-2.5-flash"
REPORT_FILENAME = "swarm-report.md"

# Scale knobs, read from the workflow host's environment (see "Scale it up" in the README).
# The most investigator runs one request may start.
MAX_ASSIGNMENTS = int(os.environ.get("SWARM_MAX_ASSIGNMENTS", "6"))
# "deployment": one warm worker per investigator. "pod": a fresh pod per assignment.
INVESTIGATOR_MODE: Any = os.environ.get("SWARM_INVESTIGATOR_MODE") or None

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

# One entry per investigator: its name and the angle it covers.
ROSTER = {
    "background": "background: what it is, who is behind it, and the key facts and dates",
    "news": "recent developments: what changed or was announced in the last 12 months",
    "market": "market: competitors, pricing, adoption and funding numbers",
    "technical": "technical details: how it works, benchmarks and known limits",
    "critic": "criticism: failures, controversies, risks and counter-arguments",
}

# Each investigator is its own kanode agent, so each gets its own warm worker.
# They are built in a loop from the roster, so they use the call form of kanode.
INVESTIGATORS = {
    name: kanode(
        Agent(
            name=name,
            model=MODEL,
            tools=[google_search],  # Gemini runs the searches; the worker needs no extra network access
            instruction=(
                f"You are the {name} investigator on a research team. Your angle is {angle}. "
                "You get a question and one assignment. Do only your assignment. "
                "Search Google for current facts instead of answering from memory. "
                "Reply with at most five short bullet points, each one a concrete finding "
                "with dates and numbers where they exist. If you could not find "
                "something, say so."
            ),
        ),
        env=_GENAI_ENV,
        mode=INVESTIGATOR_MODE,
    )
    for name, angle in ROSTER.items()
}


class Assignment(BaseModel):
    """One piece of work for one investigator."""

    investigator: str = Field(description="Name of an investigator from the roster.")
    task: str = Field(description="What this investigator should find out.")


class Plan(BaseModel):
    """Structured output of the planner agent."""

    assignments: list[Assignment]


@kanode(env=_GENAI_ENV)
def planner() -> Agent:
    """Splits the question into assignments for the relevant investigators."""
    return Agent(
        name="planner",
        model=MODEL,
        output_schema=Plan,
        instruction=(
            "You lead a research team. Split the user's question into assignments for "
            "the investigators whose angle is relevant to it. Skip investigators that "
            "have nothing useful to add. Give each assignment one concrete thing to "
            "find out, worded so that it can be searched for. "
            "When the request lists several separate items, give an investigator one "
            f"assignment per item. Use at most {MAX_ASSIGNMENTS} assignments.\n"
            "Roster:\n" + "\n".join(f"- {name}: {angle}" for name, angle in ROSTER.items())
        ),
    )


@kanode(env=_GENAI_ENV)
def lead() -> Agent:
    """Writes the brief from the investigators' findings."""
    return Agent(
        name="lead",
        model=MODEL,
        instruction=(
            "You are given a question and findings from investigators who searched the "
            "web for it. Write a short brief: a direct answer, the key findings that "
            "support it, and what is still unknown or disputed. "
            "Use only what the findings say."
        ),
    )


# --- Host process -------------------------------------------------------------


def capture_request(node_input: str) -> Event:
    """Stores the user's request so `dispatch` can hand it to each investigator."""
    request = node_input.strip()
    return Event(output=request, state={"request": request})


def _text(value: Any) -> str:
    """The text of an agent's output, whether it arrives as a string or as content."""
    if isinstance(value, str):
        return value
    parts = value.get("parts") if isinstance(value, dict) else getattr(value, "parts", None)
    if parts:
        texts = [
            p.get("text") if isinstance(p, dict) else (None if p.thought else p.text)
            for p in parts
        ]
        return "".join(t for t in texts if t)
    return str(value)


def _sources(ctx: Context, investigator: str, findings: str) -> list[dict[str, str]]:
    """The web pages Google Search gave one investigator run.

    `ctx.run_node` returns only the investigator's text. The pages it was grounded
    on travel on the same event, which is in the session by the time the run returns.
    """
    pages: dict[str, str] = {}
    for event in ctx.session.events:
        path = (event.node_info.path if event.node_info else "") or ""
        if f"/{investigator}@" not in path or not event.grounding_metadata:
            continue
        if not event.content or _text(event.content) != findings:
            continue
        for chunk in event.grounding_metadata.grounding_chunks or []:
            if chunk.web and chunk.web.uri:
                pages.setdefault(chunk.web.uri, chunk.web.title or chunk.web.uri)
    return [{"title": title, "url": url} for url, title in pages.items()]


@node(rerun_on_resume=True)  # required for any node that calls ctx.run_node()
async def dispatch(ctx: Context, node_input: dict[str, Any], request: str = "") -> Event:
    """Starts one investigator per assignment, in parallel, and collects the findings."""
    plan = Plan.model_validate(node_input)
    # The plan comes from a model: keep only known investigators, and cap the fan-out.
    assignments = [a for a in plan.assignments if a.investigator in INVESTIGATORS]
    assignments = assignments[:MAX_ASSIGNMENTS]

    async def run(assignment: Assignment) -> dict[str, Any]:
        started = time.monotonic()
        findings = str(
            await ctx.run_node(
                INVESTIGATORS[assignment.investigator],
                f"Question:\n{request}\n\nYour assignment:\n{assignment.task}",
                use_sub_branch=True,  # keep each investigator's events on its own branch
            )
        )
        return {
            "investigator": assignment.investigator,
            "task": assignment.task,
            "findings": findings,
            "sources": _sources(ctx, assignment.investigator, findings),
            "seconds": round(time.monotonic() - started, 1),
        }

    started = time.monotonic()
    runs = await asyncio.gather(*(run(a) for a in assignments))
    wall_seconds = round(time.monotonic() - started, 1)
    work_seconds = round(sum(r["seconds"] for r in runs), 1)

    sections = [
        f"## {r['investigator']}\nAssignment: {r['task']}\nFindings:\n{r['findings']}"
        for r in runs
    ]
    names = ", ".join(r["investigator"] for r in runs) or "none"
    return Event(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(text=f"Swarm of {len(runs)}: {names}"),
                types.Part.from_text(
                    text=f"{work_seconds}s of investigation finished in {wall_seconds}s."
                ),
            ],
        ),
        output=f"Question:\n{request}\n\n" + ("\n\n".join(sections) or "No findings."),
        # Kept for `publish`, which writes the report artifact.
        state={"swarm_runs": runs, "swarm_wall_seconds": wall_seconds},
    )


def _cell(value: str) -> str:
    """Makes a value safe for one Markdown table cell."""
    return " ".join(value.split()).replace("|", "\\|")


async def publish(
    ctx: Context,
    node_input: Any,
    request: str = "",
    swarm_runs: list[dict[str, Any]] | None = None,
    swarm_wall_seconds: float = 0.0,
) -> Event:
    """Saves the brief, the timings, and every finding with its sources as one Markdown artifact."""
    brief = _text(node_input)
    runs = swarm_runs or []
    work_seconds = round(sum(r["seconds"] for r in runs), 1)

    summary = (
        f"{len(runs)} assignments ran in parallel: {work_seconds}s of investigation "
        f"finished in {swarm_wall_seconds}s"
    )
    if swarm_wall_seconds > 0:
        summary += f" ({work_seconds / swarm_wall_seconds:.1f}x faster than one after another)"
    lines = [
        "# Investigation report",
        "",
        f"**Question:** {request}",
        "",
        "## Brief",
        "",
        brief,
        "",
        "## Swarm",
        "",
        f"{summary}.",
        "",
        "| # | Investigator | Assignment | Sources | Seconds |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {i} | {_cell(r['investigator'])} | {_cell(r['task'])} "
        f"| {len(r['sources'])} | {r['seconds']} |"
        for i, r in enumerate(runs, start=1)
    ]
    lines += ["", "## Findings"]
    for i, r in enumerate(runs, start=1):
        lines += [
            "",
            f"### {i}. {r['investigator']}",
            "",
            f"Assignment: {r['task']}",
            "",
            r["findings"],
        ]
        if r["sources"]:
            lines += ["", "Sources:", ""]
            lines += [f"- [{_cell(s['title'])}]({s['url']})" for s in r["sources"]]

    version = await ctx.save_artifact(
        REPORT_FILENAME,
        types.Part.from_bytes(
            data=("\n".join(lines) + "\n").encode("utf-8"), mime_type="text/markdown"
        ),
    )
    return Event(
        content=types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=f"Saved the full report as artifact {REPORT_FILENAME} (version {version})."
                )
            ],
        ),
        output=brief,
    )


root_agent = Workflow(
    name="swarm_workflow",
    edges=[("START", capture_request, planner, dispatch, lead, publish)],
)

k8s_plugin = K8sPlugin()
# Start the warm workers now. Investigators are not in the graph, so prewarm each one
# explicitly; otherwise an investigator's worker starts the first time it is picked.
for member in (root_agent, *INVESTIGATORS.values()):
    k8s_plugin.prewarm(member)

app = App(
    name="swarm",
    root_agent=root_agent,
    plugins=[k8s_plugin],
)
