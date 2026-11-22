---
layout: default
title: adk-kanode - run Google ADK workflow nodes in Kubernetes pods
description: >-
  Python plugin for the Google Agent Development Kit (ADK): one @kanode
  decorator runs a Workflow node or agent in its own Kubernetes / GKE pod,
  including gVisor pods on GKE Sandbox.
---

`adk-kanode` is a Python library for the
[Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/). It adds
a Workflow plugin and a node decorator, `@kanode`, that take one node of an ADK
Workflow and run it in a dedicated Kubernetes / GKE workload. The workflow graph
itself stays plain ADK.
{: .lead }

```python
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.workflow import Workflow
from adk_kanode import K8sPlugin, kanode


@kanode                                   # a function node, in its own pod
def count_levels(node_input: str) -> Event:
    return Event(output=f"{len(node_input.splitlines())} lines")


advisor = kanode(                         # an agent node, in a second pod
    Agent(name="advisor", model="gemini-2.5-flash", instruction="..."),
)

root_agent = Workflow(
    name="quickstart_workflow",
    edges=[("START", count_levels, advisor)],
)

app = App(name="quickstart", root_agent=root_agent, plugins=[K8sPlugin()])
```

## Install

Requires Python 3.12+.

```bash
pip install adk-kanode
```

## What it gives you

By default every node of an ADK Workflow runs in the same process as the
workflow host. With `adk-kanode` you can move individual nodes onto Kubernetes
without changing the graph.

| | |
| --- | --- |
| **Per-node resources** | Give a heavy node its own CPU and memory limits, container image, node selector and replica count. |
| **Per-node credentials** | Attach a ServiceAccount and Kubernetes Secrets to just the node that needs them. |
| **Isolation** | Run a node in a fresh single-use pod per call, optionally inside GKE Sandbox (gVisor). |
| **No extra packaging** | No per-node images or entrypoint scripts. The node's source is shipped to a generic worker image. |
| **Any ADK node** | Functions, generators, `LlmAgent`, tools and custom nodes, in chains, routing, fan-out, `JoinNode` and `ctx.run_node`. |

## Two execution modes

| Mode | What runs | Use for |
| --- | --- | --- |
| `deployment` (default) | One warm Deployment and ClusterIP Service per `@kanode` | Normal nodes: one in-cluster HTTP call per run |
| `pod` | A fresh pod per call, deleted afterwards | Strict one-shot isolation |

```python
@kanode(replicas=3)   # warm Deployment with 3 replicas
def hot_node(node_input: str): ...

@kanode(mode="pod")   # fresh single-use pod per call
def isolated_node(node_input: str): ...
```

## GKE Sandbox

[GKE Sandbox](https://cloud.google.com/kubernetes-engine/docs/how-to/sandbox-pods)
runs a pod under gVisor, a userspace kernel between the container and the host
kernel. One option puts a single workflow node inside it, which suits nodes that
handle untrusted input such as code written by a model:

```python
@kanode(
    mode="pod",                    # a fresh sandbox per call
    runtime_class_name="gvisor",   # becomes spec.runtimeClassName on the worker pod
    node_selector={"cloud.google.com/compute-class": "sandbox-gvisor-class"},
)
def run_untrusted(node_input: str) -> Event: ...
```

More in the
[GKE Sandbox section of the README](https://github.com/benmizrahi/adk-kanode#sandboxing-nodes-with-gke-sandbox).

## Examples

| Example | What it shows |
| --- | --- |
| [Quickstart](https://github.com/benmizrahi/adk-kanode/tree/main/examples/quickstart) | One function node and one Gemini agent, each in its own worker. |
| [Code interpreter on GKE Sandbox](https://github.com/benmizrahi/adk-kanode/tree/main/examples/gke-sandbox) | Gemini writes Python, and a single-use gVisor pod with no credentials and no network runs it. |
| [Dynamic agent swarm](https://github.com/benmizrahi/adk-kanode/tree/main/examples/swarm) | A planner picks specialist agents per request, and `ctx.run_node` runs each one in its own worker. |

## Documentation

- [README](https://github.com/benmizrahi/adk-kanode#readme): quick start, execution modes and every configuration option.
- [Architecture](https://github.com/benmizrahi/adk-kanode/blob/main/docs/ARCHITECTURE.md): source extraction, the worker protocol, rolling updates and the security model.
- [Issues](https://github.com/benmizrahi/adk-kanode/issues): questions, bugs and feature requests.

## Disclaimer

This is not an officially supported Google product. It is an independent project
and is not affiliated with, endorsed by, or maintained by Google.
