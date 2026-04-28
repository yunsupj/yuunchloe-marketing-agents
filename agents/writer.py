"""
Writer agent: produces a 4-slide carousel storyboard for the active app/region.

Two roles in one call:
    1. Vision Curator — sees the real photos in `state['raw_photo_urls']`
       (passed as image inputs in the HumanMessage) and picks the best 3
       for slides 1, 2, 3.
    2. Copywriter — writes per-slide overlay_text in the active profile's
       persona/tone (sourced from settings.yaml brand_voice).

Slide 4 is always a hardcoded `app_promo` card (logo + CTA). Slides 1–3 must
reference URLs from `raw_photo_urls`; the post-parse validator rejects any
hallucinated `source_url` and substitutes from the real-photo pool to defend
against the LLM occasionally inventing fake unsplash/pexels links.

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

# Carousel structure constants — kept in lockstep with prompts/writer_prompt.py.
EXPECTED_SLIDE_COUNT = 4
REAL_PHOTO_SLIDE_COUNT = 3   # slides 1, 2, 3 must be real_photo
APP_PROMO_LOGO_URL = (
    "https://aaicoyblsmdjoqmykivx.supabase.co/storage/v1/object/"
    "public/marketing-assets/logo/logo.png"
)
DEFAULT_APP_PROMO_OVERLAY = "우리 동네 진짜 정보, 깨알톡에서"

# Allowed values for the dynamic orange-pill category badge on slides 1-3.
# Kept in lockstep with prompts/writer_prompt.py [Content Category Badge].
CATEGORY_DINING = "DINING * LOCAL PICK"
CATEGORY_PLACES = "PLACES * LOCAL PICK"
CATEGORY_CHATTER = "NEIGHBORHOOD CHATTER"
ALLOWED_CONTENT_CATEGORIES = frozenset(
    {CATEGORY_DINING, CATEGORY_PLACES, CATEGORY_CHATTER}
)
DEFAULT_CONTENT_CATEGORY = CATEGORY_CHATTER


def _normalize_content_category(value: Any) -> str:
    """
    Coerce the model's `content_category` into one of the three allowed pill
    values. Accepts minor casing/whitespace variation; falls back to
    NEIGHBORHOOD CHATTER on anything unrecognized so the downstream renderer
    never sees the legacy hardcoded "주민 인증" or invented copy.
    """
    if not isinstance(value, str):
        return DEFAULT_CONTENT_CATEGORY
    normalized = " ".join(value.strip().upper().split())
    if normalized in ALLOWED_CONTENT_CATEGORIES:
        return normalized
    if "DINING" in normalized:
        return CATEGORY_DINING
    if "PLACES" in normalized:
        return CATEGORY_PLACES
    if "CHATTER" in normalized or "NEIGHBORHOOD" in normalized:
        return CATEGORY_CHATTER
    return DEFAULT_CONTENT_CATEGORY


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
            f"첨부된 {len(photo_urls)}장의 실제 사진을 직접 보고, 그 중 best 3장을 골라 "
            "slide 1 / slide 2 / slide 3 의 source_url 에 정확히 그대로 적어라. "
            "URL 을 변형하거나 단축하지 말고, 흐릿하거나 주제와 무관해 보이는 사진은 절대 고르지 마라. "
            "🚨 source_url 은 반드시 첨부된 목록 중 하나여야 한다 — unsplash.com, pexels.com, "
            "googleusercontent.com 등 첨부되지 않은 URL 을 발명하면 자동 reject 된다. "
            "사진이 3장 미만이면 같은 URL 을 재사용해도 된다 (발명은 절대 금지). "
            "Slide 4 는 무조건 app_promo 로, source_url 은 제공된 logo URL 을 글자 그대로 사용해라."
        )
    else:
        instruction_lines.append(
            "첨부된 raw photo 가 없다. Slide 1, 2, 3 은 모두 type='ai_generated' 로 채우고 "
            "source_url 키는 출력하지 말고 image_prompt + overlay_text 만 채워라. "
            "이 경우에도 가짜 URL 은 절대 만들지 마라. "
            "Slide 4 는 무조건 app_promo 로 logo URL 을 그대로 사용해라."
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


def _make_real_photo_slide(
    idx: int,
    unused_pool: list[str],
    all_photos: list[str],
    overlay: str,
    content_category: str = DEFAULT_CONTENT_CATEGORY,
) -> dict[str, Any]:
    """
    Build a single real_photo slide. Prefers an unused photo from the pool
    (mutates by popping); reuses an existing photo if the pool is exhausted
    but we still have *any* real photos; degrades to ai_generated only when
    `all_photos` is completely empty. NEVER invents a URL.

    `content_category` is the dynamic orange-pill badge text — must already
    be a member of ALLOWED_CONTENT_CATEGORIES.
    """
    if unused_pool:
        url = unused_pool.pop(0)
        return {
            "slide": idx,
            "type": "real_photo",
            "source_url": url,
            "content_category": content_category,
            "overlay_text": overlay,
        }
    if all_photos:
        # Reuse-by-index — better than fabricating. Anti-hallucination policy:
        # duplication is acceptable; invented URLs are not.
        url = all_photos[(idx - 1) % len(all_photos)]
        return {
            "slide": idx,
            "type": "real_photo",
            "source_url": url,
            "content_category": content_category,
            "overlay_text": overlay,
        }
    return {
        "slide": idx,
        "type": "ai_generated",
        "image_prompt": (
            "Cinematic editorial local photography, soft natural light, "
            "subject placed in upper two-thirds, lower third left intentionally "
            "clean for headline overlay, no text in frame, 100% opacity."
        ),
        "content_category": content_category,
        "overlay_text": overlay,
    }


def _make_app_promo_slide(overlay: str = "") -> dict[str, Any]:
    return {
        "slide": EXPECTED_SLIDE_COUNT,
        "type": "app_promo",
        "source_url": APP_PROMO_LOGO_URL,
        "overlay_text": overlay.strip() or DEFAULT_APP_PROMO_OVERLAY,
    }


def _fallback_carousel(photo_urls: list[str]) -> list[dict[str, Any]]:
    """
    Build a usable 4-slide carousel when JSON parse fails entirely: slides
    1-3 from `photo_urls` (reusing or padding with ai_generated only if no
    photos exist at all), slide 4 always the hardcoded app_promo.
    """
    unused_pool = list(photo_urls)
    slides: list[dict[str, Any]] = [
        _make_real_photo_slide(
            i, unused_pool, photo_urls, "", DEFAULT_CONTENT_CATEGORY
        )
        for i in range(1, REAL_PHOTO_SLIDE_COUNT + 1)
    ]
    slides.append(_make_app_promo_slide())
    return slides


def _coerce_carousel(raw: str, photo_urls: list[str]) -> list[dict[str, Any]]:
    """
    Parse the LLM JSON, then enforce the 4-slide invariant with strict
    anti-hallucination guards:
        - slides 1-3 must be real_photo (or ai_generated only if no photos)
        - any source_url not present in `photo_urls` is rejected and
          replaced with one from the pool — never trusted from the model
        - slide 4 is always overwritten with the hardcoded app_promo logo

    The model can technically return any garbage; this function ensures the
    downstream Designer never sees an invented unsplash/pexels URL.
    """
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

    photo_set = set(photo_urls)
    unused_pool = list(photo_urls)
    cleaned_slides: list[dict[str, Any]] = []

    # Process up to (EXPECTED_SLIDE_COUNT - 1) slides as real_photo candidates.
    # Slide 4 is always rebuilt at the end regardless of what the model said.
    raw_slides_for_photos = [
        item for item in data[: EXPECTED_SLIDE_COUNT - 1] if isinstance(item, dict)
    ]

    for i in range(1, REAL_PHOTO_SLIDE_COUNT + 1):
        item = raw_slides_for_photos[i - 1] if i - 1 < len(raw_slides_for_photos) else {}
        overlay = (item.get("overlay_text") or "").strip()
        src = (item.get("source_url") or "").strip()

        # Dynamic orange-pill category badge. Normalize so the legacy hardcoded
        # "주민 인증" / random LLM strings can never reach the renderer.
        raw_category = item.get("content_category")
        category = _normalize_content_category(raw_category)
        if isinstance(raw_category, str) and raw_category.strip() and category != raw_category.strip():
            print(
                f"[Writer] slide {i}: content_category {raw_category!r} "
                f"normalized to {category!r}."
            )

        if src and src in photo_set:
            # Valid — model picked a URL that's in the attached pool.
            if src in unused_pool:
                unused_pool.remove(src)
            cleaned_slides.append(
                {
                    "slide": i,
                    "type": "real_photo",
                    "source_url": src,
                    "content_category": category,
                    "overlay_text": overlay,
                }
            )
        else:
            if src:
                # The model invented a URL or copied one not in the attached set.
                # Reject it and substitute from the real-photo pool.
                print(
                    f"[Writer] slide {i}: rejected hallucinated source_url "
                    f"({src!r}) — substituting from raw_photo_urls."
                )
            cleaned_slides.append(
                _make_real_photo_slide(
                    i, unused_pool, photo_urls, overlay, category
                )
            )

    # Slide 4 — always the hardcoded app_promo. If the model returned an
    # overlay_text for slide 4, preserve it; otherwise use the default.
    promo_overlay = ""
    if len(data) >= EXPECTED_SLIDE_COUNT and isinstance(data[EXPECTED_SLIDE_COUNT - 1], dict):
        promo_overlay = (data[EXPECTED_SLIDE_COUNT - 1].get("overlay_text") or "").strip()
    cleaned_slides.append(_make_app_promo_slide(promo_overlay))

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
