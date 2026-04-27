"""
Writer agent: produces a 3-slide carousel storyboard for the active app/region.

Two roles in one call:
    1. Vision Curator — sees the real photos in `state['raw_photo_urls']`
       (passed as image inputs in the HumanMessage) and picks the best 2.
    2. Copywriter — writes per-slide overlay_text in the active profile's
       persona/tone (sourced from settings.yaml brand_voice).

Output is a JSON array stored in `state['carousel_draft']`. A flat textual
view is also stored in `state['draft']` (joined overlay_texts) so the
existing Critic / dashboard surfaces still have plain text to score.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from prompts.writer_prompt import (
    WRITER_REVISION_SUFFIX,
    WRITER_SYSTEM_PROMPT,
    render_do_dont,
)


# Cap how many image_url parts we shove into the request so we don't blow up
# token / per-request image limits on a long hotspot photo list.
MAX_VISION_IMAGES = 6


def _build_llm() -> ChatOpenAI:
    """
    Persona writer LLM. gpt-4o-mini is vision-capable and matches the pin we
    set for persona consistency; for richer vision curation switch to
    "gpt-4o" via env later if needed.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0.7,
            api_key=openai_key,
            # LangChain wraps each call in tenacity; on 429 it sleeps with
            # exponential backoff (≈ 4s, 8s, 16s, 32s, 64s) before giving up,
            # which keeps the Writer↔Critic loop alive through TPM throttling.
            max_retries=5,
        )

    qwen_key = os.getenv("QWEN_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL")
    model = os.getenv("QWEN_MODEL_NAME", "qwen3.5-flash")

    kwargs: dict[str, Any] = {"model": model, "temperature": 0.7, "max_retries": 5}
    if qwen_key:
        kwargs["api_key"] = qwen_key
    if base_url:
        kwargs["base_url"] = base_url

    return ChatOpenAI(**kwargs)


def _select_photo_urls(state: dict[str, Any]) -> list[str]:
    raw = state.get("raw_photo_urls") or []
    return [
        url for url in raw
        if isinstance(url, str) and url.startswith(("http://", "https://"))
    ][:MAX_VISION_IMAGES]


def _render_system_prompt(state: dict[str, Any]) -> str:
    app_context = state.get("app_context") or {}
    target_region = state.get("target_region") or {}
    brand_voice = app_context.get("brand_voice") or {}

    photo_urls = _select_photo_urls(state)

    prompt = WRITER_SYSTEM_PROMPT.format(
        app_name=app_context.get("app_name", "the app"),
        target_region_label=target_region.get("label", "this region"),
        brand_voice_persona=brand_voice.get("persona") or "(persona not specified)",
        brand_voice_tone=brand_voice.get("tone") or "(tone not specified)",
        brand_voice_do=render_do_dont(brand_voice.get("do")),
        brand_voice_dont=render_do_dont(brand_voice.get("dont")),
        research_notes=state.get("research_notes") or "(no research notes provided)",
        raw_photo_count=len(photo_urls),
    )

    revision = state.get("revision", 0) or 0
    feedback = state.get("critic_feedback")
    if revision > 0 and feedback:
        prompt += WRITER_REVISION_SUFFIX.format(critic_feedback=feedback)

    return prompt


def _build_human_message(state: dict[str, Any]) -> HumanMessage:
    """
    Build the user-turn message. When raw photos are available, we use the
    OpenAI vision content-parts format so gpt-4o-mini can actually see them
    and reference each `source_url` correctly in the JSON output.
    """
    app_context = state.get("app_context") or {}
    target_region = state.get("target_region") or {}
    sub_regions = target_region.get("sub_regions") or []
    sub_regions_str = ", ".join(sub_regions) if sub_regions else "the whole area"

    photo_urls = _select_photo_urls(state)
    instruction_lines = [
        f"App: {app_context.get('app_name', 'the app')}",
        f"Region: {target_region.get('label', 'the region')} (sub-areas: {sub_regions_str})",
        f"Tagline: {app_context.get('app_tagline', '')}",
        f"Description: {app_context.get('app_description', '')}",
        "",
    ]
    if photo_urls:
        instruction_lines.append(
            f"첨부된 {len(photo_urls)}장의 실제 사진을 직접 보고, 그 중 best 2장을 골라 "
            "slide 2 / slide 3 의 source_url 에 정확히 그대로 적어라. "
            "URL 을 변형하지 말고, 흐릿하거나 주제와 무관해 보이는 사진은 절대 고르지 마라."
        )
    else:
        instruction_lines.append(
            "첨부된 raw photo가 없다. 3장 모두 type='ai_generated' 슬라이드로 채워라. "
            "이 경우 source_url 키는 출력하지 말고 image_prompt + overlay_text 만 채워라."
        )
    text_part = "\n".join(line for line in instruction_lines if line is not None).strip()

    if not photo_urls:
        return HumanMessage(content=text_part)

    content: list[dict[str, Any]] = [{"type": "text", "text": text_part}]
    for url in photo_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return HumanMessage(content=content)


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _fallback_carousel(photo_urls: list[str]) -> list[dict[str, Any]]:
    """
    If JSON parse fails, build something usable rather than crashing the
    pipeline: AI cover + as many real_photo slides as we have URLs, padded
    with extra ai_generated slides up to 3 total.
    """
    slides: list[dict[str, Any]] = [
        {
            "slide": 1,
            "type": "ai_generated",
            "image_prompt": (
                "Cinematic editorial photography of a Korean-American "
                "neighborhood scene at golden hour, large negative space, "
                "35mm, shallow depth of field, warm color grading, "
                "lifestyle magazine aesthetic, no text in frame."
            ),
            "overlay_text": "이번주 동네 핫플",
        }
    ]
    for i, url in enumerate(photo_urls[:2], start=2):
        slides.append(
            {
                "slide": i,
                "type": "real_photo",
                "source_url": url,
                "overlay_text": "",
            }
        )
    while len(slides) < 3:
        slides.append(
            {
                "slide": len(slides) + 1,
                "type": "ai_generated",
                "image_prompt": (
                    "Editorial lifestyle photo, soft natural light, "
                    "negative space for headline, no text in frame."
                ),
                "overlay_text": "",
            }
        )
    return slides


def _coerce_carousel(raw: str, photo_urls: list[str]) -> list[dict[str, Any]]:
    cleaned = _strip_markdown_fences(raw)
    data: Any = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, list) or not data:
        print("[Writer] JSON parse failed — using fallback carousel.")
        return _fallback_carousel(photo_urls)

    cleaned_slides: list[dict[str, Any]] = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        slide = {
            "slide": item.get("slide", i),
            "type": item.get("type") or "ai_generated",
            "overlay_text": (item.get("overlay_text") or "").strip(),
        }
        if slide["type"] == "real_photo":
            slide["source_url"] = (item.get("source_url") or "").strip()
        else:
            slide["image_prompt"] = (item.get("image_prompt") or "").strip()
        cleaned_slides.append(slide)

    if not cleaned_slides:
        return _fallback_carousel(photo_urls)
    return cleaned_slides


def _flatten_to_draft(carousel: list[dict[str, Any]]) -> str:
    """Concatenate overlay_texts so the existing Critic still sees readable text."""
    parts = []
    for slide in carousel:
        text = (slide.get("overlay_text") or "").strip()
        if text:
            parts.append(text)
    return "\n---\n".join(parts)


def writer_node(state: dict[str, Any]) -> dict[str, Any]:
    revision = (state.get("revision") or 0) + 1

    system_prompt = _render_system_prompt(state)
    human_msg = _build_human_message(state)
    photo_urls = _select_photo_urls(state)

    llm = _build_llm()
    response = llm.invoke([SystemMessage(content=system_prompt), human_msg])
    raw = getattr(response, "content", str(response)) or ""

    carousel = _coerce_carousel(raw, photo_urls)
    draft = _flatten_to_draft(carousel)

    return {
        "draft": draft,
        "carousel_draft": carousel,
        "revision": revision,
        "history": [
            {
                "node": "writer",
                "revision": revision,
                "slide_count": len(carousel),
                "vision_inputs": len(photo_urls),
                "used_feedback": bool(
                    revision > 1 and state.get("critic_feedback")
                ),
            }
        ],
    }
