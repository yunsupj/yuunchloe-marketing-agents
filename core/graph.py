"""
Core LangGraph scaffolding for the marketing content pipeline.

Design principles:
  - The graph is app- and region-agnostic. Everything specific to an app
    (Kkaertalk, Pickle, ...) or region (LA/OC, SF, NYC, ...) flows in via
    `app_context` and `target_region` on the State object.
  - Node functions are thin wrappers. Real prompt + LLM logic will be filled
    in later and should live in /agents and /prompts, not here.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.critic import critic_node
from agents.designer import designer_node
from agents.publisher import publisher_node
from agents.writer import writer_node


# =============================================================================
# State Schema
# =============================================================================
# Everything an agent needs to read/write flows through this dict. Keep keys
# explicit — LangGraph uses them for reducer wiring and checkpointing.


class AppContext(TypedDict, total=False):
    """Resolved profile from config/settings.yaml (one `profiles.*` entry)."""

    app_name: str                       # e.g. "Kkaertalk"
    app_tagline: str
    app_description: str
    brand_voice: dict[str, Any]         # tone, persona, do/dont lists
    distribution_channels: list[str]


class TargetRegion(TypedDict, total=False):
    """The specific region currently being generated for."""

    id: str                             # e.g. "la_oc"
    label: str                          # e.g. "LA / OC"
    locales: list[str]                  # e.g. ["en-US", "ko-KR"]
    sub_regions: list[str]              # e.g. ["Torrance", "Irvine", ...]


def _append(existing: list[Any] | None, new: list[Any]) -> list[Any]:
    """Reducer: concatenate lists across node updates instead of overwriting."""
    return (existing or []) + (new or [])


class GraphState(TypedDict, total=False):
    # ---- Injected at invocation time (immutable during a run) ----
    app_context: AppContext
    target_region: TargetRegion
    pipeline_config: dict[str, Any]     # `pipeline.*` block from settings.yaml

    # ---- Produced by upstream data-collection nodes (future) ----
    research_notes: str                 # summarized local signals / trends

    # ---- Writer <-> Critic loop ----
    draft: str                          # current candidate content
    revision: int                       # how many Writer passes have run
    critic_feedback: str                # latest Critic notes for the Writer
    critic_score: float                 # 0.0 – 1.0; compared to min_quality_score
    approved: bool                      # Critic's gate; True = exit loop

    # ---- Designer / Publisher ----
    image_prompt: str                   # english T2I prompt produced by Designer
    image_url: str                      # URL of the generated hero image
    published: bool                     # True if Publisher hit the webhook OK
    publish_status: str                 # human-readable publish outcome

    # ---- Audit trail (use reducer so nodes can append) ----
    history: Annotated[list[dict[str, Any]], _append]


# =============================================================================
# Node Functions (dummies — real logic lives in /agents later)
# =============================================================================


# =============================================================================
# Routing
# =============================================================================


def route_after_critic(state: GraphState) -> Literal["writer", "designer", "end"]:
    """
    Conditional edge after Critic:
        - approved        -> designer (proceed to visual + publish)
        - max revs hit    -> end      (give up; no publish on a failed draft)
        - otherwise       -> writer   (loop with feedback)

    TODO: read `max_revision_loops` from injected pipeline_config instead of
    a hardcoded constant.
    """
    MAX_REVISIONS_TODO = 3
    if state.get("approved"):
        return "designer"
    if state.get("revision", 0) >= MAX_REVISIONS_TODO:
        return "end"
    return "writer"


# =============================================================================
# Graph Assembly
# =============================================================================


def build_graph():
    """
    Full content pipeline:

        START -> writer -> critic -> approved? -> designer -> publisher -> END
                              ^                  |
                              | not approved &   |
                              | under rev cap    |
                              +------------------+
                                                 |
                              max revs hit ------> END (no publish)
    """
    builder = StateGraph(GraphState)

    builder.add_node("writer", writer_node)
    builder.add_node("critic", critic_node)
    builder.add_node("designer", designer_node)
    builder.add_node("publisher", publisher_node)

    builder.add_edge(START, "writer")
    builder.add_edge("writer", "critic")
    builder.add_conditional_edges(
        "critic",
        route_after_critic,
        {"writer": "writer", "designer": "designer", "end": END},
    )
    builder.add_edge("designer", "publisher")
    builder.add_edge("publisher", END)

    return builder.compile()


# Convenience singleton for ad-hoc invocation:
#   from core.graph import graph
#   graph.invoke({"app_context": ..., "target_region": ...})
graph = build_graph()
