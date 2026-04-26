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
MAX_SNIPPETS = 4
MAX_PHOTOS = 6


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

    # Photo lookup runs on every invocation regardless of SerpApi state, so a
    # missing SERPAPI_API_KEY doesn't also cost us the carousel images.
    photo_urls = get_local_hotplace(original_query) if original_query else []

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

    target_region = state.get("target_region") or {}
    snippets_block = "\n".join(snippets)

    system_prompt = COLLECTOR_SYSTEM_PROMPT.format(
        query=original_query,
        target_region_label=target_region.get("label", "LA / OC"),
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

    enriched = (
        f"[원래 토픽]\n{original_query}\n\n[리서치 노트]\n{summary}"
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
            }
        ],
    }
