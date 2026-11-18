"""Realistic ADK 2.0 Workflow example using @kanode and K8sPlugin on GKE.

Each `@kanode` function is automatically extracted and executed in its own
dedicated GKE pod when triggered via `adk web` or `Runner`.
"""

from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import socket
import statistics
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.workflow import JoinNode
from google.adk.workflow import node
from google.adk.workflow import Workflow
from google.genai import types
from pydantic import BaseModel

from adk_kanode import K8sPlugin
from adk_kanode import kanode

# Load environment variables from example/.env if present
load_dotenv(Path(__file__).with_name(".env"), override=False)


class IncidentReport(BaseModel):
    """Structured log analysis handed from the report node to the advisor agent."""

    total_lines: int
    error_counts: dict[str, int]
    error_rate: float
    sample_errors: list[str]
    avg_latency_ms: float
    max_latency_ms: float
    latency_samples: int


@kanode(mode="pod")
def parse_logs_input(node_input: str) -> Event:
    """Parses raw newline-delimited log entries from the user prompt."""
    lines = [line.strip() for line in node_input.strip().splitlines() if line.strip()]
    return Event(
        output=lines,
        state={"total_lines": len(lines)},
    )


@kanode
def analyze_error_rates(node_input: list[str]) -> Event:
    """Runs in a dedicated GKE pod to count log levels and extract error messages."""
    levels: Counter[str] = Counter()
    errors: list[str] = []

    for line in node_input:
        upper = line.upper()
        if "ERROR" in upper:
            levels["ERROR"] += 1
            errors.append(line)
        elif "WARN" in upper:
            levels["WARN"] += 1
        else:
            levels["INFO"] += 1

    total = len(node_input) or 1
    return Event(
        output={
            "pod": socket.gethostname(),
            "counts": dict(levels),
            "error_rate": round(levels["ERROR"] / total, 3),
            "sample_errors": errors[:5],
        }
    )


@kanode
def analyze_latency(node_input: list[str]) -> Event:
    """Runs in a parallel GKE pod to extract `<N>ms` latency values and compute stats."""
    latencies: list[float] = []
    for line in node_input:
        for token in line.split():
            if token.endswith("ms") and token[:-2].replace(".", "", 1).isdigit():
                latencies.append(float(token[:-2]))

    if not latencies:
        stats = {"count": 0, "avg_ms": 0.0, "max_ms": 0.0}
    else:
        stats = {
            "count": len(latencies),
            "avg_ms": round(statistics.mean(latencies), 2),
            "max_ms": round(max(latencies), 2),
        }

    return Event(
        output={
            "pod": socket.gethostname(),
            **stats,
        }
    )


@kanode
def build_incident_report(
    node_input: dict[str, Any], total_lines: int = 0
) -> Event:
    """Runs in a dedicated GKE pod to synthesize the final incident report for ADK Web."""
    err_data = node_input.get("analyze_error_rates", {})
    lat_data = node_input.get("analyze_latency", {})

    summary = (
        f"Log Analysis Complete ({total_lines} lines)\n"
        f"- Error Analysis (Pod {err_data.get('pod')}): "
        f"counts={err_data.get('counts')}, error_rate={err_data.get('error_rate')}\n"
        f"- Latency Analysis (Pod {lat_data.get('pod')}): "
        f"avg={lat_data.get('avg_ms')}ms, max={lat_data.get('max_ms')}ms "
        f"({lat_data.get('count')} samples)\n"
        f"- Report Pod: {socket.gethostname()}"
    )

    # Structured output: validated against the next agent's input_schema.
    report = IncidentReport(
        total_lines=total_lines,
        error_counts=err_data.get("counts") or {},
        error_rate=err_data.get("error_rate") or 0.0,
        sample_errors=err_data.get("sample_errors") or [],
        avg_latency_ms=lat_data.get("avg_ms") or 0.0,
        max_latency_ms=lat_data.get("max_ms") or 0.0,
        latency_samples=lat_data.get("count") or 0,
    )

    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=summary)],
        ),
        output=report.model_dump(),
    )


# Gemini settings the LLM worker needs; copied from this process's environment
# (e.g. GOOGLE_GENAI_USE_VERTEXAI + project/location for Vertex AI).
_GENAI_ENV = {
    key: os.environ[key]
    for key in (
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    )
    if os.environ.get(key)
}

# An agent node: runs in its own GKE worker and turns the typed report into advice.
incident_advisor_agent = Agent(
    name="incident_advisor",
    model="gemini-2.5-flash",
    input_schema=IncidentReport,
    instruction="""You are an on-call SRE. You receive an IncidentReport with
    total_lines, error_counts, error_rate, sample_errors, avg_latency_ms,
    max_latency_ms and latency_samples fields.
    Reply with three short bullet points: what is going wrong, the most
    likely cause, and what to check first.""",
)

incident_advisor = kanode(incident_advisor_agent, env=_GENAI_ENV)

join_analysis = JoinNode(name="join_analysis")

root_agent = Workflow(
    name="log_analyzer_workflow",
    edges=[
        ("START", parse_logs_input),
        # Fan-out: run error analysis and latency analysis in two parallel GKE pods
        (parse_logs_input, (analyze_error_rates, analyze_latency)),
        # Fan-in: join results from both pods and build the final report in a third GKE pod
        ((analyze_error_rates, analyze_latency), join_analysis),
        (join_analysis, build_incident_report),
        # LLM step: a Gemini agent in a fourth GKE pod explains the report
        (build_incident_report, incident_advisor),
    ],
)

k8s_plugin = K8sPlugin()
# Start the warm worker Deployments now, so they are ready before the first request.
k8s_plugin.prewarm(root_agent)

app = App(
    name="example",
    root_agent=root_agent,
    plugins=[k8s_plugin],
)
