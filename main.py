"""
Pipeline entry point.

    python main.py "이번 주말 Torrance Mitsuwa 세일 정보"
    python main.py --profile pickle_sf "AI shopping agent launch"
    APP_CONFIG_PATH=config/settings.dev.yaml python main.py "..."

Pulls the active profile out of `config/settings.yaml`, builds the initial
GraphState, runs the Writer <-> Critic loop in `core.graph.graph`, and prints
a human-readable summary.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


# Load .env BEFORE importing anything that reads env vars (LangSmith picks up
# LANGCHAIN_TRACING_V2 / LANGCHAIN_API_KEY at import time on its own — we just
# need them present in os.environ when langchain modules initialize).
load_dotenv()

# Imported after load_dotenv so any LLM clients see the keys at construction.
from core.graph import graph  # noqa: E402


DEFAULT_CONFIG_PATH = "config/settings.yaml"


# =============================================================================
# Config loading
# =============================================================================


def load_config(path: str | None = None) -> dict[str, Any]:
    """
    Load the YAML config. Resolution order:
        1. explicit `path` arg
        2. APP_CONFIG_PATH env var
        3. DEFAULT_CONFIG_PATH (./config/settings.yaml)
    """
    config_path = Path(
        path or os.getenv("APP_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    )
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# =============================================================================
# State initialization
# =============================================================================


def build_initial_state(
    config: dict[str, Any],
    research_notes: str,
    profile_override: str | None = None,
    region_override: str | None = None,
) -> dict[str, Any]:
    """
    Resolve the active profile + target region from config and assemble the
    initial GraphState dict the graph expects.
    """
    profile_key = profile_override or config.get("active_profile")
    if not profile_key:
        raise ValueError("No `active_profile` in config and no override given.")

    profiles = config.get("profiles") or {}
    profile = profiles.get(profile_key)
    if not profile:
        raise ValueError(
            f"Profile '{profile_key}' not found. "
            f"Available: {list(profiles.keys())}"
        )

    regions = profile.get("target_regions") or []
    if not regions:
        raise ValueError(f"Profile '{profile_key}' has no target_regions.")

    target_region = (
        next((r for r in regions if r.get("id") == region_override), None)
        if region_override
        else regions[0]
    )
    if target_region is None:
        raise ValueError(
            f"Region '{region_override}' not found in profile '{profile_key}'."
        )

    # Strip target_regions out of app_context — the chosen one lives at the
    # top level so agents don't have to re-resolve it.
    app_context = {k: v for k, v in profile.items() if k != "target_regions"}

    return {
        "app_context": app_context,
        "target_region": target_region,
        "pipeline_config": config.get("pipeline") or {},
        "research_notes": research_notes,
        "revision": 0,
        "history": [],
    }


# =============================================================================
# Output formatting
# =============================================================================


def print_summary(final_state: dict[str, Any]) -> None:
    app_name = (final_state.get("app_context") or {}).get("app_name", "?")
    region_label = (final_state.get("target_region") or {}).get("label", "?")
    revision = final_state.get("revision", 0)
    score = final_state.get("critic_score")
    approved = final_state.get("approved", False)
    feedback = final_state.get("critic_feedback") or ""
    draft = final_state.get("draft") or "(no draft produced)"

    score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "n/a"
    status = "APPROVED ✅" if approved else "NOT APPROVED ❌"

    bar = "=" * 72
    print(f"\n{bar}")
    print(f" {app_name}  |  {region_label}  |  {status}")
    print(f" revisions: {revision}    final critic score: {score_str}")
    print(bar)
    print("\n[Final Draft]\n")
    print(draft)

    image_url = final_state.get("image_url")
    image_prompt = final_state.get("image_prompt")
    image_model = final_state.get("image_model")
    overlay_text = final_state.get("overlay_text")
    if image_url:
        print(f"\n[Image URL]  {image_url}")
    if image_model:
        print(f"[Image Model — A/B variant]  {image_model}")
    if overlay_text:
        print(f"[Overlay Text]  {overlay_text}")
    if image_prompt:
        print(f"[Image Prompt]  {image_prompt}")

    publish_status = final_state.get("publish_status")
    if publish_status:
        print(f"[Publish Status]  {publish_status}")

    if feedback and not approved:
        print("\n[Last Critic Feedback]\n")
        print(feedback)
    print(f"\n{bar}\n")


# =============================================================================
# CLI
# =============================================================================


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the marketing-content pipeline for the active profile."
    )
    parser.add_argument(
        "topic",
        nargs="?",
        help="The research notes / topic the Writer should build a draft around.",
    )
    parser.add_argument(
        "--profile",
        help="Override the `active_profile` from the YAML config.",
    )
    parser.add_argument(
        "--region",
        help="Override target region by id (e.g. 'la_oc'). Defaults to first.",
    )
    parser.add_argument(
        "--config",
        help="Path to the YAML config (overrides APP_CONFIG_PATH).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    topic = args.topic or input("Topic / research notes: ").strip()
    if not topic:
        print("No topic provided. Aborting.", file=sys.stderr)
        return 2

    config = load_config(args.config)
    initial_state = build_initial_state(
        config,
        research_notes=topic,
        profile_override=args.profile,
        region_override=args.region,
    )

    print(
        f"▶ Running pipeline: profile="
        f"{(initial_state['app_context'].get('app_name'))}  "
        f"region={initial_state['target_region'].get('label')}"
    )
    final_state = graph.invoke(initial_state)
    print_summary(final_state)
    return 0


# =============================================================================
# Slack interactive webhook (Make.com bridge)
# -----------------------------------------------------------------------------
# Run this side of main.py with:
#     uvicorn main:app --host 0.0.0.0 --port 8000
#
# The CLI block at the bottom is gated by `if __name__ == "__main__"` so
# importing main as `uvicorn main:app` doesn't fire the CLI argument parser.
# =============================================================================

import json  # noqa: E402

import requests  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402


app = FastAPI(title="Kkaertalk Marketing Webhooks")

MAKE_WEBHOOK_TIMEOUT_S = 10


def _build_webhook_supabase_client():
    """Lazy admin Supabase client used by the webhook handlers."""
    try:
        from supabase import create_client
    except ImportError:
        print("[webhook] supabase-py not installed.")
        return None

    url = os.getenv("EXPO_PUBLIC_SUPABASE_URL")
    # Service-role required: the webhook writes through RLS.
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("EXPO_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not url or not key:
        print("[webhook] Supabase env vars missing.")
        return None

    try:
        return create_client(url, key)
    except Exception as e:
        print(f"[webhook] Supabase client init failed: {e!r}")
        return None


def _slack_replacement(text: str) -> dict[str, Any]:
    """
    Slack message body that REPLACES the original (so the buttons disappear
    and the approver can't double-click). Uses both `text` and a section
    block so notifications render well across desktop / mobile / accessibility.
    """
    return {
        "replace_original": True,
        "text": text,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}}
        ],
    }


def _handle_approve(post_id: str) -> dict[str, Any]:
    """
    Approve flow:
        1. Fetch draft_text + carousel_urls from Supabase.
        2. POST {post_id, draft_text, carousel_urls} to Make.com.
        3. If Make returns 2xx → set status='approved'.
        4. Return a Slack message that replaces the original (no double-click).
    """
    client = _build_webhook_supabase_client()
    if client is None:
        return _slack_replacement(
            "❌ Supabase unavailable — approval not recorded."
        )

    # 1. Fetch draft_text + carousel_urls
    try:
        resp = (
            client.table("marketing_posts")
            .select("draft_text, carousel_urls")
            .eq("id", post_id)
            .single()
            .execute()
        )
    except Exception as e:
        print(f"[webhook] Supabase fetch failed for {post_id}: {e!r}")
        return _slack_replacement(
            f"❌ Could not fetch post `{post_id}` from Supabase."
        )

    row = getattr(resp, "data", None) or {}
    draft_text = row.get("draft_text") or ""
    carousel_urls = row.get("carousel_urls") or []

    # 2. POST to Make.com — Make handles fan-out to IG / TikTok / Reddit.
    make_url = os.getenv("MAKE_WEBHOOK_URL")
    if not make_url:
        return _slack_replacement(
            "❌ `MAKE_WEBHOOK_URL` not configured — cannot publish."
        )

    payload = {
        "post_id": post_id,
        "draft_text": draft_text,
        "carousel_urls": carousel_urls,
    }
    try:
        make_resp = requests.post(
            make_url,
            json=payload,
            timeout=MAKE_WEBHOOK_TIMEOUT_S,
        )
    except requests.RequestException as e:
        print(f"[webhook] Make.com POST failed: {e!r}")
        return _slack_replacement(
            f"❌ Make.com webhook unreachable: `{e}`"
        )

    if not make_resp.ok:
        return _slack_replacement(
            f"❌ Make.com returned `{make_resp.status_code}` — "
            "approval rolled back."
        )

    # 3. Update status — only on confirmed Make 2xx.
    try:
        (
            client.table("marketing_posts")
            .update({"status": "approved"})
            .eq("id", post_id)
            .execute()
        )
    except Exception as e:
        # Make has already accepted the job — don't roll back the publish.
        # Just surface the bookkeeping issue so it can be backfilled manually.
        print(f"[webhook] status update failed for {post_id}: {e!r}")
        return _slack_replacement(
            f"⚠️ Sent to Make.com (HTTP {make_resp.status_code}) but failed "
            f"to mark `{post_id}` as approved in Supabase: `{e}`"
        )

    # 4. Replace original Slack message — buttons disappear so no double-click.
    return _slack_replacement(
        f"✅ Post approved! Sent to Make.com for publishing.\n"
        f"_Post ID: `{post_id}`_"
    )


def _handle_reject(post_id: str) -> dict[str, Any]:
    """Reject flow: flip status to 'rejected', no Make.com call."""
    client = _build_webhook_supabase_client()
    if client is not None:
        try:
            (
                client.table("marketing_posts")
                .update({"status": "rejected"})
                .eq("id", post_id)
                .execute()
            )
        except Exception as e:
            print(f"[webhook] reject update failed for {post_id}: {e!r}")
            return _slack_replacement(
                f"⚠️ Couldn't mark `{post_id}` as rejected: `{e}`"
            )
    return _slack_replacement(f"🚫 Post `{post_id}` rejected — not published.")


@app.post("/api/webhooks/slack-interactive")
async def slack_interactive(request: Request) -> dict[str, Any]:
    """
    Slack interactive endpoint. Slack POSTs application/x-www-form-urlencoded
    with a single `payload` field carrying the JSON action context.

    Dispatches on `action_id` ∈ {"approve_post", "reject_post"} (constant per
    publisher.py spec) and reads the marketing_posts row id from `value`.

    TODO: verify the Slack signing secret on `X-Slack-Signature` /
    `X-Slack-Request-Timestamp` before processing, otherwise anyone who
    discovers the URL can fake button clicks.
    """
    form = await request.form()
    raw_payload = form.get("payload")
    if not raw_payload:
        raise HTTPException(status_code=400, detail="missing payload")

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON payload")

    actions = payload.get("actions") or []
    if not actions:
        return _slack_replacement("⚠️ No action received.")

    action = actions[0]
    action_id = action.get("action_id", "")
    post_id = (action.get("value") or "").strip()

    if not post_id:
        return _slack_replacement("⚠️ Missing post_id on the action.")

    if action_id == "approve_post":
        return _handle_approve(post_id)
    if action_id == "reject_post":
        return _handle_reject(post_id)
    return _slack_replacement(f"⚠️ Unknown action_id: `{action_id}`")


# =============================================================================
# CLI entry — kept last so `uvicorn main:app` import doesn't trigger argparse.
# =============================================================================


if __name__ == "__main__":
    raise SystemExit(main())
