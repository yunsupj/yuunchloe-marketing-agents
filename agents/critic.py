"""
Critic agent: ruthless QA editor for the Writer's draft.

Reads `state['draft']`, calls Qwen-max via DashScope's OpenAI-compatible
endpoint (same pattern as the Writer), parses a strict JSON verdict, and
returns `critic_score`, `critic_feedback`, `approved` for the loop router.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from prompts.critic_prompt import CRITIC_SYSTEM_PROMPT, CRITIC_USER_TEMPLATE


APPROVAL_THRESHOLD = 0.8


def _build_llm() -> ChatOpenAI:
    """Qwen via DashScope's OpenAI-compatible API — same shape as writer."""
    api_key = os.getenv("QWEN_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL")
    model = os.getenv("QWEN_MODEL_NAME", "qwen3.5-flash")

    kwargs: dict[str, Any] = {"model": model, "temperature": 0.2}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    return ChatOpenAI(**kwargs)


def _strip_markdown_fences(text: str) -> str:
    """Some models still wrap JSON in ```json ... ``` despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_verdict(raw: str) -> dict[str, Any]:
    """
    Pull a JSON verdict out of the model output. Tolerant of stray text:
    if the response isn't pure JSON, fall back to the first {...} block.
    On any failure, return a safe non-approving verdict.
    """
    cleaned = _strip_markdown_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return {
        "score": 0.0,
        "feedback": (
            "Critic 응답을 JSON으로 파싱하지 못함. Writer는 그대로 다시 써라. "
            f"raw output (첫 200자): {raw[:200]}"
        ),
        "approved": False,
    }


def critic_node(state: dict[str, Any]) -> dict[str, Any]:
    """Score the draft, produce feedback, and gate approval."""
    draft = state.get("draft") or ""
    app_context = state.get("app_context") or {}
    target_region = state.get("target_region") or {}

    system_prompt = CRITIC_SYSTEM_PROMPT.format(
        app_name=app_context.get("app_name", "the app"),
        target_region_label=target_region.get("label", "this region"),
    )
    user_msg = CRITIC_USER_TEMPLATE.format(draft=draft)

    llm = _build_llm()
    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_msg)]
    )
    raw = getattr(response, "content", str(response))

    verdict = _parse_verdict(raw)

    try:
        score = float(verdict.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))

    feedback = verdict.get("feedback") or ""
    if not isinstance(feedback, str):
        feedback = str(feedback)

    # Trust the model's `approved` only if it's consistent with the score;
    # otherwise enforce the threshold ourselves.
    model_approved = bool(verdict.get("approved", False))
    approved = model_approved and score >= APPROVAL_THRESHOLD

    return {
        "critic_score": score,
        "critic_feedback": feedback,
        "approved": approved,
        "history": [{"node": "critic", "score": score, "approved": approved}],
    }
