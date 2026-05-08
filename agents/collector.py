"""
Collector agent: pulls real-world facts from SerpApi (Google search) so the
Writer doesn't hallucinate business names, hours, prices, or reviews.

Flow:
    1. Read `state['research_notes']` (the user's original topic / query).
    2. Call SerpApi for that query + " review", grab top 3-4 organic snippets.
    3. Use the `fast` LLM to summarize the snippets into a Korean
       "리서치 노트" block.
    4. Replace `state['research_notes']` with `[원래 토픽]\n...\n\n[리서치 노트]\n...`
       so the Writer keeps the user's framing AND has factual ground truth.

If `SERPAPI_API_KEY` is missing or anything fails, the original
`research_notes` is left untouched and the pipeline moves on.
"""

from __future__ import annotations

import json
import os
from typing import Any

import requests
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from prompts.collector_prompt import (
    COLLECTOR_SYSTEM_PROMPT,
    COLLECTOR_USER_INSTRUCTION,
)


SERPAPI_ENDPOINT = "https://serpapi.com/search"
GOOGLE_PLACES_ENDPOINT = "https://maps.googleapis.com/maps/api/place/textsearch/json"
GOOGLE_PLACES_PHOTO_ENDPOINT = "https://maps.googleapis.com/maps/api/place/photo"
MAX_SNIPPETS = 4
MAX_PHOTOS = 6

# Gatekeeper prompt — decides whether a community topic names a specific local
# business (→ search query) or is generic chatter (→ "NONE").
GATEKEEPER_SYSTEM_PROMPT = """당신은 장소 판별기입니다. 주어진 커뮤니티 게시글 주제를 분석하세요.
만약 주제가 '특정 식당, 카페, 명소, 병원, 상호명'을 명확히 지칭하거나 리뷰/추천하는 글이라면, 구글 맵스에서 검색할 수 있는 정확한 검색어(예: 단비커피 토런스)를 출력하세요.
만약 주제가 단순 동네 수다, 푸념, 질문(예: "오늘 날씨 덥네요", "이삿짐 센터 추천해주세요")이거나 특정 상호명이 없다면, 반드시 대문자로 'NONE' 이라고만 출력하세요."""


# =============================================================================
# Supabase — fetch photo_urls for the topic from `marketing_hotspots`
# =============================================================================


def _build_supabase_client():
    """Lazy supabase client — same shape used elsewhere in the codebase."""
    try:
        from supabase import create_client
    except ImportError:
        return None

    url = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not url or not key:
        return None

    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[Collector] Supabase client init failed: {e!r}")
        return None


def _normalize_photos(value: Any) -> list[str]:
    """`photo_urls` may come back as a JSON array, a TEXT[] -> list, or a JSON string."""
    if isinstance(value, list):
        urls = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
            urls = parsed if isinstance(parsed, list) else [value]
        except json.JSONDecodeError:
            urls = [value]
    else:
        return []
    return [
        u.strip() for u in urls
        if isinstance(u, str) and u.strip().startswith(("http://", "https://"))
    ][:MAX_PHOTOS]


def get_local_hotplace(topic: str) -> list[str]:
    """
    Look the topic up against `marketing_hotspots` and return the matched
    hotspot's `photo_urls`. Returns [] when supabase / table / row is missing
    so the caller can degrade gracefully.

    Matching strategy: pull all hotspots once and pick the row whose `name`
    is a substring of the topic (or vice versa). The auto_scheduler feeds
    topics that are exactly hotspot names, so this matches reliably without
    needing fancy fuzzy logic.
    """
    if not topic:
        return []
    client = _build_supabase_client()
    if client is None:
        return []

    try:
        resp = (
            client.table("marketing_hotspots")
            .select("name, photo_urls")
            .execute()
        )
    except Exception as e:
        print(f"[Collector] marketing_hotspots query failed: {e!r}")
        return []

    rows = getattr(resp, "data", None) or []
    topic_lower = topic.lower()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or "").strip()
        if not name:
            continue
        name_lower = name.lower()
        if name_lower in topic_lower or topic_lower in name_lower:
            photos = _normalize_photos(row.get("photo_urls"))
            if photos:
                return photos
    return []


# =============================================================================
# Gatekeeper — decides if a topic names a specific local business
# =============================================================================


def _run_gatekeeper(topic: str, region_label: str) -> str:
    """
    Returns either:
      - A Google Maps search query string (e.g. "단비커피 토런스") if the topic
        names a specific business.
      - The string "NONE" if the topic is generic chatter with no identifiable
        business.
    """
    try:
        llm = _build_prompt_llm()
        response = llm.invoke(
            [
                SystemMessage(content=GATEKEEPER_SYSTEM_PROMPT),
                HumanMessage(
                    content=f"커뮤니티 게시글 주제: {topic}\n지역: {region_label}"
                ),
            ]
        )
        result = (getattr(response, "content", str(response)) or "").strip()
        return result if result else "NONE"
    except Exception as e:
        print(f"[Collector] Gatekeeper LLM failed: {e!r} — defaulting to NONE.")
        return "NONE"


# =============================================================================
# Google Places Text Search
# =============================================================================


def _fetch_google_places(
    search_query: str, api_key: str
) -> tuple[list[str], str | None]:
    """
    Call the Google Places Text Search API.
    Returns (photo_urls, found_address) where photo_urls is a list of up to
    MAX_PHOTOS direct photo URLs and found_address is the first result's
    formatted_address (or None if not found).
    """
    params = {
        "query": search_query,
        "key": api_key,
    }
    try:
        resp = requests.get(GOOGLE_PLACES_ENDPOINT, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[Collector] Google Places request failed: {e!r}")
        return [], None

    results = data.get("results") or []
    if not results:
        return [], None

    first = results[0]
    found_address: str | None = first.get("formatted_address") or None

    photos_meta = first.get("photos") or []
    photo_urls: list[str] = []
    for photo in photos_meta[:MAX_PHOTOS]:
        ref = photo.get("photo_reference")
        if not ref:
            continue
        url = (
            f"{GOOGLE_PLACES_PHOTO_ENDPOINT}"
            f"?maxwidth=800&photo_reference={ref}&key={api_key}"
        )
        photo_urls.append(url)

    return photo_urls, found_address


# =============================================================================
# Fast LLM (matches the shape used by writer / critic / designer)
# =============================================================================


def _build_prompt_llm() -> ChatOpenAI:
    """Cheap/fast tier — gpt-4o-mini if available, otherwise qwen-turbo."""
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        model = os.getenv("OPENAI_MODEL_NAME", "gpt-4o-mini")
        return ChatOpenAI(model=model, temperature=0.2, api_key=openai_key)

    qwen_key = os.getenv("QWEN_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL")
    model = os.getenv("QWEN_MODEL_NAME", "qwen-turbo")
    kwargs: dict[str, Any] = {"model": model, "temperature": 0.2}
    if qwen_key:
        kwargs["api_key"] = qwen_key
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)


# =============================================================================
# SerpApi
# =============================================================================


def _fetch_snippets(query: str, api_key: str) -> list[str]:
    params = {
        "q": f"{query} review",
        "engine": "google",
        "api_key": api_key,
    }
    resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    organic = data.get("organic_results") or []
    snippets: list[str] = []
    for result in organic[:MAX_SNIPPETS]:
        snippet = result.get("snippet")
        title = result.get("title")
        if snippet:
            snippets.append(
                f"- ({title or 'untitled'}) {snippet}".strip()
                if title
                else f"- {snippet}".strip()
            )
    return snippets


# =============================================================================
# Node
# =============================================================================


def _passthrough(
    state: dict[str, Any],
    reason: str,
    photo_urls: list[str] | None = None,
) -> dict[str, Any]:
    photos = photo_urls if photo_urls is not None else (
        state.get("raw_photo_urls") or []
    )
    return {
        "research_notes": state.get("research_notes") or "",
        "raw_photo_urls": photos,
        "history": [
            {
                "node": "collector",
                "skipped": True,
                "reason": reason,
                "photo_count": len(photos),
            }
        ],
    }


def collector_node(state: dict[str, Any]) -> dict[str, Any]:
    original_query = (state.get("research_notes") or "").strip()
    target_region = state.get("target_region") or {}
    region_label = target_region.get("label", "LA / OC")

    # 1. Try local DB first (marketing_hotspots) — fast, free, no LLM needed.
    photo_urls = get_local_hotplace(original_query) if original_query else []
    found_address: str | None = None

    # 2. If local DB has no photos, run the Gatekeeper LLM to decide whether
    #    this topic is a specific business worth hitting Google Places for.
    if original_query and not photo_urls:
        places_api_key = os.getenv("GOOGLE_PLACES_API_KEY")
        if places_api_key:
            search_query = _run_gatekeeper(original_query, region_label)
            if search_query != "NONE":
                print(
                    f"[Collector] Gatekeeper → '{search_query}'; "
                    f"calling Google Places API."
                )
                photo_urls, found_address = _fetch_google_places(
                    search_query, places_api_key
                )
                if found_address:
                    print(f"[Collector] Found address: {found_address}")
            else:
                print(
                    "[Collector] Gatekeeper returned NONE — skipping Google Places."
                )
        else:
            print(
                "[Collector] GOOGLE_PLACES_API_KEY not set — skipping gatekeeper."
            )

    if not original_query:
        return _passthrough(state, "no query in research_notes", photo_urls)

    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        return _passthrough(state, "SERPAPI_API_KEY not set", photo_urls)

    try:
        snippets = _fetch_snippets(original_query, api_key)
    except requests.RequestException as e:
        print(f"[Collector] SerpApi request failed: {e!r} — keeping raw query.")
        return _passthrough(state, f"serpapi error: {e!r}", photo_urls)

    if not snippets:
        return _passthrough(state, "no snippets returned", photo_urls)

    snippets_block = "\n".join(snippets)

    system_prompt = COLLECTOR_SYSTEM_PROMPT.format(
        query=original_query,
        target_region_label=region_label,
        snippets_block=snippets_block,
    )

    try:
        llm = _build_prompt_llm()
        response = llm.invoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=COLLECTOR_USER_INSTRUCTION),
            ]
        )
        summary = (getattr(response, "content", str(response)) or "").strip()
    except Exception as e:
        print(f"[Collector] Summarizer LLM failed: {e!r} — keeping raw query.")
        return _passthrough(state, f"llm error: {e!r}", photo_urls)

    if not summary:
        return _passthrough(state, "empty summary from llm", photo_urls)

    address_section = (
        f"\n\n[발견된 장소 정보]\n📍 주소: {found_address}" if found_address else ""
    )
    enriched = (
        f"[원래 토픽]\n{original_query}\n\n[리서치 노트]\n{summary}{address_section}"
    )

    return {
        "research_notes": enriched,
        "raw_photo_urls": photo_urls,
        "history": [
            {
                "node": "collector",
                "snippet_count": len(snippets),
                "summary_chars": len(summary),
                "photo_count": len(photo_urls),
                "found_address": found_address,
            }
        ],
    }
