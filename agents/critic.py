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


APPROVAL_THRESHOLD = 0.85


def _build_llm() -> ChatOpenAI:
    """
    Critic LLM. Same priority shape as the writer: gpt-4o-mini pinned when
    OpenAI is available (stable JSON output + tighter scoring discipline),
    Qwen via DashScope as fallback. Temp 0.2 — this is fact + scoring work,
    not creative writing.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return ChatOpenAI(
            model="gpt-4o-mini",  # pinned: matches writer for consistent grading
            temperature=0.2,
            api_key=openai_key,
            # Tenacity-backed exponential backoff on 429 / transient 5xx —
            # essential because Writer↔Critic loops can hammer TPM quickly.
            max_retries=5,
        )

    qwen_key = os.getenv("QWEN_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL")
    model = os.getenv("QWEN_MODEL_NAME", "qwen3.5-flash")

    kwargs: dict[str, Any] = {"model": model, "temperature": 0.2, "max_retries": 5}
    if qwen_key:
        kwargs["api_key"] = qwen_key
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
    draft_text_ko = (state.get("draft_text_ko") or state.get("draft") or "").strip()
    draft_text_en = (state.get("draft_text_en") or "").strip()
    reddit_promo_text = (state.get("reddit_promo_text") or "").strip()

    combined_draft = f"[🇰🇷 KO Carousel]\n{draft_text_ko}"
    if draft_text_en:
        combined_draft += f"\n\n[🇺🇸 EN Carousel]\n{draft_text_en}"
    if reddit_promo_text:
        combined_draft += f"\n\n[🇺🇸 Reddit Promo]\n{reddit_promo_text}"

    app_context = state.get("app_context") or {}
    target_region = state.get("target_region") or {}

    system_prompt = CRITIC_SYSTEM_PROMPT.format(
        app_name=app_context.get("app_name", "the app"),
        target_region_label=target_region.get("label", "this region"),
    )
    user_msg = CRITIC_USER_TEMPLATE.format(draft=combined_draft)

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

    feedback_ko = verdict.get("feedback_ko_carousel") or verdict.get("feedback") or ""
    feedback_en = verdict.get("feedback_en_carousel") or ""
    feedback_reddit = verdict.get("feedback_reddit_promo") or ""

    for field in (feedback_ko, feedback_en, feedback_reddit):
        if not isinstance(field, str):
            field = str(field)

    # Legacy combined field for any code that still reads critic_feedback.
    combined_feedback = "\n\n".join(
        part for part in [
            f"[KO] {feedback_ko}" if feedback_ko and feedback_ko != "Pass" else "",
            f"[EN] {feedback_en}" if feedback_en and feedback_en != "Pass" else "",
            f"[Reddit] {feedback_reddit}" if feedback_reddit and feedback_reddit != "Pass" else "",
        ]
        if part
    ) or "Pass"

    # Trust the model's `approved` only if it's consistent with the score;
    # otherwise enforce the threshold ourselves.
    model_approved = bool(verdict.get("approved", False))
    approved = model_approved and score >= APPROVAL_THRESHOLD

    return {
        "critic_score": score,
        "critic_feedback": combined_feedback,
        "critic_feedback_ko": feedback_ko,
        "critic_feedback_en": feedback_en,
        "critic_feedback_reddit": feedback_reddit,
        "approved": approved,
        "history": [{"node": "critic", "score": score, "approved": approved}],
    }
