"""
Writer agent: generates a localized marketing draft for the active app/region.

The Writer reads `app_context` and `target_region` off the graph state,
renders the system prompt from /prompts/writer_prompt.py, and calls the
Qwen-max model via DashScope's OpenAI-compatible endpoint (so we can keep
using the `langchain-openai` ChatOpenAI wrapper).

If `state['revision'] > 0`, the previous Critic feedback is appended to the
system prompt so the Writer knows what to fix.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from prompts.writer_prompt import (
    WRITER_REVISION_SUFFIX,
    WRITER_SYSTEM_PROMPT,
    render_do_dont,
)


def _build_llm() -> ChatOpenAI:
    """
    Persona writer LLM. Prioritizes gpt-4o-mini (pinned, not env-overridable)
    for stable instruction following + persona consistency. Falls back to
    Qwen via DashScope's OpenAI-compatible endpoint when no OpenAI key is
    present, so the project still runs in DashScope-only environments.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return ChatOpenAI(
            model="gpt-4o-mini",  # pinned: persona consistency > model choice
            temperature=0.7,
            api_key=openai_key,
        )

    qwen_key = os.getenv("QWEN_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL")
    model = os.getenv("QWEN_MODEL_NAME", "qwen3.5-flash")

    kwargs: dict[str, Any] = {"model": model, "temperature": 0.7}
    if qwen_key:
        kwargs["api_key"] = qwen_key
    if base_url:
        kwargs["base_url"] = base_url

    return ChatOpenAI(**kwargs)


def _render_system_prompt(state: dict[str, Any]) -> str:
    app_context = state.get("app_context") or {}
    target_region = state.get("target_region") or {}
    brand_voice = app_context.get("brand_voice") or {}

    prompt = WRITER_SYSTEM_PROMPT.format(
        app_name=app_context.get("app_name", "the app"),
        target_region_label=target_region.get("label", "this region"),
        brand_voice_persona=brand_voice.get("persona") or "(persona not specified)",
        brand_voice_tone=brand_voice.get("tone") or "(tone not specified)",
        brand_voice_do=render_do_dont(brand_voice.get("do")),
        brand_voice_dont=render_do_dont(brand_voice.get("dont")),
        research_notes=state.get("research_notes") or "(no research notes provided)",
    )

    revision = state.get("revision", 0) or 0
    feedback = state.get("critic_feedback")
    if revision > 0 and feedback:
        prompt += WRITER_REVISION_SUFFIX.format(critic_feedback=feedback)

    return prompt


def _build_user_message(state: dict[str, Any]) -> str:
    app_context = state.get("app_context") or {}
    target_region = state.get("target_region") or {}
    sub_regions = target_region.get("sub_regions") or []
    sub_regions_str = ", ".join(sub_regions) if sub_regions else "the whole area"

    return (
        f"Write a marketing draft for {app_context.get('app_name', 'the app')} "
        f"targeting {target_region.get('label', 'the region')} "
        f"(sub-areas: {sub_regions_str}). "
        f"Tagline for context: {app_context.get('app_tagline', '')}. "
        f"Description: {app_context.get('app_description', '')}"
    ).strip()


def writer_node(state: dict[str, Any]) -> dict[str, Any]:
    """Persona Writer node — produces the next draft and bumps revision."""
    revision = (state.get("revision") or 0) + 1

    system_prompt = _render_system_prompt(state)
    user_msg = _build_user_message(state)

    llm = _build_llm()
    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)]
    )
    draft = getattr(response, "content", str(response))

    return {
        "draft": draft,
        "revision": revision,
        "history": [
            {
                "node": "writer",
                "revision": revision,
                "used_feedback": bool(
                    revision > 1 and state.get("critic_feedback")
                ),
            }
        ],
    }
