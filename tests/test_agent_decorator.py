"""Tests for @kanode on a function that builds an agent."""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.agents import LlmAgent
from google.adk.workflow._function_node import FunctionNode

from adk_kanode import K8sNode
from adk_kanode import kanode


def test_decorated_agent_factory_becomes_an_agent_node():
    @kanode(env={"KEY": "value"}, replicas=2)
    def advisor() -> Agent:
        return Agent(name="advisor", model="gemini-2.5-flash", instruction="Advise.")

    assert isinstance(advisor, K8sNode)
    assert isinstance(advisor.inner_node, LlmAgent)
    assert advisor.name == "advisor"
    assert advisor.k8s_config.env == {"KEY": "value"}
    assert advisor.k8s_config.replicas == 2


def test_bare_decorator_on_agent_factory():
    @kanode
    def advisor() -> Agent:
        return Agent(name="advisor", model="gemini-2.5-flash", instruction="Advise.")

    assert isinstance(advisor.inner_node, LlmAgent)


def test_decorated_agent_matches_wrapped_agent():
    @kanode
    def advisor() -> Agent:
        return Agent(name="advisor", model="gemini-2.5-flash", instruction="Advise.")

    wrapped = kanode(Agent(name="advisor", model="gemini-2.5-flash", instruction="Advise."))

    assert advisor.extracted == wrapped.extracted


def test_plain_functions_stay_function_nodes():
    @kanode
    def no_args() -> str:
        return "done"

    @kanode
    def unannotated():
        return "done"

    @kanode
    def takes_input(node_input: str) -> Agent:  # has a parameter: a normal function node
        raise NotImplementedError

    for wrapped in (no_args, unannotated, takes_input):
        assert isinstance(wrapped.inner_node, FunctionNode)
