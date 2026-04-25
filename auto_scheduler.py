"""
Auto-Pilot entry point for the daily marketing pipeline.

Designed to be invoked by Cloud Scheduler / GitHub Actions / cron:

    python auto_scheduler.py
    python auto_scheduler.py --strategy community
    python auto_scheduler.py --strategy hotplace

Source strategies:
    - community : pull the hottest recent post from Supabase   -> kkaertalk_chat
    - hotplace  : pick a known LA/OC/Torrance hotspot          -> kkaertalk_info
    - auto      : 70/30 weighted; falls back to hotplace if    (default)
                  Supabase is unavailable or returns nothing.

The pipeline must NEVER miss a daily post — any failure on the community
path silently degrades to the hotplace path.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

# Importing main triggers load_dotenv() and builds `core.graph.graph`. That's
# exactly what we want before any downstream code reads env / runs the graph.
from main import build_initial_state, load_config  # noqa: E402
from core.graph import graph  # noqa: E402


# =============================================================================
# Source Strategy 1 — Supabase community topic
# =============================================================================


HOT_LOOKBACK_HOURS = 48


def _build_supabase_client():
    """
    Lazily import + construct the Supabase client. Returns None if either
    the package or the env vars are missing — callers should treat None as
    "community source unavailable" and fall back.
    """
    try:
        from supabase import create_client
    except ImportError:
        print("[auto] supabase-py not installed — community source disabled.")
        return None

    url = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
    # Prefer the service-role key for backend cron use; fall back to anon.
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not url or not key:
        print("[auto] Supabase env vars missing — community source disabled.")
        return None

    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[auto] Supabase client init failed: {e!r}")
        return None


def get_hot_community_topic() -> str | None:
    """
    Returns the hottest recent post as 'TITLE (카테고리: CATEGORY)', or None
    if Supabase is unreachable / empty.
    """
    client = _build_supabase_client()
    if client is None:
        return None

    since = (
        datetime.now(timezone.utc) - timedelta(hours=HOT_LOOKBACK_HOURS)
    ).isoformat()

    try:
        resp = (
            client.table("posts")
            .select("title, category, views, likes, created_at")
            .gte("created_at", since)
            .order("views", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        print(f"[auto] Supabase query failed: {e!r}")
        return None

    rows = getattr(resp, "data", None) or []
    if not rows:
        print("[auto] Supabase returned no recent posts.")
        return None

    row = rows[0]
    title = (row.get("title") or "").strip()
    category = (row.get("category") or "").strip()
    if not title:
        print(f"[auto] Top row missing title: {row}")
        return None

    return f"{title} (카테고리: {category})" if category else title


# =============================================================================
# Source Strategy 2 — static local hotplace
# =============================================================================


def get_local_hotplace() -> str:
    """
    Pull the hotplace pool from Supabase (`marketing_hotspots` table) and
    return one at random. On any failure (no client, query error, empty
    table) silently degrades to a tiny in-code fallback list so the daily
    post never misses.
    """
    fallback = [
        "Torrance Mitsuwa 푸드코트",
        "K-town BCD Tofu House (북창동순두부 본점)",
        "Irvine Spectrum Center",
    ]

    client = _build_supabase_client()
    if client is None:
        return random.choice(fallback)

    try:
        resp = client.table("marketing_hotspots").select("name").execute()
    except Exception as e:
        print(f"[auto] marketing_hotspots query failed: {e!r} — using fallback.")
        return random.choice(fallback)

    rows = getattr(resp, "data", None) or []
    names = [
        (row.get("name") or "").strip()
        for row in rows
        if isinstance(row, dict) and (row.get("name") or "").strip()
    ]
    if not names:
        print("[auto] marketing_hotspots returned no rows — using fallback.")
        return random.choice(fallback)

    return random.choice(names)


# =============================================================================
# Master orchestrator
# =============================================================================


# strategy -> profile mapping is the contract between source & voice
STRATEGY_TO_PROFILE = {
    "community": "kkaertalk_chat",   # venting / gossip persona
    "hotplace":  "kkaertalk_info",   # magazine-editor persona
}


def _resolve_strategy_and_topic(
    forced: str | None,
) -> tuple[str, str]:
    """
    Returns (strategy, topic). Honors --strategy override, otherwise rolls
    a 70/30 weighted dice and falls back gracefully when the community
    source is empty.
    """
    if forced == "community":
        topic = get_hot_community_topic()
        if topic is None:
            print("[auto] Forced community strategy but no topic — aborting.")
            raise SystemExit(2)
        return "community", topic

    if forced == "hotplace":
        return "hotplace", get_local_hotplace()

    # auto: 70% community, 30% hotplace, with hotplace fallback if needed.
    if random.random() < 0.7:
        topic = get_hot_community_topic()
        if topic is not None:
            return "community", topic
        print("[auto] Community source unavailable — falling back to hotplace.")
    return "hotplace", get_local_hotplace()


def _print_summary(
    strategy: str,
    topic: str,
    profile: str,
    final_state: dict[str, Any],
) -> None:
    bar = "=" * 72
    print(f"\n{bar}")
    print(f" AUTO-PILOT RUN  ·  strategy={strategy}  ·  profile={profile}")
    print(bar)
    print(f"[Topic]           {topic}")

    score = final_state.get("critic_score")
    score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
    approved = final_state.get("approved", False)
    print(
        f"[Critic]          score={score_str}  "
        f"approved={approved}  revisions={final_state.get('revision', 0)}"
    )

    image_model = final_state.get("image_model")
    image_url = final_state.get("image_url")
    overlay = final_state.get("overlay_text")
    if image_model:
        print(f"[Image Model]     {image_model}")
    if overlay:
        print(f"[Overlay Text]    {overlay}")
    if image_url:
        print(f"[Image URL]       {image_url}")

    publish_status = final_state.get("publish_status")
    if publish_status:
        print(f"[Publish Status]  {publish_status}")

    print(f"\n[Final Draft]\n{final_state.get('draft') or '(none)'}\n{bar}\n")


def run_daily_marketing(forced_strategy: str | None = None) -> int:
    strategy, topic = _resolve_strategy_and_topic(forced_strategy)
    profile = STRATEGY_TO_PROFILE[strategy]

    print(
        f"[auto] strategy={strategy}  profile={profile}\n"
        f"[auto] topic={topic}"
    )

    config = load_config()
    initial_state = build_initial_state(
        config,
        research_notes=topic,
        profile_override=profile,
    )

    try:
        final_state = graph.invoke(initial_state)
    except Exception as e:
        print(f"[auto] Graph invocation failed: {e!r}", file=sys.stderr)
        return 1

    _print_summary(strategy, topic, profile, final_state)

    # Optional: emit a single machine-readable line for log scrapers / dashboards.
    if os.getenv("AUTO_EMIT_JSON") == "1":
        print(
            "AUTO_RESULT_JSON " + json.dumps(
                {
                    "strategy": strategy,
                    "profile": profile,
                    "topic": topic,
                    "approved": bool(final_state.get("approved")),
                    "critic_score": final_state.get("critic_score"),
                    "image_model": final_state.get("image_model"),
                    "image_url": final_state.get("image_url"),
                    "publish_status": final_state.get("publish_status"),
                },
                ensure_ascii=False,
            )
        )

    return 0


# =============================================================================
# CLI
# =============================================================================


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Daily auto-pilot for the marketing pipeline."
    )
    p.add_argument(
        "--strategy",
        choices=["auto", "community", "hotplace"],
        default="auto",
        help="Force a specific source strategy (default: auto, 70/30 weighted).",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    forced = None if args.strategy == "auto" else args.strategy
    raise SystemExit(run_daily_marketing(forced))
