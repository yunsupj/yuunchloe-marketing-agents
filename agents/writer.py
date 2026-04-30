"""
Writer agent — Bilingual Two-Track carousel mode.

The Writer issues a SINGLE LLM call that returns ONE JSON object with five
top-level keys:

    {
      "carousel_ko":      [ {slide_number, photo_instruction, title, description} x 4 ],
      "carousel_en":      [ {slide_number, photo_instruction, title, description} x 4 ],
      "reddit_promo_text": "<long-form English post>",
      "caption_ko":        "<IG/TikTok KO caption>",
      "caption_en":        "<Reddit profile-post EN caption>"
    }

`_coerce_payload` parses the model's response, validates both carousels (must
be 4-slide lists), normalizes each slide to the canonical shape, and falls
back to a safe placeholder when JSON parse fails. State is populated with
both KO and EN tracks so downstream nodes (designer, publisher) can render
and ship each language independently.
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


MAX_VISION_IMAGES = 6

# Each track is exactly 4 slides: 3 content + 1 app_promo CTA.
EXPECTED_SLIDE_COUNT = 4
APP_PROMO_LOGO_URL = (
    "https://aaicoyblsmdjoqmykivx.supabase.co/storage/v1/object/"
    "public/marketing-assets/logo/logo.png"
)
DEFAULT_APP_PROMO_TITLE_KO = "우리 동네 진짜 정보,\n깨알톡 에서"
DEFAULT_APP_PROMO_DESC_KO = "동네 이웃들이 남긴 리얼 후기 확인하기"
DEFAULT_APP_PROMO_TITLE_EN = "Real local intel, in your pocket."
DEFAULT_APP_PROMO_DESC_EN = "Built by a local. Try it."

# Slide-keys we expect post-coercion. Kept stable across both languages so
# the Designer can read them without language-specific branching.
SLIDE_KEYS = ("slide_number", "photo_instruction", "title", "description")


def _build_llm(temperature: float = 0.7) -> ChatOpenAI:
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        return ChatOpenAI(
            model="gpt-4o-mini",
            temperature=temperature,
            api_key=openai_key,
            max_retries=5,
        )

    qwen_key = os.getenv("QWEN_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL")
    model = os.getenv("QWEN_MODEL_NAME", "qwen3.5-flash")

    kwargs: dict[str, Any] = {"model": model, "temperature": temperature, "max_retries": 5}
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
    feedback_ko = state.get("critic_feedback_ko") or ""
    feedback_en = state.get("critic_feedback_en") or ""
    feedback_reddit = state.get("critic_feedback_reddit") or ""
    critic_score = state.get("critic_score") or 0.0

    has_feedback = feedback_ko or feedback_en or feedback_reddit

    if revision > 0 and has_feedback:
        prev_carousel = state.get("carousel_ko") or []
        prev_caption = state.get("caption_ko") or ""

        prompt += WRITER_REVISION_SUFFIX.format(
            critic_score=critic_score,
            feedback_ko=feedback_ko if feedback_ko else "Pass",
            feedback_en=feedback_en if feedback_en else "Pass",
            feedback_reddit=feedback_reddit if feedback_reddit else "Pass",
            previous_ko_carousel_json=json.dumps(prev_carousel, ensure_ascii=False, indent=2),
            previous_ko_caption_json=json.dumps({"caption_ko": prev_caption}, ensure_ascii=False, indent=2),
        )

    return prompt


def _build_human_message(state: dict[str, Any]) -> HumanMessage:
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
            f"첨부된 {len(photo_urls)}장의 실제 사진을 직접 보고, KO 와 EN 양쪽 카드뉴스에서 "
            "best 3 장을 슬라이드 1·2·3 의 photo_instruction 으로 자연어로 지칭해라 "
            '(예: "Use the 2nd attached photo, the wide storefront shot."). '
            "URL 을 photo_instruction 에 적지 마라 — URL 매칭은 다운스트림이 처리한다. "
            "Slide 4 는 KO/EN 모두 app_promo logo 고정 (photo_instruction 그대로)."
        )
    else:
        instruction_lines.append(
            "첨부된 raw photo 가 없다. 모든 슬라이드의 photo_instruction 을 "
            '"AI-generated B-roll: <짧은 mood prompt>" 형식으로 적어라. 사람/얼굴/글자 금지.'
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


def _normalize_slide(item: Any, idx: int) -> dict[str, Any]:
    """
    Coerce a single LLM-emitted slide dict into the canonical
    {slide_number, photo_instruction, title, description} shape.

    Tolerates legacy/alt keys (`overlay_text` -> title, `image_prompt` ->
    photo_instruction) so a partially-conforming model output still produces
    a valid slide rather than dropping the field.
    """
    if not isinstance(item, dict):
        item = {}

    title = (item.get("title") or item.get("overlay_text") or "").strip()
    description = (item.get("description") or "").strip()
    photo_instruction = (
        item.get("photo_instruction")
        or item.get("image_prompt")
        or ""
    ).strip()

    return {
        "slide_number": idx,
        "photo_instruction": photo_instruction,
        "title": title,
        "description": description,
    }


def _make_app_promo_slide(
    title: str = "",
    description: str = "",
    *,
    locale: str = "ko",
) -> dict[str, Any]:
    """Slide 4 — always the same logo URL; copy varies by locale."""
    if locale == "en":
        default_title = DEFAULT_APP_PROMO_TITLE_EN
    else:
        default_title = DEFAULT_APP_PROMO_TITLE_KO

    return {
        "slide_number": EXPECTED_SLIDE_COUNT,
        "photo_instruction": "Use app_promo logo (hardcoded downstream).",
        "source_url": APP_PROMO_LOGO_URL,  # downstream Designer reads this directly
        "title": title.strip() or default_title,
        "description": "",  # always empty — clean title-only CTA slide
    }


def _coerce_carousel_list(
    raw_list: Any, *, locale: str
) -> list[dict[str, Any]]:
    """
    Validate and normalize one carousel list (KO or EN). Always returns
    EXACTLY EXPECTED_SLIDE_COUNT slides; pads / truncates as needed.

    Slides 1-3 are content slides; slide 4 is always overwritten with the
    hardcoded app_promo (preserving any title/description the model wrote).
    """
    if not isinstance(raw_list, list):
        raw_list = []

    # First, normalize whatever the model gave us.
    normalized: list[dict[str, Any]] = []
    for i, item in enumerate(raw_list[:EXPECTED_SLIDE_COUNT], start=1):
        normalized.append(_normalize_slide(item, i))

    # Pad missing content slides with empty placeholders.
    while len(normalized) < EXPECTED_SLIDE_COUNT - 1:
        normalized.append(_normalize_slide({}, len(normalized) + 1))

    # Slide 4 — always rebuilt as app_promo. Preserve copy if the model
    # wrote one in the 4th position.
    promo_title = ""
    promo_desc = ""
    if len(normalized) >= EXPECTED_SLIDE_COUNT:
        last = normalized[EXPECTED_SLIDE_COUNT - 1]
        promo_title = last.get("title", "")
        promo_desc = last.get("description", "")
        normalized = normalized[: EXPECTED_SLIDE_COUNT - 1]
    elif len(raw_list) >= EXPECTED_SLIDE_COUNT and isinstance(
        raw_list[EXPECTED_SLIDE_COUNT - 1], dict
    ):
        last = raw_list[EXPECTED_SLIDE_COUNT - 1]
        promo_title = (last.get("title") or last.get("overlay_text") or "").strip()
        promo_desc = (last.get("description") or "").strip()

    normalized.append(
        _make_app_promo_slide(promo_title, promo_desc, locale=locale)
    )
    return normalized


def _fallback_payload() -> dict[str, Any]:
    """Safe placeholder payload when JSON parse fails entirely."""
    return {
        "carousel_ko": _coerce_carousel_list([], locale="ko"),
        "carousel_en": _coerce_carousel_list([], locale="en"),
        "reddit_promo_text": "",
        "caption_ko": "",
        "caption_en": "",
        "og_category_tag": "",
    }


def _coerce_payload(raw: str) -> dict[str, Any]:
    """
    Parse the LLM JSON object and return a fully-validated bilingual payload.

    Validation guarantees:
        - top-level result is a dict with all 7 keys present
        - carousel_ko / carousel_en are lists of EXACTLY 4 normalized slides
        - reddit_promo_text / caption_ko / caption_en / og_category_tag are strings
    """
    cleaned = _strip_markdown_fences(raw)
    data: Any = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                data = None

    if not isinstance(data, dict):
        print("[Writer] JSON parse failed — using fallback bilingual payload.")
        return _fallback_payload()

    carousel_ko = _coerce_carousel_list(data.get("carousel_ko"), locale="ko")
    carousel_en = _coerce_carousel_list(data.get("carousel_en"), locale="en")

    reddit_promo = data.get("reddit_promo_text")
    caption_ko = data.get("caption_ko")
    caption_en = data.get("caption_en")
    og_category_tag = data.get("og_category_tag")

    return {
        "carousel_ko": carousel_ko,
        "carousel_en": carousel_en,
        "reddit_promo_text": reddit_promo if isinstance(reddit_promo, str) else "",
        "caption_ko": caption_ko if isinstance(caption_ko, str) else "",
        "caption_en": caption_en if isinstance(caption_en, str) else "",
        "og_category_tag": og_category_tag if isinstance(og_category_tag, str) else "",
    }


def _flatten_slides(carousel: list[dict[str, Any]]) -> str:
    """Concatenate per-slide title + description for Critic-readable text."""
    parts: list[str] = []
    for slide in carousel:
        title = (slide.get("title") or "").strip()
        desc = (slide.get("description") or "").strip()
        if title and desc:
            parts.append(f"{title}\n{desc}")
        elif title:
            parts.append(title)
        elif desc:
            parts.append(desc)
    return "\n---\n".join(parts)


def writer_node(state: dict[str, Any]) -> dict[str, Any]:
    revision = (state.get("revision") or 0) + 1

    system_prompt = _render_system_prompt(state)
    human_msg = _build_human_message(state)
    photo_urls = _select_photo_urls(state)

    # Lower temperature for revision passes so the model follows surgical
    # fix instructions precisely rather than regenerating creatively.
    temperature = 0.3 if revision > 1 else 0.7
    llm = _build_llm(temperature=temperature)
    response = llm.invoke([SystemMessage(content=system_prompt), human_msg])
    raw = getattr(response, "content", str(response)) or ""

    payload = _coerce_payload(raw)

    carousel_ko = payload["carousel_ko"]
    carousel_en = payload["carousel_en"]
    draft_text_ko = _flatten_slides(carousel_ko)
    draft_text_en = _flatten_slides(carousel_en)

    # `draft` and `carousel_draft` remain populated as KO aliases for
    # back-compatibility with Critic / dashboard surfaces that still read them.
    return {
        "draft": draft_text_ko,
        "draft_text_ko": draft_text_ko,
        "draft_text_en": draft_text_en,
        "carousel_draft": carousel_ko,
        "carousel_ko": carousel_ko,
        "carousel_en": carousel_en,
        "reddit_promo_text": payload["reddit_promo_text"],
        "caption_ko": payload["caption_ko"],
        "caption_en": payload["caption_en"],
        "og_category_tag": payload["og_category_tag"],
        "revision": revision,
        "history": [
            {
                "node": "writer",
                "revision": revision,
                "ko_slide_count": len(carousel_ko),
                "en_slide_count": len(carousel_en),
                "vision_inputs": len(photo_urls),
                "has_reddit_promo": bool(payload["reddit_promo_text"]),
                "used_feedback": bool(
                    revision > 1 and state.get("critic_feedback")
                ),
            }
        ],
    }
