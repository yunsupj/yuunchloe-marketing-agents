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


def _passthrough(state: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "research_notes": state.get("research_notes") or "",
        "history": [{"node": "collector", "skipped": True, "reason": reason}],
    }


def collector_node(state: dict[str, Any]) -> dict[str, Any]:
    original_query = (state.get("research_notes") or "").strip()
    if not original_query:
        return _passthrough(state, "no query in research_notes")

    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        return _passthrough(state, "SERPAPI_API_KEY not set")

    try:
        snippets = _fetch_snippets(original_query, api_key)
    except requests.RequestException as e:
        print(f"[Collector] SerpApi request failed: {e!r} — keeping raw query.")
        return _passthrough(state, f"serpapi error: {e!r}")

    if not snippets:
        return _passthrough(state, "no snippets returned")

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
        return _passthrough(state, f"llm error: {e!r}")

    if not summary:
        return _passthrough(state, "empty summary from llm")

    enriched = (
        f"[원래 토픽]\n{original_query}\n\n[리서치 노트]\n{summary}"
    )

    return {
        "research_notes": enriched,
        "history": [
            {
                "node": "collector",
                "snippet_count": len(snippets),
                "summary_chars": len(summary),
            }
        ],
    }
