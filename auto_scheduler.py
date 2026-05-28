"""
Auto-Pilot entry point for the daily marketing pipeline.

Designed to be invoked by Cloud Scheduler / GitHub Actions / cron:

    python auto_scheduler.py

Source strategy is now **100% hotplace** — a pending row from the
`marketing_hotspots` table (status='pending', address + photo_urls 모두
존재) 을 무작위로 한 건 골라 LangGraph 파이프라인에 투입한다.

기존의 community(posts 테이블) 경로는 핫딜/쇼핑 카테고리 오염 문제로
폐기되었으며, 마케팅 콘텐츠는 이제 오직 에버그린(Evergreen) 로컬
핫스팟만 다룬다.

DB 업데이트 락: 선택된 hotspot 은 즉시 status='processing' 으로 마킹되어
다음 cron 실행이 같은 행을 재선택하지 못한다.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from typing import Any

# main 임포트 시점에 .env 로드 + LangGraph 빌드가 이뤄진다.
from main import build_initial_state, load_config  # noqa: E402
from core.graph import graph  # noqa: E402


# 모든 실행이 hotplace 전략이므로 프로필도 고정 (매거진 에디터 페르소나).
HOTPLACE_PROFILE = "kkaertalk_info"


# =============================================================================
# Supabase client
# =============================================================================


def _build_supabase_client():
    """Lazy supabase client; 패키지/env 가 없으면 None."""
    try:
        from supabase import create_client
    except ImportError:
        print("[auto] supabase-py not installed — hotspot source disabled.")
        return None

    url = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not url or not key:
        print("[auto] Supabase env vars missing — hotspot source disabled.")
        return None

    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[auto] Supabase client init failed: {e!r}")
        return None


# =============================================================================
# Source — marketing_hotspots (only)
# =============================================================================


def _normalize_photo_urls(value: Any) -> list[str]:
    """`photo_urls` 가 list 또는 JSON 문자열일 수 있어 list[str] 로 보장."""
    if isinstance(value, list):
        urls = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
            urls = parsed if isinstance(parsed, list) else []
        except Exception:
            urls = []
    else:
        return []
    return [u.strip() for u in urls if isinstance(u, str) and u.strip()]


def get_local_hotplace() -> tuple[str, Any, list[str]] | tuple[None, None, list[str]]:
    """
    `marketing_hotspots` 에서 status='pending' 이고 address/photo_urls 가 모두
    있는 행을 무작위로 한 건 선택해 (topic_string, hotspot_id, photo_urls) 를
    반환한다. 후보가 없으면 (None, None, []) 를 반환하므로 호출자는 파이프라인을
    중단해야 한다.

    동시에 선택된 행은 status='processing' 으로 atomically 마킹되어 다음
    cron 이 동일 행을 재선택할 수 없게 한다 (중복 방지 락).
    """
    client = _build_supabase_client()
    if client is None:
        print("[auto] Supabase unavailable — cannot fetch hotspot.")
        return None, None, []

    try:
        resp = (
            client.table("marketing_hotspots")
            .select("id, name, address, photo_urls")
            .eq("status", "pending")
            .execute()
        )
    except Exception as e:
        print(f"[auto] marketing_hotspots query failed: {e!r}")
        return None, None, []

    rows = getattr(resp, "data", None) or []
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
        return None, None, []

    row = random.choice(valid_rows)
    row_id = row.get("id")

    # 중복 방지 락 — 다음 cron run 이 같은 hotspot 을 못 잡게 한다.
    if row_id is not None:
        try:
            client.table("marketing_hotspots").update(
                {"status": "processing"}
            ).eq("id", row_id).execute()
        except Exception as e:
            print(
                f"[auto] Failed to mark hotspot id={row_id} as processing: "
                f"{e!r} — proceeding."
            )

    name = row.get("name", "").strip()
    address = row.get("address", "").strip()
    photo_urls = _normalize_photo_urls(row.get("photo_urls"))

    return f"{name} (Address: {address})", row_id, photo_urls


# =============================================================================
# Run
# =============================================================================


def _print_summary(topic: str, final_state: dict[str, Any]) -> None:
    bar = "=" * 72
    print(f"\n{bar}")
    print(f" AUTO-PILOT RUN  ·  profile={HOTPLACE_PROFILE}")
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


def run_daily_marketing() -> int:
    """Hotspot 1건을 골라 LangGraph 파이프라인을 1회 실행한다."""
    topic, hotspot_id, photo_urls = get_local_hotplace()
    if topic is None:
        print("[auto] Pipeline aborted — no valid hotplace topic was found.")
        return 0  # 정상 종료 (스킵). Cloud Scheduler 가 실패로 인식하지 않게.

    print(
        f"[auto] profile={HOTPLACE_PROFILE}\n"
        f"[auto] topic={topic}\n"
        f"[auto] photo_urls preloaded={len(photo_urls)} from marketing_hotspots"
    )

    config = load_config()
    initial_state = build_initial_state(
        config,
        research_notes=topic,
        profile_override=HOTPLACE_PROFILE,
        hotspot_id=hotspot_id,
    )
    # 사진을 state 에 미리 주입 — collector 가 다시 marketing_hotspots 를
    # 풀스캔하지 않아도 되도록 한다.
    if photo_urls:
        initial_state["raw_photo_urls"] = photo_urls

    try:
        final_state = graph.invoke(initial_state)
    except Exception as e:
        print(f"[auto] Graph invocation failed: {e!r}", file=sys.stderr)
        return 1

    _print_summary(topic, final_state)

    # Optional: emit a single machine-readable line for log scrapers / dashboards.
    if os.getenv("AUTO_EMIT_JSON") == "1":
        print(
            "AUTO_RESULT_JSON " + json.dumps(
                {
                    "profile": HOTPLACE_PROFILE,
                    "topic": topic,
                    "hotspot_id": hotspot_id,
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
        description=(
            "Daily auto-pilot for the marketing pipeline. "
            "Source is fixed to hotplace (marketing_hotspots) — community "
            "(posts) path has been removed."
        )
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    parse_args(sys.argv[1:])
    raise SystemExit(run_daily_marketing())
