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
HOTDEAL_CATEGORY = "핫딜/쇼핑"   # 도배를 막을 대상 카테고리
HOTDEAL_PROBABILITY = 0.00      # 비-금요일에 핫딜이 노출될 최대 확률 (10%)
HOT_FETCH_LIMIT = 25            # 카테고리 분류용으로 상위 N개를 받아온다


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


def _extract_title(row: dict) -> str:
    """posts.title 이 JSON 문자열({"ko":...}) 일 수 있으므로 안전하게 제목을 추출한다."""
    title_raw = (row.get("title") or "").strip()
    title = title_raw
    if title_raw.startswith("{"):
        try:
            title_dict = json.loads(title_raw)
            title = title_dict.get("ko") or title_dict.get("en") or title_raw
        except Exception:
            pass
    return title.strip()


def get_hot_community_topic() -> str | None:
    """
    최근 인기글 1개를 'TITLE (카테고리: CATEGORY)' 형태로 반환. 없으면 None.

    카테고리 비중 조절 (핫딜 도배 방지):
      1. 기본적으로 '핫딜/쇼핑'을 제외한 일반 카테고리(자유수다·동네맛집·
         정보/소식 등)를 최우선으로 뽑는다.
      2. '핫딜/쇼핑'은 금요일(weekday==4)이거나 10% 확률에 당첨됐을 때만
         노출 슬롯을 얻는다.
      3. 일반 카테고리 글이 하나도 없을 때만 핫딜글로 폴백한다.
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
            .limit(HOT_FETCH_LIMIT)
            .execute()
        )
    except Exception as e:
        print(f"[auto] Supabase query failed: {e!r}")
        return None

    rows = [r for r in (getattr(resp, "data", None) or []) if isinstance(r, dict)]
    if not rows:
        print("[auto] Supabase returned no recent posts.")
        return None

    # 핫딜 vs 일반 카테고리로 분리 (둘 다 이미 views 내림차순 유지)
    general_rows = [
        r for r in rows if (r.get("category") or "").strip() != HOTDEAL_CATEGORY
    ]
    hotdeal_rows = [
        r for r in rows if (r.get("category") or "").strip() == HOTDEAL_CATEGORY
    ]

    # 핫딜 노출 허용 여부: 금요일 또는 10% 확률
    hotdeal_allowed = (
        datetime.today().weekday() == 4 or random.random() < HOTDEAL_PROBABILITY
    )

    if hotdeal_allowed and hotdeal_rows:
        chosen = hotdeal_rows[0]
        print("[auto] 핫딜 노출 슬롯(금요일/확률 당첨) — '핫딜/쇼핑' 글 선택.")
    elif general_rows:
        chosen = general_rows[0]
        print("[auto] 일반 카테고리 글 선택 (핫딜 제외 최우선).")
    elif hotdeal_rows:
        chosen = hotdeal_rows[0]
        print("[auto] 일반 카테고리 글이 없어 핫딜글로 폴백.")
    else:
        print("[auto] 사용할 만한 게시글이 없음.")
        return None

    title = _extract_title(chosen)
    category = (chosen.get("category") or "").strip()
    if not title:
        print(f"[auto] Top row missing title: {chosen}")
        return None

    return f"{title} (카테고리: {category})" if category else title


# =============================================================================
# Source Strategy 2 — static local hotplace
# =============================================================================


def get_local_hotplace() -> tuple[str, Any] | tuple[None, None]:
    """
    Pull a single pending, fully-populated hotspot from the DB and return
    (topic_string, hotspot_id). Returns (None, None) on any failure or empty
    queue. Callers MUST abort rather than proceeding with a None topic.
    """
    client = _build_supabase_client()
    if client is None:
        print("[auto] Supabase unavailable — cannot fetch hotspot.")
        return None, None

    try:
        resp = (
            client.table("marketing_hotspots")
            .select("id, name, address, photo_urls")
            .eq("status", "pending")
            .execute()
        )
    except Exception as e:
        print(f"[auto] marketing_hotspots query failed: {e!r}")
        return None, None

    rows = getattr(resp, "data", None) or []
    # Only accept rows with a name, a real address, AND at least one photo.
    valid_rows = [
        row for row in rows
        if (
            isinstance(row, dict)
            and (row.get("name") or "").strip()
            and (row.get("address") or "").strip()
            and row.get("photo_urls")
        )
    ]
    if not valid_rows:
        print(
            "[auto] No valid hotspots found in DB. "
            "Need: status='pending' + address + photo_urls."
        )
        return None, None

    row = random.choice(valid_rows)
    row_id = row.get("id")

    # Mark as 'processing' so the next cron run cannot re-pick this row.
    if row_id is not None:
        try:
            client.table("marketing_hotspots").update(
                {"status": "processing"}
            ).eq("id", row_id).execute()
        except Exception as e:
            print(f"[auto] Failed to mark hotspot id={row_id} as processing: {e!r} — proceeding.")

    name = row.get("name", "").strip()
    address = row.get("address", "").strip()
    return f"{name} (Address: {address})", row_id


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
) -> tuple[str, str, Any]:
    """
    Returns (strategy, topic, hotspot_id). hotspot_id is None for the
    community strategy (no hotspot row involved). Raises SystemExit on abort.
    """
    if forced == "community":
        topic = get_hot_community_topic()
        if topic is None:
            print("[auto] Forced community strategy but no topic — aborting.")
            raise SystemExit(2)
        return "community", topic, None

    if forced == "hotplace":
        topic, hotspot_id = get_local_hotplace()
        if topic is None:
            print("[auto] Pipeline aborted because no valid hotplace topic was found.")
            raise SystemExit(0)
        return "hotplace", topic, hotspot_id

    # auto: 70% community, 30% hotplace, with hotplace fallback if needed.
    if random.random() < 0.7:
        topic = get_hot_community_topic()
        if topic is not None:
            return "community", topic, None
        print("[auto] Community source unavailable — falling back to hotplace.")
    topic, hotspot_id = get_local_hotplace()
    if topic is None:
        print("[auto] Pipeline aborted because no valid hotplace topic was found.")
        raise SystemExit(0)
    return "hotplace", topic, hotspot_id


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
    strategy, topic, hotspot_id = _resolve_strategy_and_topic(forced_strategy)
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
        hotspot_id=hotspot_id,
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
