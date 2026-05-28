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
    caption_ko = (state.get("caption_ko") or "").strip()
    caption_en = (state.get("caption_en") or "").strip()

    # Missing-caption guard — caption_ko 가 비었으면 즉시 하드 reject.
    # Writer 가 다시 작업해 caption_ko 를 반드시 채우도록 강제.
    if not caption_ko:
        print(
            "[Critic] Intercepted missing caption_ko — "
            "forcing score=0.0, approved=False (hard reject)."
        )
        missing_caption_feedback = (
            "캡션(caption_ko)이 누락되었습니다. 반드시 생성해야 합니다. "
            "writer_prompt.py 의 6단계 caption_ko 구조(intro / ⭐️picks / "
            "💡tip / 📍address-or-skip / by.#깨알톡 / hashtags)를 그대로 따라 작성하세요."
        )
        return {
            "critic_score": 0.0,
            "critic_feedback": missing_caption_feedback,
            "critic_feedback_ko": missing_caption_feedback,
            "critic_feedback_en": "[EMPTY] caption_ko missing — must be regenerated.",
            "critic_feedback_reddit": "[EMPTY] caption_ko missing — must be regenerated.",
            "approved": False,
            "history": [{
                "node": "critic",
                "score": 0.0,
                "approved": False,
                "missing_caption_intercept": True,
            }],
        }

    # Empty/trivial-draft guard — must run BEFORE banned-word scoring.
    # An empty KO draft trivially has 0 banned words and would otherwise
    # auto-pass with score 0.9. The "---" check requires at least one slide
    # separator (i.e. at least 2 of slides 1-3 carry real content).
    has_meaningful_content = len(draft_text_ko) > 30 and "---" in draft_text_ko
    if not has_meaningful_content:
        print(
            "[Critic] Intercepted empty/trivial KO draft "
            f"(len={len(draft_text_ko)}). Forcing score=0.0, approved=False."
        )
        empty_feedback_ko = (
            "[EMPTY] Draft has no meaningful content for slides 1-3. "
            "You MUST write actual text for title and description."
        )
        empty_feedback_other = "[EMPTY] Draft has no content."
        return {
            "critic_score": 0.0,
            "critic_feedback": empty_feedback_ko,
            "critic_feedback_ko": empty_feedback_ko,
            "critic_feedback_en": empty_feedback_other,
            "critic_feedback_reddit": empty_feedback_other,
            "approved": False,
            "history": [{
                "node": "critic",
                "score": 0.0,
                "approved": False,
                "empty_draft_intercept": True,
            }],
        }

    combined_draft = f"[🇰🇷 KO Carousel]\n{draft_text_ko}"
    if caption_ko:
        combined_draft += f"\n\n[🇰🇷 KO Caption]\n{caption_ko}"
    if draft_text_en:
        combined_draft += f"\n\n[🇺🇸 EN Carousel]\n{draft_text_en}"
    if caption_en:
        combined_draft += f"\n\n[🇺🇸 EN Caption]\n{caption_en}"
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

    approved = score >= APPROVAL_THRESHOLD

    return {
        "critic_score": score,
        "critic_feedback": combined_feedback,
        "critic_feedback_ko": feedback_ko,
        "critic_feedback_en": feedback_en,
        "critic_feedback_reddit": feedback_reddit,
        "approved": approved,
        "history": [{"node": "critic", "score": score, "approved": approved}],
    }
